"""Build the static GitHub Pages data file from the tracked SQLite database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "cb_history.db"
OUTPUT_PATH = ROOT / "docs" / "data.json"
TABLE_NAME = "cb_daily"
REQUIRED_COLUMNS = {
    "trade_date",
    "cb_code",
    "cb_name",
    "close_price",
    "volume_lots",
}


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
        if TABLE_NAME not in tables:
            raise RuntimeError(f"Required SQLite table not found: {TABLE_NAME}")

        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})")
        }
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise RuntimeError(
                f"Required SQLite columns missing from {TABLE_NAME}: {sorted(missing)}"
            )

        cursor = connection.execute(
            """
            SELECT trade_date, cb_code, cb_name, close_price, volume_lots
            FROM cb_daily
            ORDER BY trade_date DESC, cb_code ASC
            """
        )
        return [dict(row) for row in cursor]


def build_dashboard_data() -> tuple[int, int]:
    rows = load_rows()
    payload = {"records": rows}
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

