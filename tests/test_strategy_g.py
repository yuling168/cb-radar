from datetime import date, timedelta

from db import connect, upsert_daily, upsert_stock_daily_market
from strategy_engine import run_a_v1
from strategy_b import run_b_v1
from strategy_c import run_c_v1
from strategy_g import STRATEGY_CODE, STRATEGY_VERSION, evaluate_g_v1_on, parse_args, run_g_v1


START = date(2026, 5, 1)
DATES = [(START + timedelta(days=index)).isoformat() for index in range(20)]
TRADE_DATE = DATES[-1]


def _seed(connection, *, issue_date="2025-05-20", maturity_date="2027-05-20", put_date="2026-05-20",
          closes=None, volumes=None):
    closes = closes or [100.0] * 19 + [110.0]
    volumes = volumes or [10] * 19 + [31]
    connection.execute(
        """INSERT INTO cb_master (cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date, put_date,
            issue_amount, source, source_url, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("10001", "CB G", "9001", "母股", issue_date, maturity_date, put_date, 1_000_000, "test", "u", "x"),
    )
    connection.execute("INSERT INTO conversion_price_events VALUES (?,?,?,?,?,?)", ("10001", "2025-05-01", 100, "test", "u", "x"))
    connection.execute("INSERT INTO cb_monthly_balance VALUES (?,?,?,?,?,?)", ("10001", "2026-04", 950_000, "test", "u", "x"))
    upsert_daily(connection, [
        {"trade_date": day, "cb_code": "10001", "cb_name": "CB G", "close_price": close,
         "volume_lots": volume, "source": "test", "collected_at": "x"}
        for day, close, volume in zip(DATES, closes, volumes)
    ])
    upsert_stock_daily_market(connection, [{
        "trade_date": day, "p_stock_code": "9001", "p_open_price": 100, "p_high_price": 100,
        "p_low_price": 100, "p_close_price": 100, "p_volume_shares": 1,
    } for day in DATES])


def _result(connection, trade_date=TRADE_DATE):
    return evaluate_g_v1_on(connection, trade_date)[0]


def test_g_v1_combines_all_three_same_day_events_in_one_snapshot(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        result = _result(connection)
    assert result["data_status"] == "AVAILABLE"
    assert result["values"]["trigger_types"] == ["G1", "G2", "G3"]
    assert result["conditions"]["g2_close_strictly_above_prior_19_high"]
    assert result["conditions"]["g2_volume_above_prior_5_average_times_3"]
    assert result["values"]["prior_19_high_close_price"] == 100.0
    assert result["values"]["prior_5_average_volume_lots"] == 10.0
    assert result["values"]["balance_date"] == "2026-04-30"


def test_g_v1_g1_and_g3_only_trigger_on_the_first_effective_trade_day(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        first = _result(connection)
        next_day = "2026-05-21"
        upsert_daily(connection, [{"trade_date": next_day, "cb_code": "10001", "cb_name": "CB G",
                                   "close_price": 110, "volume_lots": 31, "source": "test", "collected_at": "x"}])
        upsert_stock_daily_market(connection, [{"trade_date": next_day, "p_stock_code": "9001", "p_open_price": 100,
                                               "p_high_price": 100, "p_low_price": 100, "p_close_price": 100,
                                               "p_volume_shares": 1}])
        second = _result(connection, next_day)
    assert "G1" in first["values"]["trigger_types"]
    assert "G3" in first["values"]["trigger_types"]
    # The event date is 2026-05-20, so later dates cannot re-trigger G1/G3.
    assert "G1" not in second["values"]["trigger_types"]
    assert "G3" not in second["values"]["trigger_types"]


def test_g_v1_g2_requires_strict_price_high_and_strict_volume_breakout(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection, closes=[100.0] * 20)
        equal_close = _result(connection)
        connection.execute("UPDATE cb_daily SET close_price=110, volume_lots=30 WHERE trade_date=?", (TRADE_DATE,))
        equal_volume = _result(connection)
        connection.execute("UPDATE cb_daily SET volume_lots=31 WHERE trade_date=?", (TRADE_DATE,))
        matched = _result(connection)
    assert not equal_close["conditions"]["g2_close_strictly_above_prior_19_high"]
    assert not equal_volume["conditions"]["g2_volume_above_prior_5_average_times_3"]
    assert "G2" in matched["values"]["trigger_types"]


def test_g_v1_basic_condition_boundaries(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        assert _result(connection)["conditions"]["converted_ratio_below_10_pct"]
        connection.execute("UPDATE cb_monthly_balance SET balance_amount=900000")
        assert not _result(connection)["conditions"]["converted_ratio_below_10_pct"]
        connection.execute("UPDATE cb_monthly_balance SET balance_amount=950000")
        connection.execute("UPDATE stock_daily_market SET p_close_price=89 WHERE trade_date=?", (TRADE_DATE,))
        assert not _result(connection)["conditions"]["conversion_value_at_least_90"]
        connection.execute("UPDATE stock_daily_market SET p_close_price=100 WHERE trade_date=?", (TRADE_DATE,))
        connection.execute("UPDATE cb_daily SET close_price=130 WHERE trade_date=?", (TRADE_DATE,))
        assert _result(connection)["conditions"]["close_price_at_most_130"]
        connection.execute("UPDATE cb_daily SET close_price=130.001 WHERE trade_date=?", (TRADE_DATE,))
        assert not _result(connection)["conditions"]["close_price_at_most_130"]


def test_g_v1_can_trigger_once_when_basics_turn_true_after_a_verified_event_start(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        connection.execute("UPDATE cb_daily SET close_price=130.001 WHERE trade_date=?", (TRADE_DATE,))
        assert _result(connection)["values"]["trigger_types"] == []
        next_day = "2026-05-21"
        upsert_daily(connection, [{"trade_date": next_day, "cb_code": "10001", "cb_name": "CB G",
                                   "close_price": 130, "volume_lots": 31, "source": "test", "collected_at": "x"}])
        upsert_stock_daily_market(connection, [{"trade_date": next_day, "p_stock_code": "9001", "p_open_price": 100,
                                               "p_high_price": 100, "p_low_price": 100, "p_close_price": 100,
                                               "p_volume_shares": 1}])
        transitioned = _result(connection, next_day)
    assert set(transitioned["values"]["trigger_types"]) == {"G1", "G3"}


def test_g_v1_marks_first_historical_day_after_event_as_baseline_unknown(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection, issue_date="2025-01-01", maturity_date="2028-01-01", put_date=None)
        result = _result(connection, DATES[0])
    assert result["data_status"] == "UNAVAILABLE"
    assert result["unavailable_reasons"] == ["baseline_unknown"]
    assert result["values"]["baseline_unknown_trigger_types"] == ["G1"]
    assert result["values"]["trigger_types"] == []


def test_g_v1_uses_zero_volume_prior_days_and_marks_window_data_gaps_unavailable(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection, volumes=[0] * 19 + [1])
        available = _result(connection)
        upsert_daily(connection, [{"trade_date": DATES[5], "cb_code": "99999", "cb_name": "Calendar",
                                   "close_price": 1, "volume_lots": 0, "source": "test", "collected_at": "x"}])
        connection.execute("DELETE FROM cb_daily WHERE cb_code='10001' AND trade_date=?", (DATES[5],))
        unavailable = _result(connection)
    assert available["data_status"] == "AVAILABLE"
    assert available["values"]["prior_5_average_volume_lots"] == 0.0
    assert unavailable["data_status"] == "UNAVAILABLE"
    assert unavailable["unavailable_reasons"] == ["missing_cb_daily_rows"]


def test_g_v1_marks_required_core_history_missing_without_using_future_balance(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        connection.execute("UPDATE cb_master SET balance_amount=100000, balance_date='2026-06-01'")
        assert _result(connection)["values"]["balance_date"] == "2026-04-30"
        connection.execute("DELETE FROM cb_monthly_balance")
        missing_balance = _result(connection)
        connection.execute("UPDATE cb_daily SET close_price=NULL WHERE trade_date=?", (TRADE_DATE,))
        missing_close = _result(connection)
    assert missing_balance["unavailable_reasons"] == ["missing_historical_balance"]
    assert missing_close["unavailable_reasons"] == ["missing_cb_close_price"]


def test_g_v1_persists_only_g_and_keeps_a_b_c_isolated(tmp_path):
    with connect(tmp_path / "g.db") as connection:
        _seed(connection)
        run_a_v1(connection, [TRADE_DATE])
        run_b_v1(connection, [TRADE_DATE])
        run_c_v1(connection, [TRADE_DATE])
        totals = run_g_v1(connection, [TRADE_DATE])
        codes = {row["strategy_code"] for row in connection.execute("SELECT strategy_code FROM strategy_evaluations")}
        signal = connection.execute("SELECT condition_values_json FROM strategy_signals WHERE strategy_code='G'").fetchone()
    assert (STRATEGY_CODE, STRATEGY_VERSION) == ("G", "v1")
    assert totals["signals_inserted"] == 1
    assert codes == {"A", "B", "C", "G"}
    assert '"trigger_types":["G1","G2","G3"]' in signal[0]


def test_g_v1_cli_accepts_one_day_and_an_inclusive_range():
    assert parse_args(["--date", TRADE_DATE]).date == date.fromisoformat(TRADE_DATE)
    args = parse_args(["--start-date", DATES[0], "--end-date", TRADE_DATE])
    assert (args.start_date, args.end_date) == (date.fromisoformat(DATES[0]), date.fromisoformat(TRADE_DATE))
