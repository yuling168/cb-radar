"""Backfill official Phase 1 CB daily data for recent verified trading days."""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from collector import DataNotPublished, TpexFormatError, collect
from config import DEFAULT_DB_PATH


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
        "request_count": http.request_count,
        "dry_run": dry_run,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill recent official TPEx CB daily market reports"
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--end-date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--max-calendar-days", type=int, default=180)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = backfill_cb_daily(
            args.database,
            args.days,
            args.end_date,
            args.dry_run,
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
