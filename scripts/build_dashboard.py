"""Build the static GitHub Pages data file from the tracked SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import tempfile
from datetime import date
from pathlib import Path

from cb_price import effective_cb_price_source_sql, effective_cb_price_sql
from strategy_registry import active_strategy_codes, get_strategy


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "cb_history.db"
OUTPUT_PATH = ROOT / "docs" / "data.json"
SHARD_ROOT = ROOT / "docs" / "data" / "v2"
SHARD_SCHEMA_VERSION = 2
# A deliberate operational ceiling, far below GitHub's 100 MiB blob limit.
MAX_SHARD_BYTES = 20 * 1024 * 1024
TABLE_NAME = "cb_daily"
MASTER_TABLE_NAME = "cb_master"
STOCK_DAILY_TABLE_NAME = "stock_daily_market"
CONVERSION_EVENT_TABLE_NAME = "conversion_price_events"
PARENT_FLOW_TABLE_NAME = "parent_flow_metrics"
INSTITUTIONAL_COVERAGE_TABLE_NAME = "institutional_coverage"
ETF_STATUS_TABLE_NAME = "active_etf_collection_status"
STRATEGY_SIGNAL_TABLE_NAME = "strategy_signals"
STRATEGY_EVALUATION_TABLE_NAME = "strategy_evaluations"
STRATEGY_DEFINITION_TABLE_NAME = "strategy_definition"
STRATEGY_RUN_TABLE_NAME = "strategy_run"
STRATEGY_RUN_SIGNAL_TABLE_NAME = "strategy_run_signals"
STRATEGY_RUN_EVALUATION_TABLE_NAME = "strategy_run_evaluations"
STRATEGY_PUBLISHED_SERIES_TABLE_NAME = "strategy_published_series"
STRATEGY_PUBLISHED_DATE_TABLE_NAME = "strategy_published_date"
DAILY_REQUIRED_COLUMNS = {
    "trade_date",
    "cb_code",
    "cb_name",
    "close_price",
    "reference_price",
    "volume_lots",
}
MASTER_REQUIRED_COLUMNS = {
    "cb_code",
    "stock_code",
    "issue_date",
    "maturity_date",
    "put_date",
    "issue_units",
    "issue_amount",
    "balance_amount",
    "balance_date",
    "current_conversion_price",
    "current_conversion_price_effective_date",
    "is_secured",
    "delisting_date",
    "delisting_reason",
}
STOCK_DAILY_REQUIRED_COLUMNS = {
    "trade_date",
    "p_stock_code",
    "p_close_price",
    "p_volume_shares",
}
CONVERSION_EVENT_REQUIRED_COLUMNS = {
    "cb_code",
    "effective_date",
    "conversion_price",
}
PARENT_FLOW_REQUIRED_COLUMNS = {
    "trade_date", "stock_code", "foreign_status", "foreign_net_lots",
    "foreign_volume_pct", "foreign_streak_days", "foreign_streak_lots",
    "trust_status", "trust_net_lots", "trust_volume_pct", "trust_streak_days",
    "trust_streak_lots", "active_etf_status", "active_etf_change_lots",
    "active_etf_change_value_twd", "active_etf_streak_days", "active_etf_streak_lots",
}
INSTITUTIONAL_COVERAGE_REQUIRED_COLUMNS = {"trade_date", "stock_code", "status", "reason"}
ETF_STATUS_REQUIRED_COLUMNS = {"trade_date", "etf_code", "status"}
TRACKED_ACTIVE_ETFS = ("00980A", "00985A", "00999A", "00982A", "00992A")
ANNOUNCEMENT_TABLE_NAME = "company_announcements"


def load_strategy_rows(strategy_code: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read complete signals and compact latest-evaluation summaries for one strategy."""
    strategy_version = get_strategy(strategy_code).active_version
    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        if {STRATEGY_SIGNAL_TABLE_NAME, STRATEGY_EVALUATION_TABLE_NAME} - tables:
            return [], []
        signals = [dict(row) for row in connection.execute(
            """SELECT signal.cb_code, signal.trade_date, signal.strategy_code,
                      signal.strategy_version, signal.strategy_name,
                      signal.condition_results_json, signal.condition_values_json,
                      signal.data_status, daily.cb_name, daily.close_price, daily.volume_lots,
                      master.put_date, master.maturity_date
               FROM strategy_signals AS signal
               LEFT JOIN cb_daily AS daily
                 ON daily.cb_code = signal.cb_code AND daily.trade_date = signal.trade_date
               LEFT JOIN cb_master AS master ON master.cb_code = signal.cb_code
               WHERE signal.strategy_code = ? AND signal.strategy_version = ?
               ORDER BY signal.trade_date DESC, signal.cb_code ASC""",
            (strategy_code, strategy_version)
        )]
        evaluations = [dict(row) for row in connection.execute(
            """WITH latest AS (
                   SELECT cb_code, trade_date, strategy_code, strategy_version,
                          MAX(evaluation_id) AS evaluation_id
                   FROM strategy_evaluations
                   WHERE strategy_code = ? AND strategy_version = ?
                   GROUP BY cb_code, trade_date, strategy_code, strategy_version
               ), current AS (
                   SELECT evaluation.trade_date, evaluation.strategy_code,
                          evaluation.strategy_version, evaluation.data_status,
                          evaluation.unavailable_reasons_json
                   FROM latest
                   INNER JOIN strategy_evaluations AS evaluation
                     ON evaluation.evaluation_id = latest.evaluation_id
                   WHERE evaluation.cb_code != '__RUN__'
               ), available AS (
                   SELECT trade_date, strategy_code, strategy_version, data_status,
                          NULL AS unavailable_reason, COUNT(*) AS evaluation_count
                   FROM current WHERE data_status = 'AVAILABLE'
                   GROUP BY trade_date, strategy_code, strategy_version, data_status
               ), unavailable AS (
                   SELECT current.trade_date, current.strategy_code, current.strategy_version,
                          current.data_status, json_each.value AS unavailable_reason,
                          COUNT(*) AS evaluation_count
                   FROM current, json_each(current.unavailable_reasons_json)
                   WHERE current.data_status = 'UNAVAILABLE'
                   GROUP BY current.trade_date, current.strategy_code, current.strategy_version,
                            current.data_status, json_each.value
               )
               SELECT * FROM available UNION ALL SELECT * FROM unavailable
               ORDER BY trade_date DESC, data_status ASC, unavailable_reason ASC""",
            (strategy_code, strategy_version)
        )]
    for row in signals:
        row["condition_results"] = json.loads(row.pop("condition_results_json"))
        row["condition_values"] = json.loads(row.pop("condition_values_json"))
    return signals, evaluations


