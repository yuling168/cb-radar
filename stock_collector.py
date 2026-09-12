"""Collect official parent-stock daily quotes for the Phase 1 CB trade date."""

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    DEFAULT_DB_PATH,
    HTTP_TIMEOUT_SECONDS,
    TPEX_DAILY_MARKET_URL,
    TWSE_DAILY_MARKET_URL,
)
from db import (
    connect,
    parent_stock_mappings_for_trade_date,
    upsert_stock_daily_coverage,
    upsert_stock_daily_market,
)
from tpex_tls import build_tpex_session


TWSE_REQUIRED_FIELDS = {
    "證券代號",
    "證券名稱",
    "成交股數",
    "開盤價",
    "最高價",
    "最低價",
    "收盤價",
}
TPEX_REQUIRED_FIELDS = {"代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數"}
TWSE_RECOVERY_REFERENCE_URL = "https://www.twse.com.tw/exchangeReport/TWTAUU"
TWSE_RECOVERY_REQUIRED_FIELDS = {
    "恢復買賣日期", "股票代號", "停止買賣前收盤價格", "減資原因", "詳細資料",
}


class StockMarketFormatError(RuntimeError):
    """An official daily market response is unavailable or structurally invalid."""


class ParentStockMappingError(RuntimeError):
    """The requested date has no exact-date official CB parent mapping."""


def _roc_date(value: Any) -> date:
    """Parse TWSE ROC ``YYY/MM/DD`` dates without accepting guessed formats."""
    match = re.fullmatch(r"\s*(\d{3})/(\d{2})/(\d{2})\s*", str(value))
    if not match:
        raise StockMarketFormatError(f"Invalid TWSE ROC date: {value!r}")
    return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))


def _twse_last_trade_date(detail: Any, stock_code: str) -> date:
    """Read the official TWTAUU detail key containing the last tradable date."""
    parts = [part.strip() for part in str(detail).split(",")]
    if len(parts) != 2 or parts[0] != stock_code or not re.fullmatch(r"\d{8}", parts[1]):
        raise StockMarketFormatError("TWSE recovery evidence detail is malformed")
    return date(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:]))


def verify_twse_suspensions(
    session: requests.Session, stock_codes: set[str], trade_date: date,
) -> dict[str, dict[str, object]]:
    """Return only TWSE-confirmed suspension intervals covering ``trade_date``.

    TWTAUU explicitly labels its quoted price as the price *before trading was
    stopped* and gives the recovery trading date.  A target strictly between
    those two official dates is therefore unavailable, not a zero-volume row.
    """
    response = session.get(TWSE_RECOVERY_REFERENCE_URL, params={"response": "json"}, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    try:
        payload = response.json()
        fields = payload["fields"]
        rows = payload["data"]
    except (KeyError, TypeError, ValueError) as exc:
        raise StockMarketFormatError("TWSE recovery evidence response structure changed") from exc
    positions = _field_positions(fields, TWSE_RECOVERY_REQUIRED_FIELDS, "TWSE recovery evidence")
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, list):
            raise StockMarketFormatError("TWSE recovery evidence contains a malformed row")
        try:
            stock_code = str(row[positions["股票代號"]]).strip()
        except IndexError as exc:
            raise StockMarketFormatError("TWSE recovery evidence row has too few columns") from exc
        if stock_code not in stock_codes:
            continue
        recovery_date = _roc_date(row[positions["恢復買賣日期"]])
        last_trade_date = _twse_last_trade_date(row[positions["詳細資料"]], stock_code)
        if last_trade_date >= recovery_date:
            raise StockMarketFormatError("TWSE recovery evidence has an invalid suspension interval")
        if last_trade_date < trade_date < recovery_date:
            result[stock_code] = {
                "source_url": TWSE_RECOVERY_REFERENCE_URL,
                "last_trade_date": last_trade_date.isoformat(),
                "recovery_date": recovery_date.isoformat(),
                "pre_suspension_close": row[positions["停止買賣前收盤價格"]],
                "corporate_action_reason": row[positions["減資原因"]],
            }
    return result


