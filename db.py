import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA = """
CREATE TABLE IF NOT EXISTS cb_daily (
    trade_date TEXT NOT NULL,
    cb_code TEXT NOT NULL,
    cb_name TEXT NOT NULL,
    close_price REAL,
    reference_price REAL,
    volume_lots INTEGER NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, cb_code)
);

CREATE TABLE IF NOT EXISTS cb_master (
    cb_code TEXT PRIMARY KEY,
    cb_name TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    maturity_date TEXT NOT NULL,
    put_date TEXT,
    issue_units INTEGER,
    issue_amount INTEGER NOT NULL,
    balance_amount INTEGER,
    balance_date TEXT,
    current_conversion_price REAL,
    current_conversion_price_effective_date TEXT,
    is_secured INTEGER CHECK (is_secured IN (0, 1) OR is_secured IS NULL),
    delisting_date TEXT,
    delisting_reason TEXT CHECK (
        delisting_reason IN ('已贖回', '提前贖回', '到期', '已下市')
        OR delisting_reason IS NULL
    ),
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversion_price_events (
    cb_code TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    conversion_price REAL NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (cb_code, effective_date),
    FOREIGN KEY (cb_code) REFERENCES cb_master(cb_code)
);

CREATE TABLE IF NOT EXISTS cb_monthly_balance (
    cb_code TEXT NOT NULL,
    year_month TEXT NOT NULL,
    balance_amount INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (cb_code, year_month),
    FOREIGN KEY (cb_code) REFERENCES cb_master(cb_code)
);

CREATE TABLE IF NOT EXISTS stock_daily_market (
    trade_date TEXT NOT NULL,
    p_stock_code TEXT NOT NULL,
    p_open_price REAL,
    p_high_price REAL,
    p_low_price REAL,
    p_close_price REAL,
    p_volume_shares INTEGER NOT NULL CHECK (p_volume_shares >= 0),
    PRIMARY KEY (trade_date, p_stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_market_stock_date
    ON stock_daily_market (p_stock_code, trade_date);

CREATE TABLE IF NOT EXISTS cb_parent_stock_mapping (
    cb_code TEXT NOT NULL,
    mapping_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEX', 'TIB', 'UNKNOWN')),
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (cb_code, mapping_date)
);

CREATE INDEX IF NOT EXISTS idx_cb_parent_stock_mapping_date
    ON cb_parent_stock_mapping (mapping_date, stock_code);

CREATE TABLE IF NOT EXISTS cb_parent_stock_monthly_mapping (
    cb_code TEXT NOT NULL,
    year_month TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEX', 'TIB', 'UNKNOWN')),
    source TEXT NOT NULL CHECK (source = 'MOPS:t120sg01'),
    source_url TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (cb_code, year_month)
);

CREATE INDEX IF NOT EXISTS idx_cb_parent_stock_monthly_mapping_month
    ON cb_parent_stock_monthly_mapping (year_month, stock_code);

CREATE TABLE IF NOT EXISTS stock_daily_coverage (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEX', 'TIB', 'UNKNOWN')),
    status TEXT NOT NULL CHECK (status IN (
        'COMPLETE', 'OFFICIAL_ZERO', 'MISSING_CLOSE', 'MISSING_OFFICIAL_ROW',
        'SOURCE_ERROR'
    )),
    reason TEXT,
    source_url TEXT,
    response_date TEXT,
    mapping_level TEXT NOT NULL DEFAULT 'EXACT'
        CHECK (mapping_level IN ('EXACT', 'MONTHLY_VERIFIED')),
    mapping_source_url TEXT,
    mapping_year_month TEXT,
    mapping_verified_at TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_coverage_stock_date
    ON stock_daily_coverage (stock_code, trade_date);

CREATE TABLE IF NOT EXISTS institutional_daily (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    foreign_buy_shares INTEGER NOT NULL,
    foreign_sell_shares INTEGER NOT NULL,
    foreign_net_shares INTEGER NOT NULL,
    trust_buy_shares INTEGER NOT NULL,
    trust_sell_shares INTEGER NOT NULL,
    trust_net_shares INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_institutional_daily_stock_date
    ON institutional_daily (stock_code, trade_date);

CREATE TABLE IF NOT EXISTS institutional_coverage (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    market TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETE','OFFICIAL_ZERO','UNAVAILABLE_MARKET','SOURCE_ERROR')),
    reason TEXT,
    source_url TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);

CREATE TABLE IF NOT EXISTS active_etf_master (
    etf_code TEXT PRIMARY KEY,
    etf_name TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_identifier TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    last_status TEXT NOT NULL CHECK (last_status IN ('pending', 'succeeded', 'failed')),
    last_error TEXT,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_etf_holdings (
    trade_date TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    etf_name TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    holding_shares INTEGER NOT NULL CHECK (holding_shares >= 0),
    source_url TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, etf_code, stock_code),
    FOREIGN KEY (etf_code) REFERENCES active_etf_master(etf_code)
);

CREATE INDEX IF NOT EXISTS idx_active_etf_holdings_stock_date
    ON active_etf_holdings (stock_code, trade_date);

CREATE TABLE IF NOT EXISTS active_etf_collection_status (
    trade_date TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error_message TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, etf_code),
    FOREIGN KEY (etf_code) REFERENCES active_etf_master(etf_code)
);

CREATE TABLE IF NOT EXISTS parent_flow_metrics (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    foreign_status TEXT NOT NULL CHECK (foreign_status IN ('AVAILABLE', 'UNAVAILABLE')),
    foreign_net_lots REAL,
    foreign_volume_pct REAL,
    foreign_streak_days INTEGER,
    foreign_streak_lots REAL,
    trust_status TEXT NOT NULL CHECK (trust_status IN ('AVAILABLE', 'UNAVAILABLE')),
    trust_net_lots REAL,
    trust_volume_pct REAL,
    trust_streak_days INTEGER,
    trust_streak_lots REAL,
    active_etf_status TEXT NOT NULL CHECK (active_etf_status IN ('AVAILABLE', 'UNAVAILABLE')),
    active_etf_change_lots REAL,
    active_etf_change_value_twd REAL,
    active_etf_streak_days INTEGER,
    active_etf_streak_lots REAL,
    PRIMARY KEY (trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_parent_flow_metrics_stock_date
    ON parent_flow_metrics (stock_code, trade_date);

CREATE TABLE IF NOT EXISTS announcement_fetch (
    fetch_id INTEGER PRIMARY KEY,
    source_market TEXT NOT NULL CHECK (source_market IN ('TWSE', 'TPEX')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    http_status INTEGER,
    api_batch_date_roc TEXT,
    api_batch_date TEXT,
    row_count INTEGER,
    payload_sha256 TEXT,
    snapshot_id INTEGER,
    error_message TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES announcement_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS announcement_snapshot (
    snapshot_id INTEGER PRIMARY KEY,
    fetch_id INTEGER NOT NULL REFERENCES announcement_fetch(fetch_id),
    source_market TEXT NOT NULL CHECK (source_market IN ('TWSE', 'TPEX')),
    api_batch_date_roc TEXT NOT NULL,
    api_batch_date TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_market, api_batch_date, payload_sha256)
);

CREATE TABLE IF NOT EXISTS company_announcements (
    announcement_id INTEGER PRIMARY KEY,
    source_market TEXT NOT NULL CHECK (source_market IN ('TWSE', 'TPEX')),
    company_code TEXT NOT NULL,
    company_name TEXT NOT NULL,
    api_batch_date_roc TEXT NOT NULL,
    api_batch_date TEXT NOT NULL,
    spoken_date_roc TEXT NOT NULL,
    spoken_date TEXT NOT NULL,
    spoken_time TEXT NOT NULL,
    fact_date_roc TEXT,
    fact_date TEXT,
    clause TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    subject_sha256 TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_snapshot_id INTEGER NOT NULL REFERENCES announcement_snapshot(snapshot_id),
    last_snapshot_id INTEGER NOT NULL REFERENCES announcement_snapshot(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_company_announcements_company_date
    ON company_announcements (source_market, company_code, spoken_date);

CREATE INDEX IF NOT EXISTS idx_company_announcements_fact_date
    ON company_announcements (fact_date);

CREATE TABLE IF NOT EXISTS historical_company_announcements (
    historical_announcement_id INTEGER PRIMARY KEY,
    company_code TEXT NOT NULL,
    company_name TEXT NOT NULL,
    spoken_date TEXT NOT NULL,
    spoken_time TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type = 'MOPS_HISTORICAL_DETAIL'),
    event_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_historical_company_announcements_company_date
    ON historical_company_announcements (company_code, spoken_date);

CREATE TABLE IF NOT EXISTS strategy_signals (
    cb_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    condition_results_json TEXT NOT NULL,
    condition_values_json TEXT NOT NULL,
    data_status TEXT NOT NULL CHECK (data_status = 'AVAILABLE'),
    created_at TEXT NOT NULL,
    PRIMARY KEY (cb_code, trade_date, strategy_code, strategy_version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_signals_date_code
    ON strategy_signals (trade_date, strategy_code, strategy_version);

CREATE TABLE IF NOT EXISTS strategy_evaluations (
    evaluation_id INTEGER PRIMARY KEY,
    cb_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    strategy_code TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    condition_results_json TEXT NOT NULL,
    condition_values_json TEXT NOT NULL,
    data_status TEXT NOT NULL CHECK (data_status IN ('AVAILABLE', 'UNAVAILABLE')),
    unavailable_reasons_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_evaluations_lookup
    ON strategy_evaluations (cb_code, trade_date, strategy_code, strategy_version);
"""


