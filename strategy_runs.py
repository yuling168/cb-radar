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


def dashboard_a_baseline_coverage(connection: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Validate baseline coverage against all Dashboard dates observed at publication time."""
    required = connection.execute(
        "SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM cb_daily"
    ).fetchone()
    start_date, end_date, date_count = required
    if start_date is None:
        raise ValueError("Cannot publish Strategy A run: Dashboard has no cb_daily dates")
    run = connection.execute(
        "SELECT start_date, end_date FROM strategy_run WHERE run_id=?", (run_id,)
    ).fetchone()
    assert run is not None
    missing_date = connection.execute(
        """SELECT daily.trade_date
           FROM (SELECT DISTINCT trade_date FROM cb_daily) AS daily
           WHERE NOT EXISTS (
               SELECT 1 FROM strategy_run_evaluations AS evaluation
               WHERE evaluation.run_id = ? AND evaluation.trade_date = daily.trade_date
           )
           ORDER BY daily.trade_date LIMIT 1""",
        (run_id,),
    ).fetchone()
    coverage = {
        "start_date": start_date,
        "end_date": end_date,
        "trade_date_count": date_count,
        "run_start_date": run["start_date"],
        "run_end_date": run["end_date"],
    }
    if run["start_date"] > start_date or run["end_date"] < end_date:
        coverage["valid"] = False
        coverage["reason"] = (
            f"run range {run['start_date']}..{run['end_date']} does not cover "
            f"Dashboard range {start_date}..{end_date}"
        )
    elif missing_date is not None:
        coverage["valid"] = False
        coverage["reason"] = f"run cache is missing Dashboard trade date {missing_date['trade_date']}"
    else:
        coverage["valid"] = True
    return coverage


def _a_run(connection: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    run = connection.execute(
        """SELECT run.run_id, run.status, run.start_date, run.end_date, definition.definition_id,
                  definition.strategy_code, definition.strategy_version
           FROM strategy_run AS run
           INNER JOIN strategy_definition AS definition ON definition.definition_id = run.definition_id
           WHERE run.run_id=?""",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Cannot publish Strategy A run {run_id}: run does not exist")
    return run


def _validate_active_completed_a_run(connection: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    """Return an A run only when it is completed at the registry-active version."""
    strategy = get_strategy("A")
    run = _a_run(connection, run_id)
    if run["strategy_code"] != strategy.strategy_code:
        raise ValueError(f"Cannot publish run {run_id}: strategy is {run['strategy_code']}, expected A")
    if run["strategy_version"] != strategy.active_version:
        raise ValueError(
            f"Cannot publish A run {run_id}: version is {run['strategy_version']}, "
            f"registry active version is {strategy.active_version}"
        )
    if run["status"] != "COMPLETED":
        raise ValueError(f"Cannot publish A run {run_id}: status is {run['status']}, expected COMPLETED")
    return run


def publish_a_baseline(connection: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Publish a complete historical A baseline for the registry-active definition.

    Recalculation never calls this function, so ordinary historical and test
    runs cannot affect the site.  Publishing a new version replaces the series
    pointer; date overrides remain definition-scoped and cannot cross versions.
    """
    strategy = get_strategy("A")
    run = _validate_active_completed_a_run(connection, run_id)
    coverage = dashboard_a_baseline_coverage(connection, run_id)
    if not coverage["valid"]:
        raise ValueError(f"Cannot publish A run {run_id}: insufficient Dashboard coverage: {coverage['reason']}")
    connection.execute(
        """INSERT INTO strategy_published_series
           (strategy_code, definition_id, baseline_run_id, published_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(strategy_code) DO UPDATE SET
             definition_id=excluded.definition_id, baseline_run_id=excluded.baseline_run_id,
             published_at=excluded.published_at""",
        (strategy.strategy_code, run["definition_id"], run_id, _now()),
    )
    connection.commit()
    return {
        "strategy_code": strategy.strategy_code,
        "run_id": run_id,
        "definition_id": run["definition_id"],
        "coverage": coverage,
    }


def publish_a_date(connection: sqlite3.Connection, run_id: int, trade_date: str) -> dict[str, Any]:
    """Publish one completed A incremental or correction run for one date."""
    series = connection.execute(
        "SELECT definition_id FROM strategy_published_series WHERE strategy_code='A'"
    ).fetchone()
    if series is None:
        raise ValueError("Cannot publish A date: no published Strategy A baseline series")
    run = _validate_active_completed_a_run(connection, run_id)
    if run["definition_id"] != series["definition_id"]:
        raise ValueError(
            f"Cannot publish A date {trade_date}: run definition {run['definition_id']} "
            f"does not match published series definition {series['definition_id']}"
        )
    missing = connection.execute(
        """SELECT daily.cb_code FROM cb_daily AS daily
           WHERE daily.trade_date=?
             AND NOT EXISTS (
                 SELECT 1 FROM strategy_run_evaluations AS evaluation
                 WHERE evaluation.run_id=? AND evaluation.trade_date=daily.trade_date
                   AND evaluation.cb_code=daily.cb_code
             )
           ORDER BY daily.cb_code LIMIT 1""",
        (trade_date, run_id),
    ).fetchone()
    if missing is not None:
        raise ValueError(
            f"Cannot publish A date {trade_date}: run {run_id} is missing evaluation cache for {missing['cb_code']}"
        )
    cached = connection.execute(
        "SELECT 1 FROM strategy_run_evaluations WHERE run_id=? AND trade_date=? LIMIT 1",
        (run_id, trade_date),
    ).fetchone()
    if cached is None:
        raise ValueError(f"Cannot publish A date {trade_date}: run {run_id} has no evaluation cache for that date")
    connection.execute(
        """INSERT INTO strategy_published_date (definition_id, trade_date, run_id, published_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(definition_id, trade_date) DO UPDATE SET
             run_id=excluded.run_id, published_at=excluded.published_at""",
        (series["definition_id"], trade_date, run_id, _now()),
    )
    connection.commit()
    return {"definition_id": series["definition_id"], "trade_date": trade_date, "run_id": run_id}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an isolated Strategy A-v2 historical recalculation")
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date", help="one effective trade date (YYYY-MM-DD)")
    dates.add_argument("--start-date", help="inclusive range start (YYYY-MM-DD)")
    dates.add_argument("--publish-baseline", type=int, help="publish a completed, Dashboard-complete A-v2 baseline")
    dates.add_argument("--publish-date", type=int, help="publish one completed A-v2 run for --publish-trade-date")
    parser.add_argument("--end-date", help="inclusive range end; required with --start-date")
    parser.add_argument("--publish-trade-date", help="date to override with --publish-date (YYYY-MM-DD)")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    if args.start_date and not args.end_date:
        parser.error("--start-date requires --end-date")
    if args.end_date and not args.start_date:
        parser.error("--end-date requires --start-date")
    if args.start_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    if args.publish_date is not None and not args.publish_trade_date:
        parser.error("--publish-date requires --publish-trade-date")
    if args.publish_trade_date and args.publish_date is None:
        parser.error("--publish-trade-date requires --publish-date")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start_date = args.date or args.start_date
    with connect(args.database) as connection:
        if args.publish_baseline is not None:
            published = publish_a_baseline(connection, args.publish_baseline)
            print(f"published baseline_run_id: {published['run_id']}")
        elif args.publish_date is not None:
            published = publish_a_date(connection, args.publish_date, args.publish_trade_date)
            print(f"published date: {published['trade_date']} run_id: {published['run_id']}")
        else:
            run_id = run_a_v2_recalculation(connection, start_date, args.end_date)
            print(f"run_id: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