def load_strategy_a_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Compatibility helper retained for Strategy A consumers and tests."""
    signals, evaluations, _ = load_strategy_a_rows_with_source()
    return signals, evaluations


def select_active_strategy_a_run(connection: sqlite3.Connection) -> dict[str, object] | None:
    """Select the registry-active published A baseline, never an ordinary run."""
    required_tables = {
        STRATEGY_DEFINITION_TABLE_NAME,
        STRATEGY_RUN_TABLE_NAME,
        STRATEGY_RUN_SIGNAL_TABLE_NAME,
        STRATEGY_RUN_EVALUATION_TABLE_NAME,
        STRATEGY_PUBLISHED_SERIES_TABLE_NAME,
        STRATEGY_PUBLISHED_DATE_TABLE_NAME,
    }
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    if not required_tables.issubset(tables):
        return None
    strategy = get_strategy("A")
    row = connection.execute(
        """SELECT series.baseline_run_id, definition.definition_id, definition.strategy_code,
                  definition.strategy_version, definition.strategy_name, run.start_date, run.end_date
           FROM strategy_published_series AS series
           INNER JOIN strategy_run AS run ON run.run_id = series.baseline_run_id
           INNER JOIN strategy_definition AS definition ON definition.definition_id = series.definition_id
           WHERE series.strategy_code = ?
             AND definition.strategy_code = ?
             AND definition.strategy_version = ?
             AND run.definition_id = definition.definition_id
             AND run.status = 'COMPLETED'
           LIMIT 1""",
        (strategy.strategy_code, strategy.strategy_code, strategy.active_version),
    ).fetchone()
    if row is None:
        return None
    selected = dict(row)
    override = connection.execute(
        """SELECT COUNT(*), MAX(trade_date) FROM strategy_published_date
           WHERE definition_id=?""",
        (row["definition_id"],),
    ).fetchone()
    selected["coverage"] = {
        "baseline_start_date": row["start_date"], "baseline_end_date": row["end_date"],
        "published_through_date": max(row["end_date"], override[1]) if override[1] else row["end_date"],
        "override_date_count": override[0],
    }
    return selected


def load_strategy_a_rows_with_source() -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Load A from its latest completed active-version run, or from the legacy snapshot."""
    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        active_run = select_active_strategy_a_run(connection)
        if active_run is None:
            signals, evaluations = load_strategy_rows("A")
            return signals, evaluations, {
                "source": "LEGACY", "definition_id": None, "baseline_run_id": None,
            }
        baseline_run_id = active_run["baseline_run_id"]
        signals = [dict(row) for row in connection.execute(
            """WITH published_dates AS (
                   SELECT published.trade_date, published.run_id
                   FROM strategy_published_date AS published
                   INNER JOIN strategy_run AS run ON run.run_id = published.run_id
                   WHERE published.definition_id = ? AND run.definition_id = published.definition_id
                     AND run.status = 'COMPLETED'
                     AND EXISTS (SELECT 1 FROM strategy_run_evaluations AS evaluation
                                 WHERE evaluation.run_id = published.run_id
                                   AND evaluation.trade_date = published.trade_date)
               ), selected_signals AS (
                   SELECT signal.cb_code, signal.trade_date, signal.condition_results_json,
                          signal.condition_values_json
                   FROM strategy_run_signals AS signal
                   WHERE signal.run_id = ?
                     AND NOT EXISTS (SELECT 1 FROM published_dates
                                     WHERE published_dates.trade_date = signal.trade_date)
                   UNION ALL
                   SELECT signal.cb_code, signal.trade_date, signal.condition_results_json,
                          signal.condition_values_json
                   FROM strategy_run_signals AS signal
                   INNER JOIN published_dates
                     ON published_dates.run_id = signal.run_id
                    AND published_dates.trade_date = signal.trade_date
               )
               SELECT signal.cb_code, signal.trade_date, definition.strategy_code,
                      definition.strategy_version, definition.strategy_name,
                      signal.condition_results_json, signal.condition_values_json,
                      'AVAILABLE' AS data_status, daily.cb_name, daily.close_price, daily.volume_lots,
                      master.put_date, master.maturity_date
               FROM selected_signals AS signal
               LEFT JOIN cb_daily AS daily
                 ON daily.cb_code = signal.cb_code AND daily.trade_date = signal.trade_date
               LEFT JOIN cb_master AS master ON master.cb_code = signal.cb_code
               CROSS JOIN strategy_definition AS definition
               WHERE definition.definition_id = ?
               ORDER BY signal.trade_date DESC, signal.cb_code ASC""",
            (active_run["definition_id"], baseline_run_id, active_run["definition_id"]),
        )]
        evaluations = [dict(row) for row in connection.execute(
            """WITH published_dates AS (
                   SELECT published.trade_date, published.run_id
                   FROM strategy_published_date AS published
                   INNER JOIN strategy_run AS run ON run.run_id = published.run_id
                   WHERE published.definition_id = ? AND run.definition_id = published.definition_id
                     AND run.status = 'COMPLETED'
                     AND EXISTS (SELECT 1 FROM strategy_run_evaluations AS cached
                                 WHERE cached.run_id = published.run_id
                                   AND cached.trade_date = published.trade_date)
               ), current AS (
                   SELECT evaluation.trade_date, definition.strategy_code, definition.strategy_version,
                          evaluation.data_status, evaluation.unavailable_reasons_json
                   FROM strategy_run_evaluations AS evaluation
                   CROSS JOIN strategy_definition AS definition
                   WHERE evaluation.run_id = ? AND definition.definition_id = ?
                     AND NOT EXISTS (SELECT 1 FROM published_dates
                                     WHERE published_dates.trade_date = evaluation.trade_date)
                   UNION ALL
                   SELECT evaluation.trade_date, definition.strategy_code, definition.strategy_version,
                          evaluation.data_status, evaluation.unavailable_reasons_json
                   FROM strategy_run_evaluations AS evaluation
                   INNER JOIN published_dates
                     ON published_dates.run_id = evaluation.run_id
                    AND published_dates.trade_date = evaluation.trade_date
                   CROSS JOIN strategy_definition AS definition
                   WHERE definition.definition_id = ?
               ), available AS (
                   SELECT trade_date, strategy_code, strategy_version, data_status,
                          NULL AS unavailable_reason, COUNT(*) AS evaluation_count
                   FROM current WHERE data_status = 'AVAILABLE'
                   GROUP BY trade_date, strategy_code, strategy_version, data_status
               ), unavailable AS (
                   SELECT current.trade_date, current.strategy_code, current.strategy_version,
                          current.data_status, json_each.value AS unavailable_reason,
                          COUNT(*) AS evaluation_count
                   FROM current, json_each(current.unavailable_reasons_json)
                   WHERE current.data_status = 'UNAVAILABLE'
                   GROUP BY current.trade_date, current.strategy_code, current.strategy_version,
                            current.data_status, json_each.value
               )
               SELECT * FROM available UNION ALL SELECT * FROM unavailable
               ORDER BY trade_date DESC, data_status ASC, unavailable_reason ASC""",
            (active_run["definition_id"], baseline_run_id, active_run["definition_id"], active_run["definition_id"]),
        )]
    for row in signals:
        row["condition_results"] = json.loads(row.pop("condition_results_json"))
        row["condition_values"] = json.loads(row.pop("condition_values_json"))
    return signals, evaluations, {
        "source": "RUN_CACHE", "definition_id": active_run["definition_id"],
        "baseline_run_id": baseline_run_id,
        "coverage": active_run["coverage"],
    }