def _migrate_delisting_reason_constraint(connection: sqlite3.Connection) -> None:
    """Allow the confirmed redemption lifecycle reason without losing history."""
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'cb_master'"
    ).fetchone()[0]
    if "'已贖回'" in table_sql:
        return
    columns = (
        "cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date, "
        "put_date, issue_units, issue_amount, balance_amount, balance_date, "
        "current_conversion_price, current_conversion_price_effective_date, "
        "is_secured, delisting_date, delisting_reason, source, source_url, collected_at"
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE cb_master_replacement (
            cb_code TEXT PRIMARY KEY, cb_name TEXT NOT NULL, stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL, issue_date TEXT NOT NULL, maturity_date TEXT NOT NULL,
            put_date TEXT, issue_units INTEGER, issue_amount INTEGER NOT NULL,
            balance_amount INTEGER, balance_date TEXT, current_conversion_price REAL,
            current_conversion_price_effective_date TEXT,
            is_secured INTEGER CHECK (is_secured IN (0, 1) OR is_secured IS NULL),
            delisting_date TEXT,
            delisting_reason TEXT CHECK (delisting_reason IN ('已贖回', '提前贖回', '到期', '已下市') OR delisting_reason IS NULL),
            source TEXT NOT NULL, source_url TEXT NOT NULL, collected_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"INSERT INTO cb_master_replacement ({columns}) SELECT {columns} FROM cb_master"
    )
    connection.execute("DROP TABLE cb_master")
    connection.execute("ALTER TABLE cb_master_replacement RENAME TO cb_master")
    connection.execute("PRAGMA foreign_keys = ON")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cb_master)")
    }
    if "issue_units" not in columns:
        connection.execute("ALTER TABLE cb_master ADD COLUMN issue_units INTEGER")
    if "is_secured" not in columns:
        connection.execute(
            "ALTER TABLE cb_master ADD COLUMN is_secured INTEGER "
            "CHECK (is_secured IN (0, 1) OR is_secured IS NULL)"
        )
    if "current_conversion_price_effective_date" not in columns:
        connection.execute(
            "ALTER TABLE cb_master ADD COLUMN "
            "current_conversion_price_effective_date TEXT"
        )
    if "balance_date" not in columns:
        connection.execute("ALTER TABLE cb_master ADD COLUMN balance_date TEXT")
    if "delisting_date" not in columns:
        connection.execute("ALTER TABLE cb_master ADD COLUMN delisting_date TEXT")
    if "delisting_reason" not in columns:
        connection.execute(
            "ALTER TABLE cb_master ADD COLUMN delisting_reason TEXT "
            "CHECK (delisting_reason IN ('已贖回', '提前贖回', '到期', '已下市') "
            "OR delisting_reason IS NULL)"
        )
    _migrate_delisting_reason_constraint(connection)
    daily_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cb_daily)")
    }
    if "reference_price" not in daily_columns:
        connection.execute("ALTER TABLE cb_daily ADD COLUMN reference_price REAL")
    coverage_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(stock_daily_coverage)")
    }
    if coverage_columns and "mapping_level" not in coverage_columns:
        connection.execute(
            "ALTER TABLE stock_daily_coverage ADD COLUMN mapping_level TEXT "
            "NOT NULL DEFAULT 'EXACT' CHECK (mapping_level IN ('EXACT', 'MONTHLY_VERIFIED'))"
        )
    if coverage_columns and "mapping_source_url" not in coverage_columns:
        connection.execute("ALTER TABLE stock_daily_coverage ADD COLUMN mapping_source_url TEXT")
    if coverage_columns and "mapping_year_month" not in coverage_columns:
        connection.execute("ALTER TABLE stock_daily_coverage ADD COLUMN mapping_year_month TEXT")
    if coverage_columns and "mapping_verified_at" not in coverage_columns:
        connection.execute("ALTER TABLE stock_daily_coverage ADD COLUMN mapping_verified_at TEXT")
    connection.commit()
    return connection