def build_session() -> requests.Session:
    session = build_tpex_session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504, 520),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; cb-radar parent stock collector)",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/mi-pricing.html",
        }
    )
    return session


def _field_name(value: Any) -> str:
    text = re.sub(r"<br\\s*/?>", "", str(value), flags=re.IGNORECASE)
    return re.sub(r"\\s+", "", text).strip()


def _field_positions(fields: Iterable[Any], required: set[str], source: str) -> dict[str, int]:
    positions = {_field_name(field): index for index, field in enumerate(fields)}
    missing = required - positions.keys()
    if missing:
        raise StockMarketFormatError(
            f"{source} required fields changed: missing {sorted(missing)}"
        )
    return positions


def _number(
    value: Any, *, integer: bool = False, allow_missing: bool = False
) -> float | int | None:
    text = str(value).strip().replace(",", "")
    if text == "" or re.fullmatch(r"-+", text):
        if allow_missing:
            return None
        raise StockMarketFormatError(f"Official numeric value is missing: {value!r}")
    try:
        numeric = float(text)
    except ValueError as exc:
        raise StockMarketFormatError(f"Invalid official numeric value: {value!r}") from exc
    if integer:
        if not numeric.is_integer():
            raise StockMarketFormatError(
                f"Official share volume is not an integer: {value!r}"
            )
        return int(numeric)
    return numeric


def _record_from_row(
    row: list[Any], positions: Mapping[str, int], trade_date: date
) -> dict[str, object]:
    try:
        values = {name: row[index] for name, index in positions.items()}
    except IndexError as exc:
        raise StockMarketFormatError("Official market row has too few columns") from exc
    def value_for(*names: str) -> Any:
        for name in names:
            if name in values:
                return values[name]
        raise StockMarketFormatError(f"Official market row is missing {names[0]}")

    code = str(value_for("證券代號", "代號")).strip()
    if not code:
        raise StockMarketFormatError("Official market row is missing security code")
    volume = _number(value_for("成交股數"), integer=True)
    if volume < 0:
        raise StockMarketFormatError(f"Official share volume is missing for {code}")
    return {
        "trade_date": trade_date.isoformat(),
        "p_stock_code": code,
        "p_open_price": _number(value_for("開盤價", "開盤"), allow_missing=True),
        "p_high_price": _number(value_for("最高價", "最高"), allow_missing=True),
        "p_low_price": _number(value_for("最低價", "最低"), allow_missing=True),
        "p_close_price": _number(value_for("收盤價", "收盤"), allow_missing=True),
        "p_volume_shares": volume,
    }


def _select_target_records(
    rows: Iterable[list[Any]], positions: Mapping[str, int], target_codes: set[str], trade_date: date
) -> dict[str, dict[str, object]]:
    code_field = "證券代號" if "證券代號" in positions else "代號"
    selected: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, list):
            raise StockMarketFormatError("Official market response contains a malformed row")
        try:
            code = str(row[positions[code_field]]).strip()
        except IndexError as exc:
            raise StockMarketFormatError("Official market row has too few columns") from exc
        if code not in target_codes:
            continue
        if code in selected:
            raise StockMarketFormatError(f"Official market response duplicates {code}")
        selected[code] = _record_from_row(row, positions, trade_date)
    return selected