def load_announcements(limit: int = 12) -> list[dict[str, object]]:
    """Export saved, material announcements for issuers with an active CB."""
    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({ANNOUNCEMENT_TABLE_NAME})")
        }
        required = {"company_code", "company_name", "subject"}
        if not required.issubset(columns):
            return []
        date_column = "fact_date" if "fact_date" in columns else "api_batch_date"
        time_column = "spoken_time" if "spoken_time" in columns else "NULL"
        material_terms = (
            "%可轉換公司債%", "%可轉債%", "%公司債%", "%轉換價格%", "%轉換價%",
            "%提前贖回%", "%強制贖回%", "%到期%", "%下市%", "%庫藏股%",
            "%現金增資%", "%私募%", "%併購%", "%合併%", "%收購%", "%重大處分%",
            "%重大資產%", "%重大財務%", "%重大營運%",
        )
        subject_filter = " OR ".join("lower(announcement.subject) LIKE ?" for _ in material_terms)
        return [dict(row) for row in connection.execute(
            f"""WITH latest_trade_date AS (
                    SELECT MAX(trade_date) AS trade_date FROM cb_daily
                ), active_issuers AS (
                    SELECT DISTINCT master.stock_code
                    FROM cb_master AS master, latest_trade_date
                    WHERE master.issue_date <= latest_trade_date.trade_date
                      AND (master.delisting_date IS NULL OR master.delisting_date > latest_trade_date.trade_date)
                )
                SELECT announcement.company_code, announcement.company_name,
                       announcement.{date_column} AS announcement_date,
                       announcement.{time_column} AS announcement_time, announcement.subject
                FROM {ANNOUNCEMENT_TABLE_NAME} AS announcement
                WHERE announcement.company_code IN (SELECT stock_code FROM active_issuers)
                  AND ({subject_filter})
                ORDER BY announcement_date DESC, announcement_time DESC
                LIMIT ?""",
            (*material_terms, limit),
        )]


