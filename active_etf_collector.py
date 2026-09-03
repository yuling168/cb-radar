"""Active ETF raw holdings collector.  Holdings changes are deliberately not trades."""
from datetime import date, datetime, timezone
import math
import requests
from config import HTTP_TIMEOUT_SECONDS
from db import active_parent_stock_codes_on, upsert_active_etf_holdings

NOMURA_PCF_URL = "https://www.nomurafunds.com.tw/ETFWEB/pcf"
NOMURA_ASSET_API_URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
NOMURA_ETFS = {
    "00980A": "主動野村臺灣優選",
    "00985A": "主動野村台灣50",
    "00999A": "主動野村臺灣高息",
}
CAPITAL_BUYBACK_URL = "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback"
CAPITAL_ETFS = {
    "00982A": {"fund_id": "399"},
    "00992A": {"fund_id": "500"},
}
TRACKED_ACTIVE_ETF_CODES = ("00980A", "00985A", "00999A", "00982A", "00992A")

class ActiveEtfSourceError(RuntimeError): pass


def active_etf_source_metadata(etf_code: str):
    """Metadata used to retain a clear per-ETF failure status in the pipeline."""
    if etf_code in NOMURA_ETFS:
        return {
            "etf_code": etf_code, "etf_name": NOMURA_ETFS[etf_code],
            "manager_name": "野村證券投資信託", "source_url": NOMURA_PCF_URL,
            "source_identifier": f"nomura-etfweb-pcf-{etf_code.lower()}",
        }
    if etf_code in CAPITAL_ETFS:
        return {
            "etf_code": etf_code,
            "etf_name": "主動群益台灣強棒" if etf_code == "00982A" else "主動群益科技創新",
            "manager_name": "群益證券投資信託", "source_url": CAPITAL_BUYBACK_URL,
            "source_identifier": f"capital-etf-buyback-{etf_code.lower()}",
        }
    raise ActiveEtfSourceError(f"unsupported active ETF: {etf_code}")

def parse_nomura(payload, trade_date: date, etf_code: str):
    """Parse the manager's API-normalised data; require its disclosed as-of date."""
    # Accept a compact test fixture and the manager's real GetFundAssets shape.
    if "Entries" in payload:
        try:
            table = next(t for t in payload["Entries"]["Data"]["Table"] if t["TableTitle"] == "股票")
            payload = {"etfCode": etf_code, "tradeDate": table["NavDate"].replace("/", "-"),
                       "stocks": [{"code": r[0], "name": r[1], "shares": int(r[2])} for r in table["Rows"]]}
        except (KeyError, StopIteration, ValueError, TypeError) as exc:
            raise ActiveEtfSourceError("Nomura PCF response structure changed") from exc
    if etf_code not in NOMURA_ETFS:
        raise ActiveEtfSourceError(f"unsupported Nomura ETF: {etf_code}")
    if payload.get("etfCode") != etf_code or payload.get("tradeDate") != trade_date.isoformat():
        raise ActiveEtfSourceError(f"Nomura PCF is not the requested {etf_code} holding date")
    rows = payload.get("stocks")
    if not isinstance(rows, list) or not rows: raise ActiveEtfSourceError("Nomura PCF has no stock holdings")
    stamp = datetime.now(timezone.utc).isoformat()
    seen, output = set(), []
    for row in rows:
        code, name, shares = str(row.get("code", "")).strip(), str(row.get("name", "")).strip(), row.get("shares")
        if not (code.isdigit() and name): raise ActiveEtfSourceError("Nomura PCF holding identity is incomplete")
        if code in seen or isinstance(shares, bool) or not isinstance(shares, int) or shares < 0: raise ActiveEtfSourceError("Nomura PCF holding shares are invalid")
        seen.add(code); output.append({"trade_date": trade_date.isoformat(), "etf_code": etf_code, "etf_name": NOMURA_ETFS[etf_code], "stock_code": code, "stock_name": name, "holding_shares": shares, "source_url": NOMURA_PCF_URL, "source_identifier": f"nomura-etfweb-pcf-{etf_code.lower()}", "collected_at": stamp})
    return output

def parse_nomura_00980a(payload, trade_date: date):
    return parse_nomura(payload, trade_date, "00980A")

