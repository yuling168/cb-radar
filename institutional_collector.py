"""Official per-parent-stock institutional flow collection with explicit coverage."""
from datetime import date, datetime, timezone
from pathlib import Path
import requests
from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS
from db import active_parent_stock_codes_on, connect, upsert_institutional_coverage, upsert_institutional_daily

TWSE_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
TWSE_UNAVAILABLE_MARKETS = {"6645": "資料未提供（創新板）"}
class InstitutionalSourceError(RuntimeError): pass

def _number(value):
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---"}: raise InstitutionalSourceError("official institutional value is missing")
    try: return int(text)
    except ValueError as exc: raise InstitutionalSourceError(f"invalid official integer: {value!r}") from exc

def _record(code, name, market, day, values, url):
    return {"trade_date":day.isoformat(),"stock_code":code,"stock_name":name,"market":market,
      "foreign_buy_shares":_number(values[0]),"foreign_sell_shares":_number(values[1]),"foreign_net_shares":_number(values[2]),"trust_buy_shares":_number(values[3]),"trust_sell_shares":_number(values[4]),"trust_net_shares":_number(values[5]),"source_url":url,"collected_at":datetime.now(timezone.utc).isoformat()}

def parse_twse(payload, day, targets):
    if payload.get("stat") != "OK" or payload.get("date") != day.strftime("%Y%m%d"): raise InstitutionalSourceError("TWSE response is not the requested published trade date")
    f={x:i for i,x in enumerate(payload.get("fields",[]))}; need=["證券代號","證券名稱","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外陸資買賣超股數(不含外資自營商)","投信買進股數","投信賣出股數","投信買賣超股數"]
    if any(x not in f for x in need): raise InstitutionalSourceError("TWSE required institutional fields changed")
    url=f"{TWSE_URL}?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json"; out={}
    for row in payload.get("data",[]):
        code=str(row[f[need[0]]]).strip()
        if code in targets: out[code]=_record(code,str(row[f[need[1]]]).strip(),"TWSE",day,[row[f[x]] for x in need[2:]],url)
    return out

def parse_tpex_daily_trade(payload, day, targets):
    if not isinstance(payload, dict): raise InstitutionalSourceError("TPEx dailyTrade response is not an object")
    if payload.get("stat") != "ok" or payload.get("date") != day.strftime("%Y%m%d"): raise InstitutionalSourceError("TPEx dailyTrade is not the requested published trade date")
    try: table=payload["tables"][0]; f={str(x).strip():i for i,x in enumerate(table["fields"])}; rows=table["data"]
    except (KeyError,IndexError,TypeError) as exc: raise InstitutionalSourceError("TPEx dailyTrade response structure changed") from exc
    # The official JSON uses grouped table headers, so its leaf labels repeat.
    # Positions 2..4 are foreign/China and 11..13 are investment trust.
    if payload.get("columnNum") != 25 or not isinstance(rows,list) or len(table["fields"]) != 24 or [str(x).strip() for x in table["fields"][:2]] != ["代號", "名稱"]:
        raise InstitutionalSourceError("TPEx dailyTrade required fields changed")
    pos=[0,1,2,3,4,11,12,13]
    out={}
    for row in rows:
        if not isinstance(row, list) or len(row) != 24:
            raise InstitutionalSourceError("TPEx dailyTrade row length changed")
        code=str(row[pos[0]]).strip()
        if code in targets:
            if code in out: raise InstitutionalSourceError(f"TPEx dailyTrade duplicates {code}")
            out[code]=_record(code,str(row[pos[1]]).strip(),"TPEX",day,[row[x] for x in pos[2:]],TPEX_URL)
    return out

def _coverage(day, code, market, status, reason, url):
    return {"trade_date":day.isoformat(),"stock_code":code,"market":market,"status":status,"reason":reason,"source_url":url,"checked_at":datetime.now(timezone.utc).isoformat()}

def collect_institutional_daily(day: date, db_path: Path|str=DEFAULT_DB_PATH, session=None):
    with connect(db_path) as con: targets=active_parent_stock_codes_on(con,day.isoformat())
    if not targets: return {"trade_date":day.isoformat(),"target_stocks":0,"coverage":"complete","records_inserted":0,"records_updated":0}
    http=session or requests.Session(); records={}; coverage=[]
    try:
        r=http.get(TWSE_URL,params={"date":day.strftime("%Y%m%d"),"selectType":"ALLBUT0999","response":"json"},timeout=HTTP_TIMEOUT_SECONDS); r.raise_for_status(); twse=parse_twse(r.json(),day,targets); twse_error=None
    except (requests.RequestException,InstitutionalSourceError,ValueError) as exc: twse={}; twse_error=str(exc)
    for code,row in twse.items(): records[code]=row; coverage.append(_coverage(day,code,"TWSE","COMPLETE",None,row["source_url"]))
    for code in targets & TWSE_UNAVAILABLE_MARKETS.keys(): coverage.append(_coverage(day,code,"TWSE_TIB","UNAVAILABLE_MARKET",TWSE_UNAVAILABLE_MARKETS[code],TWSE_URL))
    remaining=targets-set(twse)-set(TWSE_UNAVAILABLE_MARKETS)
    try:
        r=http.post(TPEX_URL,data={"type":"Daily","cate":"EW","date":day.strftime("%Y/%m/%d"),"response":"json"},timeout=HTTP_TIMEOUT_SECONDS); r.raise_for_status(); tpex=parse_tpex_daily_trade(r.json(),day,remaining); tpex_error=None
    except (requests.RequestException,InstitutionalSourceError,ValueError) as exc: tpex={}; tpex_error=str(exc)
    for code in remaining:
        if code in tpex: records[code]=tpex[code]; coverage.append(_coverage(day,code,"TPEX","COMPLETE",None,TPEX_URL))
        elif tpex_error or twse_error: coverage.append(_coverage(day,code,"TPEX","SOURCE_ERROR",tpex_error or twse_error,TPEX_URL))
        else:
            records[code]=_record(code,code,"TPEX",day,[0]*6,TPEX_URL); coverage.append(_coverage(day,code,"TPEX","OFFICIAL_ZERO","not listed in successful complete TPEx dailyTrade report",TPEX_URL))
    with connect(db_path) as con:
        inserted,updated=upsert_institutional_daily(con,records.values()); upsert_institutional_coverage(con,coverage)
    incomplete=any(x["status"] in {"UNAVAILABLE_MARKET","SOURCE_ERROR"} for x in coverage)
    return {"trade_date":day.isoformat(),"target_stocks":len(targets),"coverage":"incomplete" if incomplete else "complete","records_inserted":inserted,"records_updated":updated}
