import sqlite3
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA = """
CREATE TABLE IF NOT EXISTS cb_daily (
    trade_date TEXT NOT NULL,
    cb_code TEXT NOT NULL,
    cb_name TEXT NOT NULL,
    close_price REAL,
    volume_lots INTEGER NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, cb_code)
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(SCHEMA)
    connection.commit()
    return connection


def upsert_daily(
    connection: sqlite3.Connection, records: Iterable[Mapping[str, object]]
) -> tuple[int, int]:
    rows = list(records)
    if not rows:
        return 0, 0

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
            (trade_date, cb_code, cb_name, close_price, volume_lots, source, collected_at)
        VALUES
            (:trade_date, :cb_code, :cb_name, :close_price, :volume_lots, :source, :collected_at)
        ON CONFLICT(trade_date, cb_code) DO UPDATE SET
            cb_name = excluded.cb_name,
            close_price = excluded.close_price,
            volume_lots = excluded.volume_lots,
            source = excluded.source,
            collected_at = excluded.collected_at
        """,
        rows,
    )
    connection.commit()
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

