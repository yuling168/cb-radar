"""Backfill parent-stock market data for verified historical CB trading days."""

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import requests

from config import DEFAULT_DB_PATH
from db import connect, parent_stock_codes_for_trade_date
from stock_collector import StockMarketFormatError, collect_stock_daily_market


class BackfillPreconditionError(RuntimeError):
    """The database cannot establish a complete, date-specific backfill scope."""


def cb_trade_dates_for_backfill(
    db_path: Path | str, days: int, end_date: date | None = None
) -> list[date]:
    """Return the latest verified Phase 1 dates, oldest first, for a backfill."""
    if days <= 0:
        raise ValueError("days must be positive")
    with connect(db_path) as connection:
        query = "SELECT DISTINCT trade_date FROM cb_daily"
        parameters: tuple[object, ...] = ()
        if end_date is not None:
            query += " WHERE trade_date <= ?"
            parameters = (end_date.isoformat(),)
        query += " ORDER BY trade_date DESC LIMIT ?"
        rows = connection.execute(query, (*parameters, days)).fetchall()

    dates = [date.fromisoformat(str(row[0])) for row in rows]
    if len(dates) != days:
        newest = "none" if not dates else dates[0].isoformat()
        raise BackfillPreconditionError(
            f"Need {days} verified cb_daily trade dates, found {len(dates)} "
            f"(newest {newest}). Run the Phase 1 historical backfill first."
        )
    return list(reversed(dates))


def backfill_stock_daily_market(
    db_path: Path | str = DEFAULT_DB_PATH,
    days: int = 60,
    end_date: date | None = None,
    collector: Callable[..., dict[str, object]] = collect_stock_daily_market,
) -> dict[str, object]:
    """Backfill one verified CB universe at a time using the official stock sources."""
    trade_dates = cb_trade_dates_for_backfill(db_path, days, end_date)
    inserted = 0
    updated = 0
    total_targets = 0

    for trade_date in trade_dates:
        with connect(db_path) as connection:
            target_codes = parent_stock_codes_for_trade_date(
                connection, trade_date.isoformat()
            )
        if not target_codes:
            raise BackfillPreconditionError(
                f"No cb_master parent-stock universe for {trade_date.isoformat()}"
            )

        result = collector(trade_date, db_path)
        total_targets += int(result["target_stocks"])
        inserted += int(result["records_inserted"])
        updated += int(result["records_updated"])

    return {
        "start_date": trade_dates[0].isoformat(),
        "end_date": trade_dates[-1].isoformat(),
        "trade_days": len(trade_dates),
        "target_stock_observations": total_targets,
        "records_inserted": inserted,
        "records_updated": updated,
        "missing": 0,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill parent-stock data for verified Phase 1 trade dates"
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--end-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = backfill_stock_daily_market(
            args.database, args.days, args.end_date
        )
    except (
        BackfillPreconditionError,
        StockMarketFormatError,
        requests.RequestException,
        ValueError,
    ) as exc:
        print(f"stock_backfill_error: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
