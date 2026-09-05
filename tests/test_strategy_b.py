from datetime import date, timedelta

import pytest

from db import connect, upsert_daily, upsert_stock_daily_market
from strategy_b import STRATEGY_CODE, STRATEGY_VERSION, evaluate_b_v1_on, parse_args, run_b_v1
from strategy_c import run_c_v1
from strategy_engine import run_a_v1


START_DATE = date(2026, 6, 1)
DATES = [(START_DATE + timedelta(days=index)).isoformat() for index in range(43)]
TRADE_DATE = DATES[-1]


def _seed(connection, *, closes=None, volumes=None, balance_month="2026-06"):
    closes = closes or [100.0] * 42 + [110.0]
    volumes = volumes or [60] * 42 + [100]
    assert len(closes) == len(volumes) == 43
    connection.execute(
        """INSERT INTO cb_master (cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
            issue_amount, source, source_url, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("10001", "CB B", "9001", "母股", "2026-01-01", "2030-01-01", 1000000, "test", "u", "x"),
    )
    connection.execute(
        "INSERT INTO conversion_price_events VALUES (?,?,?,?,?,?)",
        ("10001", "2026-01-01", 100, "test", "u", "x"),
    )
    connection.execute(
        "INSERT INTO cb_monthly_balance VALUES (?,?,?,?,?,?)",
        ("10001", balance_month, 800000, "test", "u", "x"),
    )
    upsert_daily(connection, [
        {"trade_date": day, "cb_code": "10001", "cb_name": "CB B", "close_price": close,
         "volume_lots": volume, "source": "test", "collected_at": "x"}
        for day, close, volume in zip(DATES, closes, volumes)
    ])
    upsert_stock_daily_market(connection, [{
        "trade_date": TRADE_DATE, "p_stock_code": "9001", "p_open_price": 100,
        "p_high_price": 100, "p_low_price": 100, "p_close_price": 100, "p_volume_shares": 1,
    }])


def _only_result(connection):
    return evaluate_b_v1_on(connection, TRADE_DATE)[0]


def test_b_v1_uses_inclusive_windows_and_saves_complete_snapshot(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection)
        result = _only_result(connection)
    assert result["data_status"] == "AVAILABLE"
    assert all(result["conditions"].values())
    values = result["values"]
    assert len(values["window_43_trade_dates"]) == 43
    assert len(values["window_10_trade_dates"]) == 10
    assert len(values["window_5_trade_dates"]) == 5
    assert len(values["prior_19_trade_dates"]) == 19
    assert values["window_43_trade_dates"][-1] == TRADE_DATE
    assert values["balance_date"] == "2026-06-30"
    assert values["average_43_close_price"] == pytest.approx((42 * 100 + 110) / 43)
    assert values["trigger_reason"] == "all_b_v1_conditions_met"


def test_b_v1_requires_43_effective_market_days(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection)
        connection.execute("DELETE FROM cb_daily WHERE trade_date = ?", (DATES[0],))
        result = _only_result(connection)
    assert result["data_status"] == "UNAVAILABLE"
    assert result["unavailable_reasons"] == ["insufficient_market_calendar_for_43_days"]


def test_b_v1_same_close_is_not_a_new_high(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection, closes=[100.0] * 43)
        result = _only_result(connection)
    assert result["data_status"] == "AVAILABLE"
    assert not result["conditions"]["close_price_strictly_above_prior_19_high"]
    assert result["values"]["prior_19_high_close_price"] == 100.0


def test_b_v1_uses_strict_and_inclusive_condition_boundaries(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection)
        connection.execute("UPDATE stock_daily_market SET p_close_price=90")
        assert _only_result(connection)["conditions"]["conversion_value_in_90_to_110"]
        connection.execute("UPDATE stock_daily_market SET p_close_price=110")
        assert _only_result(connection)["conditions"]["conversion_value_in_90_to_110"]
        connection.execute("UPDATE stock_daily_market SET p_close_price=89.999")
        assert not _only_result(connection)["conditions"]["conversion_value_in_90_to_110"]
        connection.execute("UPDATE stock_daily_market SET p_close_price=100")
        connection.execute("UPDATE cb_daily SET close_price=105 WHERE trade_date=?", (TRADE_DATE,))
        assert not _only_result(connection)["conditions"]["premium_rate_above_5_pct"]
        connection.execute("UPDATE cb_daily SET close_price=105.001 WHERE trade_date=?", (TRADE_DATE,))
        assert _only_result(connection)["conditions"]["premium_rate_above_5_pct"]
        connection.execute("UPDATE cb_monthly_balance SET balance_amount=799999")
        assert not _only_result(connection)["conditions"]["converted_ratio_at_most_20_pct"]
        connection.execute("UPDATE cb_monthly_balance SET balance_amount=800000")
        connection.execute("UPDATE cb_daily SET volume_lots=100 WHERE trade_date IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", DATES[-10:])
        assert not _only_result(connection)["conditions"]["volume_above_10_day_average"]
        connection.execute("UPDATE cb_daily SET volume_lots=50 WHERE trade_date IN (?, ?, ?, ?, ?)", DATES[-5:])
        assert not _only_result(connection)["conditions"]["five_day_average_volume_above_50_lots"]


def test_b_v1_treats_observed_zero_volume_as_a_valid_day(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection, volumes=[0] + [60] * 41 + [100])
        result = _only_result(connection)
    assert result["data_status"] == "AVAILABLE"
    assert result["values"]["window_43_trade_dates"][0] == DATES[0]


def test_b_v1_uses_historical_balance_and_marks_absent_rows_or_prices_unavailable(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection)
        connection.execute("UPDATE cb_master SET balance_amount=100000, balance_date='2026-09-01'")
        assert _only_result(connection)["values"]["balance_date"] == "2026-06-30"
        connection.execute("DELETE FROM cb_monthly_balance")
        assert _only_result(connection)["unavailable_reasons"] == ["missing_historical_balance"]
        connection.execute("INSERT INTO cb_monthly_balance VALUES (?,?,?,?,?,?)", ("10001", "2026-06", 800000, "test", "u", "x"))
        connection.execute("INSERT INTO cb_daily VALUES (?,?,?,?,?,?,?,?)", (DATES[4], "99999", "Calendar", 1, None, 0, "test", "x"))
        connection.execute("DELETE FROM cb_daily WHERE trade_date = ? AND cb_code = '10001'", (DATES[4],))
        assert _only_result(connection)["unavailable_reasons"] == ["missing_cb_daily_rows"]
        connection.execute("INSERT INTO cb_daily VALUES (?,?,?,?,?,?,?,?)", (DATES[4], "10001", "CB B", None, None, 60, "test", "x"))
        result = _only_result(connection)
    assert result["unavailable_reasons"] == ["missing_cb_close_price"]
    assert result["values"]["missing_close_trade_dates"] == [DATES[4]]


def test_b_v1_persists_only_b_and_does_not_change_a_or_c_history(tmp_path):
    with connect(tmp_path / "b.db") as connection:
        _seed(connection)
        run_a_v1(connection, [TRADE_DATE])
        run_c_v1(connection, [TRADE_DATE])
        totals = run_b_v1(connection, [TRADE_DATE])
        stored = connection.execute(
            "SELECT strategy_code, strategy_version FROM strategy_evaluations ORDER BY evaluation_id"
        ).fetchall()
        signals = connection.execute(
            "SELECT strategy_code FROM strategy_signals ORDER BY strategy_code"
        ).fetchall()
    assert (STRATEGY_CODE, STRATEGY_VERSION) == ("B", "v1")
    assert totals["signals_inserted"] == 1
    assert {row["strategy_code"] for row in stored} == {"A", "B", "C"}
    assert {row["strategy_code"] for row in signals} == {"B", "C"}


def test_b_v1_cli_accepts_single_date_and_history_range():
    assert parse_args(["--date", TRADE_DATE]).date == date.fromisoformat(TRADE_DATE)
    args = parse_args(["--start-date", "2026-06-01", "--end-date", TRADE_DATE])
    assert (args.start_date, args.end_date) == (date(2026, 6, 1), date.fromisoformat(TRADE_DATE))