def balance_units_for_display(
    issue_amount: int | None, issue_units: int | None, balance_amount: int | None
) -> int | None:
    """Return whole CB units only when the official amounts define them exactly."""
    if balance_amount is None:
        return None
    if issue_amount is None or issue_units is None:
        raise RuntimeError("cb_master balance is missing its official issue basis")
    if issue_amount <= 0 or issue_units <= 0 or issue_amount % issue_units != 0:
        raise RuntimeError("cb_master issue amount/units cannot define a par value")
    par_value = issue_amount // issue_units
    if balance_amount < 0 or balance_amount % par_value != 0:
        raise RuntimeError("cb_master balance amount is not a whole CB unit")
    return balance_amount // par_value


def remaining_days(
    trade_date: str,
    put_date: str | None,
    maturity_date: str | None,
    delisting_date: str | None = None,
    delisting_reason: str | None = None,
) -> int | None:
    """Use a redemption lifecycle countdown; otherwise retain deadline behavior."""
    as_of = date.fromisoformat(trade_date)
    if delisting_date is not None and delisting_reason == "已贖回":
        return max((date.fromisoformat(delisting_date) - as_of).days, 0)
    if delisting_date is not None and as_of >= date.fromisoformat(delisting_date):
        return 0
    candidates = [
        (date.fromisoformat(value) - as_of).days
        for value in (put_date, maturity_date)
        if value is not None and date.fromisoformat(value) >= as_of
    ]
    return min(candidates) if candidates else None


