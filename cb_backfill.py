"""Backfill official Phase 1 CB daily data for recent verified trading days."""

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from collector import DataNotPublished, TpexFormatError, collect
from config import DEFAULT_DB_PATH
from db import connect, record_cb_daily_backfill_status


class BackfillIncompleteError(RuntimeError):
    """The requested number of official trading days could not be found."""


class CountingSession:
    """Count official report HTTP requests while preserving requests.Session behavior."""

    RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 520}
    MAX_ATTEMPTS = 4

    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self.headers = self._session.headers
        self.request_count = 0

    def get(self, *args: Any, **kwargs: Any) -> requests.Response:
        for attempt in range(self.MAX_ATTEMPTS):
            self.request_count += 1
            try:
                response = self._session.get(*args, **kwargs)
            except requests.RequestException:
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            if (
                response.status_code not in self.RETRY_STATUS_CODES
                or attempt == self.MAX_ATTEMPTS - 1
            ):
                return response
            time.sleep(2**attempt)
        raise AssertionError("unreachable")


def backfill_cb_daily(
    db_path: Path | str = DEFAULT_DB_PATH,
    days: int = 60,
    end_date: date | None = None,
    dry_run: bool = False,
    max_calendar_days: int = 180,
    session: CountingSession | None = None,
) -> dict[str, object]:
    """Collect the most recent official CB trading days, without using a current universe."""
    if days <= 0:
        raise ValueError("days must be positive")
    if max_calendar_days <= 0:
        raise ValueError("max_calendar_days must be positive")

    http = session or CountingSession()
    candidate = end_date or date.today()
    collected_dates: list[date] = []
    inserted = 0
    updated = 0
    unpublished_dates = 0
    reference_prices = 0

    for _ in range(max_calendar_days):
        try:
            result = collect(
                candidate,
                db_path,
                latest_available=False,
                session=http,
                write=not dry_run,
            )
        except DataNotPublished:
            unpublished_dates += 1
        except (requests.RequestException, TpexFormatError):
            raise
        else:
            collected_dates.append(date.fromisoformat(str(result["trade_date"])))
            inserted += int(result["records_inserted"])
            updated += int(result["records_updated"])
            reference_prices += int(result.get("reference_price_count", 0))
            if len(collected_dates) == days:
                break
        candidate -= timedelta(days=1)

    if len(collected_dates) != days:
        raise BackfillIncompleteError(
            f"Found {len(collected_dates)} official trading days in the last "
            f"{max_calendar_days} calendar days; need {days}."
        )

    return {
        "start_date": collected_dates[-1].isoformat(),
        "end_date": collected_dates[0].isoformat(),
        "trade_days": len(collected_dates),
        "records_inserted": inserted,
        "records_updated": updated,
        "unpublished_dates": unpublished_dates,
        "reference_price_count": reference_prices,
        "request_count": http.request_count,
        "dry_run": dry_run,
    }


def backfill_cb_daily_range(
    start_date: date,
    end_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    batch_size: int = 20,
    delay_seconds: float = 0.2,
    session: CountingSession | None = None,
) -> dict[str, object]:
    """Probe every calendar day only through the official CB endpoint.

    A date is a market trading day only after ``collect`` accepts the official
    response for that same date.  Prior successful/non-trading statuses are
    skipped; source errors are retried by later invocations.
    """
    if start_date > end_date or batch_size <= 0 or delay_seconds < 0:
        raise ValueError("invalid date range, batch_size, or delay_seconds")
    http = session or CountingSession()
    candidate = start_date
    processed = succeeded = non_trading = source_errors = skipped = 0
    while candidate <= end_date and processed < batch_size:
        iso = candidate.isoformat()
        with connect(db_path) as connection:
            prior = connection.execute(
                "SELECT status FROM cb_daily_backfill_status WHERE trade_date=?", (iso,)
            ).fetchone()
        if prior is not None and prior[0] in {"SUCCEEDED", "NON_TRADING"}:
            skipped += 1
            candidate += timedelta(days=1)
            continue
        try:
            result = collect(candidate, db_path, latest_available=False, session=http, write=True)
            if result["trade_date"] != iso:
                raise TpexFormatError("official CB response date does not match requested date")
        except DataNotPublished as exc:
            status, error = "NON_TRADING", str(exc)
            non_trading += 1
        except (requests.RequestException, TpexFormatError) as exc:
            status, error = "SOURCE_ERROR", str(exc)
            source_errors += 1
        else:
            status, error = "SUCCEEDED", None
            succeeded += 1
        with connect(db_path) as connection:
            record_cb_daily_backfill_status(connection, {
                "trade_date": iso, "status": status, "last_error": error,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        processed += 1
        candidate += timedelta(days=1)
        if delay_seconds:
            time.sleep(delay_seconds)
    return {
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "processed": processed, "succeeded": succeeded, "non_trading": non_trading,
        "source_errors": source_errors, "skipped": skipped,
        "next_date": candidate.isoformat() if candidate <= end_date else None,
        "request_count": http.request_count,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill recent official TPEx CB daily market reports"
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--end-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--max-calendar-days", type=int, default=180)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.start_date:
            if args.dry_run:
                raise ValueError("dry-run is not supported for range mode")
            if not args.end_date:
                raise ValueError("--end-date is required with --start-date")
            result = backfill_cb_daily_range(
                args.start_date, args.end_date, args.database,
                batch_size=args.batch_size, delay_seconds=args.delay_seconds,
            )
        else:
            result = backfill_cb_daily(
                args.database, args.days, args.end_date, args.dry_run,
                args.max_calendar_days,
            )
    except (
        BackfillIncompleteError,
        requests.RequestException,
        TpexFormatError,
        ValueError,
    ) as exc:
        print(f"cb_backfill_error: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
