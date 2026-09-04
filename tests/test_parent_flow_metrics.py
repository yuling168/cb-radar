from db import connect
from parent_flow_metrics import recompute_parent_flow_metrics


ETFS = ("00980A", "00985A", "00999A", "00982A", "00992A")


def seed_parent(con, stock="1111", cbs=("11111",)):
    for cb in cbs:
        con.execute("""INSERT INTO cb_master (cb_code,cb_name,stock_code,stock_name,issue_date,maturity_date,issue_amount,source,source_url,collected_at)
                     VALUES (?, 'CB', ?, '母股', '2020-01-01', '2030-01-01', 1, 't', 't', 'x')""", (cb, stock))


def market(con, day, stock="1111", volume=10000, close=10):
    con.execute("INSERT INTO stock_daily_market VALUES (?, ?, NULL, NULL, NULL, ?, ?)", (day, stock, close, volume))


def institution(con, day, net, kind="foreign", stock="1111", status="COMPLETE"):
    con.execute("INSERT INTO institutional_coverage VALUES (?, ?, 'TWSE', ?, NULL, 't', 'x')", (day, stock, status))
    if status in ("COMPLETE", "OFFICIAL_ZERO"):
        foreign, trust = (net, 0) if kind == "foreign" else (0, net)
        con.execute("""INSERT INTO institutional_daily VALUES (?, ?, '母股', 'TWSE', 0, 0, ?, 0, 0, ?, 't', 'x')""", (day, stock, foreign, trust))


def etf_day(con, day, total, stock="1111", complete=True, holdings_by_etf=None):
    for code in ETFS:
        con.execute("INSERT OR IGNORE INTO active_etf_master VALUES (?, ?, 'm', 'u', ?, 1, 'succeeded', NULL, 'x')", (code, code, code))
        con.execute("INSERT INTO active_etf_collection_status VALUES (?, ?, ?, NULL, 'x')", (day, code, "succeeded" if complete else "failed"))
        shares = total // 5 if holdings_by_etf is None else holdings_by_etf.get(code)
        if complete and shares is not None:
            con.execute("INSERT INTO active_etf_holdings VALUES (?, ?, ?, ?, '母股', ?, 'u', ?, 'x')", (day, code, code, stock, shares, code))


def row(con, day, stock="1111"):
    recompute_parent_flow_metrics(con, day)
    return con.execute("SELECT * FROM parent_flow_metrics WHERE trade_date=? AND stock_code=?", (day, stock)).fetchone()


def test_institutional_buy_sell_zero_and_volume_zero(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con)
        for day, net, volume in [("2026-09-01", 1000, 10000), ("2026-09-02", 2000, 10000), ("2026-09-03", 3000, 0)]:
            market(con, day, volume=volume); institution(con, day, net)
        value = row(con, "2026-09-03")
        assert (value["foreign_net_lots"], value["foreign_volume_pct"], value["foreign_streak_days"], value["foreign_streak_lots"]) == (3, None, 3, 6)
        market(con, "2026-09-04"); institution(con, "2026-09-04", -4000)
        value = row(con, "2026-09-04")
        assert (value["foreign_streak_days"], value["foreign_streak_lots"]) == (1, -4)
        market(con, "2026-09-05"); institution(con, "2026-09-05", 0)
        value = row(con, "2026-09-05")
        assert (value["foreign_streak_days"], value["foreign_streak_lots"]) == (0, 0)


def test_institutional_unavailable_and_tib_6645_never_zero_filled(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con, "6645")
        market(con, "2026-09-03", "6645")
        institution(con, "2026-09-03", 0, stock="6645", status="UNAVAILABLE_MARKET")
        value = row(con, "2026-09-03", "6645")
        assert value["foreign_status"] == value["trust_status"] == "UNAVAILABLE"
        assert value["foreign_net_lots"] is None


def test_trust_sell_streak_is_separate_from_foreign(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con)
        for day, net in [("2026-09-01", -1000), ("2026-09-02", -2000)]:
            market(con, day); institution(con, day, net, kind="trust")
        value = row(con, "2026-09-02")
        assert (value["trust_net_lots"], value["trust_streak_days"], value["trust_streak_lots"]) == (-2, 2, -3)


def test_tracked_etf_increase_decrease_zero_and_incomplete_previous_day(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con)
        for day in ("2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"):
            market(con, day)
        etf_day(con, "2026-08-31", 10000); etf_day(con, "2026-09-01", 5000)
        etf_day(con, "2026-09-02", 10000); etf_day(con, "2026-09-03", 15000)
        value = row(con, "2026-09-03")
        assert (value["active_etf_change_lots"], value["active_etf_change_value_twd"], value["active_etf_streak_days"], value["active_etf_streak_lots"]) == (5, 50000, 2, 10)
        con.execute("UPDATE active_etf_holdings SET holding_shares=2000 WHERE trade_date='2026-09-03'")
        value = row(con, "2026-09-03")
        assert (value["active_etf_change_lots"], value["active_etf_change_value_twd"], value["active_etf_streak_days"]) == (0, 0, 0)
        con.execute("UPDATE active_etf_holdings SET holding_shares=600 WHERE trade_date='2026-09-03'")
        value = row(con, "2026-09-03")
        assert (value["active_etf_change_lots"], value["active_etf_change_value_twd"], value["active_etf_streak_days"], value["active_etf_streak_lots"]) == (-7, -70000, 1, -7)
        con.execute("UPDATE active_etf_collection_status SET status='failed' WHERE trade_date='2026-09-02' AND etf_code='00980A'")
        value = row(con, "2026-09-03")
        assert value["active_etf_status"] == "UNAVAILABLE" and value["active_etf_change_lots"] is None


def test_complete_coverage_with_partial_etf_holdings_uses_derived_zeroes(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con); market(con, "2026-09-01"); market(con, "2026-09-02")
        etf_day(con, "2026-09-01", 0, holdings_by_etf={"00980A": 2000})
        etf_day(con, "2026-09-02", 0, holdings_by_etf={"00980A": 3000, "00992A": 1000})
        value = row(con, "2026-09-02")
        assert (value["active_etf_status"], value["active_etf_change_lots"], value["active_etf_change_value_twd"]) == ("AVAILABLE", 2, 20000)


def test_missing_current_etf_row_after_complete_snapshot_means_zero_and_decrease(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con); market(con, "2026-09-01"); market(con, "2026-09-02")
        etf_day(con, "2026-09-01", 0, holdings_by_etf={"00985A": 4000})
        etf_day(con, "2026-09-02", 0, holdings_by_etf={})
        value = row(con, "2026-09-02")
        assert (value["active_etf_status"], value["active_etf_change_lots"], value["active_etf_change_value_twd"]) == ("AVAILABLE", -4, -40000)


def test_one_parent_metric_for_multiple_current_cbs(tmp_path):
    with connect(tmp_path / "x.db") as con:
        seed_parent(con, cbs=("11111", "11112")); market(con, "2026-09-03"); institution(con, "2026-09-03", 1000)
        assert recompute_parent_flow_metrics(con, "2026-09-03") == 1
        assert con.execute("SELECT count(*) FROM parent_flow_metrics").fetchone()[0] == 1
