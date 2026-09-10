import json
import shutil
from pathlib import Path

import pytest

import strategy_runs
from db import connect
from strategy_registry import StrategyDefinition
from strategy_runs import (
    A_V2_PARAMETERS,
    RUN_TYPE_HISTORICAL_RECALCULATION,
    a_v2_rule_hash,
    current_git_commit,
    parse_args,
    publish_a_baseline,
    publish_a_date,
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


def _publish_fixture(connection):
    """Create a two-day Dashboard history and a fully cached completed A-v2 run."""
    connection.executemany(
        """INSERT INTO cb_daily
           (trade_date, cb_code, cb_name, close_price, reference_price, volume_lots, source, collected_at)
           VALUES (?, '12345', '測試 CB', 120, 120, 10, 'test', '2026-01-01T00:00:00+00:00')""",
        [("2026-01-02",), ("2026-01-03",)],
    )
    connection.execute(
        """INSERT INTO strategy_definition
           (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
            git_commit, is_active, created_at)
           VALUES ('A', 'v2', 'test A-v2', '{}', 'test-rule', 'test-commit', 1, 'x')"""
    )
    definition_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.execute(
        """INSERT INTO strategy_run
           (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
           VALUES (?, '2026-01-02', '2026-01-03', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'y')""",
        (definition_id,),
    )
    run_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.executemany(
        """INSERT INTO strategy_run_evaluations
           (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
            data_status, unavailable_reasons_json, evaluated_at)
           VALUES (?, '12345', ?, '{}', '{}', 'AVAILABLE', '[]', 'x')""",
        [(run_id, "2026-01-02"), (run_id, "2026-01-03")],
    )
    return definition_id, run_id


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
    assert parse_args(["--publish-baseline", "42"]).publish_baseline == 42
    args = parse_args(["--publish-date", "42", "--publish-trade-date", "2026-09-09"])
    assert (args.publish_date, args.publish_trade_date) == (42, "2026-09-09")


def test_publish_a_baseline_requires_completed_active_version_and_full_dashboard_coverage(tmp_path):
    with connect(tmp_path / "publish.db") as connection:
        definition_id, complete_run = _publish_fixture(connection)
        published = publish_a_baseline(connection, complete_run)
        assert published["run_id"] == complete_run
        assert published["definition_id"] == definition_id
        assert published["coverage"] == {
            "start_date": "2026-01-02", "end_date": "2026-01-03", "trade_date_count": 2,
            "run_start_date": "2026-01-02", "run_end_date": "2026-01-03", "valid": True,
        }
        assert connection.execute(
            "SELECT baseline_run_id FROM strategy_published_series WHERE strategy_code='A'"
        ).fetchone()[0] == complete_run


def test_publish_a_date_requires_the_published_definition_and_replaces_only_that_date(tmp_path, monkeypatch):
    with connect(tmp_path / "publish-date.db") as connection:
        definition_id, baseline_run = _publish_fixture(connection)
        publish_a_baseline(connection, baseline_run)

        def completed_incremental(date_text, marker):
            connection.execute(
                """INSERT INTO strategy_run
                   (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
                   VALUES (?, ?, ?, 'HISTORICAL_RECALCULATION', 'COMPLETED', ?, ?)""",
                (definition_id, date_text, date_text, marker, marker),
            )
            run_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                """INSERT INTO strategy_run_evaluations
                   (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
                    data_status, unavailable_reasons_json, evaluated_at)
                   VALUES (?, '12345', ?, '{}', ?, 'AVAILABLE', '[]', ?)""",
                (run_id, date_text, json.dumps({"marker": marker}), marker),
            )
            return run_id

        first = completed_incremental("2026-01-03", "first")
        assert publish_a_date(connection, first, "2026-01-03") == {
            "definition_id": definition_id, "trade_date": "2026-01-03", "run_id": first,
        }
        unpublish = completed_incremental("2026-01-03", "unpublished")
        assert connection.execute(
            "SELECT run_id FROM strategy_published_date WHERE definition_id=? AND trade_date='2026-01-03'",
            (definition_id,),
        ).fetchone()[0] == first
        replacement = completed_incremental("2026-01-03", "replacement")
        publish_a_date(connection, replacement, "2026-01-03")
        assert connection.execute(
            "SELECT run_id FROM strategy_published_date WHERE definition_id=? AND trade_date='2026-01-03'",
            (definition_id,),
        ).fetchone()[0] == replacement
        assert unpublish != replacement
        with pytest.raises(ValueError, match="missing evaluation cache"):
            publish_a_date(connection, replacement, "2026-01-02")

        connection.execute(
            """INSERT INTO strategy_definition
               (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
                git_commit, is_active, created_at)
               VALUES ('A', 'v3', 'test A-v3', '{}', 'v3-rule', 'v3-commit', 0, 'x')"""
        )
        v3_definition_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-01-03', '2026-01-03', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'z')""",
            (v3_definition_id,),
        )
        v3_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run_evaluations
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
                data_status, unavailable_reasons_json, evaluated_at)
               VALUES (?, '12345', '2026-01-03', '{}', '{}', 'AVAILABLE', '[]', 'x')""",
            (v3_run,),
        )
        with monkeypatch.context() as patched:
            patched.setattr(
                strategy_runs, "get_strategy", lambda _code: StrategyDefinition("A", "v3", "test A-v3"),
            )
            with pytest.raises(ValueError, match="does not match published series definition"):
                publish_a_date(connection, v3_run, "2026-01-03")

        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-01-03', '2026-01-03', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'z')""",
            (definition_id,),
        )
        single_day_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(ValueError, match="does not cover Dashboard range"):
            publish_a_baseline(connection, single_day_run)

        for status in ("FAILED", "RUNNING"):
            connection.execute(
                """INSERT INTO strategy_run
                   (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
                   VALUES (?, '2026-01-02', '2026-01-03', 'HISTORICAL_RECALCULATION', ?, 'x', 'z')""",
                (definition_id, status),
            )
            run_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            with pytest.raises(ValueError, match=f"status is {status}"):
                publish_a_baseline(connection, run_id)

        connection.execute(
            """INSERT INTO strategy_definition
               (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
                git_commit, is_active, created_at)
               VALUES ('A', 'v1', 'test A-v1', '{}', 'old-rule', 'old-commit', 0, 'x')"""
        )
        v1_definition_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-01-02', '2026-01-03', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'z')""",
            (v1_definition_id,),
        )
        v1_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(ValueError, match="registry active version is v2"):
            publish_a_baseline(connection, v1_run)

        # The rejected runs cannot replace the existing published pointer.
        assert connection.execute(
            "SELECT baseline_run_id FROM strategy_published_series WHERE strategy_code='A'"
        ).fetchone()[0] == baseline_run


def test_current_git_commit_uses_the_repository_head():
    assert len(current_git_commit()) == 40
