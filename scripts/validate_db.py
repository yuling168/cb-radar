"""Reusable validation gate for a candidate CB history database."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REQUIRED_TABLES = (
    "cb_daily", "cb_master", "cb_parent_stock_mapping", "stock_daily_market",
    "strategy_definition", "strategy_run", "strategy_published_series",
)


def validate_database(path: Path, *, run_date: str | None = None) -> None:
    today = run_date or datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    connection = sqlite3.connect(path)
    try:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(REQUIRED_TABLES) - names
        if missing:
            raise RuntimeError("missing required tables: " + ", ".join(sorted(missing)))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"SQLite foreign_key_check failed: {foreign_keys}")
        future = connection.execute("SELECT cb_code, balance_date FROM cb_master WHERE balance_date > ? ORDER BY cb_code", (today,)).fetchall()
        if future:
            raise RuntimeError("Future balance_date rows: " + ", ".join(f"{code}={date}" for code, date in future))
        unfinished = connection.execute("SELECT cb_code, year_month FROM cb_monthly_balance WHERE year_month >= ? ORDER BY cb_code", (today[:7],)).fetchall()
        if unfinished:
            raise RuntimeError("Unfinished monthly balance rows: " + ", ".join(f"{code}={month}" for code, month in unfinished))
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a CB history SQLite database")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-date")
    args = parser.parse_args(argv)
    validate_database(args.database, run_date=args.run_date)
    print(f"SQLite validation passed for Asia/Taipei run date {args.run_date or 'today'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
