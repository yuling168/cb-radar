"""Official daily foreign/China and investment-trust flow collector."""
from datetime import date, datetime, timezone
from pathlib import Path
import requests

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS
from db import active_parent_stock_codes_on, connect, upsert_institutional_daily

TWSE_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
# TPEx replaced its legacy endpoint in 2026. Keep this explicit until its new
# documented machine-readable per-security endpoint is validated.
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"

class InstitutionalSourceError(RuntimeError): pass

def _number(value):
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---"}: raise InstitutionalSourceError("official institutional value is missing")
    try: return int(text)
    except ValueError as exc: raise InstitutionalSourceError(f"invalid official integer: {value!r}") from exc

def parse_twse(payload, trade_date, targets):
    if payload.get("stat") != "OK" or payload.get("date") != trade_date.strftime("%Y%m%d"):
        raise InstitutionalSourceError("TWSE response is not the requested published trade date")
    fields = {name: i for i, name in enumerate(payload.get("fields", []))}
    needed = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)", "投信買進股數", "投信賣出股數", "投信買賣超股數"]
    if any(name not in fields for name in needed): raise InstitutionalSourceError("TWSE required institutional fields changed")
    source_url = f"{TWSE_URL}?date={trade_date:%Y%m%d}&selectType=ALLBUT0999&response=json"
    stamp = datetime.now(timezone.utc).isoformat()
    result = {}
    for row in payload.get("data", []):
        code = str(row[fields["證券代號"]]).strip()
        if code not in targets: continue
        result[code] = {"trade_date": trade_date.isoformat(), "stock_code": code, "stock_name": str(row[fields["證券名稱"]]).strip(), "market": "TWSE", "foreign_buy_shares": _number(row[fields[needed[2]]]), "foreign_sell_shares": _number(row[fields[needed[3]]]), "foreign_net_shares": _number(row[fields[needed[4]]]), "trust_buy_shares": _number(row[fields[needed[5]]]), "trust_sell_shares": _number(row[fields[needed[6]]]), "trust_net_shares": _number(row[fields[needed[7]]]), "source_url": source_url, "collected_at": stamp}
    return result

def _tpex_date(value):
    text = str(value).strip()
    if len(text) != 7 or not text.isdigit():
        raise InstitutionalSourceError(f"TPEx date is invalid: {value!r}")
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:]))

def parse_tpex(payload, trade_date, targets):
    """Parse TPEx OpenAPI daily per-security quantities (all numbers are shares)."""
    if not isinstance(payload, list) or not payload:
        raise InstitutionalSourceError("TPEx response has no daily per-security rows")
    foreign_buy = "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy"
    foreign_sell = "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell"
    foreign_net = "ForeignInvestorsInclude MainlandAreaInvestors-Difference"
    trust_buy = "SecuritiesInvestmentTrustCompanies-TotalBuy"
    trust_sell = "SecuritiesInvestmentTrustCompanies-TotalSell"
    trust_net = "SecuritiesInvestmentTrustCompanies-Difference"
    required = {"Date", "SecuritiesCompanyCode", "CompanyName", foreign_buy, foreign_sell, foreign_net, trust_buy, trust_sell, trust_net}
    if any(not required <= set(row) for row in payload if isinstance(row, dict)):
        raise InstitutionalSourceError("TPEx required institutional fields changed")
    response_dates = {_tpex_date(row["Date"]) for row in payload}
    if response_dates != {trade_date}:
        raise InstitutionalSourceError(f"TPEx response is not requested published date: {sorted(map(str, response_dates))}")
    stamp, result = datetime.now(timezone.utc).isoformat(), {}
    for row in payload:
        code = str(row["SecuritiesCompanyCode"]).strip()
        if code not in targets: continue
        if code in result: raise InstitutionalSourceError(f"TPEx duplicates {code}")
        result[code] = {"trade_date": trade_date.isoformat(), "stock_code": code, "stock_name": str(row["CompanyName"]).strip(), "market": "TPEX", "foreign_buy_shares": _number(row[foreign_buy]), "foreign_sell_shares": _number(row[foreign_sell]), "foreign_net_shares": _number(row[foreign_net]), "trust_buy_shares": _number(row[trust_buy]), "trust_sell_shares": _number(row[trust_sell]), "trust_net_shares": _number(row[trust_net]), "source_url": TPEX_URL, "collected_at": stamp}
    return result

def collect_institutional_daily(trade_date: date, db_path: Path | str = DEFAULT_DB_PATH, session=None):
    with connect(db_path) as con: targets = active_parent_stock_codes_on(con, trade_date.isoformat())
    if not targets: return {"trade_date": trade_date.isoformat(), "target_stocks": 0, "records_inserted": 0, "records_updated": 0}
    http = session or requests.Session()
    response = http.get(TWSE_URL, params={"date": trade_date.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"}, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    twse = parse_twse(response.json(), trade_date, targets)
    # Both full-market reports are validated before one write is allowed.
    tpex_targets = targets - twse.keys()
    tpex = {}
    if tpex_targets:
        response = http.get(TPEX_URL, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        tpex = parse_tpex(response.json(), trade_date, tpex_targets)
    records = {**twse, **tpex}
    missing = targets - records.keys()
    if missing: raise InstitutionalSourceError(f"parent stocks missing from official institutional data: {sorted(missing)}")
    with connect(db_path) as con: inserted, updated = upsert_institutional_daily(con, records.values())
    return {"trade_date": trade_date.isoformat(), "target_stocks": len(targets), "records_inserted": inserted, "records_updated": updated}
