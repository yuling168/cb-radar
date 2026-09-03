"""Active ETF raw holdings collector.  Holdings changes are deliberately not trades."""
from datetime import date, datetime, timezone
import requests
from config import HTTP_TIMEOUT_SECONDS
from db import active_parent_stock_codes_on, upsert_active_etf_holdings

NOMURA_PCF_URL = "https://www.nomurafunds.com.tw/ETFWEB/pcf"
NOMURA_SOURCE_ID = "nomura-etfweb-pcf-00980a"

class ActiveEtfSourceError(RuntimeError): pass

def parse_nomura_00980a(payload, trade_date: date):
    """Parse the manager's API-normalised data; require its disclosed as-of date."""
    # Accept a compact test fixture and the manager's real GetFundAssets shape.
    if "Entries" in payload:
        try:
            table = next(t for t in payload["Entries"]["Data"]["Table"] if t["TableTitle"] == "股票")
            payload = {"etfCode": "00980A", "tradeDate": table["NavDate"].replace("/", "-"),
                       "stocks": [{"code": r[0], "name": r[1], "shares": int(r[2])} for r in table["Rows"]]}
        except (KeyError, StopIteration, ValueError, TypeError) as exc:
            raise ActiveEtfSourceError("Nomura PCF response structure changed") from exc
    if payload.get("etfCode") != "00980A" or payload.get("tradeDate") != trade_date.isoformat():
        raise ActiveEtfSourceError("Nomura PCF is not the requested 00980A holding date")
    rows = payload.get("stocks")
    if not isinstance(rows, list) or not rows: raise ActiveEtfSourceError("Nomura PCF has no stock holdings")
    stamp = datetime.now(timezone.utc).isoformat()
    seen, output = set(), []
    for row in rows:
        code, name, shares = str(row.get("code", "")).strip(), str(row.get("name", "")).strip(), row.get("shares")
        if not (code.isdigit() and name): raise ActiveEtfSourceError("Nomura PCF holding identity is incomplete")
        if code in seen or isinstance(shares, bool) or not isinstance(shares, int) or shares < 0: raise ActiveEtfSourceError("Nomura PCF holding shares are invalid")
        seen.add(code); output.append({"trade_date": trade_date.isoformat(), "etf_code": "00980A", "etf_name": "主動野村臺灣優選", "stock_code": code, "stock_name": name, "holding_shares": shares, "source_url": NOMURA_PCF_URL, "source_identifier": NOMURA_SOURCE_ID, "collected_at": stamp})
    return output

def collect_nomura_00980a(trade_date: date, connection, session=None):
    """Download the fund manager's disclosed daily PCF and persist stock rows only."""
    http = session or requests.Session()
    response = http.post("https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets",
                         json={"FundID": "00980A", "SearchDate": trade_date.isoformat()},
                         timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    holdings = parse_nomura_00980a(response.json(), trade_date)
    return save_nomura_00980a(connection, holdings)

def save_nomura_00980a(connection, holdings):
    master = {"etf_code":"00980A", "etf_name":"主動野村臺灣優選", "manager_name":"野村證券投資信託", "source_url":NOMURA_PCF_URL, "source_identifier":NOMURA_SOURCE_ID, "enabled":1, "last_status":"succeeded", "last_error":None, "last_checked_at":datetime.now(timezone.utc).isoformat()}
    rows = list(holdings)
    dates = {row["trade_date"] for row in rows}
    if len(dates) != 1:
        raise ActiveEtfSourceError("one ETF snapshot must contain exactly one trade date")
    active_codes = active_parent_stock_codes_on(connection, dates.pop())
    # A non-CB stock is not a missing holding and must never be represented as 0.
    return upsert_active_etf_holdings(
        connection, master, [row for row in rows if row["stock_code"] in active_codes]
    )