def balance_ratio(
    balance_amount: int | None, issue_units: int | None
) -> float | None:
    if balance_amount is None or issue_units is None or issue_units <= 0:
        return None
    return balance_amount / 100_000 / issue_units * 100


def add_cb_rolling_averages(records: list[dict[str, object]]) -> None:
    """Add display-only CB rolling averages from observed cb_daily rows.

    Rows are grouped by CB and ordered by their actual trade dates.  This never
    invents non-trading-day rows or substitutes missing prices; a metric remains
    None until its complete window of observed values is available.
    """
    by_code: dict[str, dict[str, dict[str, object]]] = {}
    for record in records:
        by_code.setdefault(str(record["cb_code"]), {})[str(record["trade_date"])] = record
    calendar = sorted({str(record["trade_date"]) for record in records})
    for observed_by_date in by_code.values():
        for index, trade_date in enumerate(calendar):
            record = observed_by_date.get(trade_date)
            if record is None:
                continue
            for key, source, window in (
                ("volume_ma5", "volume_lots", 5),
                ("volume_ma10", "volume_lots", 10),
                ("price_ma20", "effective_cb_price", 20),
                ("price_ma43", "effective_cb_price", 43),
            ):
                dates = calendar[index - window + 1:index + 1]
                values = [observed_by_date[day][source] for day in dates if day in observed_by_date]
                record[key] = (
                    sum(values) / window
                    if len(dates) == window and len(values) == window and all(value is not None for value in values)
                    else None
                )


def add_display_averages_to_signals(
    signals: list[dict[str, object]], records: list[dict[str, object]]
) -> None:
    """Expose precomputed dashboard metrics beside saved strategy snapshots."""
    metrics = {
        (str(record["trade_date"]), str(record["cb_code"])): record
        for record in records
    }
    for signal in signals:
        record = metrics.get((str(signal["trade_date"]), str(signal["cb_code"])))
        for key in ("volume_ma5", "volume_ma10", "price_ma20", "price_ma43"):
            signal[key] = record[key] if record is not None else None


def load_rows() -> list[dict[str, object]]:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"SQLite database not found: {DB_PATH}")

    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        for table_name, required_columns in (
            (TABLE_NAME, DAILY_REQUIRED_COLUMNS),
            (MASTER_TABLE_NAME, MASTER_REQUIRED_COLUMNS),
            (STOCK_DAILY_TABLE_NAME, STOCK_DAILY_REQUIRED_COLUMNS),
            (CONVERSION_EVENT_TABLE_NAME, CONVERSION_EVENT_REQUIRED_COLUMNS),
        ):
            if table_name not in tables:
                raise RuntimeError(f"Required SQLite table not found: {table_name}")
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            missing = required_columns - columns
            if missing:
                raise RuntimeError(
                    f"Required SQLite columns missing from {table_name}: {sorted(missing)}"
                )

        cursor = connection.execute(
            f"""
            SELECT
                daily.trade_date,
                daily.cb_code,
                daily.cb_name,
                daily.close_price,
                daily.reference_price,
                {effective_cb_price_sql("daily.")} AS effective_cb_price,
                {effective_cb_price_source_sql("daily.")} AS effective_cb_price_source,
                daily.volume_lots,
                stock.p_close_price,
                COALESCE(stock.p_market_volume_shares, stock.p_volume_shares) AS p_volume_shares,
                (
                    SELECT event.conversion_price
                    FROM conversion_price_events AS event
                    WHERE event.cb_code = daily.cb_code
                      AND event.effective_date <= daily.trade_date
                    ORDER BY event.effective_date DESC
                    LIMIT 1
                ) AS conversion_price_on_trade_date,
                master.issue_date,
                master.maturity_date,
                master.put_date,
                master.issue_units,
                master.issue_amount,
                master.balance_amount,
                master.balance_date,
                master.current_conversion_price,
                master.current_conversion_price_effective_date,
                master.is_secured,
                master.delisting_date,
                master.delisting_reason
            FROM cb_daily AS daily
            LEFT JOIN cb_master AS master ON master.cb_code = daily.cb_code
            LEFT JOIN stock_daily_market AS stock
              ON stock.trade_date = daily.trade_date
             AND stock.p_stock_code = master.stock_code
            ORDER BY daily.trade_date DESC, daily.cb_code ASC
            """
        )
        records = []
        for row in cursor:
            record = dict(row)
            conversion_price = record.pop("conversion_price_on_trade_date")
            p_volume_shares = record.pop("p_volume_shares")
            issue_amount = record.pop("issue_amount")
            issue_units = record["issue_units"]
            balance_amount = record.pop("balance_amount")
            record["issue_amount_yi"] = (
                issue_amount / 100_000_000 if issue_amount is not None else None
            )
            record["balance_units"] = balance_units_for_display(
                issue_amount, issue_units, balance_amount
            )
            record["remaining_days"] = remaining_days(
                str(record["trade_date"]), record["put_date"], record["maturity_date"],
                record["delisting_date"], record["delisting_reason"],
            )
            record["balance_ratio"] = balance_ratio(balance_amount, issue_units)
            record["p_volume_lots"] = (
                p_volume_shares // 1_000 if p_volume_shares is not None else None
            )
            record["conversion_value"] = None
            record["premium_rate"] = None
            if conversion_price is not None and record["p_close_price"] is not None:
                if conversion_price <= 0:
                    raise RuntimeError(
                        "conversion_price_events conversion price must be positive"
                    )
                conversion_value = round(
                    record["p_close_price"] / conversion_price * 100, 8
                )
                record["conversion_value"] = conversion_value
                valuation_price = record["effective_cb_price"]
                if valuation_price is not None and conversion_value != 0:
                    record["premium_rate"] = round(
                        (valuation_price / conversion_value - 1) * 100, 8
                    )
            record["is_secured"] = (
                "有" if record["is_secured"] == 1
                else "無" if record["is_secured"] == 0
                else "未知" if record["is_secured"] is None
                else _invalid_is_secured(record["is_secured"])
            )
            records.append(record)
        add_cb_rolling_averages(records)
        return records