def upsert_master_data(
    connection: sqlite3.Connection,
    masters: Iterable[Mapping[str, object]],
    events: Iterable[Mapping[str, object]],
    balances: Iterable[Mapping[str, object]],
    excluded_codes: Iterable[str] = (),
    lifecycle_updates: Iterable[Mapping[str, object]] = (),
    as_of_date: date | None = None,
) -> tuple[int, int, int]:
    master_rows = [dict(row) for row in masters]
    if as_of_date is not None:
        for row in master_rows:
            balance_date = row.get("balance_date")
            if balance_date is not None and str(balance_date) > as_of_date.isoformat():
                raise ValueError(
                    f"balance_date for {row['cb_code']} is after run date: "
                    f"{balance_date} > {as_of_date.isoformat()}"
                )
    for row in master_rows:
        row.setdefault("balance_date", None)
        row.setdefault("delisting_date", None)
        row.setdefault("delisting_reason", None)
    event_rows = list(events)
    balance_rows = list(balances)
    if as_of_date is not None:
        for row in balance_rows:
            year_month = str(row["year_month"])
            date.fromisoformat(f"{year_month}-01")
            if year_month >= as_of_date.strftime("%Y-%m"):
                raise ValueError(
                    f"monthly balance for {row['cb_code']} is not from a completed "
                    f"month: {year_month} at {as_of_date.isoformat()}"
                )
    excluded_rows = [(code,) for code in excluded_codes]
    lifecycle_rows = list(lifecycle_updates)
    with connection:
        connection.executemany(
            "DELETE FROM conversion_price_events WHERE cb_code = ?", excluded_rows
        )
        connection.executemany(
            "DELETE FROM cb_monthly_balance WHERE cb_code = ?", excluded_rows
        )
        connection.executemany(
            "DELETE FROM cb_master WHERE cb_code = ?", excluded_rows
        )
        connection.executemany(
            """
            INSERT INTO cb_master (
                cb_code, cb_name, stock_code, stock_name, issue_date,
                maturity_date, put_date, issue_units, issue_amount, balance_amount,
                balance_date,
                current_conversion_price, current_conversion_price_effective_date,
                is_secured, delisting_date, delisting_reason, source, source_url,
                collected_at
            ) VALUES (
                :cb_code, :cb_name, :stock_code, :stock_name, :issue_date,
                :maturity_date, :put_date, :issue_units, :issue_amount, :balance_amount,
                :balance_date,
                :current_conversion_price, :current_conversion_price_effective_date,
                :is_secured, :delisting_date, :delisting_reason, :source,
                :source_url, :collected_at
            )
            ON CONFLICT(cb_code) DO UPDATE SET
                cb_name = excluded.cb_name,
                stock_code = excluded.stock_code,
                stock_name = excluded.stock_name,
                issue_date = excluded.issue_date,
                maturity_date = excluded.maturity_date,
                put_date = excluded.put_date,
                issue_units = excluded.issue_units,
                issue_amount = excluded.issue_amount,
                balance_amount = excluded.balance_amount,
                balance_date = excluded.balance_date,
                current_conversion_price = excluded.current_conversion_price,
                current_conversion_price_effective_date =
                    excluded.current_conversion_price_effective_date,
                is_secured = excluded.is_secured,
                delisting_date = COALESCE(excluded.delisting_date, cb_master.delisting_date),
                delisting_reason = CASE
                    WHEN excluded.delisting_reason IS NULL THEN cb_master.delisting_reason
                    WHEN cb_master.delisting_reason IS NULL THEN excluded.delisting_reason
                    WHEN cb_master.delisting_reason = '已下市'
                         AND excluded.delisting_reason != '已下市'
                    THEN excluded.delisting_reason
                    ELSE cb_master.delisting_reason
                END,
                source = excluded.source,
                source_url = excluded.source_url,
                collected_at = excluded.collected_at
            """,
            master_rows,
        )
        connection.executemany(
            """
            UPDATE cb_master
            SET delisting_date = COALESCE(:delisting_date, delisting_date),
                delisting_reason = CASE
                    WHEN :delisting_reason IS NULL THEN delisting_reason
                    WHEN delisting_reason IS NULL THEN :delisting_reason
                    WHEN delisting_reason = '已下市'
                         AND :delisting_reason != '已下市'
                    THEN :delisting_reason
                    ELSE delisting_reason
                END
            WHERE cb_code = :cb_code
            """,
            lifecycle_rows,
        )
        connection.executemany(
            """
            INSERT INTO conversion_price_events (
                cb_code, effective_date, conversion_price,
                source, source_url, collected_at
            ) VALUES (
                :cb_code, :effective_date, :conversion_price,
                :source, :source_url, :collected_at
            )
            ON CONFLICT(cb_code, effective_date) DO UPDATE SET
                conversion_price = excluded.conversion_price,
                source = excluded.source,
                source_url = excluded.source_url,
                collected_at = excluded.collected_at
            """,
            event_rows,
        )
        connection.executemany(
            """
            INSERT INTO cb_monthly_balance (
                cb_code, year_month, balance_amount,
                source, source_url, collected_at
            ) VALUES (
                :cb_code, :year_month, :balance_amount,
                :source, :source_url, :collected_at
            )
            ON CONFLICT(cb_code, year_month) DO UPDATE SET
                balance_amount = excluded.balance_amount,
                source = excluded.source,
                source_url = excluded.source_url,
                collected_at = excluded.collected_at
            """,
            balance_rows,
        )
        if as_of_date is not None:
            mapping_rows = [
                {
                    "cb_code": str(row["cb_code"]),
                    "mapping_date": as_of_date.isoformat(),
                    "stock_code": str(row["stock_code"]),
                    "stock_name": str(row["stock_name"]),
                    "market": "UNKNOWN",
                    "source": str(row["source"]),
                    "source_url": str(row["source_url"]),
                    "verified_at": str(row["collected_at"]),
                }
                for row in master_rows
            ]
            upsert_parent_stock_mappings(connection, mapping_rows)
    return len(master_rows), len(event_rows), len(balance_rows)


