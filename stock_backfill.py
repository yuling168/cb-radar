"""Backfill parent-stock market data for verified historical CB trading days."""

import argparse
from calendar import monthrange
import json
import sqlite3
import sys
import time
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


def existing_stock_trade_dates_for_range(
    db_path: Path | str, start_date: date, end_date: date,
) -> list[date]:
    """Return only dates that already have parent-stock observations.

    This deliberately does not consult cb_daily or any parent mapping: V2
    enrichment must never create a historical parent-stock universe.
    """
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT DISTINCT trade_date FROM stock_daily_market
               WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    return [date.fromisoformat(str(row[0])) for row in rows]


def _readonly_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open the historical DB without letting checkpoint reads mutate it."""
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def pending_stock_trade_dates_v2_for_range(
    db_path: Path | str, start_date: date, end_date: date,
) -> list[date]:
    """Return dates with existing rows that are not all V2 COMPLETE.

    A mixed date is rejected rather than re-fetching rows that are already
    COMPLETE.  The collector's per-day write is atomic, so mixed dates signal
    an unexpected state that needs investigation.
    """
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    with _readonly_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT trade_date,
                   SUM(p_volume_definition='REGULAR_ODD_FIXED_V2'
                       AND p_volume_component_status='COMPLETE') AS complete_rows,
                   COUNT(*) AS target_rows
            FROM stock_daily_market
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date
            HAVING complete_rows < target_rows
            ORDER BY trade_date
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    mixed = [str(row[0]) for row in rows if int(row[1]) != 0]
    if mixed:
        raise BackfillPreconditionError(
            f"Refusing to re-fetch partially COMPLETE V2 dates: {mixed}"
        )
    return [date.fromisoformat(str(row[0])) for row in rows]


def volume_v2_month_checkpoint(
    db_path: Path | str, start_date: date, end_date: date,
) -> dict[str, object]:
    """Read-only monthly V2 gate, explicitly treating an empty month as skip."""
    with _readonly_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT trade_date), COUNT(*),
                   COALESCE(SUM(p_volume_definition='REGULAR_ODD_FIXED_V2'), 0),
                   COALESCE(SUM(p_volume_component_status='COMPLETE'), 0),
                   COALESCE(SUM(p_volume_component_status='RECONCILIATION_FAILURE'), 0),
                   COALESCE(SUM(p_volume_component_status='SOURCE_ERROR'), 0),
                   COALESCE(SUM(p_volume_definition='REGULAR_ONLY_V1'), 0)
            FROM stock_daily_market
            WHERE trade_date BETWEEN ? AND ?
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchone()
    days, target, v2, complete, reconciliation, source_error, legacy = map(int, row)
    status = "SKIPPED_NO_TARGET_ROWS" if target == 0 else (
        "PASS" if v2 == target and complete == target
        and reconciliation == 0 and source_error == 0 and legacy == 0 else "FAIL"
    )
    return {
        "event": "month_checkpoint", "status": status,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "trading_days": days, "target_rows": target, "v2_rows": v2,
        "complete": complete, "reconciliation_failure": reconciliation,
        "source_error": source_error, "regular_only_v1": legacy,
    }


def backfill_pending_stock_daily_market_v2_by_month(
    db_path: Path | str, start_date: date, end_date: date | None = None,
    *, collector: Callable[..., dict[str, object]] = collect_stock_daily_market,
    enricher: Callable[..., dict[str, object]] = None,
    day_timeout_seconds: float = 300,
) -> list[dict[str, object]]:
    """Enrich only fully-pending existing dates, with strict monthly gates.

    Empty calendar months are read-only checkpoints and are recorded as
    ``SKIPPED_NO_TARGET_ROWS``.  They never invoke an official endpoint or
    create a historical row.
    """
    if end_date is None:
        with _readonly_connection(db_path) as connection:
            value = connection.execute(
                "SELECT MAX(trade_date) FROM stock_daily_market"
            ).fetchone()[0]
        if value is None:
            raise BackfillPreconditionError("No stock_daily_market rows exist")
        end_date = date.fromisoformat(str(value))
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if day_timeout_seconds <= 0:
        raise ValueError("day_timeout_seconds must be positive")
    if enricher is None:
        enricher = enrich_existing_stock_daily_market_v2

    cursor = date(start_date.year, start_date.month, 1)
    checkpoints: list[dict[str, object]] = []
    while cursor <= end_date:
        month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        effective_start = max(cursor, start_date)
        effective_end = min(month_end, end_date)
        pending_dates = pending_stock_trade_dates_v2_for_range(
            db_path, effective_start, effective_end
        )
        print(json.dumps({
            "event": "month_start", "start_date": effective_start.isoformat(),
            "end_date": effective_end.isoformat(),
            "pending_trade_dates": [item.isoformat() for item in pending_dates],
        }, ensure_ascii=False), flush=True)
        for trade_date in pending_dates:
            enricher(
                db_path, trade_date, trade_date, collector=collector,
                day_timeout_seconds=day_timeout_seconds,
            )
        checkpoint = volume_v2_month_checkpoint(db_path, cursor, effective_end)
        print(json.dumps(checkpoint, ensure_ascii=False), flush=True)
        if checkpoint["status"] == "FAIL":
            raise RuntimeError("monthly V2 checkpoint failed: " + json.dumps(checkpoint, ensure_ascii=False))
        checkpoints.append(checkpoint)
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return checkpoints


def enrich_existing_stock_daily_market_v2(
    db_path: Path | str, start_date: date, end_date: date,
    collector: Callable[..., dict[str, object]] = collect_stock_daily_market,
    day_timeout_seconds: float = 300,
) -> dict[str, object]:
    """Enrich exactly the existing stock rows with V2 volumes.

    The target codes are read directly from each existing date's
    stock_daily_market rows.  No mapping is resolved, no missing parent row is
    inserted, and coverage provenance is left untouched.
    """
    trade_dates = existing_stock_trade_dates_for_range(db_path, start_date, end_date)
    if not trade_dates:
        raise BackfillPreconditionError("No existing stock_daily_market rows in requested range")
    if day_timeout_seconds <= 0:
        raise ValueError("day_timeout_seconds must be positive")
    inserted = updated = target_rows = complete = reconciliation_failures = 0
    for trade_date in trade_dates:
        with connect(db_path) as connection:
            codes = [str(row[0]) for row in connection.execute(
                "SELECT p_stock_code FROM stock_daily_market WHERE trade_date = ? ORDER BY p_stock_code",
                (trade_date.isoformat(),),
            )]
        # These entries carry no mapping assertion.  The collector obtains both
        # official markets and identifies the market from the official row.
        existing_rows = {
            f"existing:{code}": {
                "stock_code": code, "stock_name": code, "market": "UNKNOWN",
                "source_url": "existing_stock_daily_market", "verified_at": "",
                "mapping_level": "EXACT", "mapping_year_month": trade_date.strftime("%Y-%m"),
            }
            for code in codes
        }
        started = time.monotonic()
        print(json.dumps({"event": "trade_day_start", "trade_date": trade_date.isoformat(), "target_rows": len(codes)}, ensure_ascii=False), flush=True)
        def progress(event):
            print(json.dumps({"event": "endpoint", **event}, ensure_ascii=False), flush=True)
        result = collector(
            trade_date, db_path, verified_mappings=existing_rows, write_coverage=False,
            progress=progress, deadline_monotonic=started + day_timeout_seconds,
        )
        target_rows += len(codes)
        inserted += int(result["records_inserted"])
        updated += int(result["records_updated"])
        complete += int(result.get("complete_records", 0))
        reconciliation_failures += int(result.get("reconciliation_failures", 0))
        print(json.dumps({
            "event": "trade_day_complete", "trade_date": trade_date.isoformat(),
            "twse_rows": result.get("twse_records"), "tpex_rows": result.get("tpex_records"),
            "complete": result.get("complete_records"),
            "reconciliation_failure": result.get("reconciliation_failures"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }, ensure_ascii=False), flush=True)
    return {
        "start_date": trade_dates[0].isoformat(), "end_date": trade_dates[-1].isoformat(),
        "trade_days": len(trade_dates), "target_stock_observations": target_rows,
        "records_inserted": inserted, "records_updated": updated,
        "complete_records": complete, "reconciliation_failures": reconciliation_failures,
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