def load_institutional_rows() -> list[dict[str, object]]:
    """Read already-derived parent flow metrics for each active CB; never recompute."""
    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for table_name, required_columns in (
            (PARENT_FLOW_TABLE_NAME, PARENT_FLOW_REQUIRED_COLUMNS),
            (INSTITUTIONAL_COVERAGE_TABLE_NAME, INSTITUTIONAL_COVERAGE_REQUIRED_COLUMNS),
            (ETF_STATUS_TABLE_NAME, ETF_STATUS_REQUIRED_COLUMNS),
        ):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            missing = required_columns - columns
            if missing:
                raise RuntimeError(f"Required SQLite columns missing from {table_name}: {sorted(missing)}")
        return [dict(row) for row in connection.execute(
            """
            WITH etf_coverage AS (
                SELECT trade_date,
                       CASE WHEN COUNT(*) = 5 AND SUM(status = 'succeeded') = 5
                            THEN 'complete' ELSE 'incomplete' END AS active_etf_coverage
                FROM active_etf_collection_status
                WHERE etf_code IN ('00980A','00985A','00999A','00982A','00992A')
                GROUP BY trade_date
            )
            SELECT daily.trade_date, daily.cb_code, daily.cb_name,
                   master.stock_code AS parent_stock_code,
                   master.stock_name AS parent_stock_name,
                   metrics.foreign_status, metrics.foreign_net_lots,
                   metrics.foreign_volume_pct, metrics.foreign_streak_days,
                   metrics.foreign_streak_lots, metrics.trust_status,
                   metrics.trust_net_lots, metrics.trust_volume_pct,
                   metrics.trust_streak_days, metrics.trust_streak_lots,
                   metrics.active_etf_status, metrics.active_etf_change_lots,
                   metrics.active_etf_change_value_twd, metrics.active_etf_streak_days,
                   metrics.active_etf_streak_lots, coverage.reason AS institutional_reason,
                   COALESCE(etf_coverage.active_etf_coverage, 'incomplete') AS active_etf_coverage
            FROM cb_daily AS daily
            INNER JOIN cb_master AS master ON master.cb_code = daily.cb_code
            INNER JOIN parent_flow_metrics AS metrics
              ON metrics.trade_date = daily.trade_date AND metrics.stock_code = master.stock_code
            LEFT JOIN institutional_coverage AS coverage
              ON coverage.trade_date = daily.trade_date AND coverage.stock_code = master.stock_code
            LEFT JOIN etf_coverage ON etf_coverage.trade_date = daily.trade_date
            WHERE master.issue_date <= daily.trade_date
              AND (master.delisting_date IS NULL OR master.delisting_date > daily.trade_date)
            ORDER BY daily.trade_date DESC, daily.cb_code ASC
            """
        )]


