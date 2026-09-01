from datetime import date

import pytest

from db import connect, upsert_daily
from stock_backfill import BackfillPreconditionError, backfill_stock_daily_market


def add_phase1_day(connection, trade_date: str, cb_code: str = "11111"):
    upsert_daily(
        connection,
        [{
            "trade_date": trade_date,
            "cb_code": cb_code,
            "cb_name": "甲一",
            "close_price": 100.0,
            "volume_lots": 1,
            "source": "test",
            "collected_at": "2026-08-31T00:00:00+00:00",
        }],
    )


def seed_master(db_path):
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO cb_master (
                cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
                issue_amount, source, source_url, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "11111", "甲一", "1101", "台泥", "2024-01-01", "2027-01-01",
                100_000_000, "test", "test", "2026-08-31T00:00:00+00:00",
            ),
        )
        add_phase1_day(connection, "2026-08-27")
        add_phase1_day(connection, "2026-08-28")
        add_phase1_day(connection, "2026-08-31")


def test_backfill_processes_verified_trade_dates_oldest_first(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    calls = []

    def collector(trade_date, database):
        calls.append((trade_date.isoformat(), database))
        return {
            "target_stocks": 1,
            "records_inserted": 1,
            "records_updated": 0,
        }

    result = backfill_stock_daily_market(db_path, days=2, collector=collector)

    assert calls == [
        ("2026-08-28", db_path),
        ("2026-08-31", db_path),
    ]
    assert result == {
        "start_date": "2026-08-28",
        "end_date": "2026-08-31",
        "trade_days": 2,
        "target_stock_observations": 2,
        "records_inserted": 2,
        "records_updated": 0,
        "missing": 0,
    }


def test_backfill_requires_all_requested_phase1_trade_dates_before_network_work(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    called = False

    def collector(*_args):
        nonlocal called
        called = True
        raise AssertionError("collector must not run")

    with pytest.raises(BackfillPreconditionError, match="Need 60 verified cb_daily"):
        backfill_stock_daily_market(db_path, days=60, collector=collector)
    assert not called


def test_backfill_rejects_date_without_a_master_parent_universe(tmp_path):
    db_path = tmp_path / "history.db"
    with connect(db_path) as connection:
        add_phase1_day(connection, "2026-08-31", cb_code="99999")

    with pytest.raises(BackfillPreconditionError, match="No cb_master parent-stock universe"):
        backfill_stock_daily_market(db_path, days=1)


def test_backfill_honors_an_end_date(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    calls = []

    def collector(trade_date, _database):
        calls.append(trade_date)
        return {"target_stocks": 1, "records_inserted": 0, "records_updated": 1}

    result = backfill_stock_daily_market(
        db_path, days=2, end_date=date(2026, 8, 28), collector=collector
    )

    assert calls == [date(2026, 8, 27), date(2026, 8, 28)]
    assert result["start_date"] == "2026-08-27"
    assert result["end_date"] == "2026-08-28"
    assert result["records_inserted"] == 0
    assert result["records_updated"] == 2
