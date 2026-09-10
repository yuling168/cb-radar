"""Run-scoped, rebuildable Strategy A-v2 calculation cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import DEFAULT_DB_PATH
from db import connect
from strategy_engine import evaluate_a_v2
from strategy_registry import get_strategy


RUN_TYPE_HISTORICAL_RECALCULATION = "HISTORICAL_RECALCULATION"

A_V2_PARAMETERS = {
    "conditions": [
        {"key": "volume_above_10_day_average", "rule": "today_volume_lots > trailing_10_day_average_volume_lots"},
        {"key": "close_price_in_115_to_150", "rule": "115 <= close_price <= 150"},
        {"key": "close_price_and_premium", "rule": "close_price > conversion_value and premium_rate_pct > 1"},
        {"key": "ten_day_volume_above_300_lots", "rule": "trailing_10_day_total_volume_lots > 300"},
        {"key": "volume_above_prior_5_average_times_3", "rule": "today_volume_lots > prior_5_day_average_volume_lots * 3"},
    ],
    "volume_unit": "lots",
    "valid_trade_days": "cb_daily dates; zero volume is observed; non-trading dates excluded",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def a_v2_rule_hash() -> str:
    """Return a stable hash of the formal A-v2 rule parameters."""
    return hashlib.sha256(_json(A_V2_PARAMETERS).encode("utf-8")).hexdigest()


def current_git_commit(repository_root: Path | None = None) -> str:
    """Resolve HEAD exactly, or fail instead of inventing provenance."""
    root = repository_root or Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Unable to determine git commit for strategy definition") from exc
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError("Unable to determine git commit for strategy definition")
    return commit


def ensure_a_v2_definition(connection: sqlite3.Connection, *, git_commit: str | None = None) -> int:
    """Create or return the provenance-bearing A-v2 definition.

    ``is_active`` records the registry state when the definition was first
    observed.  It is metadata only: active-version dispatch always uses the
    registry and never reads this database column.
    """
    strategy = get_strategy("A")
    if strategy.active_version != "v2":
        raise RuntimeError("Strategy A active version is not A-v2")
    commit = git_commit if git_commit is not None else current_git_commit()
    if not commit:
        raise RuntimeError("Unable to determine git commit for strategy definition")
    parameters_json = _json(A_V2_PARAMETERS)
    rule_hash = a_v2_rule_hash()
    connection.execute(
        """INSERT OR IGNORE INTO strategy_definition
           (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
            git_commit, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy.strategy_code, strategy.active_version, strategy.strategy_name,
         parameters_json, rule_hash, commit, int(strategy.active_version == "v2"), _now()),
    )
    row = connection.execute(
        """SELECT definition_id FROM strategy_definition
           WHERE strategy_code=? AND strategy_version=? AND rule_hash=? AND git_commit=?""",
        (strategy.strategy_code, strategy.active_version, rule_hash, commit),
    ).fetchone()
    assert row is not None
    return int(row["definition_id"])


def _insert_run(connection: sqlite3.Connection, definition_id: int, start_date: str, end_date: str) -> int:
    cursor = connection.execute(
        """INSERT INTO strategy_run
           (definition_id, start_date, end_date, run_type, status, started_at)
           VALUES (?, ?, ?, ?, 'RUNNING', ?)""",
        (definition_id, start_date, end_date, RUN_TYPE_HISTORICAL_RECALCULATION, _now()),
    )
    return int(cursor.lastrowid)


def _cache_result(connection: sqlite3.Connection, run_id: int, result: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO strategy_run_evaluations
           (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
            data_status, unavailable_reasons_json, evaluated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, result["cb_code"], result["trade_date"], _json(result["conditions"]),
         _json(result["values"]), result["data_status"], _json(result["unavailable_reasons"]),
         result["evaluated_at"]),
    )
    if result["data_status"] == "AVAILABLE" and all(result["conditions"].values()):
        connection.execute(
            """INSERT INTO strategy_run_signals
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, result["cb_code"], result["trade_date"], _json(result["conditions"]),
             _json(result["values"]), result["evaluated_at"]),
        )


def run_a_v2_recalculation(
    connection: sqlite3.Connection,
    start_date: str,
    end_date: str | None = None,
    *,
    git_commit: str | None = None,
    evaluator: Callable[[sqlite3.Connection, str], list[dict[str, Any]]] = evaluate_a_v2,
) -> int:
    """Cache a fresh A-v2 historical calculation without touching legacy strategy tables."""
    final_date = end_date or start_date
    if start_date > final_date:
        raise ValueError("start_date must not be after end_date")
    definition_id = ensure_a_v2_definition(connection, git_commit=git_commit)
    run_id = _insert_run(connection, definition_id, start_date, final_date)
    try:
        dates = [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date, final_date),
        )]
        if not dates:
            raise ValueError("No effective trade dates in requested range")
        for trade_date in dates:
            for result in evaluator(connection, trade_date):
                _cache_result(connection, run_id, result)
        connection.execute(
            "UPDATE strategy_run SET status='COMPLETED', completed_at=? WHERE run_id=?",
            (_now(), run_id),
        )
        connection.commit()
        return run_id
    except Exception as exc:
        connection.execute(
            """UPDATE strategy_run SET status='FAILED', completed_at=?, error_message=?
               WHERE run_id=?""",
            (_now(), str(exc), run_id),
        )
        connection.commit()
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an isolated Strategy A-v2 historical recalculation")
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date", help="one effective trade date (YYYY-MM-DD)")
    dates.add_argument("--start-date", help="inclusive range start (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="inclusive range end; required with --start-date")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    if args.start_date and not args.end_date:
        parser.error("--start-date requires --end-date")
    if args.end_date and not args.start_date:
        parser.error("--end-date requires --start-date")
    if args.start_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start_date = args.date or args.start_date
    with connect(args.database) as connection:
        run_id = run_a_v2_recalculation(connection, start_date, args.end_date)
    print(f"run_id: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
