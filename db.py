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
