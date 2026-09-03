from datetime import date
import pytest
from db import active_parent_stock_codes_on, connect, upsert_daily
from institutional_collector import InstitutionalSourceError, parse_tpex, parse_twse
from active_etf_collector import ActiveEtfSourceError, parse_nomura_00980a, save_nomura_00980a

def test_twse_institutional_parser_keeps_share_units():
    payload = {"stat":"OK", "date":"20260903", "fields":["證券代號","證券名稱","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外陸資買賣超股數(不含外資自營商)","投信買進股數","投信賣出股數","投信買賣超股數"], "data":[["1101","台泥","6,815,000","12,749,846","-5,934,846","284,000","0","284,000"]]}
    record = parse_twse(payload, date(2026,9,3), {"1101"})["1101"]
    assert record["foreign_net_shares"] == -5934846
    assert record["trust_buy_shares"] == 284000

def test_tpex_institutional_parser_normalizes_roc_date_and_negative_net():
    payload = [{"Date":"1150903", "SecuritiesCompanyCode":"3131", "CompanyName":"弘塑",
      "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy":"130024",
      "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell":"130600",
      "ForeignInvestorsInclude MainlandAreaInvestors-Difference":"-576",
      "SecuritiesInvestmentTrustCompanies-TotalBuy":"500",
      "SecuritiesInvestmentTrustCompanies-TotalSell":"0",
      "SecuritiesInvestmentTrustCompanies-Difference":"500"}]
    record = parse_tpex(payload, date(2026,9,3), {"3131"})["3131"]
    assert (record["market"], record["foreign_net_shares"], record["trust_net_shares"]) == ("TPEX", -576, 500)

def test_tpex_missing_value_or_wrong_date_fails_without_zero_fill():
    payload = [{"Date":"1150902", "SecuritiesCompanyCode":"3131", "CompanyName":"弘塑",
      "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy":"", "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell":"1",
      "ForeignInvestorsInclude MainlandAreaInvestors-Difference":"-1", "SecuritiesInvestmentTrustCompanies-TotalBuy":"0",
      "SecuritiesInvestmentTrustCompanies-TotalSell":"0", "SecuritiesInvestmentTrustCompanies-Difference":"0"}]
    with pytest.raises(InstitutionalSourceError, match="not requested"):
        parse_tpex(payload, date(2026,9,3), {"3131"})

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

def test_new_schema_is_created_without_touching_daily_data(tmp_path):
    with connect(tmp_path / "db.sqlite") as con:
        upsert_daily(con, [{"trade_date":"2026-09-03","cb_code":"11011","cb_name":"台泥一","close_price":100.0,"volume_lots":1,"source":"test","collected_at":"x"}])
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"institutional_daily", "active_etf_master", "active_etf_holdings"} <= tables
