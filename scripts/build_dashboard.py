"""Build the static GitHub Pages data file from the tracked SQLite database."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "cb_history.db"
OUTPUT_PATH = ROOT / "docs" / "data.json"
TABLE_NAME = "cb_daily"
MASTER_TABLE_NAME = "cb_master"
STOCK_DAILY_TABLE_NAME = "stock_daily_market"
CONVERSION_EVENT_TABLE_NAME = "conversion_price_events"
PARENT_FLOW_TABLE_NAME = "parent_flow_metrics"
INSTITUTIONAL_COVERAGE_TABLE_NAME = "institutional_coverage"
ETF_STATUS_TABLE_NAME = "active_etf_collection_status"
STRATEGY_SIGNAL_TABLE_NAME = "strategy_signals"
STRATEGY_EVALUATION_TABLE_NAME = "strategy_evaluations"
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


def load_strategy_a_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read saved A-v1 snapshots only; legacy DBs simply have no strategy data."""
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
                      signal.data_status, daily.cb_name, daily.close_price, daily.volume_lots
               FROM strategy_signals AS signal
               LEFT JOIN cb_daily AS daily
                 ON daily.cb_code = signal.cb_code AND daily.trade_date = signal.trade_date
               WHERE signal.strategy_code = 'A' AND signal.strategy_version = 'v1'
               ORDER BY signal.trade_date DESC, signal.cb_code ASC"""
        )]
        evaluations = [dict(row) for row in connection.execute(
            """WITH latest AS (
                   SELECT cb_code, trade_date, strategy_code, strategy_version,
                          MAX(evaluation_id) AS evaluation_id
                   FROM strategy_evaluations
                   WHERE strategy_code = 'A' AND strategy_version = 'v1'
                   GROUP BY cb_code, trade_date, strategy_code, strategy_version
               )
               SELECT evaluation.cb_code, evaluation.trade_date, evaluation.strategy_code,
                      evaluation.strategy_version, evaluation.strategy_name,
                      evaluation.condition_results_json, evaluation.condition_values_json,
                      evaluation.data_status, evaluation.unavailable_reasons_json,
                      daily.cb_name
               FROM latest
               INNER JOIN strategy_evaluations AS evaluation
                 ON evaluation.evaluation_id = latest.evaluation_id
               LEFT JOIN cb_daily AS daily
                 ON daily.cb_code = evaluation.cb_code AND daily.trade_date = evaluation.trade_date
               WHERE evaluation.cb_code != '__RUN__'
               ORDER BY evaluation.trade_date DESC, evaluation.cb_code ASC"""
        )]
    for row in signals + evaluations:
        row["condition_results"] = json.loads(row.pop("condition_results_json"))
        row["condition_values"] = json.loads(row.pop("condition_values_json"))
        if "unavailable_reasons_json" in row:
            row["unavailable_reasons"] = json.loads(row.pop("unavailable_reasons_json"))
    return signals, evaluations


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
            """
            SELECT
                daily.trade_date,
                daily.cb_code,
                daily.cb_name,
                daily.close_price,
                daily.reference_price,
                daily.volume_lots,
                stock.p_close_price,
                stock.p_volume_shares,
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
                valuation_price = (
                    record["close_price"]
                    if record["volume_lots"] > 0
                    else record["reference_price"]
                )
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


def build_dashboard_data() -> tuple[int, int]:
    rows = load_rows()
    institutional_rows = load_institutional_rows()
    strategy_signals, strategy_evaluations = load_strategy_a_rows()
    payload = {
        "records": rows,
        "institutional_records": institutional_rows,
        "strategy_a_signals": strategy_signals,
        "strategy_a_evaluations": strategy_evaluations,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return len(rows), OUTPUT_PATH.stat().st_size


def main() -> None:
    records, size = build_dashboard_data()
    print(f"database: {DB_PATH.relative_to(ROOT).as_posix()}")
    print(f"output: {OUTPUT_PATH.relative_to(ROOT).as_posix()}")
    print(f"records: {records}")
    print(f"bytes: {size}")


if __name__ == "__main__":
    main()

