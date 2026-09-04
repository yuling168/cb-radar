"""Local daily integration for CB-parent institutional and tracked active-ETF data."""

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from active_etf_collector import (
    ActiveEtfSourceError,
    TRACKED_ACTIVE_ETF_CODES,
    active_etf_source_metadata,
    collect_capital,
    collect_nomura,
)
from config import DEFAULT_DB_PATH
from db import connect, upsert_active_etf_collection_status
from institutional_collector import InstitutionalSourceError, collect_institutional_daily
from parent_flow_metrics import recompute_parent_flow_metrics


class DailyPipelinePreconditionError(RuntimeError):
    """The requested date is not a verified Phase 1 trading date."""


def _is_verified_trade_date(db_path: Path | str, trade_date: date) -> bool:
    with connect(db_path) as connection:
        return connection.execute(
            "SELECT 1 FROM cb_daily WHERE trade_date = ? LIMIT 1", (trade_date.isoformat(),)
        ).fetchone() is not None


def _record_etf_failure(connection, trade_date: date, etf_code: str, error: Exception) -> None:
    metadata = active_etf_source_metadata(etf_code)
    connection.execute(
        """INSERT INTO active_etf_master VALUES
          (:etf_code,:etf_name,:manager_name,:source_url,:source_identifier,1,'failed',:last_error,:last_checked_at)
        ON CONFLICT(etf_code) DO UPDATE SET last_status='failed',last_error=excluded.last_error,
          last_checked_at=excluded.last_checked_at""",
        {**metadata, "last_error": str(error), "last_checked_at": datetime.now(timezone.utc).isoformat()},
    )
    upsert_active_etf_collection_status(connection, {
        "trade_date": trade_date.isoformat(), "etf_code": etf_code, "status": "failed",
        "error_message": str(error), "checked_at": datetime.now(timezone.utc).isoformat(),
    })


def collect_tracked_active_etfs_daily(trade_date: date, db_path: Path | str = DEFAULT_DB_PATH, session=None):
    """Collect each verified source independently; failures leave holdings untouched."""
    successes, failures, inserted, updated = [], {}, 0, 0
    with connect(db_path) as connection:
        for etf_code in TRACKED_ACTIVE_ETF_CODES:
            try:
                if etf_code in {"00980A", "00985A", "00999A"}:
                    change = collect_nomura(trade_date, etf_code, connection, session)
                else:
                    change = collect_capital(trade_date, etf_code, connection, session)
                upsert_active_etf_collection_status(connection, {
                    "trade_date": trade_date.isoformat(), "etf_code": etf_code,
                    "status": "succeeded", "error_message": None,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })
                inserted += change[0]
                updated += change[1]
                successes.append(etf_code)
            except (ActiveEtfSourceError, requests.RequestException, ValueError) as exc:
                _record_etf_failure(connection, trade_date, etf_code, exc)
                failures[etf_code] = str(exc)
    return {
        "trade_date": trade_date.isoformat(), "tracked_etfs": list(TRACKED_ACTIVE_ETF_CODES),
        "succeeded_etfs": successes, "failed_etfs": failures,
        "coverage": "complete" if not failures else "incomplete",
        "records_inserted": inserted, "records_updated": updated,
    }


def run_daily_pipeline(trade_date: date, db_path: Path | str = DEFAULT_DB_PATH, session=None):
    if not _is_verified_trade_date(db_path, trade_date):
        raise DailyPipelinePreconditionError(
            f"{trade_date.isoformat()} is not a verified CB trading date; no collectors ran"
        )
    institutional = collect_institutional_daily(trade_date, db_path, session)
    etfs = collect_tracked_active_etfs_daily(trade_date, db_path, session)
    with connect(db_path) as connection:
        metrics_rows = recompute_parent_flow_metrics(connection, trade_date.isoformat())
    return {"trade_date": trade_date.isoformat(), "institutional": institutional,
            "active_etfs": etfs, "parent_flow_metrics_rows": metrics_rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run CB-parent institutional and tracked active-ETF collectors")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    try:
        print(run_daily_pipeline(args.date, args.database))
    except (DailyPipelinePreconditionError, InstitutionalSourceError, requests.RequestException) as exc:
        print(f"daily_pipeline_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
