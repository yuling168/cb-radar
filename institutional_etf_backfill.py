"""Backfill verified CB trading dates for institutional and tracked active-ETF data."""

import argparse
import sys
from datetime import date
from pathlib import Path

from config import DEFAULT_DB_PATH
from daily_pipeline import run_daily_pipeline
from stock_backfill import BackfillPreconditionError, cb_trade_dates_for_backfill


def backfill_institutional_etf(
    db_path: Path | str = DEFAULT_DB_PATH, *, days: int = 10,
    start_date: date | None = None, end_date: date | None = None, runner=run_daily_pipeline,
):
    """Run only over existing verified Phase 1 dates, oldest first."""
    if start_date is not None:
        if end_date is None or start_date > end_date:
            raise ValueError("--start-date requires an equal or later --end-date")
        from db import connect
        with connect(db_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        trade_dates = [date.fromisoformat(row[0]) for row in rows]
        if not trade_dates:
            raise BackfillPreconditionError("No verified CB trading dates in requested range")
    else:
        trade_dates = cb_trade_dates_for_backfill(db_path, days, end_date)

    results = [runner(trade_date, db_path) for trade_date in trade_dates]
    incomplete = [item["trade_date"] for item in results if item["active_etfs"]["coverage"] != "complete"]
    return {
        "start_date": trade_dates[0].isoformat(), "end_date": trade_dates[-1].isoformat(),
        "trade_days": len(trade_dates), "active_etf_coverage_incomplete_dates": incomplete,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill institutional and tracked active-ETF data")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    try:
        print(backfill_institutional_etf(args.database, days=args.days, start_date=args.start_date, end_date=args.end_date))
    except (BackfillPreconditionError, ValueError) as exc:
        print(f"institutional_etf_backfill_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