def parse_twse_market(payload: Mapping[str, Any], trade_date: date, target_codes: set[str]) -> dict[str, dict[str, object]]:
    if payload.get("stat") != "OK" or payload.get("date") != trade_date.strftime("%Y%m%d"):
        raise StockMarketFormatError("TWSE response is not the requested published trade date")
    try:
        table = payload["tables"][8]
        positions = _field_positions(table["fields"], TWSE_REQUIRED_FIELDS, "TWSE")
        rows = table["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StockMarketFormatError("TWSE response structure changed") from exc
    return _select_target_records(rows, positions, target_codes, trade_date)


def parse_tpex_market(payload: Mapping[str, Any], trade_date: date, target_codes: set[str]) -> dict[str, dict[str, object]]:
    if str(payload.get("stat", "")).lower() != "ok" or payload.get("date") != trade_date.strftime("%Y%m%d"):
        raise StockMarketFormatError("TPEx response is not the requested published trade date")
    try:
        table = payload["tables"][0]
        positions = _field_positions(table["fields"], TPEX_REQUIRED_FIELDS, "TPEx")
        rows = table["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StockMarketFormatError("TPEx response structure changed") from exc
    return _select_target_records(rows, positions, target_codes, trade_date)


def fetch_twse_market(session: requests.Session, trade_date: date) -> Mapping[str, Any]:
    response = session.get(
        TWSE_DAILY_MARKET_URL,
        params={"response": "json", "date": trade_date.strftime("%Y%m%d"), "type": "ALLBUT0999"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise StockMarketFormatError("TWSE response is not JSON") from exc


def fetch_tpex_market(session: requests.Session, trade_date: date) -> Mapping[str, Any]:
    response = session.post(
        TPEX_DAILY_MARKET_URL,
        data={"date": trade_date.strftime("%Y/%m/%d"), "type": "EW", "response": "json"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise StockMarketFormatError("TPEx response is not JSON") from exc


def collect_stock_daily_market(
    trade_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    session: requests.Session | None = None,
    *,
    allow_monthly_verified: bool = False,
    verified_mappings: Mapping[str, Mapping[str, str]] | None = None,
    suspension_verifier=verify_twse_suspensions,
) -> dict[str, object]:
    if verified_mappings is None:
        with connect(db_path) as connection:
            try:
                mappings = parent_stock_mappings_for_trade_date(
                    connection, trade_date.isoformat(),
                    allow_monthly_verified=allow_monthly_verified,
                )
            except ValueError as exc:
                raise ParentStockMappingError(str(exc)) from exc
    else:
        mappings = {str(code): dict(mapping) for code, mapping in verified_mappings.items()}
    if not mappings:
        return {"trade_date": trade_date.isoformat(), "target_stocks": 0, "records_inserted": 0, "records_updated": 0}

    target_codes = {mapping["stock_code"] for mapping in mappings.values()}
    mapping_by_stock = {
        mapping["stock_code"]: mapping for mapping in mappings.values()
    }
    checked_at = datetime.now(timezone.utc).isoformat()

    http = session or build_session()
    try:
        # Both full-market responses are validated before any parent-stock record is written.
        twse_records = parse_twse_market(fetch_twse_market(http, trade_date), trade_date, target_codes)
        tpex_records = parse_tpex_market(fetch_tpex_market(http, trade_date), trade_date, target_codes)
    except (requests.RequestException, StockMarketFormatError) as exc:
        with connect(db_path) as connection:
            upsert_stock_daily_coverage(connection, [
                {
                    "trade_date": trade_date.isoformat(),
                    "stock_code": stock_code,
                    "market": mapping_by_stock[stock_code]["market"],
                    "status": "SOURCE_ERROR",
                    "reason": str(exc),
                    "source_url": None,
                    "response_date": None,
                    "mapping_level": mapping_by_stock[stock_code]["mapping_level"],
                    "mapping_source_url": mapping_by_stock[stock_code]["source_url"],
                    "mapping_year_month": mapping_by_stock[stock_code]["mapping_year_month"],
                    "mapping_verified_at": mapping_by_stock[stock_code]["verified_at"],
                    "availability_status": "SOURCE_ERROR",
                    "availability_evidence_json": None,
                    "checked_at": checked_at,
                }
                for stock_code in sorted(target_codes)
            ])
        raise
    records = {**twse_records, **tpex_records}
    duplicate_codes = set(twse_records) & set(tpex_records)
    if duplicate_codes:
        raise StockMarketFormatError(f"Parent stocks appear in both markets: {sorted(duplicate_codes)}")
    missing_codes = target_codes - records.keys()
    if missing_codes:
        suspended = suspension_verifier(http, missing_codes, trade_date)
        unverified_missing_codes = missing_codes - suspended.keys()
        with connect(db_path) as connection:
            upsert_stock_daily_coverage(connection, [
                {
                    "trade_date": trade_date.isoformat(),
                    "stock_code": stock_code,
                    "market": mapping_by_stock[stock_code]["market"],
                    "status": "MISSING_OFFICIAL_ROW",
                    "reason": "parent_stock_suspended" if stock_code in suspended else "missing_from_official_daily_market",
                    "source_url": suspended.get(stock_code, {}).get("source_url"),
                    "response_date": suspended.get(stock_code, {}).get("recovery_date"),
                    "mapping_level": mapping_by_stock[stock_code]["mapping_level"],
                    "mapping_source_url": mapping_by_stock[stock_code]["source_url"],
                    "mapping_year_month": mapping_by_stock[stock_code]["mapping_year_month"],
                    "mapping_verified_at": mapping_by_stock[stock_code]["verified_at"],
                    "availability_status": "VERIFIED_SUSPENDED" if stock_code in suspended else "UNVERIFIED_MISSING",
                    "availability_evidence_json": json.dumps(
                        suspended[stock_code], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ) if stock_code in suspended else None,
                    "checked_at": checked_at,
                }
                for stock_code in sorted(missing_codes)
            ])
        if unverified_missing_codes:
            raise StockMarketFormatError(
                f"Parent stocks missing from official daily markets: {sorted(unverified_missing_codes)}"
            )

    with connect(db_path) as connection:
        inserted, updated = upsert_stock_daily_market(connection, records.values())
        coverage = []
        for stock_code, record in records.items():
            source_market = "TWSE" if stock_code in twse_records else "TPEX"
            mapped_market = mapping_by_stock[stock_code]["market"]
            market = mapped_market if mapped_market != "UNKNOWN" else source_market
            if record["p_close_price"] is None:
                status, reason = "MISSING_CLOSE", "official_close_price_missing"
            elif record["p_volume_shares"] == 0:
                status, reason = "OFFICIAL_ZERO", "official_zero_volume"
            else:
                status, reason = "COMPLETE", None
            coverage.append({
                "trade_date": trade_date.isoformat(),
                "stock_code": stock_code,
                "market": market,
                "status": status,
                "reason": reason,
                "source_url": (
                    TWSE_DAILY_MARKET_URL if source_market == "TWSE"
                    else TPEX_DAILY_MARKET_URL
                ),
                "response_date": trade_date.isoformat(),
                "mapping_level": mapping_by_stock[stock_code]["mapping_level"],
                "mapping_source_url": mapping_by_stock[stock_code]["source_url"],
                "mapping_year_month": mapping_by_stock[stock_code]["mapping_year_month"],
                "mapping_verified_at": mapping_by_stock[stock_code]["verified_at"],
                "availability_status": "AVAILABLE",
                "availability_evidence_json": None,
                "checked_at": checked_at,
            })
        upsert_stock_daily_coverage(connection, coverage)
    return {
        "trade_date": trade_date.isoformat(),
        "target_stocks": len(target_codes),
        "twse_records": len(twse_records),
        "tpex_records": len(tpex_records),
        "records_inserted": inserted,
        "records_updated": updated,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official parent-stock daily market data")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="Phase 1 trade date (YYYY-MM-DD)")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = collect_stock_daily_market(args.date, args.database)
    except (requests.RequestException, StockMarketFormatError, ParentStockMappingError) as exc:
        print(f"stock_collector_error: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
