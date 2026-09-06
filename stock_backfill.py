"""Backfill parent-stock market data for verified historical CB trading days."""

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from config import DEFAULT_DB_PATH
from db import (
    connect, parent_stock_mappings_for_trade_date,
    parent_stock_mapping_resolution_for_trade_date,
    upsert_stock_backfill_mapping_coverage,
)
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
    allow_partial_mapping: bool = False,
    collector: Callable[..., dict[str, object]] = collect_stock_daily_market,
) -> dict[str, object]:
    """Backfill one verified CB universe at a time using the official stock sources."""
    if allow_partial_mapping and not allow_monthly_verified:
        raise ValueError("--allow-partial-mapping requires --allow-monthly-verified")
    if start_date is not None:
        if end_date is None:
            raise ValueError("end_date is required when start_date is supplied")
        trade_dates = cb_trade_dates_for_range(db_path, start_date, end_date)
    else:
        trade_dates = cb_trade_dates_for_backfill(db_path, days, end_date)
    if not allow_partial_mapping:
        _require_verified_mappings(
            db_path, trade_dates, allow_monthly_verified=allow_monthly_verified
        )
    inserted = 0
    updated = 0
    total_targets = 0
    resolved_total = 0
    unresolved_total = 0

    for trade_date in trade_dates:
        if allow_partial_mapping:
            with connect(db_path) as connection:
                mappings, unresolved = parent_stock_mapping_resolution_for_trade_date(
                    connection, trade_date.isoformat(), allow_monthly_verified=True
                )
                checked_at = datetime.now(timezone.utc).isoformat()
                coverage = [
                    {"trade_date": trade_date.isoformat(), "cb_code": cb_code,
                     "mapping_resolution": mapping["mapping_level"],
                     "mapping_month": mapping["mapping_year_month"],
                     "mapping_status": None, "unresolved_reason": None,
                     "checked_at": checked_at}
                    for cb_code, mapping in mappings.items()
                ] + [
                    {"trade_date": trade_date.isoformat(), "cb_code": item["cb_code"],
                     "mapping_resolution": "UNRESOLVED", "mapping_month": trade_date.strftime("%Y-%m"),
                     "mapping_status": item["mapping_status"],
                     "unresolved_reason": item["unresolved_reason"], "checked_at": checked_at}
                    for item in unresolved
                ]
                upsert_stock_backfill_mapping_coverage(connection, coverage)
            result = collector(
                trade_date, db_path, allow_monthly_verified=True, verified_mappings=mappings
            )
            resolved_total += len(mappings)
            unresolved_total += len(unresolved)
        elif allow_monthly_verified:
            result = collector(
                trade_date, db_path, allow_monthly_verified=True
            )
        else:
            result = collector(trade_date, db_path)
        total_targets += int(result["target_stocks"])
        inserted += int(result["records_inserted"])
        updated += int(result["records_updated"])

    result_summary = {
        "start_date": trade_dates[0].isoformat(),
        "end_date": trade_dates[-1].isoformat(),
        "trade_days": len(trade_dates),
        "target_stock_observations": total_targets,
        "records_inserted": inserted,
        "records_updated": updated,
        "missing": 0,
    }
    if allow_partial_mapping:
        result_summary.update({
            "resolved_cb_mappings": resolved_total,
            "unresolved_cb_mappings": unresolved_total,
        })
    return result_summary


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
    parser.add_argument(
        "--allow-partial-mapping", action="store_true",
        help="Historical only: backfill verified mappings while recording unresolved CBs",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = backfill_stock_daily_market(
            args.database, args.days, args.end_date, args.start_date,
            args.allow_monthly_verified, args.allow_partial_mapping,
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
