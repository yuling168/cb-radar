"""Backfill parent-stock market data for verified historical CB trading days."""

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import requests

from config import DEFAULT_DB_PATH
from db import connect, parent_stock_mappings_for_trade_date
from stock_collector import (
    ParentStockMappingError,
    StockMarketFormatError,
    collect_stock_daily_market,
)


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


def cb_trade_dates_for_range(
    db_path: Path | str, start_date: date, end_date: date
) -> list[date]:
    """Return existing verified CB trading dates in an inclusive date range."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT trade_date FROM cb_daily
            WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    if not rows:
        raise BackfillPreconditionError(
            f"No verified cb_daily trade dates from {start_date} through {end_date}"
        )
    return [date.fromisoformat(str(row[0])) for row in rows]


def _require_verified_mappings(
    db_path: Path | str,
    trade_dates: list[date],
    *,
    allow_monthly_verified: bool = False,
) -> None:
    """Reject before network I/O unless each date has an allowed verified mapping."""
    with connect(db_path) as connection:
        for trade_date in trade_dates:
            try:
                parent_stock_mappings_for_trade_date(
                    connection, trade_date.isoformat(),
                    allow_monthly_verified=allow_monthly_verified,
                )
            except ValueError as exc:
                raise BackfillPreconditionError(
                    f"Unverified parent-stock mapping on {trade_date}: {exc}"
                ) from exc


def backfill_stock_daily_market(
    db_path: Path | str = DEFAULT_DB_PATH,
    days: int = 60,
    end_date: date | None = None,
    start_date: date | None = None,
    allow_monthly_verified: bool = False,
    collector: Callable[..., dict[str, object]] = collect_stock_daily_market,
) -> dict[str, object]:
    """Backfill one verified CB universe at a time using the official stock sources."""
    if start_date is not None:
        if end_date is None:
            raise ValueError("end_date is required when start_date is supplied")
        trade_dates = cb_trade_dates_for_range(db_path, start_date, end_date)
    else:
        trade_dates = cb_trade_dates_for_backfill(db_path, days, end_date)
    _require_verified_mappings(
        db_path, trade_dates, allow_monthly_verified=allow_monthly_verified
    )
    inserted = 0
    updated = 0
    total_targets = 0

    for trade_date in trade_dates:
        if allow_monthly_verified:
            result = collector(
                trade_date, db_path, allow_monthly_verified=True
            )
        else:
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
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--days", type=int, default=60)
    selection.add_argument("--start-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--end-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument(
        "--allow-monthly-verified", action="store_true",
        help="Allow MOPS-verified monthly parent mappings when exact-date mapping is absent",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = backfill_stock_daily_market(
            args.database, args.days, args.end_date, args.start_date,
            args.allow_monthly_verified,
        )
    except (
        BackfillPreconditionError,
        StockMarketFormatError, ParentStockMappingError,
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
