"""Batch backfill parent stock daily market quotes for historical trading days."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_DB_PATH
from db import connect
from stock_collector import collect_stock_daily_market, build_session


def get_existing_stock_dates(db_path: Path | str, start_date: date, end_date: date) -> set[date]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_market WHERE trade_date BETWEEN ? AND ?",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    return {date.fromisoformat(row[0]) for row in rows}


def get_cb_trade_dates(db_path: Path | str, start_date: date, end_date: date) -> list[date]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    return [date.fromisoformat(row[0]) for row in rows]


def backfill_stocks_range(
    start_date: date,
    end_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    delay_seconds: float = 1.5,
    max_retries: int = 3,
) -> dict[str, int]:
    print(f"=== Stage 2: Parent Stock Market Backfill [{start_date} to {end_date}] ===")
    cb_dates = get_cb_trade_dates(db_path, start_date, end_date)
    existing_stock_dates = get_existing_stock_dates(db_path, start_date, end_date)

    print(f"Total CB trade dates in range: {len(cb_dates)}")
    print(f"Already collected stock dates: {len(existing_stock_dates)}")

    session = build_session()
    success_count = 0
    skipped_count = 0
    error_count = 0
    total_records = 0

    try:
        for idx, trade_date in enumerate(cb_dates, 1):
            if trade_date in existing_stock_dates:
                skipped_count += 1
                continue

            for attempt in range(1, max_retries + 1):
                try:
                    result = collect_stock_daily_market(trade_date, db_path=db_path, session=session)
                    inserted = int(result.get("records_inserted", 0))
                    total_records += inserted
                    success_count += 1
                    print(
                        f"[{idx}/{len(cb_dates)}] [{trade_date}] OK - "
                        f"{result.get('target_stocks')} stocks ({inserted} inserted). Total days: {success_count}"
                    )
                    time.sleep(delay_seconds)
                    break
                except Exception as exc:
                    print(f"[{trade_date}] Attempt {attempt}/{max_retries} failed: {exc}")
                    if attempt == max_retries:
                        error_count += 1
                        print(f"[{trade_date}] ERROR: Skipping after {max_retries} failures.")
                    else:
                        time.sleep(attempt * 4)

    except KeyboardInterrupt:
        print("\nBackfill interrupted by user. Safe exit (all completed dates are saved).")
    finally:
        session.close()

    print("\n=== Stage 2 Summary ===")
    print(f"New stock trading days collected: {success_count}")
    print(f"Already existed (skipped): {skipped_count}")
    print(f"Errors / failed days: {error_count}")
    print(f"Total stock market records upserted: {total_records}")

    return {
        "success_count": success_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "total_records": total_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill parent stock daily quotes for a date range.")
    parser.add_argument("--start", default="2019-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2023-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests in seconds")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    backfill_stocks_range(start_date, end_date, db_path=args.db, delay_seconds=args.delay)


if __name__ == "__main__":
    main()
