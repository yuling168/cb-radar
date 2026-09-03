from datetime import date
import requests
from db import connect
from institutional_collector import collect_institutional_daily

DAY = date(2026, 9, 3)
FIELDS = ["代號","名稱"] + ["買進股數","賣出股數","買賣超股數"] * 7 + ["三大法人買賣超股數合計"]

class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload

def seed(db_path, codes):
    with connect(db_path) as con:
        for i, code in enumerate(codes):
            con.execute("""INSERT INTO cb_master (cb_code,cb_name,stock_code,stock_name,issue_date,maturity_date,issue_amount,source,source_url,collected_at)
                VALUES (?,?,?,?,?,'2030-01-01',1,'t','t','x')""", (f"{code}1", "CB", code, f"股{code}", "2024-01-01"))

def twse_payload():
    return {"stat":"OK","date":"20260903","fields":["證券代號","證券名稱","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外陸資買賣超股數(不含外資自營商)","投信買進股數","投信賣出股數","投信買賣超股數"],"data":[]}

def tpex_payload(rows): return {"columnNum":25,"stat":"ok","date":"20260903","tables":[{"fields":FIELDS,"data":rows}]}

def test_per_stock_coverage_writes_tpex_zero_but_not_tib(tmp_path):
    db = tmp_path / "db.sqlite"; seed(db, ["3131","5212","6645"])
    class Session:
        def get(self,*_a,**_k): return Response(twse_payload())
        def post(self,*_a,**_k): return Response(tpex_payload([["3131","弘塑","1","2","-1","0","0","0","0","0","0","3","0","3"] + ["0"] * 10]))
    result = collect_institutional_daily(DAY, db, Session())
    assert result["coverage"] == "incomplete"
    with connect(db) as con:
        data = [tuple(x) for x in con.execute("SELECT stock_code,foreign_buy_shares,trust_net_shares FROM institutional_daily ORDER BY stock_code")]
        status = [tuple(x) for x in con.execute("SELECT stock_code,status,reason FROM institutional_coverage ORDER BY stock_code")]
    assert data == [("3131",1,3),("5212",0,0)]
    assert status[0][1] == "COMPLETE" and status[1][1] == "OFFICIAL_ZERO"
    assert status[2] == ("6645","UNAVAILABLE_MARKET","資料未提供（創新板）")

def test_tpex_error_records_source_error_without_zero(tmp_path):
    db = tmp_path / "db.sqlite"; seed(db, ["3131"])
    class Session:
        def get(self,*_a,**_k): return Response(twse_payload())
        def post(self,*_a,**_k): raise requests.ConnectionError("offline")
    collect_institutional_daily(DAY, db, Session())
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM institutional_daily").fetchone()[0] == 0
        assert tuple(con.execute("SELECT status FROM institutional_coverage").fetchone()) == ("SOURCE_ERROR",)
