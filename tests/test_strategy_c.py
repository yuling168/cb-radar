from datetime import date

import pytest

from db import connect, upsert_daily, upsert_stock_daily_market
from strategy_c import STRATEGY_CODE, STRATEGY_VERSION, evaluate_c_v1_on, parse_args, run_c_v1
from strategy_engine import run_a_v1


TRADE_DATE = "2026-08-14"


def _seed(connection, rows, *, balance_month="2026-07"):
    daily = []
    stocks = []
    for code, conversion_value, premium, converted_pct in rows:
        stock_code = f"9{code[-3:]}"
        daily.append({"trade_date": TRADE_DATE, "cb_code": code, "cb_name": f"CB {code}",
                      "close_price": round(conversion_value * (1 + premium / 100), 6), "volume_lots": 1,
                      "source": "test", "collected_at": "x"})
        stocks.append({"trade_date": TRADE_DATE, "p_stock_code": stock_code, "p_open_price": conversion_value,
                       "p_high_price": conversion_value, "p_low_price": conversion_value,
                       "p_close_price": conversion_value, "p_volume_shares": 1})
        connection.execute(
            """INSERT INTO cb_master (cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
                issue_amount, source, source_url, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (code, f"CB {code}", stock_code, "母股", "2026-01-01", "2030-01-01", 1000000, "test", "u", "x"))
        connection.execute("INSERT INTO conversion_price_events VALUES (?,?,?,?,?,?)",
                           (code, "2026-01-01", 100, "test", "u", "x"))
        connection.execute("INSERT INTO cb_monthly_balance VALUES (?,?,?,?,?,?)",
                           (code, balance_month, round(1000000 * (1 - converted_pct / 100)), "test", "u", "x"))
    upsert_daily(connection, daily)
    upsert_stock_daily_market(connection, stocks)


def _available(results):
    return {row["cb_code"]: row for row in results if row["data_status"] == "AVAILABLE"}


def test_c_v1_four_bucket_boundaries_and_snapshots(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        _seed(connection, [("10001", 100, 6, 10), ("10002", 105, 6, 10),
                           ("10003", 110, 6, 10), ("10004", 115, 6, 10), ("10005", 120, 6, 10)])
        rows = _available(evaluate_c_v1_on(connection, TRADE_DATE))
    assert [rows[code]["values"]["conversion_value_bucket"] for code in sorted(rows)] == [
        "100-105", "105-110", "110-115", "115-120", "115-120"]
    value = rows["10001"]["values"]
    assert value["balance_date"] == "2026-07-31"
    assert value["converted_ratio_pct"] == pytest.approx(10)


def test_c_v1_keeps_top_two_per_bucket_and_tie_breaks_by_code(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        _seed(connection, [("10003", 101, 10, 10), ("10001", 101, 10, 10),
                           ("10002", 101, 9, 10), ("10004", 101, 8, 10)])
        totals = run_c_v1(connection, [TRADE_DATE])
        signals = connection.execute(
            "SELECT cb_code, condition_values_json FROM strategy_signals WHERE strategy_code = 'C' ORDER BY cb_code"
        ).fetchall()
    assert totals["signals_inserted"] == 2
    assert [row["cb_code"] for row in signals] == ["10001", "10003"]
    assert '"bucket_candidate_count":4' in signals[0]["condition_values_json"]
    assert '"bucket_rank":1' in signals[0]["condition_values_json"]


def test_c_v1_uses_only_historical_balance_and_marks_missing_data(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        _seed(connection, [("10001", 101, 6, 10)])
        connection.execute("UPDATE cb_master SET balance_amount = 100000, balance_date = '2026-09-01'")
        result = _available(evaluate_c_v1_on(connection, TRADE_DATE))["10001"]
        assert result["values"]["balance_date"] == "2026-07-31"
        connection.execute("DELETE FROM cb_monthly_balance")
        assert evaluate_c_v1_on(connection, TRADE_DATE)[0]["unavailable_reasons"] == ["missing_historical_balance"]


def test_c_v1_conditions_and_a_v1_history_are_independent(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        _seed(connection, [("10001", 99.999, 6, 10), ("10002", 101, 5, 10), ("10003", 101, 6, 21)])
        run_a_v1(connection, [TRADE_DATE])
        totals = run_c_v1(connection, [TRADE_DATE])
        conditions = _available(evaluate_c_v1_on(connection, TRADE_DATE))
        codes = {row["cb_code"] for row in connection.execute("SELECT cb_code FROM strategy_evaluations WHERE strategy_code = 'A'")}
    assert totals["matched"] == 0
    assert not conditions["10001"]["conditions"]["conversion_value_in_100_to_120"]
    assert not conditions["10002"]["conditions"]["premium_rate_above_5_pct"]
    assert not conditions["10003"]["conditions"]["converted_ratio_at_most_20_pct"]
    assert codes == {"10001", "10002", "10003"}


def test_c_v1_cli_accepts_single_date_and_history_range():
    assert parse_args(["--date", TRADE_DATE]).date == date(2026, 8, 14)
    args = parse_args(["--start-date", "2026-08-01", "--end-date", "2026-08-31"])
    assert (args.start_date, args.end_date) == (date(2026, 8, 1), date(2026, 8, 31))
