from datetime import date
from pathlib import Path

import pytest

from active_etf_collector import ActiveEtfSourceError
from daily_pipeline import DailyPipelinePreconditionError, collect_tracked_active_etfs_daily, run_daily_pipeline
from db import connect, upsert_daily
from institutional_etf_backfill import backfill_institutional_etf


DAY = date(2026, 9, 3)


def seed_trade_date(db_path, value=DAY):
    with connect(db_path) as con:
        upsert_daily(con, [{"trade_date": value.isoformat(), "cb_code": "11111", "cb_name": "甲一",
            "close_price": 1.0, "volume_lots": 1, "source": "test", "collected_at": "x"}])


def test_daily_pipeline_refuses_non_verified_date_before_collectors(tmp_path, monkeypatch):
    called = False
    def institutional(*_args):
        nonlocal called
        called = True
    monkeypatch.setattr("daily_pipeline.collect_institutional_daily", institutional)
    with pytest.raises(DailyPipelinePreconditionError, match="not a verified"):
        run_daily_pipeline(DAY, tmp_path / "db.sqlite")
    assert not called


def test_daily_pipeline_runs_institutional_before_etfs(tmp_path, monkeypatch):
    db_path, calls = tmp_path / "db.sqlite", []
    seed_trade_date(db_path)
    monkeypatch.setattr("daily_pipeline.collect_institutional_daily", lambda *_args: calls.append("institutional") or {"target_stocks": 1})
    monkeypatch.setattr("daily_pipeline.collect_tracked_active_etfs_daily", lambda *_args: calls.append("etfs") or {"coverage": "complete"})
    assert run_daily_pipeline(DAY, db_path)["active_etfs"]["coverage"] == "complete"
    assert calls == ["institutional", "etfs"]


def test_daily_pipeline_runs_etfs_when_institutional_coverage_is_incomplete(tmp_path, monkeypatch):
    db_path, calls = tmp_path / "db.sqlite", []
    seed_trade_date(db_path)
    monkeypatch.setattr("daily_pipeline.collect_institutional_daily", lambda *_args: calls.append("institutional") or {"coverage": "incomplete"})
    monkeypatch.setattr("daily_pipeline.collect_tracked_active_etfs_daily", lambda *_args: calls.append("etfs") or {"coverage": "complete"})
    assert run_daily_pipeline(DAY, db_path)["institutional"]["coverage"] == "incomplete"
    assert calls == ["institutional", "etfs"]


def test_one_etf_failure_is_persisted_and_does_not_stop_other_etfs(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite"
    def save_master(connection, code):
        metadata = __import__("daily_pipeline").active_etf_source_metadata(code)
        connection.execute("""INSERT INTO active_etf_master VALUES
            (:etf_code,:etf_name,:manager_name,:source_url,:source_identifier,1,'succeeded',NULL,'x')""", metadata)
    def nomura(_day, code, con, _session):
        if code == "00985A":
            raise ActiveEtfSourceError("Nomura unavailable")
        save_master(con, code)
        return (1, 0)
    def capital(_day, code, con, _session):
        save_master(con, code)
        return (1, 0)
    monkeypatch.setattr("daily_pipeline.collect_nomura", nomura)
    monkeypatch.setattr("daily_pipeline.collect_capital", capital)
    result = collect_tracked_active_etfs_daily(DAY, db_path)
    assert result["coverage"] == "incomplete"
    assert result["succeeded_etfs"] == ["00980A", "00999A", "00982A", "00992A"]
    assert result["failed_etfs"] == {"00985A": "Nomura unavailable"}
    with connect(db_path) as con:
        row = con.execute("SELECT status, error_message FROM active_etf_collection_status WHERE trade_date=? AND etf_code='00985A'", (DAY.isoformat(),)).fetchone()
    assert tuple(row) == ("failed", "Nomura unavailable")


def test_backfill_defaults_to_verified_dates_and_supports_explicit_range(tmp_path):
    db_path = tmp_path / "db.sqlite"
    for value in (date(2026, 9, 1), date(2026, 9, 2), DAY):
        seed_trade_date(db_path, value)
    calls = []
    def runner(value, _db):
        calls.append(value)
        return {"trade_date": value.isoformat(), "active_etfs": {"coverage": "complete"}}
    result = backfill_institutional_etf(db_path, days=2, runner=runner)
    assert calls == [date(2026, 9, 2), DAY]
    assert result["trade_days"] == 2
    calls.clear()
    backfill_institutional_etf(db_path, start_date=date(2026, 9, 1), end_date=date(2026, 9, 2), runner=runner)
    assert calls == [date(2026, 9, 1), date(2026, 9, 2)]


def test_workflow_runs_strategies_after_parent_stock_collection_before_dashboard():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/daily-collector.yml").read_text(encoding="utf-8")
    assert workflow.index("- name: Run parent stock market collector") < workflow.index("- name: Run strategy A-v1")
    assert workflow.index("- name: Run strategy A-v1") < workflow.index("- name: Run strategy C-v1")
    assert workflow.index("- name: Run strategy C-v1") < workflow.index("- name: Build dashboard data")
    assert "python strategy_engine.py" in workflow
    assert "python strategy_c.py" in workflow
