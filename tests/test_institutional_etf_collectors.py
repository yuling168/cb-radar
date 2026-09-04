from datetime import date
import pytest
import requests
from db import active_parent_stock_codes_on, connect, upsert_daily
from institutional_collector import InstitutionalSourceError, parse_tpex_daily_trade, parse_twse
from active_etf_collector import (
    ActiveEtfSourceError,
    CAPITAL_BUYBACK_URL,
    _post_with_retry,
    collect_capital,
    parse_capital,
    parse_nomura,
    parse_nomura_00980a,
    save_capital,
    save_nomura_00980a,
)


class RetryResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload, self.status_code = payload, status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def json(self):
        return self.payload


class RetrySession:
    def __init__(self, outcomes):
        self.outcomes, self.calls, self.headers = list(outcomes), 0, {}

    def post(self, *_args, **_kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

def test_twse_institutional_parser_keeps_share_units():
    payload = {"stat":"OK", "date":"20260903", "fields":["證券代號","證券名稱","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外陸資買賣超股數(不含外資自營商)","投信買進股數","投信賣出股數","投信買賣超股數"], "data":[["1101","台泥","6,815,000","12,749,846","-5,934,846","284,000","0","284,000"]]}
    record = parse_twse(payload, date(2026,9,3), {"1101"})["1101"]
    assert record["foreign_net_shares"] == -5934846
    assert record["trust_buy_shares"] == 284000

def test_tpex_dailytrade_parser_normalizes_historical_date_and_negative_net():
    payload = {"columnNum":25, "stat":"ok", "date":"20260903", "tables":[{"fields":["代號","名稱"]+["買進股數","賣出股數","買賣超股數"]*7+["三大法人買賣超股數合計"], "data":[["3131","弘塑","130024","130600","-576","0","0","0","130024","130600","-576","500","0","500"]+["0"]*10]}]}
    record = parse_tpex_daily_trade(payload, date(2026,9,3), {"3131"})["3131"]
    assert (record["market"], record["foreign_net_shares"], record["trust_net_shares"]) == ("TPEX", -576, 500)

def test_tpex_missing_value_or_wrong_date_fails_without_zero_fill():
    payload = {"columnNum":25, "stat":"ok", "date":"20260902", "tables":[{"fields":["代號","名稱"]+["買進股數","賣出股數","買賣超股數"]*7+["三大法人買賣超股數合計"], "data":[["3131","弘塑",""]+["0"]*21]}]}
    with pytest.raises(InstitutionalSourceError, match=r"not.*requested.*trade date"):
        parse_tpex_daily_trade(payload, date(2026,9,3), {"3131"})

def test_tpex_dailytrade_rejects_column_and_row_shape_or_non_integer():
    fields = ["代號","名稱"] + ["買進股數","賣出股數","買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
    row = ["3131","弘塑","1","2","-1","0","0","0","0","0","0","500","0","500"] + ["0"] * 10
    base = {"columnNum":25,"stat":"ok","date":"20260903","tables":[{"fields":fields,"data":[row]}]}
    with pytest.raises(InstitutionalSourceError, match="required fields"):
        parse_tpex_daily_trade({**base,"columnNum":24}, date(2026,9,3), {"3131"})
    with pytest.raises(InstitutionalSourceError, match="row length"):
        parse_tpex_daily_trade({**base,"tables":[{"fields":fields,"data":[row[:-1]]}]}, date(2026,9,3), {"3131"})
    row[2] = "1.5"
    with pytest.raises(InstitutionalSourceError, match="invalid official integer"):
        parse_tpex_daily_trade(base, date(2026,9,3), {"3131"})

def test_etf_holding_snapshot_is_raw_shares_and_idempotent(tmp_path):
    rows = parse_nomura_00980a({"etfCode":"00980A", "tradeDate":"2026-09-03", "stocks":[{"code":"2330","name":"台積電","shares":753000}]}, date(2026,9,3))
    with connect(tmp_path / "db.sqlite") as con:
        con.execute("""INSERT INTO cb_master (cb_code,cb_name,stock_code,stock_name,issue_date,maturity_date,issue_amount,source,source_url,collected_at)
                       VALUES ('23301','台積一','2330','台積電','2025-01-01','2030-01-01',100000000,'test','test','x')""")
        assert save_nomura_00980a(con, rows) == (1, 0)
        assert save_nomura_00980a(con, rows) == (0, 1)
        assert con.execute("SELECT holding_shares FROM active_etf_holdings").fetchone()[0] == 753000

def test_active_universe_excludes_delisted_and_filters_etf_rows(tmp_path):
    with connect(tmp_path / "db.sqlite") as con:
        con.executemany("""INSERT INTO cb_master (cb_code,cb_name,stock_code,stock_name,issue_date,maturity_date,issue_amount,delisting_date,source,source_url,collected_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""", [
            ('a','現行','1111','現行股','2025-01-01','2030-01-01',1,None,'t','t','x'),
            ('b','下市','2222','下市股','2025-01-01','2030-01-01',1,'2026-09-03','t','t','x')])
        assert active_parent_stock_codes_on(con, '2026-09-03') == {'1111'}

def test_missing_etf_holdings_never_become_zero():
    with pytest.raises(ActiveEtfSourceError, match="no stock holdings"):
        parse_nomura_00980a({"etfCode":"00980A", "tradeDate":"2026-09-03", "stocks":[]}, date(2026,9,3))

def test_nomura_00985a_uses_same_official_pcf_shape():
    rows = parse_nomura({"etfCode":"00985A", "tradeDate":"2026-09-03", "stocks":[{"code":"2330","name":"台積電","shares":1000}]}, date(2026,9,3), "00985A")
    assert rows[0]["etf_code"] == "00985A"
    assert rows[0]["holding_shares"] == 1000


@pytest.mark.parametrize(("etf_code", "fund_name"), [
    ("00982A", "群益台灣精選強棒主動式ETF基金"),
    ("00992A", "群益科技創新主動式ETF基金"),
])
def test_capital_parses_official_holdings_for_both_etfs(etf_code, fund_name):
    rows = parse_capital({"code": 200, "data": {"pcf": {
        "date1": "2026-09-03", "fundName": fund_name}, "stocks": [{
        "stocNo": "2330", "stocName": "台積電", "share": 1794000.0,
        "shareFormat": "1,794,000"}]}}, date(2026, 9, 3), etf_code)
    assert rows[0]["etf_name"] == fund_name
    assert rows[0]["holding_shares"] == 1794000
    assert rows[0]["source_identifier"] == f"capital-etf-buyback-{etf_code.lower()}"


def test_capital_accepts_requested_historical_date():
    rows = parse_capital({"data": {"pcf": {"date1": "2026-09-02", "fundName": "群益科技創新主動式ETF基金"},
        "stocks": [{"stocNo": "2317", "stocName": "鴻海", "share": 5000}]}},
        date(2026, 9, 2), "00992A")
    assert rows[0]["trade_date"] == "2026-09-02"


def test_capital_rejects_date_mismatch_and_fractional_shares():
    with pytest.raises(ActiveEtfSourceError, match="not the requested"):
        parse_capital({"data": {"pcf": {"date1": "2026-09-02", "fundName": "群益"}, "stocks": [
            {"stocNo": "2330", "stocName": "台積電", "share": 1}]}}, date(2026, 9, 3), "00982A")
    with pytest.raises(ActiveEtfSourceError, match="shares are invalid"):
        parse_capital({"data": {"pcf": {"date1": "2026-09-03", "fundName": "群益"}, "stocks": [
            {"stocNo": "2330", "stocName": "台積電", "share": 1.5}]}}, date(2026, 9, 3), "00982A")


@pytest.mark.parametrize("payload", [
    {"data": {"pcf": {"fundName": "群益"}, "stocks": [{"stocNo": "2330", "stocName": "台積電", "share": 1}]}},
    {"data": {"pcf": {"date1": "2026-09-03", "fundName": "群益"}, "stocks": [{"stocName": "台積電", "share": 1}]}},
    {"data": {"pcf": {"date1": "2026-09-03", "fundName": "群益"}, "stocks": [{"stocNo": "2330", "share": 1}]}},
    {"data": {"pcf": {"date1": "2026-09-03", "fundName": "群益"}, "stocks": [{"stocNo": "2330", "stocName": "台積電"}]}},
])
def test_capital_rejects_missing_required_date_or_holding_fields(payload):
    with pytest.raises(ActiveEtfSourceError):
        parse_capital(payload, date(2026, 9, 3), "00982A")


def test_capital_persists_only_active_cb_parent_holdings(tmp_path):
    holdings = parse_capital({"data": {"pcf": {"date1": "2026-09-03", "fundName": "群益台灣精選強棒主動式ETF基金"}, "stocks": [
        {"stocNo": "2330", "stocName": "台積電", "share": 1000},
        {"stocNo": "9999", "stocName": "非CB母股", "share": 2000}]}}, date(2026, 9, 3), "00982A")
    with connect(tmp_path / "db.sqlite") as con:
        con.execute("""INSERT INTO cb_master (cb_code,cb_name,stock_code,stock_name,issue_date,maturity_date,issue_amount,source,source_url,collected_at)
                       VALUES ('23301','台積一','2330','台積電','2025-01-01','2030-01-01',100000000,'test','test','x')""")
        assert save_capital(con, holdings) == (1, 0)
        assert con.execute("SELECT etf_name FROM active_etf_master WHERE etf_code = '00982A'").fetchone()[0] == "群益台灣精選強棒主動式ETF基金"
        rows = [tuple(row) for row in con.execute(
            "SELECT stock_code, holding_shares FROM active_etf_holdings"
        ).fetchall()]
        assert rows == [("2330", 1000)]

def test_new_schema_is_created_without_touching_daily_data(tmp_path):
    with connect(tmp_path / "db.sqlite") as con:
        upsert_daily(con, [{"trade_date":"2026-09-03","cb_code":"11011","cb_name":"台泥一","close_price":100.0,"volume_lots":1,"source":"test","collected_at":"x"}])
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"institutional_daily", "active_etf_master", "active_etf_holdings"} <= tables


def test_active_etf_retries_timeout_then_succeeds(monkeypatch):
    session = RetrySession([requests.Timeout("slow"), RetryResponse({})])
    delays = []
    monkeypatch.setattr("active_etf_collector.time.sleep", delays.append)
    assert _post_with_retry(session, CAPITAL_BUYBACK_URL, json={}) is session.outcomes[1]
    assert (session.calls, delays) == (2, [0.5])


def test_active_etf_fails_after_three_timeouts(monkeypatch):
    session = RetrySession([requests.Timeout("slow")] * 3)
    delays = []
    monkeypatch.setattr("active_etf_collector.time.sleep", delays.append)
    with pytest.raises(requests.Timeout):
        _post_with_retry(session, CAPITAL_BUYBACK_URL, json={})
    assert (session.calls, delays) == (3, [0.5, 1.0])


def test_active_etf_does_not_retry_http_403(monkeypatch):
    session = RetrySession([RetryResponse(status_code=403)])
    monkeypatch.setattr("active_etf_collector.time.sleep", lambda _: pytest.fail("must not retry 403"))
    with pytest.raises(requests.HTTPError):
        _post_with_retry(session, CAPITAL_BUYBACK_URL, json={})
    assert session.calls == 1


def test_active_etf_does_not_retry_date_mismatch(tmp_path, monkeypatch):
    payload = {"data": {"pcf": {"date1": "2026-09-02", "fundName": "群益"},
                        "stocks": [{"stocNo": "2330", "stocName": "台積電", "share": 1}]}}
    session = RetrySession([RetryResponse(payload)])
    monkeypatch.setattr("active_etf_collector.time.sleep", lambda _: pytest.fail("must not retry date mismatch"))
    with connect(tmp_path / "db.sqlite") as con:
        with pytest.raises(ActiveEtfSourceError, match="not the requested"):
            collect_capital(date(2026, 9, 3), "00992A", con, session)
    assert session.calls == 1