def _invalid_is_secured(value: object) -> None:
    raise RuntimeError(f"Invalid cb_master.is_secured value: {value!r}")


def dashboard_payload() -> dict[str, object]:
    """Read and derive the complete legacy payload without writing an artifact."""
    rows = load_rows()
    institutional_rows = load_institutional_rows()
    strategy_a_signals, strategy_a_evaluations, strategy_a_source = load_strategy_a_rows_with_source()
    strategy_rows = {code: load_strategy_rows(code) for code in active_strategy_codes() if code != "A"}
    strategy_b_signals, strategy_b_evaluations = strategy_rows["B"]
    strategy_c_signals, strategy_c_evaluations = strategy_rows["C"]
    strategy_g_signals, strategy_g_evaluations = strategy_rows["G"]
    for signals in (strategy_a_signals, strategy_b_signals, strategy_c_signals, strategy_g_signals):
        add_display_averages_to_signals(signals, rows)
    announcements = load_announcements()
    return {
        # Additive provenance; existing strategy collections retain their established schema.
        "metadata": {"strategy_sources": {"A": strategy_a_source}},
        "records": rows,
        "institutional_records": institutional_rows,
        "announcements": announcements,
        # Generic collections let pages show all saved strategies together.
        "strategy_signals": [*strategy_a_signals, *strategy_b_signals, *strategy_c_signals, *strategy_g_signals],
        "strategy_evaluations": [*strategy_a_evaluations, *strategy_b_evaluations, *strategy_c_evaluations, *strategy_g_evaluations],
        # Keep the established Strategy A contract for existing pages and consumers.
        "strategy_a_signals": strategy_a_signals,
        "strategy_a_evaluations": strategy_a_evaluations,
        "strategy_b_signals": strategy_b_signals,
        "strategy_b_evaluations": strategy_b_evaluations,
        "strategy_c_signals": strategy_c_signals,
        "strategy_c_evaluations": strategy_c_evaluations,
        "strategy_g_signals": strategy_g_signals,
        "strategy_g_evaluations": strategy_g_evaluations,
    }


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _write_json(path: Path, value: object) -> int:
    encoded = _json_bytes(value)
    if len(encoded) > MAX_SHARD_BYTES:
        raise RuntimeError(f"Dashboard shard exceeds {MAX_SHARD_BYTES} bytes: {path} ({len(encoded)} bytes)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return len(encoded)


def _month_groups(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        trade_date = row.get("trade_date")
        if not isinstance(trade_date, str) or len(trade_date) < 7:
            raise RuntimeError("Dashboard row has no ISO trade_date for shard routing")
        groups.setdefault(trade_date[:7], []).append(row)
    return groups


def _file_metadata(path: Path, root: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    encoded = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "records": len(rows),
        "dates": sorted({str(row["trade_date"]) for row in rows}, reverse=True),
    }


def _copy_if_changed(source: Path, destination: Path) -> bool:
    if destination.is_file() and source.read_bytes() == destination.read_bytes():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def build_dashboard_shards() -> tuple[int, int]:
    """Build validated static data shards; published files change only when bytes differ.

    A failed build never reaches Git publication: every artifact is first written and
    cross-checked in an isolated staging directory.  The workflow commits the full
    manifest/artifact set atomically after this function succeeds.
    """
    payload = dashboard_payload()
    records = payload["records"]
    institutional = payload["institutional_records"]
    assert isinstance(records, list) and isinstance(institutional, list)
    record_groups = _month_groups(records)
    institutional_groups = _month_groups(institutional)
    latest_date = max(str(row["trade_date"]) for row in records)
    with tempfile.TemporaryDirectory(prefix="cb-radar-dashboard-") as directory:
        staging = Path(directory) / "v2"
        market_manifest = {}
        institutional_manifest = {}
        for month, rows in sorted(record_groups.items()):
            path = staging / "market" / f"{month}.json"
            _write_json(path, {"schema_version": SHARD_SCHEMA_VERSION, "month": month, "records": rows})
            market_manifest[month] = _file_metadata(path, staging, rows)
        for month, rows in sorted(institutional_groups.items()):
            path = staging / "institutional" / f"{month}.json"
            _write_json(path, {"schema_version": SHARD_SCHEMA_VERSION, "month": month, "institutional_records": rows})
            institutional_manifest[month] = _file_metadata(path, staging, rows)
        strategy_manifest = {}
        for code in active_strategy_codes():
            lower = code.lower()
            signals = payload[f"strategy_{lower}_signals"]
            evaluations = payload[f"strategy_{lower}_evaluations"]
            assert isinstance(signals, list) and isinstance(evaluations, list)
            strategy_payload = {"schema_version": SHARD_SCHEMA_VERSION, "strategy_code": code,
                                "signals": signals, "evaluations": evaluations}
            if code == "A":
                strategy_payload["source"] = payload["metadata"]["strategy_sources"]["A"]
            path = staging / "strategies" / f"{code}.json"
            _write_json(path, strategy_payload)
            strategy_manifest[code] = _file_metadata(path, staging, [*signals, *evaluations])
        latest_payload = {
            "schema_version": SHARD_SCHEMA_VERSION,
            "trade_date": latest_date,
            "records": [row for row in records if row["trade_date"] == latest_date],
            "institutional_records": [row for row in institutional if row["trade_date"] == latest_date],
            "strategy_signals": [row for row in payload["strategy_signals"] if row["trade_date"] == latest_date],
            "announcements": payload["announcements"],
        }
        _write_json(staging / "latest.json", latest_payload)
        manifest = {
            "schema_version": SHARD_SCHEMA_VERSION,
            "latest_trade_date": latest_date,
            "market_dates": sorted({str(row["trade_date"]) for row in records}, reverse=True),
            "institutional_dates": sorted({str(row["trade_date"]) for row in institutional}, reverse=True),
            "market_months": market_manifest,
            "institutional_months": institutional_manifest,
            "strategies": strategy_manifest,
        }
        _write_json(staging / "manifest.json", manifest)
        for metadata in [*market_manifest.values(), *institutional_manifest.values(), *strategy_manifest.values()]:
            candidate = staging / str(metadata["path"])
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != metadata["sha256"]:
                raise RuntimeError(f"Dashboard shard checksum validation failed: {candidate}")
        changed = 0
        for source in staging.rglob("*.json"):
            if source.name == "manifest.json":
                continue
            changed += _copy_if_changed(source, SHARD_ROOT / source.relative_to(staging))
        # Publish the index only after every referenced artifact has been installed.
        changed += _copy_if_changed(staging / "manifest.json", SHARD_ROOT / "manifest.json")
    # Compatibility is deliberately small: legacy strategy pages retain their exact
    # payload contract while historical market/flow rows are loaded from shards.
    g_keys = {(row["trade_date"], row["cb_code"]) for row in payload["strategy_g_signals"]}
    latest_institutional = [row for row in institutional if row["trade_date"] == latest_date]
    legacy_payload = {
        "strategy_a_signals": payload["strategy_a_signals"], "strategy_a_evaluations": payload["strategy_a_evaluations"],
        "strategy_b_signals": payload["strategy_b_signals"], "strategy_b_evaluations": payload["strategy_b_evaluations"],
        "strategy_c_signals": payload["strategy_c_signals"], "strategy_c_evaluations": payload["strategy_c_evaluations"],
        "strategy_g_signals": payload["strategy_g_signals"], "strategy_g_evaluations": payload["strategy_g_evaluations"],
        "records": [row for row in records if (row["trade_date"], row["cb_code"]) in g_keys],
        "institutional_records": [*latest_institutional,
                                  *[{"trade_date": value} for value in sorted({str(row["trade_date"]) for row in institutional if row["trade_date"] != latest_date})]],
    }
    _write_json(OUTPUT_PATH, legacy_payload)
    return len(records), changed


def build_dashboard_data() -> tuple[int, int]:
    """Compatibility writer retained for direct legacy-payload tests only."""
    payload = dashboard_payload()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    records = payload["records"]
    assert isinstance(records, list)
    return len(records), OUTPUT_PATH.stat().st_size


def main() -> None:
    records, changed = build_dashboard_shards()
    print(f"database: {DB_PATH.relative_to(ROOT).as_posix()}")
    print(f"output: {SHARD_ROOT.relative_to(ROOT).as_posix()}")
    print(f"records: {records}")
    print(f"changed shards: {changed}")


if __name__ == "__main__":
    main()