def conversion_price_on(
    connection: sqlite3.Connection, cb_code: str, on_date: str
) -> float | None:
    row = connection.execute(
        """
        SELECT conversion_price
        FROM conversion_price_events
        WHERE cb_code = ? AND effective_date <= ?
        ORDER BY effective_date DESC
        LIMIT 1
        """,
        (cb_code, on_date),
    ).fetchone()
    return None if row is None else float(row[0])


def upsert_daily(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    rows = [dict(row) for row in records]
    if not rows:
        return 0, 0
    for row in rows:
        row.setdefault("reference_price", None)

    keys = [(str(row["trade_date"]), str(row["cb_code"])) for row in rows]
    existing = {
        key
        for key in keys
        if connection.execute(
            "SELECT 1 FROM cb_daily WHERE trade_date = ? AND cb_code = ?", key
        ).fetchone()
    }
    connection.executemany(
        """
        INSERT INTO cb_daily
            (trade_date, cb_code, cb_name, close_price, reference_price, volume_lots,
             source, collected_at)
        VALUES
            (:trade_date, :cb_code, :cb_name, :close_price, :reference_price,
             :volume_lots, :source, :collected_at)
        ON CONFLICT(trade_date, cb_code) DO UPDATE SET
            cb_name = excluded.cb_name,
            close_price = excluded.close_price,
            reference_price = excluded.reference_price,
            volume_lots = excluded.volume_lots,
            source = excluded.source,
            collected_at = excluded.collected_at
        """,
        rows,
    )
    connection.commit()
    return len(rows) - len(existing), len(existing)


def parent_stock_codes_for_trade_date(
    connection: sqlite3.Connection, trade_date: str
) -> set[str]:
    """Return the parent stocks for CBs that Phase 1 recorded on this date."""
    rows = connection.execute(
        """
        SELECT DISTINCT master.stock_code
        FROM cb_daily AS daily
        INNER JOIN cb_master AS master ON master.cb_code = daily.cb_code
        WHERE daily.trade_date = ?
        """,
        (trade_date,),
    )
    return {str(row[0]) for row in rows}


def upsert_parent_stock_mappings(
    connection: sqlite3.Connection, mappings: Iterable[Mapping[str, object]]
) -> None:
    """Save an official CB-to-parent-stock observation for its exact date.

    A mapping is intentionally keyed by observation date.  Callers resolving a
    historical market date must use an exact-date observation rather than a
    current ``cb_master`` value.
    """
    rows = [dict(row) for row in mappings]
    if not rows:
        return
    for row in rows:
        if row.get("market") not in {"TWSE", "TPEX", "TIB", "UNKNOWN"}:
            raise ValueError("parent-stock mapping market is invalid")
        for key in ("cb_code", "mapping_date", "stock_code", "stock_name", "source", "source_url", "verified_at"):
            if not str(row.get(key, "")).strip():
                raise ValueError(f"parent-stock mapping {key} is required")
        date.fromisoformat(str(row["mapping_date"]))
    with connection:
        connection.executemany(
            """
            INSERT INTO cb_parent_stock_mapping (
                cb_code, mapping_date, stock_code, stock_name, market,
                source, source_url, verified_at
            ) VALUES (
                :cb_code, :mapping_date, :stock_code, :stock_name, :market,
                :source, :source_url, :verified_at
            )
            ON CONFLICT(cb_code, mapping_date) DO UPDATE SET
                stock_code = excluded.stock_code,
                stock_name = excluded.stock_name,
                market = excluded.market,
                source = excluded.source,
                source_url = excluded.source_url,
                verified_at = excluded.verified_at
            """,
            rows,
        )


def parent_stock_mappings_for_trade_date(
    connection: sqlite3.Connection,
    trade_date: str,
    *,
    allow_monthly_verified: bool = False,
) -> dict[str, dict[str, str]]:
    """Return exact-date verified parent mappings for CBs observed that day.

    Missing mappings are deliberately not recovered from ``cb_master`` because
    that would apply a later master record to a historical date.
    """
    exact_rows = connection.execute(
        """
        SELECT daily.cb_code, mapping.stock_code, mapping.stock_name, mapping.market,
               mapping.source, mapping.source_url, mapping.verified_at
        FROM cb_daily AS daily
        LEFT JOIN cb_parent_stock_mapping AS mapping
          ON mapping.cb_code = daily.cb_code
         AND mapping.mapping_date = daily.trade_date
        WHERE daily.trade_date = ?
        """,
        (trade_date,),
    ).fetchall()
    result = {
        str(row["cb_code"]): {
            "stock_code": str(row["stock_code"]),
            "stock_name": str(row["stock_name"]),
            "market": str(row["market"]),
            "source": str(row["source"]),
            "source_url": str(row["source_url"]),
            "verified_at": str(row["verified_at"]),
            "mapping_level": "EXACT",
            "mapping_year_month": trade_date[:7],
        }
        for row in exact_rows if row["stock_code"] is not None
    }
    missing = [str(row["cb_code"]) for row in exact_rows if row["stock_code"] is None]
    if missing and allow_monthly_verified:
        placeholders = ",".join("?" for _ in missing)
        monthly_rows = connection.execute(
            f"""
            SELECT cb_code, stock_code, stock_name, market, source, source_url, verified_at
            FROM cb_parent_stock_monthly_mapping
            WHERE year_month = ? AND cb_code IN ({placeholders})
            """,
            (trade_date[:7], *missing),
        ).fetchall()
        for row in monthly_rows:
            result[str(row["cb_code"])] = {
                "stock_code": str(row["stock_code"]),
                "stock_name": str(row["stock_name"]),
                "market": str(row["market"]),
                "source": str(row["source"]),
                "source_url": str(row["source_url"]),
                "verified_at": str(row["verified_at"]),
                "mapping_level": "MONTHLY_VERIFIED",
                "mapping_year_month": trade_date[:7],
            }
        missing = [code for code in missing if code not in result]
    missing = sorted(missing)
    if missing:
        raise ValueError(
            "unverified_parent_stock_mapping: " + ",".join(missing)
        )
    return result


def upsert_parent_stock_monthly_mappings(
    connection: sqlite3.Connection, mappings: Iterable[Mapping[str, object]]
) -> None:
    """Save a MOPS-verified monthly mapping without converting it to daily data."""
    rows = [dict(row) for row in mappings]
    for row in rows:
        if row.get("source") != "MOPS:t120sg01":
            raise ValueError("monthly parent-stock mapping must be sourced from MOPS:t120sg01")
        if row.get("market") not in {"TWSE", "TPEX", "TIB", "UNKNOWN"}:
            raise ValueError("monthly parent-stock mapping market is invalid")
        for key in ("cb_code", "year_month", "stock_code", "stock_name", "source_url", "verified_at"):
            if not str(row.get(key, "")).strip():
                raise ValueError(f"monthly parent-stock mapping {key} is required")
        date.fromisoformat(f"{row['year_month']}-01")
    if not rows:
        return
    with connection:
        connection.executemany(
            """
            INSERT INTO cb_parent_stock_monthly_mapping (
                cb_code, year_month, stock_code, stock_name, market, source,
                source_url, verified_at
            ) VALUES (
                :cb_code, :year_month, :stock_code, :stock_name, :market, :source,
                :source_url, :verified_at
            )
            ON CONFLICT(cb_code, year_month) DO UPDATE SET
                stock_code = excluded.stock_code,
                stock_name = excluded.stock_name,
                market = excluded.market,
                source = excluded.source,
                source_url = excluded.source_url,
                verified_at = excluded.verified_at
            """,
            rows,
        )


def upsert_stock_daily_coverage(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> None:
    """Persist the official availability/provenance outcome without imputation."""
    rows = [dict(row) for row in records]
    if not rows:
        return
    valid_markets = {"TWSE", "TPEX", "TIB", "UNKNOWN"}
    valid_statuses = {
        "COMPLETE", "OFFICIAL_ZERO", "MISSING_CLOSE", "MISSING_OFFICIAL_ROW",
        "SOURCE_ERROR",
    }
    for row in rows:
        row.setdefault("mapping_level", "EXACT")
        row.setdefault("mapping_source_url", None)
        row.setdefault("mapping_year_month", None)
        row.setdefault("mapping_verified_at", None)
        if row.get("market") not in valid_markets or row.get("status") not in valid_statuses:
            raise ValueError("stock daily coverage market or status is invalid")
        if row["mapping_level"] not in {"EXACT", "MONTHLY_VERIFIED"}:
            raise ValueError("stock daily coverage mapping_level is invalid")
        for key in ("trade_date", "stock_code", "checked_at"):
            if not str(row.get(key, "")).strip():
                raise ValueError(f"stock daily coverage {key} is required")
        date.fromisoformat(str(row["trade_date"]))
    with connection:
        connection.executemany(
            """
            INSERT INTO stock_daily_coverage (
                trade_date, stock_code, market, status, reason, source_url,
                response_date, mapping_level, mapping_source_url, mapping_year_month,
                mapping_verified_at, checked_at
            ) VALUES (
                :trade_date, :stock_code, :market, :status, :reason, :source_url,
                :response_date, :mapping_level, :mapping_source_url, :mapping_year_month,
                :mapping_verified_at, :checked_at
            )
            ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                market = excluded.market,
                status = excluded.status,
                reason = excluded.reason,
                source_url = excluded.source_url,
                response_date = excluded.response_date,
                mapping_level = excluded.mapping_level,
                mapping_source_url = excluded.mapping_source_url,
                mapping_year_month = excluded.mapping_year_month,
                mapping_verified_at = excluded.mapping_verified_at,
                checked_at = excluded.checked_at
            """,
            rows,
        )


def active_parent_stock_codes_on(
    connection: sqlite3.Connection, on_date: str
) -> set[str]:
    """Current-CB parent stocks only; deduplicated even when several CBs share one stock.

    This is deliberately based on the master lifecycle, rather than an all-market
    source response or historical cb_daily rows.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT stock_code
        FROM cb_master
        WHERE issue_date <= ?
          AND (delisting_date IS NULL OR delisting_date > ?)
        """,
        (on_date, on_date),
    )
    return {str(row[0]) for row in rows}


def upsert_stock_daily_market(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    """Atomically upsert validated parent-stock daily market records."""
    rows = list(records)
    if not rows:
        return 0, 0

    keys = [(str(row["trade_date"]), str(row["p_stock_code"])) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate parent-stock market records")
    for row in rows:
        volume = row["p_volume_shares"]
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise ValueError("p_volume_shares must be a non-negative integer")
    existing = {
        key
        for key in keys
        if connection.execute(
            """
            SELECT 1 FROM stock_daily_market
            WHERE trade_date = ? AND p_stock_code = ?
            """,
            key,
        ).fetchone()
    }
    with connection:
        connection.executemany(
            """
            INSERT INTO stock_daily_market (
                trade_date, p_stock_code, p_open_price, p_high_price,
                p_low_price, p_close_price, p_volume_shares
            ) VALUES (
                :trade_date, :p_stock_code, :p_open_price, :p_high_price,
                :p_low_price, :p_close_price, :p_volume_shares
            )
            ON CONFLICT(trade_date, p_stock_code) DO UPDATE SET
                p_open_price = excluded.p_open_price,
                p_high_price = excluded.p_high_price,
                p_low_price = excluded.p_low_price,
                p_close_price = excluded.p_close_price,
                p_volume_shares = excluded.p_volume_shares
            """,
            rows,
        )
    return len(rows) - len(existing), len(existing)


def upsert_institutional_daily(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    """Upsert only complete official institutional observations (all units: shares)."""
    rows = [dict(row) for row in records]
    for row in rows:
        for key in ("foreign_buy_shares", "foreign_sell_shares", "foreign_net_shares",
                    "trust_buy_shares", "trust_sell_shares", "trust_net_shares"):
            if isinstance(row[key], bool) or not isinstance(row[key], int):
                raise ValueError(f"{key} must be an integer number of shares")
    keys = [(row["trade_date"], row["stock_code"]) for row in rows]
    existing = sum(bool(connection.execute(
        "SELECT 1 FROM institutional_daily WHERE trade_date=? AND stock_code=?", key
    ).fetchone()) for key in keys)
    with connection:
        connection.executemany("""
            INSERT INTO institutional_daily VALUES (
              :trade_date,:stock_code,:stock_name,:market,:foreign_buy_shares,
              :foreign_sell_shares,:foreign_net_shares,:trust_buy_shares,
              :trust_sell_shares,:trust_net_shares,:source_url,:collected_at)
            ON CONFLICT(trade_date,stock_code) DO UPDATE SET
              stock_name=excluded.stock_name, market=excluded.market,
              foreign_buy_shares=excluded.foreign_buy_shares,
              foreign_sell_shares=excluded.foreign_sell_shares,
              foreign_net_shares=excluded.foreign_net_shares,
              trust_buy_shares=excluded.trust_buy_shares,
              trust_sell_shares=excluded.trust_sell_shares,
              trust_net_shares=excluded.trust_net_shares,
              source_url=excluded.source_url, collected_at=excluded.collected_at
        """, rows)
    return len(rows) - existing, existing


def upsert_institutional_coverage(connection: sqlite3.Connection, rows: Iterable[Mapping[str, object]]) -> None:
    with connection:
        connection.executemany("""
          INSERT INTO institutional_coverage VALUES (:trade_date,:stock_code,:market,:status,:reason,:source_url,:checked_at)
          ON CONFLICT(trade_date,stock_code) DO UPDATE SET market=excluded.market,status=excluded.status,
            reason=excluded.reason,source_url=excluded.source_url,checked_at=excluded.checked_at
        """, [dict(row) for row in rows])


def upsert_active_etf_holdings(
    connection: sqlite3.Connection, master: Mapping[str, object], holdings: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    """Save a validated raw holding snapshot; absent rows are never synthesized."""
    rows = [dict(row) for row in holdings]
    for row in rows:
        if isinstance(row["holding_shares"], bool) or not isinstance(row["holding_shares"], int) or row["holding_shares"] < 0:
            raise ValueError("holding_shares must be a non-negative integer number of shares")
    with connection:
        connection.execute("""
          INSERT INTO active_etf_master VALUES (:etf_code,:etf_name,:manager_name,:source_url,
            :source_identifier,:enabled,:last_status,:last_error,:last_checked_at)
          ON CONFLICT(etf_code) DO UPDATE SET etf_name=excluded.etf_name,
            manager_name=excluded.manager_name,source_url=excluded.source_url,
            source_identifier=excluded.source_identifier,enabled=excluded.enabled,
            last_status=excluded.last_status,last_error=excluded.last_error,last_checked_at=excluded.last_checked_at
        """, dict(master))
        keys = [(row["trade_date"], row["etf_code"], row["stock_code"]) for row in rows]
        existing = sum(bool(connection.execute(
            "SELECT 1 FROM active_etf_holdings WHERE trade_date=? AND etf_code=? AND stock_code=?", key
        ).fetchone()) for key in keys)
        connection.executemany("""
          INSERT INTO active_etf_holdings VALUES (:trade_date,:etf_code,:etf_name,:stock_code,
            :stock_name,:holding_shares,:source_url,:source_identifier,:collected_at)
          ON CONFLICT(trade_date,etf_code,stock_code) DO UPDATE SET stock_name=excluded.stock_name,
            holding_shares=excluded.holding_shares,source_url=excluded.source_url,
            source_identifier=excluded.source_identifier,collected_at=excluded.collected_at
        """, rows)
    return len(rows) - existing, existing


def upsert_active_etf_collection_status(
    connection: sqlite3.Connection, status: Mapping[str, object]
) -> None:
    """Record one ETF source result; a failed source never implies zero holdings."""
    with connection:
        connection.execute(
            """
            INSERT INTO active_etf_collection_status VALUES
              (:trade_date, :etf_code, :status, :error_message, :checked_at)
            ON CONFLICT(trade_date, etf_code) DO UPDATE SET
              status=excluded.status, error_message=excluded.error_message,
              checked_at=excluded.checked_at
            """,
            dict(status),
        )


def query_company_cbs(
    connection: sqlite3.Connection,
    trade_date: str,
    company_names: Iterable[str],
) -> list[sqlite3.Row]:
    clauses = " OR ".join("cb_name LIKE ?" for _ in company_names)
    params = [trade_date, *(f"%{name}%" for name in company_names)]
    return connection.execute(
        f"""
        SELECT trade_date, cb_code, cb_name, close_price, volume_lots
        FROM cb_daily
        WHERE trade_date = ? AND ({clauses})
        ORDER BY cb_code
        """,
        params,
    ).fetchall()
