from datetime import date, timedelta

import pytest

from db import connect, upsert_daily, upsert_stock_daily_market
from strategy_engine import (
    STRATEGY_CODE,
    STRATEGY_VERSION,
    evaluate_a_v1_on,
    parse_args,
    run_a_v1,
)


def _seed_a_v1_data(connection, *, missing_day: int | None = None):
    start = date(2026, 8, 3)
    volumes = [10, 20, 30, 40, 50, 10, 10, 20, 30, 100]
    records = []
    for index, volume in enumerate(volumes):
        if index == missing_day:
            # A different official CB establishes that this remains an observed
            # market day, so 12345's absence must not be treated as a zero.
            records.append({
                "trade_date": (start + timedelta(days=index)).isoformat(),
                "cb_code": "99999", "cb_name": "日曆 CB", "close_price": 100.0,
                "volume_lots": 0, "source": "test", "collected_at": "2026-08-20T00:00:00+00:00",
            })
            continue
        records.append({
            "trade_date": (start + timedelta(days=index)).isoformat(),
            "cb_code": "12345", "cb_name": "測試 CB", "close_price": 130.0,
            "volume_lots": volume, "source": "test", "collected_at": "2026-08-20T00:00:00+00:00",
        })
    upsert_daily(connection, records)
    connection.execute(
        """INSERT INTO cb_master (
            cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
            issue_amount, source, source_url, collected_at
        ) VALUES ('12345', '測試 CB', '1234', '測試股', '2026-01-01', '2030-01-01',
                  100000000, 'test', 'https://example.test', '2026-08-20T00:00:00+00:00')"""
    )
    connection.execute(
        """INSERT INTO conversion_price_events
            (cb_code, effective_date, conversion_price, source, source_url, collected_at)
            VALUES ('12345', '2026-01-01', 100, 'test', 'https://example.test', '2026-08-20T00:00:00+00:00')"""
    )
    upsert_stock_daily_market(connection, [{
        "trade_date": "2026-08-12", "p_stock_code": "1234", "p_open_price": 110.0,
        "p_high_price": 110.0, "p_low_price": 110.0, "p_close_price": 110.0,
        "p_volume_shares": 1000,
    }])


def test_a_v1_includes_zero_volume_days_and_saves_immutable_snapshot(tmp_path):
    with connect(tmp_path / "strategy.db") as connection:
        _seed_a_v1_data(connection)
        result = evaluate_a_v1_on(connection, "2026-08-12")[0]

        assert result["data_status"] == "AVAILABLE"
        assert all(result["conditions"].values())
        assert result["values"]["prior_9_max_volume_lots"] == 50
        assert result["values"]["ten_day_total_volume_lots"] == 320
        assert result["values"]["conversion_value"] == pytest.approx(110)
        assert result["values"]["premium_rate_pct"] > 1

        assert run_a_v1(connection, ["2026-08-12"])["signals_inserted"] == 1
        first = connection.execute(
            "SELECT condition_values_json FROM strategy_signals WHERE cb_code = '12345'"
        ).fetchone()[0]
        connection.execute("UPDATE cb_daily SET close_price = 115 WHERE cb_code = '12345' AND trade_date = '2026-08-12'")
        rerun = run_a_v1(connection, ["2026-08-12"])
        assert rerun["signals_existing"] == 1
        assert connection.execute(
            "SELECT condition_values_json FROM strategy_signals WHERE cb_code = '12345'"
        ).fetchone()[0] == first
        row = connection.execute(
            "SELECT strategy_code, strategy_version, data_status FROM strategy_signals"
        ).fetchone()
        assert tuple(row) == (STRATEGY_CODE, STRATEGY_VERSION, "AVAILABLE")
        assert connection.execute("SELECT COUNT(*) FROM strategy_evaluations").fetchone()[0] == 2


def test_a_v1_missing_daily_row_is_unavailable_not_zero_filled(tmp_path):
    with connect(tmp_path / "strategy.db") as connection:
        _seed_a_v1_data(connection, missing_day=4)
        result = evaluate_a_v1_on(connection, "2026-08-12")[0]

        assert result["data_status"] == "UNAVAILABLE"
        assert result["unavailable_reasons"] == ["missing_cb_daily_rows"]
        assert result["values"]["missing_trade_dates"] == ["2026-08-07"]
        totals = run_a_v1(connection, ["2026-08-12"])
        assert totals["signals_inserted"] == 0
        assert totals["unavailable"] == 1
        assert connection.execute("SELECT COUNT(*) FROM strategy_signals").fetchone()[0] == 0


def test_a_v1_missing_conversion_or_parent_close_is_diagnostic(tmp_path):
    with connect(tmp_path / "strategy.db") as connection:
        _seed_a_v1_data(connection)
        connection.execute("DELETE FROM conversion_price_events")
        assert evaluate_a_v1_on(connection, "2026-08-12")[0]["unavailable_reasons"] == [
            "missing_conversion_price_event"
        ]
        connection.execute(
            """INSERT INTO conversion_price_events
                (cb_code, effective_date, conversion_price, source, source_url, collected_at)
                VALUES ('12345', '2026-01-01', 100, 'test', 'https://example.test', 'x')"""
        )
        connection.execute("DELETE FROM stock_daily_market")
        assert evaluate_a_v1_on(connection, "2026-08-12")[0]["unavailable_reasons"] == [
            "missing_parent_stock_close"
        ]


def test_cli_accepts_a_single_date_or_an_inclusive_history_range():
    assert parse_args(["--date", "2026-08-12"]).date == date(2026, 8, 12)
    args = parse_args(["--start-date", "2026-08-01", "--end-date", "2026-08-31"])
    assert (args.start_date, args.end_date) == (date(2026, 8, 1), date(2026, 8, 31))
