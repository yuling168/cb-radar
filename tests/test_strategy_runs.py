import json
import shutil
from pathlib import Path

import pytest

from db import connect
from strategy_runs import (
    A_V2_PARAMETERS,
    RUN_TYPE_HISTORICAL_RECALCULATION,
    a_v2_rule_hash,
    current_git_commit,
    parse_args,
    run_a_v2_recalculation,
)


SOURCE_DATABASE = Path(__file__).resolve().parents[1] / "data" / "cb_history.db"


def _history_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "history.db"
    shutil.copy2(SOURCE_DATABASE, destination)
    return destination


def _legacy_counts(connection):
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("strategy_signals", "strategy_evaluations")
    )


def test_a_v2_historical_run_caches_2026_09_09_without_touching_legacy_tables(tmp_path):
    with connect(_history_copy(tmp_path)) as connection:
        before = _legacy_counts(connection)
        run_id = run_a_v2_recalculation(
            connection, "2026-09-09", git_commit="test-commit-a-v2",
        )

        assert _legacy_counts(connection) == before
        run = connection.execute("SELECT * FROM strategy_run WHERE run_id=?", (run_id,)).fetchone()
        assert run["run_type"] == RUN_TYPE_HISTORICAL_RECALCULATION
        assert run["status"] == "COMPLETED"
        definition = connection.execute(
            "SELECT * FROM strategy_definition WHERE definition_id=?", (run["definition_id"],)
        ).fetchone()
        assert definition["strategy_code"] == "A"
        assert definition["strategy_version"] == "v2"
        assert json.loads(definition["parameters_json"]) == A_V2_PARAMETERS
        assert len(A_V2_PARAMETERS["conditions"]) == 5
        assert definition["rule_hash"] == a_v2_rule_hash()
        assert definition["git_commit"] == "test-commit-a-v2"
        assert definition["is_active"] == 1

        evaluations = {
            row["cb_code"]: row
            for row in connection.execute(
                "SELECT * FROM strategy_run_evaluations WHERE run_id=?", (run_id,)
            )
            if row["cb_code"] in {"47394", "81473", "811210"}
        }
        assert evaluations["811210"]["data_status"] == "AVAILABLE"
        assert evaluations["47394"]["data_status"] == "AVAILABLE"
        assert evaluations["81473"]["data_status"] == "AVAILABLE"
        assert json.loads(evaluations["47394"]["condition_results_json"])["ten_day_volume_above_300_lots"] is False
        assert json.loads(evaluations["81473"]["condition_results_json"])["ten_day_volume_above_300_lots"] is False
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_run_signals WHERE run_id=? AND cb_code='811210'", (run_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_run_signals WHERE run_id=? AND cb_code IN ('47394','81473')", (run_id,)
        ).fetchone()[0] == 0


def test_a_v2_repeated_runs_are_isolated_and_do_not_overwrite(tmp_path):
    with connect(_history_copy(tmp_path)) as connection:
        first_run = run_a_v2_recalculation(connection, "2026-09-09", git_commit="test-commit-a-v2")
        definition_id = connection.execute(
            "SELECT definition_id FROM strategy_run WHERE run_id=?", (first_run,)
        ).fetchone()[0]
        connection.execute("UPDATE strategy_definition SET is_active=0 WHERE definition_id=?", (definition_id,))
        second_run = run_a_v2_recalculation(connection, "2026-09-09", git_commit="test-commit-a-v2")

        assert first_run != second_run
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_run WHERE run_id IN (?, ?)", (first_run, second_run)
        ).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM strategy_definition").fetchone()[0] == 1
        assert connection.execute(
            "SELECT definition_id FROM strategy_run WHERE run_id=?", (second_run,)
        ).fetchone()[0] == definition_id
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_run_evaluations WHERE run_id=?", (first_run,)
        ).fetchone()[0] == connection.execute(
            "SELECT COUNT(*) FROM strategy_run_evaluations WHERE run_id=?", (second_run,)
        ).fetchone()[0]


def test_a_v2_failed_run_keeps_status_and_error(tmp_path):
    with connect(_history_copy(tmp_path)) as connection:
        def failing_evaluator(_connection, _trade_date):
            raise RuntimeError("intentional evaluator failure")

        with pytest.raises(RuntimeError, match="intentional evaluator failure"):
            run_a_v2_recalculation(
                connection, "2026-09-09", git_commit="test-commit-a-v2", evaluator=failing_evaluator,
            )

        failed = connection.execute(
            "SELECT status, error_message FROM strategy_run ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert failed["status"] == "FAILED"
        assert failed["error_message"] == "intentional evaluator failure"


def test_a_v2_definition_requires_real_git_provenance_when_not_supplied(tmp_path, monkeypatch):
    with connect(_history_copy(tmp_path)) as connection:
        monkeypatch.setattr("strategy_runs.current_git_commit", lambda: (_ for _ in ()).throw(RuntimeError("git unavailable")))
        with pytest.raises(RuntimeError, match="git unavailable"):
            run_a_v2_recalculation(connection, "2026-09-09")


def test_run_cli_accepts_one_date_or_a_range():
    assert parse_args(["--date", "2026-09-09"]).date == "2026-09-09"
    args = parse_args(["--start-date", "2026-09-08", "--end-date", "2026-09-09"])
    assert (args.start_date, args.end_date) == ("2026-09-08", "2026-09-09")


def test_current_git_commit_uses_the_repository_head():
    assert len(current_git_commit()) == 40
