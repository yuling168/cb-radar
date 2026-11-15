"""Batch backfill whole-market CB daily records for a date range into SQLite."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Add cb-radar root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from collector import DataNotPublished, TpexCP950DecodeError, TpexFormatError, collect
from config import DEFAULT_DB_PATH
from db import connect
from tpex_tls import build_tpex_session


def get_existing_cb_trade_dates(db_path: Path | str, start_date: date, end_date: date) -> set[date]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ?",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    return {date.fromisoformat(row[0]) for row in rows}


def backfill_cb_daily_range(
    start_date: date,
    end_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    delay_seconds: float = 1.5,
    max_retries: int = 3,
) -> dict[str, int]:
    print(f"=== Stage 1: CB Daily Backfill [{start_date} to {end_date}] ===")
    existing_dates = get_existing_cb_trade_dates(db_path, start_date, end_date)
    print(f"Already existing trade dates in DB: {len(existing_dates)}")

    session = build_tpex_session()
    current = start_date
    success_count = 0
    skipped_count = 0
    holiday_count = 0
    total_records = 0

    calendar_days = (end_date - start_date).days + 1
    processed_days = 0

    try:
        while current <= end_date:
            processed_days += 1
            if current.weekday() >= 5:  # Saturday or Sunday
                current += timedelta(days=1)
                continue

            if current in existing_dates:
                skipped_count += 1
                current += timedelta(days=1)
                continue

            # Attempt collection with retries
            for attempt in range(1, max_retries + 1):
                try:
                    result = collect(current, db_path=db_path, write=True, session=session)
                    inserted = int(result["records_inserted"])
                    updated = int(result["records_updated"])
                    total_records += (inserted + updated)
                    success_count += 1
                    print(
                        f"[{current}] OK - {result['equivalent_market_rows']} CBs "
                        f"({inserted} inserted, {updated} updated). Total saved: {success_count}"
                    )
                    time.sleep(delay_seconds)
                    break
                except DataNotPublished:
                    holiday_count += 1
                    # print(f"[{current}] Non-trading day / Holiday")
                    break
                except Exception as exc:
                    print(f"[{current}] Attempt {attempt}/{max_retries} failed: {exc}")
                    if attempt == max_retries:
                        print(f"[{current}] ERROR: Max retries exceeded. Skipping.")
                    else:
                        time.sleep(attempt * 3)

            current += timedelta(days=1)

    except KeyboardInterrupt:
        print("\nBackfill interrupted by user. Safe exit (all completed dates are saved).")

    print("\n=== Stage 1 Summary ===")
    print(f"New trading days collected: {success_count}")
    print(f"Already existed (skipped): {skipped_count}")
    print(f"Holidays / non-trading days: {holiday_count}")
    print(f"Total CB records upserted: {total_records}")

    return {
        "success_count": success_count,
        "skipped_count": skipped_count,
        "holiday_count": holiday_count,
        "total_records": total_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CB daily data for a date range.")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2025-05-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests in seconds")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    backfill_cb_daily_range(start_date, end_date, db_path=args.db, delay_seconds=args.delay)


if __name__ == "__main__":
    main()