def collect_nomura_00980a(trade_date: date, connection, session=None):
    """Download the fund manager's disclosed daily PCF and persist stock rows only."""
    http = session or requests.Session()
    response = http.post(NOMURA_ASSET_API_URL,
                         json={"FundID": "00980A", "SearchDate": trade_date.isoformat()},
                         timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    holdings = parse_nomura_00980a(response.json(), trade_date)
    return save_nomura_00980a(connection, holdings)

def collect_nomura(trade_date: date, etf_code: str, connection, session=None):
    http = session or requests.Session()
    response = http.post(NOMURA_ASSET_API_URL, json={"FundID": etf_code, "SearchDate": trade_date.isoformat()}, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return save_nomura(connection, parse_nomura(response.json(), trade_date, etf_code))

def parse_capital(payload, trade_date: date, etf_code: str):
    """Parse Capital's official JSON; `shareFormat` is presentation-only."""
    if etf_code not in CAPITAL_ETFS:
        raise ActiveEtfSourceError(f"unsupported Capital ETF: {etf_code}")
    try:
        data = payload["data"]
        response_date = data["pcf"]["date1"]
        fund_name = data["pcf"]["fundName"]
        rows = data["stocks"]
    except (KeyError, TypeError) as exc:
        raise ActiveEtfSourceError("Capital PCF response structure changed") from exc
    if not isinstance(response_date, str) or not isinstance(fund_name, str):
        raise ActiveEtfSourceError("Capital PCF date or fund name is incomplete")
    fund_name = fund_name.strip()
    if response_date != trade_date.isoformat():
        raise ActiveEtfSourceError(
            f"Capital PCF is not the requested {etf_code} holding date: {response_date}"
        )
    if not fund_name:
        raise ActiveEtfSourceError("Capital PCF fund name is incomplete")
    if not isinstance(rows, list) or not rows:
        raise ActiveEtfSourceError("Capital PCF has no stock holdings")
    stamp, seen, output = datetime.now(timezone.utc).isoformat(), set(), []
    for row in rows:
        try:
            code = row["stocNo"]
            name = row["stocName"]
            shares = row["share"]
        except (KeyError, TypeError) as exc:
            raise ActiveEtfSourceError("Capital PCF holding fields are incomplete") from exc
        if not isinstance(code, str) or not isinstance(name, str):
            raise ActiveEtfSourceError("Capital PCF holding identity is incomplete")
        code, name = code.strip(), name.strip()
        if not (code.isdigit() and name):
            raise ActiveEtfSourceError("Capital PCF holding identity is incomplete")
        # The manager currently serializes whole shares as JSON values such as
        # 1794000.0.  Normalize only mathematically integral finite values.
        if isinstance(shares, float):
            if not math.isfinite(shares) or not shares.is_integer():
                raise ActiveEtfSourceError("Capital PCF holding shares are invalid")
            shares = int(shares)
        if code in seen or isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
            raise ActiveEtfSourceError("Capital PCF holding shares are invalid")
        seen.add(code)
        output.append({"trade_date": trade_date.isoformat(), "etf_code": etf_code,
            "etf_name": fund_name, "stock_code": code,
            "stock_name": name, "holding_shares": shares, "source_url": CAPITAL_BUYBACK_URL,
            "source_identifier": f"capital-etf-buyback-{etf_code.lower()}", "collected_at": stamp})
    return output

def collect_capital(trade_date: date, etf_code: str, connection, session=None):
    if etf_code not in CAPITAL_ETFS:
        raise ActiveEtfSourceError(f"unsupported Capital ETF: {etf_code}")
    http = session or requests.Session()
    if hasattr(http, "headers"):
        http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; cb-radar active ETF collector)",
            "Accept": "application/json, text/plain, */*", "Origin": "https://www.capitalfund.com.tw",
            "Referer": f"https://www.capitalfund.com.tw/etf/product/detail/{CAPITAL_ETFS[etf_code]['fund_id']}/portfolio"})
    response = http.post(CAPITAL_BUYBACK_URL,
        json={"fundId": CAPITAL_ETFS[etf_code]["fund_id"], "date": trade_date.isoformat()},
        timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return save_capital(connection, parse_capital(response.json(), trade_date, etf_code))

def save_nomura_00980a(connection, holdings):
    return save_nomura(connection, holdings)

def save_nomura(connection, holdings):
    rows = list(holdings)
    dates = {row["trade_date"] for row in rows}
    if len(dates) != 1:
        raise ActiveEtfSourceError("one ETF snapshot must contain exactly one trade date")
    codes = {row["etf_code"] for row in rows}
    if len(codes) != 1 or codes.pop() not in NOMURA_ETFS:
        raise ActiveEtfSourceError("one snapshot must be for a supported Nomura ETF")
    etf_code = rows[0]["etf_code"]
    master = {"etf_code":etf_code, "etf_name":NOMURA_ETFS[etf_code], "manager_name":"野村證券投資信託", "source_url":NOMURA_PCF_URL, "source_identifier":f"nomura-etfweb-pcf-{etf_code.lower()}", "enabled":1, "last_status":"succeeded", "last_error":None, "last_checked_at":datetime.now(timezone.utc).isoformat()}
    active_codes = active_parent_stock_codes_on(connection, dates.pop())
    # A non-CB stock is not a missing holding and must never be represented as 0.
    return upsert_active_etf_holdings(
        connection, master, [row for row in rows if row["stock_code"] in active_codes]
    )

def save_capital(connection, holdings):
    rows = list(holdings)
    dates, codes = {row["trade_date"] for row in rows}, {row["etf_code"] for row in rows}
    if len(dates) != 1 or len(codes) != 1 or next(iter(codes)) not in CAPITAL_ETFS:
        raise ActiveEtfSourceError("one snapshot must be for one supported Capital ETF and trade date")
    etf_code = next(iter(codes))
    names = {row["etf_name"] for row in rows}
    if len(names) != 1 or not next(iter(names)).strip():
        raise ActiveEtfSourceError("one Capital snapshot must contain one ETF name")
    master = {"etf_code": etf_code, "etf_name": next(iter(names)),
        "manager_name": "群益證券投資信託", "source_url": CAPITAL_BUYBACK_URL,
        "source_identifier": f"capital-etf-buyback-{etf_code.lower()}", "enabled": 1,
        "last_status": "succeeded", "last_error": None,
        "last_checked_at": datetime.now(timezone.utc).isoformat()}
    active_codes = active_parent_stock_codes_on(connection, next(iter(dates)))
    return upsert_active_etf_holdings(
        connection, master, [row for row in rows if row["stock_code"] in active_codes]
    )
