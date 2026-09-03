"""Collect official TPEx/MOPS convertible-bond master data into SQLite."""

from __future__ import annotations

import argparse
import calendar
import csv
from dataclasses import dataclass
from decimal import Decimal
import html
from io import BytesIO, StringIO
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    DEFAULT_DB_PATH,
    HTTP_TIMEOUT_SECONDS,
    MOPS_BASE_URL,
    MOPS_CB_ANNOUNCEMENT_URL,
    TPEX_CB_DELISTED_URL,
    TPEX_CB_ISSUE_URL,
    TPEX_CB_LISTED_URL,
    TDCC_BOOK_ENTRY_URL,
)
from db import connect, upsert_master_data


TPEX_REQUIRED_FIELDS = {
    "IssuerCode",
    "IssuerName",
    "BondCode",
    "BondType",
    "SeriesNumber",
    "IssueDate",
    "MaturityDate",
    "IssueAmount",
    "ShortName",
    "ListingStatus",
    "PutOptionDate",
    "Guaranteed",
    "GuaranteeDescription",
    "Currency",
    "Conversion/ExchangePriceAtIssuance",
}
TPEX_LIST_FIELDS = ["發行機構代碼", "發行機構名稱", "債券名稱", "掛牌日期", "發行資料"]
TPEX_DELISTED_FIELDS = ["代碼", "簡稱", "下櫃日期"]
MASTER_SOURCE = "TPEx:bond_ISSBD5_data+MOPS:t120sg01+t108sb08_1+TDCC:bookEntry"
BOOTSTRAP_SOURCE = "TPEx:bond_ISSBD5_data+MOPS:t120sg01"
MOPS_SOURCE = "MOPS:t120sg01"
MOPS_ANNOUNCEMENT_SOURCE = "MOPS:t108sb08_1"
TPEX_ISSUE_SOURCE = "TPEx:bond_ISSBD5_data"
MOPS_RULES_SOURCE = "MOPS:official_conversion_terms"


class MasterFormatError(RuntimeError):
    """An official response no longer has the required verified structure."""


class MopsNoDataError(MasterFormatError):
    """MOPS explicitly reports that a requested historical month has no row."""


class ModuleDependencyError(MasterFormatError):
    """A Phase 2 module cannot run because its verified inputs are unavailable."""


@dataclass
class RequestCounts:
    tpex: int = 0
    tdcc: int = 0
    mops_detail: int = 0
    mops_announcement: int = 0


@dataclass
class ExistingMasterState:
    masters: dict[str, dict[str, object]]
    monthly_year_months: dict[str, set[str]]
    events: dict[str, list[dict[str, object]]]


def _compact_html(content: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", content)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _required_match(text: str, pattern: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise MasterFormatError(f"MOPS required field missing: {label}")
    return match.group(1).strip()


def _parse_yyyymmdd(value: str, label: str) -> str:
    if not re.fullmatch(r"\d{8}", value):
        raise MasterFormatError(f"Invalid {label}: {value!r}")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
    except ValueError as exc:
        raise MasterFormatError(f"Invalid {label}: {value!r}") from exc


def _parse_roc_date(value: str, label: str) -> str:
    match = re.fullmatch(r"(\d{2,3})/(\d{2})/(\d{2})", value.strip())
    if not match:
        raise MasterFormatError(f"Invalid {label}: {value!r}")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year + 1911, month, day).isoformat()
    except ValueError as exc:
        raise MasterFormatError(f"Invalid {label}: {value!r}") from exc


def _positive_int(value: str, label: str, *, allow_zero: bool = False) -> int:
    text = value.strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise MasterFormatError(f"Invalid {label}: {value!r}")
    number = int(text)
    if number < 0 or (number == 0 and not allow_zero):
        raise MasterFormatError(f"Invalid {label}: {value!r}")
    return number


def _positive_float(value: str, label: str) -> float:
    try:
        number = float(value.strip().replace(",", ""))
    except ValueError as exc:
        raise MasterFormatError(f"Invalid {label}: {value!r}") from exc
    if number <= 0:
        raise MasterFormatError(f"Invalid {label}: {value!r}")
    return number


def parse_tpex_issues(payload: object) -> dict[str, dict[str, object]]:
    if not isinstance(payload, list) or not payload:
        raise MasterFormatError("TPEx issue endpoint returned no rows")
    parsed: dict[str, dict[str, object]] = {}
    for raw in payload:
        if not isinstance(raw, dict) or not TPEX_REQUIRED_FIELDS.issubset(raw):
            raise MasterFormatError("TPEx issue endpoint required fields changed")
        if raw["BondType"] != "5" or raw["ListingStatus"] != "2":
            continue
        if raw["Currency"] != "1":
            continue
        cb_code = str(raw["BondCode"]).strip()
        if not cb_code:
            raise MasterFormatError("A listed TPEx CB is missing BondCode")
        put_raw = str(raw["PutOptionDate"]).strip()
        guaranteed = str(raw["Guaranteed"]).strip()
        guarantee_description = str(raw["GuaranteeDescription"]).strip()
        if guaranteed == "1":
            is_secured = 1
            if not guarantee_description:
                raise MasterFormatError(
                    f"TPEx secured CB {cb_code} has no GuaranteeDescription"
                )
        elif guaranteed == "2":
            is_secured = 0
            if guarantee_description:
                raise MasterFormatError(
                    f"TPEx unsecured CB {cb_code} unexpectedly has a guarantor"
                )
        else:
            is_secured = None
        parsed[cb_code] = {
            "cb_code": cb_code,
            "cb_name": str(raw["ShortName"]).strip(),
            "stock_code": str(raw["IssuerCode"]).strip(),
            "stock_name": str(raw["IssuerName"]).strip(),
            "issue_date": _parse_yyyymmdd(str(raw["IssueDate"]), "IssueDate"),
            "maturity_date": _parse_yyyymmdd(str(raw["MaturityDate"]), "MaturityDate"),
            "put_date": _parse_yyyymmdd(put_raw, "PutOptionDate") if put_raw else None,
            "issue_amount": _positive_int(str(raw["IssueAmount"]), "IssueAmount"),
            "tpex_reported_issue_amount": _positive_int(
                str(raw["IssueAmount"]), "IssueAmount"
            ),
            "is_secured": is_secured,
            "issue_conversion_price": _positive_float(
                str(raw["Conversion/ExchangePriceAtIssuance"]),
                "Conversion/ExchangePriceAtIssuance",
            ),
            "series_number": _positive_int(
                str(raw["SeriesNumber"]), "SeriesNumber"
            ),
        }
        if not parsed[cb_code]["cb_name"] or not parsed[cb_code]["stock_code"] or not parsed[cb_code]["stock_name"]:
            raise MasterFormatError(f"TPEx listed CB {cb_code} has blank identity fields")
    if not parsed:
        raise MasterFormatError("TPEx issue endpoint contained no active TWD CBs")
    return parsed


def parse_tpex_mops_links(payload: object) -> dict[str, str]:
    try:
        table = payload["tables"][0]  # type: ignore[index]
        fields = table["fields"]
        rows = table["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MasterFormatError("TPEx listed-CB response structure changed") from exc
    if not isinstance(payload, dict) or payload.get("stat") != "ok" or fields != TPEX_LIST_FIELDS:
        raise MasterFormatError("TPEx listed-CB required fields changed")
    links: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise MasterFormatError("TPEx listed-CB response contains a malformed row")
        url = str(row[4]).strip()
        query = parse_qs(urlparse(url).query)
        code = query.get("bond_id", [""])[0]
        if code and url:
            links[code] = url
    if not links:
        raise MasterFormatError("TPEx listed-CB response contained no MOPS links")
    return links


def parse_tpex_delistings(payload: object) -> dict[str, dict[str, str]]:
    """Parse TPEx's official recent delisting list without inferring a reason."""
    try:
        table = payload["tables"][0]  # type: ignore[index]
        fields = table["fields"]
        rows = table["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MasterFormatError("TPEx delisted-CB response structure changed") from exc
    if not isinstance(payload, dict) or payload.get("stat") != "ok" or fields != TPEX_DELISTED_FIELDS:
        raise MasterFormatError("TPEx delisted-CB required fields changed")
    parsed: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise MasterFormatError("TPEx delisted-CB response contains a malformed row")
        cb_code = str(row[0]).strip()
        if not cb_code:
            raise MasterFormatError("TPEx delisted-CB row is missing a code")
        parsed[cb_code] = {
            "cb_code": cb_code,
            "cb_name": str(row[1]).strip(),
            "delisting_date": _parse_roc_date(str(row[2]), "TPEx delisting date"),
        }
    return parsed


def is_active_on(
    issue_date: str, delisting_date: str | None, run_date: date
) -> bool:
    """Return lifecycle activity on run_date; a future delisting remains active."""
    today = run_date.isoformat()
    return issue_date <= today and (delisting_date is None or delisting_date > today)


def _url_reporting_month(url: str) -> str:
    value = parse_qs(urlparse(url).query).get("monyr_reg", [""])[0]
    if not re.fullmatch(r"\d{6}", value):
        raise MasterFormatError(f"MOPS URL has no valid reporting month: {url}")
    return f"{value[:4]}-{value[4:]}"


def month_end_date(year_month: str) -> str:
    """Return the official calendar month-end for a normalized reporting month."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", year_month)
    if not match:
        raise MasterFormatError(f"Invalid reporting month: {year_month!r}")
    year, month = (int(part) for part in match.groups())
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def is_complete_reporting_month(year_month: str, as_of: date) -> bool:
    """Return whether a MOPS reporting month ended before the collection date."""
    return month_end_date(year_month) < as_of.isoformat()


def parse_tdcc_book_entries(content: str) -> dict[str, dict[str, object]]:
    """Parse TDCC's current book-entry CSV for all convertible bonds."""
    reader = csv.DictReader(StringIO(content.lstrip("\ufeff")))
    required_fields = {
        "資料日期", "證券代號", "證券名稱", "市場別", "證券種類", "登錄數額",
    }
    if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
        raise MasterFormatError("TDCC book-entry endpoint required fields changed")

    parsed: dict[str, dict[str, object]] = {}
    for row in reader:
        if not isinstance(row, dict):
            raise MasterFormatError("TDCC book-entry endpoint contains a malformed row")
        security_type = str(row["證券種類"] or "").strip()
        if not security_type.startswith("可轉債"):
            continue
        cb_code = str(row["證券代號"] or "").strip()
        if not cb_code:
            raise MasterFormatError("TDCC convertible-bond row is missing a code")
        if cb_code in parsed:
            raise MasterFormatError(f"TDCC book-entry response has duplicate CB code: {cb_code}")
        units = _positive_int(
            str(row["登錄數額"] or ""), "TDCC 登錄數額", allow_zero=True
        )
        parsed[cb_code] = {
            "cb_code": cb_code,
            "cb_name": str(row["證券名稱"] or "").strip(),
            "balance_amount": units * 100_000,
            "balance_date": _parse_yyyymmdd(
                str(row["資料日期"] or ""), "TDCC 資料日期"
            ),
        }
    if not parsed:
        raise MasterFormatError("TDCC book-entry endpoint contained no convertible bonds")
    return parsed


def select_current_balance(
    tdcc_balance: dict[str, object],
    as_of: date,
) -> tuple[int, str]:
    """Return a TDCC book-entry balance and its verified as-of date."""
    balance_amount = int(tdcc_balance["balance_amount"])
    balance_date = str(tdcc_balance["balance_date"])
    if balance_date > as_of.isoformat():
        raise MasterFormatError(
            f"TDCC balance date for {tdcc_balance['cb_code']} is after run date: "
            f"{balance_date} > {as_of.isoformat()}"
        )
    return balance_amount, balance_date


def _url_for_month(url: str, year_month: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["monyr_reg"] = [year_month.replace("-", "")]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _fallback_mops_url(master: dict[str, object], year_month: str) -> str:
    """Build the official MOPS detail URL when TPEx omits its convSearch link."""
    parameters = {
        "bond_id": str(master["cb_code"]),
        "issuer_stock_code": str(master["stock_code"]),
        "bond_kind": "5",
        "bond_yrn": str(master["series_number"]),
        "bond_subn": f"$M{int(master['series_number']):08d}",
        "monyr_reg": year_month.replace("-", ""),
        "step": "0",
        "firstin": "ture",
    }
    return f"{MOPS_BASE_URL}/mops/web/t120sg01?{urlencode(parameters)}"


def _previous_month(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    if parsed.month == 1:
        return f"{parsed.year - 1:04d}-12"
    return f"{parsed.year:04d}-{parsed.month - 1:02d}"


def _month_before(year_month: str) -> str:
    return _previous_month(f"{year_month}-01")


def parse_mops_snapshot(content: str, source_url: str) -> dict[str, object]:
    text = _compact_html(content)
    if "之轉(交)換公司債發行資料" not in text:
        raise MasterFormatError("MOPS response is not a CB issue detail page")
    official_name = _required_match(
        text, r"債券中文名稱：\s*(.*?)\s*發行人：", "債券中文名稱"
    )
    if "交換公司債" in official_name:
        instrument_kind = "exchangeable"
    elif "轉換公司債" in official_name:
        instrument_kind = "convertible"
    else:
        raise MasterFormatError(
            f"MOPS official bond name cannot identify CB kind: {official_name!r}"
        )
    issue_date = _parse_roc_date(
        _required_match(text, r"發行日期：\s*(\d{2,3}/\d{2}/\d{2})", "發行日期"),
        "MOPS issue date",
    )
    maturity_date = _parse_roc_date(
        _required_match(text, r"到期日期：\s*(\d{2,3}/\d{2}/\d{2})", "到期日期"),
        "MOPS maturity date",
    )
    application_issue_amount = _positive_int(
        _required_match(text, r"申請發行總額：\s*([0-9,]+)元", "申請發行總額"),
        "MOPS issue face amount",
    )
    actual_issue_amount = _positive_int(
        _required_match(text, r"實際發行總額：\s*([0-9,]+)元", "實際發行總額"),
        "MOPS actual proceeds",
    )
    par_value = _positive_int(
        _required_match(text, r"發行面額：\s*([0-9,]+)元", "發行面額"),
        "MOPS par value",
    )
    reported_issue_units = _positive_int(
        _required_match(text, r"發行張數：\s*([0-9,]+)張", "發行張數"),
        "MOPS reported issue units",
    )
    balance = _positive_int(
        _required_match(text, r"本月底發行餘額：\s*([0-9,]+)元", "本月底發行餘額"),
        "MOPS balance",
        allow_zero=True,
    )
    if application_issue_amount % par_value != 0 or balance % par_value != 0:
        raise MasterFormatError("MOPS amounts are not whole official par-value units")
    issue_amount = application_issue_amount
    actual_issue_units = (
        actual_issue_amount // par_value
        if actual_issue_amount % par_value == 0
        else None
    )
    if (
        actual_issue_units is not None
        and reported_issue_units == actual_issue_units
        and actual_issue_amount != application_issue_amount
    ):
        # A partially issued CB can retain its authorized application amount
        # while MOPS's issued-unit count and TPEx both identify the actual face
        # principal. Use that verified issued principal, never a guessed value.
        issue_amount = actual_issue_amount
    issue_units = issue_amount // par_value
    balance_units = balance // par_value
    if reported_issue_units not in {issue_units, balance_units}:
        raise MasterFormatError(
            "MOPS reported issue units match neither original issue units nor balance units"
        )
    price = _positive_float(
        _required_match(text, r"最新轉\(交\)換價格：\s*([0-9,.]+)元", "最新轉換價格"),
        "MOPS latest conversion price",
    )
    effective = _parse_roc_date(
        _required_match(
            text,
            r"最近轉\(交\)換價格生效日期：\s*(\d{2,3}/\d{2}/\d{2})",
            "最近轉換價格生效日期",
        ),
        "MOPS conversion price effective date",
    )
    rules_match = re.search(
        r"href=['\"]([^'\"]*/nas/t56/t56bondb/[^'\"]+\.pdf)['\"]",
        content,
        re.IGNORECASE,
    )
    return {
        "year_month": _url_reporting_month(source_url),
        "official_name": official_name,
        "instrument_kind": instrument_kind,
        "issue_date": issue_date,
        "maturity_date": maturity_date,
        "application_issue_amount": application_issue_amount,
        "issue_amount": issue_amount,
        "actual_issue_amount": actual_issue_amount,
        "issue_units": issue_units,
        "par_value": par_value,
        "balance_amount": balance,
        "conversion_price": price,
        "effective_date": effective,
        "source_url": source_url,
        "rules_url": urljoin(source_url, rules_match.group(1)) if rules_match else None,
    }


def parse_mops_rules_conversion_events(
    pdf_content: bytes,
    cb_code: str,
    collected_at: str,
    source_url: str,
) -> list[dict[str, object]]:
    try:
        reader = PdfReader(BytesIO(pdf_content))
        text = " ".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise MasterFormatError("MOPS conversion terms PDF cannot be parsed") from exc
    text = re.sub(r"\s+", "", text)
    initial_roc = re.search(
        r"(\d{2,3})年(\d{1,2})月(\d{1,2})日為轉換價格訂定基準日"
        r".*?轉換價格為每股新[台臺]幣([0-9,.]+)元",
        text,
    )
    initial_ad = re.search(
        r"(?:西元)?(20\d{2})年(\d{1,2})月(\d{1,2})日為轉換價格訂定基準日"
        r".*?轉換價格為每股新[台臺]幣([0-9,.]+)元",
        text,
    )
    adjustment = re.search(
        r"轉換價格由每股新[台臺]幣([0-9,.]+)元調整為"
        r"每股新[台臺]幣([0-9,.]+)元，並於.*?"
        r"(\d{2,3})年(\d{1,2})月(\d{1,2})日進行轉換價格調整",
        text,
    )
    initial = initial_ad or initial_roc
    if not initial:
        raise MasterFormatError(
            f"MOPS conversion terms do not explicitly state initial price for {cb_code}"
        )
    initial_year, initial_month, initial_day, initial_price = initial.groups()
    if len(initial_year) == 4:
        initial_effective = date(
            int(initial_year), int(initial_month), int(initial_day)
        ).isoformat()
    else:
        initial_effective = _parse_roc_date(
            f"{initial_year}/{int(initial_month):02d}/{int(initial_day):02d}",
            "MOPS pricing basis date",
        )
    events = [{
        "cb_code": cb_code,
        "effective_date": initial_effective,
        "conversion_price": _positive_float(initial_price, "MOPS terms initial price"),
        "source": MOPS_RULES_SOURCE,
        "source_url": source_url,
        "collected_at": collected_at,
    }]
    if adjustment:
        _old_price, new_price, year, month, day = adjustment.groups()
        events.append({
            "cb_code": cb_code,
            "effective_date": _parse_roc_date(
                f"{year}/{int(month):02d}/{int(day):02d}",
                "MOPS terms adjustment date",
            ),
            "conversion_price": _positive_float(new_price, "MOPS terms adjusted price"),
            "source": MOPS_RULES_SOURCE,
            "source_url": source_url,
            "collected_at": collected_at,
        })
    return events


def parse_mops_conversion_announcements(
    content: str, cb_code: str, collected_at: str, source_url: str
) -> list[dict[str, object]]:
    text = _compact_html(content)
    if "轉換公司債轉換價格變更公告" not in text:
        raise MasterFormatError("MOPS CB announcement response structure changed")
    pattern = re.compile(
        rf"(\d{{2,3}}/\d{{2}}/\d{{2}})\s+(\d+)\s+"
        rf"公告[^。]{{0,500}}?代碼[：:]\s*{re.escape(cb_code)}[)）][^。]{{0,100}}?"
        r"自(\d{2,3})年(\d{2})月(\d{2})日起，?\s*"
        r"轉換價格自([0-9,.]+)元調整為([0-9,.]+)元[。.]"
    )
    events: dict[str, dict[str, object]] = {}
    event_orders: dict[str, tuple[str, int]] = {}
    for match in pattern.finditer(text):
        filed_raw, sequence, year, month, day, _old_price, new_price = match.groups()
        filed_date = _parse_roc_date(filed_raw, "MOPS announcement filing date")
        filing_order = (filed_date, int(sequence))
        effective = _parse_roc_date(
            f"{year}/{month}/{day}", "MOPS announcement effective date"
        )
        price = _positive_float(new_price, "MOPS announcement conversion price")
        existing = events.get(effective)
        if existing and existing["conversion_price"] != price:
            if filing_order <= event_orders[effective]:
                if filing_order == event_orders[effective]:
                    raise MasterFormatError(
                        f"Conflicting MOPS announcements for {cb_code} on {effective}"
                    )
                continue
        events[effective] = {
            "cb_code": cb_code,
            "effective_date": effective,
            "conversion_price": price,
            "source": MOPS_ANNOUNCEMENT_SOURCE,
            "source_url": source_url,
            "collected_at": collected_at,
        }
        event_orders[effective] = filing_order
    return list(events.values())


def is_forced_redemption_announcement(content: str, cb_code: str) -> bool:
    """Require both the exact CB code and an exercised redemption right."""
    return (
        "行使債券贖回權" in content
        and re.search(rf"(?<!\d){re.escape(cb_code)}(?!\d)", content) is not None
    )


def parse_forced_redemption_date(body: str, cb_code: str) -> str:
    """Read one contractual redemption baseline date from an official body."""
    matches = re.findall(
        r"轉換公司債收回基準日\s*[：:]\s*(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日",
        body,
    )
    dates = {
        _parse_roc_date(f"{year}/{int(month):02d}/{int(day):02d}", "redemption date")
        for year, month, day in matches
    }
    if not dates:
        raise MasterFormatError(f"Forced-redemption date missing for {cb_code}")
    if len(dates) != 1:
        raise MasterFormatError(f"Conflicting forced-redemption dates for {cb_code}")
    return dates.pop()


def latest_effective_event(
    events: list[dict[str, object]], as_of: date
) -> dict[str, object]:
    effective = [
        event
        for event in events
        if str(event["effective_date"]) <= as_of.isoformat()
    ]
    if not effective:
        raise MasterFormatError(f"No effective conversion price on {as_of}")
    return max(effective, key=lambda row: str(row["effective_date"]))


def balance_units_for_display(
    issue_amount: int, issue_units: int, balance_amount: int
) -> int:
    """Convert an official balance amount using this bond's verified par value."""
    if issue_amount <= 0 or issue_units <= 0 or issue_amount % issue_units != 0:
        raise MasterFormatError("Official issue amount/units cannot define a par value")
    par_value = issue_amount // issue_units
    if balance_amount < 0 or balance_amount % par_value != 0:
        raise MasterFormatError("Official balance amount is not a whole bond unit")
    return balance_amount // par_value


def issue_amount_yi_for_display(issue_amount: int) -> str:
    if issue_amount <= 0:
        raise MasterFormatError("Official issue amount must be positive")
    value = Decimal(issue_amount) / Decimal(100_000_000)
    return format(value.normalize(), "f")


def secured_for_display(is_secured: int | None) -> str:
    if is_secured == 1:
        return "有"
    if is_secured == 0:
        return "無"
    if is_secured is None:
        return "未知"
    raise MasterFormatError(f"Invalid is_secured value: {is_secured!r}")


def _get_json(
    session: requests.Session, url: str, request_counts: RequestCounts | None = None
) -> object:
    if request_counts is not None:
        request_counts.tpex += 1
    response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    try:
        return json.loads(response.content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MasterFormatError(f"Official endpoint returned invalid JSON: {url}") from exc


def _get_tdcc_book_entries(
    session: requests.Session, request_counts: RequestCounts | None = None
) -> dict[str, dict[str, object]]:
    if request_counts is not None:
        request_counts.tdcc += 1
    response = session.get(TDCC_BOOK_ENTRY_URL, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    try:
        content = response.content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MasterFormatError("TDCC book-entry endpoint returned non-UTF-8 CSV") from exc
    return parse_tdcc_book_entries(content)


def sync_tdcc_balances(
    db_path: Path | str,
    balances: dict[str, dict[str, object]],
    as_of: date,
    collected_at: str,
) -> tuple[int, list[str]]:
    """Atomically update only TDCC-verified latest balances for known CBs."""
    rows = [
        (int(row["balance_amount"]), str(row["balance_date"]), collected_at, code)
        for code, row in balances.items()
        if str(row["balance_date"]) <= as_of.isoformat()
    ]
    if len(rows) != len(balances):
        raise MasterFormatError("TDCC book-entry balance date is after run date")
    warnings: list[str] = []
    with connect(db_path) as connection:
        existing = {
            str(row["cb_code"]): dict(row)
            for row in connection.execute("SELECT * FROM cb_master")
        }
        tdcc_date = next(iter(balances.values()))["balance_date"]
        missing_rows = []
        for code, master in existing.items():
            if code in balances or "opendata.tdcc.com.tw/getOD.ashx?id=1-16" in str(master["source_url"]):
                continue
            missing_rows.append((code,))
            if str(master["issue_date"]) > str(tdcc_date):
                warnings.append(
                    f"new CB awaiting TDCC data: {code} issue_date={master['issue_date']}"
                )
            elif master.get("delisting_date") is None or str(master["delisting_date"]) > as_of.isoformat():
                warnings.append(f"active CB missing from TDCC data: {code}")
        with connection:
            cursor = connection.executemany(
                """
                UPDATE cb_master
                SET balance_amount = ?, balance_date = ?, collected_at = ?,
                    source = ?,
                    source_url = CASE
                        WHEN instr(source_url, ?) > 0 THEN source_url
                        ELSE source_url || ' | ' || ?
                    END
                WHERE cb_code = ?
                """,
                [
                    (
                        amount, balance_date, timestamp, MASTER_SOURCE,
                        TDCC_BOOK_ENTRY_URL, TDCC_BOOK_ENTRY_URL, code,
                    )
                    for amount, balance_date, timestamp, code in rows
                ],
            )
            connection.executemany(
                "UPDATE cb_master SET balance_amount = NULL, balance_date = NULL WHERE cb_code = ?",
                missing_rows,
            )
            return cursor.rowcount, warnings


def sync_tpex_lifecycle(
    db_path: Path | str,
    delistings: dict[str, dict[str, str]],
    as_of: date,
    redemption_dates: dict[str, str] | None = None,
) -> int:
    """Atomically apply TPEx delisting facts without touching balances or MOPS data."""
    redemptions = redemption_dates or {}
    lifecycle_updates = [
        {
            "cb_code": code,
            "delisting_date": redemptions.get(code, row["delisting_date"]),
            "delisting_reason": (
                "已贖回" if code in redemptions else "已下市"
            ) if row["delisting_date"] <= as_of.isoformat() else None,
        }
        for code, row in delistings.items()
    ]
    with connect(db_path) as connection:
        upsert_master_data(connection, [], [], [], lifecycle_updates=lifecycle_updates)
    return len(lifecycle_updates)


def bootstrap_new_tpex_masters(
    db_path: Path | str,
    issues: dict[str, dict[str, object]],
    links: dict[str, str],
    delistings: dict[str, dict[str, str]],
    session: requests.Session,
    as_of: date,
    collected_at: str,
    codes: set[str] | None = None,
) -> tuple[int, int, int]:
    """Create only newly active ordinary CB masters from verified TPEx/MOPS data."""
    state = _load_existing_master_state(db_path)
    selected_codes = codes if codes is not None else set(issues)
    missing = sorted(selected_codes - set(issues))
    if missing:
        raise MasterFormatError(
            f"Requested CBs are not active in TPEx issue data: {missing}"
        )

    lifecycle_updates = [
        {
            "cb_code": code,
            "delisting_date": row["delisting_date"],
            "delisting_reason": "已下市"
            if row["delisting_date"] <= as_of.isoformat() else None,
        }
        for code, row in delistings.items()
    ]
    candidates = sorted(
        code
        for code in selected_codes
        if code not in state.masters
        and is_active_on(
            str(issues[code]["issue_date"]),
            delistings.get(code, {}).get("delisting_date"),
            as_of,
        )
    )
    masters: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    balances: list[dict[str, object]] = []
    excluded_exchangeables: list[str] = []

    for code in candidates:
        master = dict(issues[code])
        if code in links:
            current_url = links[code]
            snapshot = _get_mops_snapshot(session, current_url)
        else:
            current_url, snapshot = _latest_fallback_mops_snapshot(
                session, master, as_of
            )
        _validate_snapshot(master, snapshot)
        if snapshot["instrument_kind"] == "exchangeable":
            excluded_exchangeables.append(code)
            continue

        cb_events, cb_balances, ambiguities = _history_for_cb(
            session, master, current_url, snapshot, collected_at, as_of
        )
        _validate_ambiguous_monthly_prices(code, ambiguities, cb_events)
        latest_event = latest_effective_event(cb_events, as_of)
        master.update(
            {
                "issue_amount": snapshot["issue_amount"],
                # TDCC owns the latest balance and may be unavailable on issue day.
                "balance_amount": None,
                "balance_date": None,
                "issue_units": snapshot["issue_units"],
                "current_conversion_price": latest_event["conversion_price"],
                "current_conversion_price_effective_date": latest_event[
                    "effective_date"
                ],
                "delisting_date": delistings.get(code, {}).get("delisting_date"),
                "delisting_reason": None,
                "source": BOOTSTRAP_SOURCE,
                "source_url": (
                    f"{TPEX_CB_ISSUE_URL} | {snapshot['source_url']} | "
                    f"{latest_event['source_url']}"
                ),
                "collected_at": collected_at,
            }
        )
        for key in (
            "issue_conversion_price",
            "series_number",
            "tpex_reported_issue_amount",
        ):
            master.pop(key, None)
        masters.append(master)
        events.extend(cb_events)
        balances.extend(cb_balances)

    with connect(db_path) as connection:
        master_count, event_count, balance_count = upsert_master_data(
            connection,
            masters,
            events,
            balances,
            excluded_exchangeables,
            lifecycle_updates,
            as_of,
        )
    return master_count, event_count, balance_count


def _get_mops_snapshot(
    session: requests.Session,
    url: str,
    request_counts: RequestCounts | None = None,
) -> dict[str, object]:
    absolute = urljoin(MOPS_BASE_URL, url)
    last_error: MasterFormatError | None = None
    for attempt in range(5):
        time.sleep(0.15)
        if request_counts is not None:
            request_counts.mops_detail += 1
        response = session.get(absolute, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        response.encoding = "utf-8"
        if "查無債券基本資料" in _compact_html(response.text):
            raise MopsNoDataError("MOPS explicitly reports no bond data")
        try:
            return parse_mops_snapshot(response.text, absolute)
        except MasterFormatError as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(attempt + 1)
    assert last_error is not None
    raise last_error


def _latest_fallback_mops_snapshot(
    session: requests.Session,
    master: dict[str, object],
    as_of: date,
    request_counts: RequestCounts | None = None,
) -> tuple[str, dict[str, object]]:
    year_month = as_of.strftime("%Y-%m")
    issue_month = str(master["issue_date"])[:7]
    while year_month >= issue_month:
        url = _fallback_mops_url(master, year_month)
        if request_counts is not None:
            request_counts.mops_detail += 1
        response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        response.encoding = "utf-8"
        text = _compact_html(response.text)
        if "查無債券基本資料" not in text and text:
            snapshot = parse_mops_snapshot(response.text, url)
            return url, snapshot
        year_month = _month_before(year_month)
    raise MasterFormatError(
        f"No complete official MOPS snapshot found for active TPEx CB {master['cb_code']}"
    )


def _get_mops_announcements(
    session: requests.Session,
    stock_code: str,
    cb_code: str,
    year: int,
    collected_at: str,
    page_cache: dict[tuple[str, int], tuple[str, str]],
    request_counts: RequestCounts | None = None,
) -> list[dict[str, object]]:
    roc_year = year - 1911
    parameters = {
        "step": "1",
        "TYPEK": "all",
        "co_id_1": stock_code,
        "co_id_2": stock_code,
        "year": str(roc_year),
        "month": "",
        "day1": "",
        "day2": "",
        "coid": "",
        "firstin": "true",
    }
    cache_key = (stock_code, year)
    cached = page_cache.get(cache_key)
    if cached is None:
        audit_url = f"{MOPS_CB_ANNOUNCEMENT_URL}?{urlencode(parameters)}"
        content = ""
        for attempt in range(6):
            time.sleep(0.15)
            if request_counts is not None:
                request_counts.mops_announcement += 1
            response = session.post(
                MOPS_CB_ANNOUNCEMENT_URL,
                data=parameters,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; cb-radar/0.2)",
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            content = response.text
            if "轉換公司債轉換價格變更公告" in _compact_html(content):
                break
            if attempt < 5:
                time.sleep(min(2 ** (attempt + 1), 16))
        cached = (content, audit_url)
        page_cache[cache_key] = cached
    content, audit_url = cached
    try:
        return parse_mops_conversion_announcements(
            content, cb_code, collected_at, audit_url
        )
    except MasterFormatError as exc:
        raise MasterFormatError(
            f"MOPS announcement page failed for {cb_code}, year {year}: {exc}"
        ) from exc


def _announcement_redemption_dates(
    db_path: Path | str,
    delistings: dict[str, dict[str, str]],
    masters: dict[str, dict[str, object]],
    as_of: date,
) -> dict[str, str]:
    """Read confirmed redemption dates from saved official announcement bodies."""
    codes_by_stock: dict[str, list[str]] = {}
    for code, row in delistings.items():
        if row["delisting_date"] > as_of.isoformat() or code not in masters:
            continue
        codes_by_stock.setdefault(str(masters[code]["stock_code"]), []).append(code)
    redeemed: dict[str, str] = {}
    with connect(db_path) as connection:
        for stock_code, codes in codes_by_stock.items():
            rows = connection.execute(
                "SELECT subject, body FROM company_announcements WHERE company_code = ?",
                (stock_code,),
            ).fetchall()
            for code in codes:
                dates = set()
                for row in rows:
                    content = f"{row['subject']}\n{row['body']}"
                    if is_forced_redemption_announcement(content, code):
                        dates.add(parse_forced_redemption_date(str(row["body"]), code))
                if len(dates) > 1:
                    raise MasterFormatError(
                        f"Conflicting forced-redemption dates for {code}"
                    )
                if dates:
                    redeemed[code] = dates.pop()
    return redeemed


def _get_mops_rules_events(
    session: requests.Session,
    rules_url: str,
    cb_code: str,
    collected_at: str,
) -> list[dict[str, object]]:
    response = session.get(rules_url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise MasterFormatError(f"MOPS conversion terms are not a PDF for {cb_code}")
    return parse_mops_rules_conversion_events(
        response.content, cb_code, collected_at, rules_url
    )


def _validate_snapshot(
    master: dict[str, object],
    snapshot: dict[str, object],
    *,
    verified_issue_units: int | None = None,
) -> None:
    code = master["cb_code"]
    for key in ("issue_date", "maturity_date"):
        if master[key] != snapshot[key]:
            raise MasterFormatError(
                f"TPEx/MOPS mismatch for {code} {key}: {master[key]!r} != {snapshot[key]!r}"
            )
    if master["tpex_reported_issue_amount"] != snapshot["actual_issue_amount"]:
        raise MasterFormatError(
            f"TPEx/MOPS mismatch for {code} actual issue amount: "
            f"{master['tpex_reported_issue_amount']!r} != "
            f"{snapshot['actual_issue_amount']!r}"
        )
    try:
        balance_units_for_display(
            int(snapshot["issue_amount"]),
            verified_issue_units or int(snapshot["issue_units"]),
            int(snapshot["balance_amount"]),
        )
    except MasterFormatError as exc:
        raise MasterFormatError(
            f"Official par-value validation failed for {code}: "
            f"issue_amount={snapshot['issue_amount']}, "
            f"issue_units={snapshot['issue_units']}, "
            f"balance_amount={snapshot['balance_amount']}: {exc}"
        ) from exc


def _history_for_cb(
    session: requests.Session,
    master: dict[str, object],
    current_url: str,
    current: dict[str, object],
    collected_at: str,
    as_of: date,
    request_counts: RequestCounts | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    code = str(master["cb_code"])
    issue_date = str(master["issue_date"])
    events: dict[str, dict[str, object]] = {}
    ambiguous_mops_dates: set[str] = set()
    ambiguous_monthly_prices: list[dict[str, object]] = []
    balances: dict[str, dict[str, object]] = {}
    snapshot = current
    events[issue_date] = {
        "cb_code": code,
        "effective_date": issue_date,
        "conversion_price": master["issue_conversion_price"],
        "source": TPEX_ISSUE_SOURCE,
        "source_url": TPEX_CB_ISSUE_URL,
        "collected_at": collected_at,
    }
    while True:
        effective = str(snapshot["effective_date"])
        existing = events.get(effective)
        if existing and existing["conversion_price"] != snapshot["conversion_price"]:
            ambiguous_mops_dates.add(effective)
            ambiguous_monthly_prices.append(
                {
                    "reported_effective_date": effective,
                    "prices": {
                        float(existing["conversion_price"]),
                        float(snapshot["conversion_price"]),
                    },
                }
            )
            if existing["source"] == MOPS_SOURCE:
                del events[effective]
        elif effective not in ambiguous_mops_dates and existing is None:
            events[effective] = {
                "cb_code": code,
                "effective_date": effective,
                "conversion_price": snapshot["conversion_price"],
                "source": MOPS_SOURCE,
                "source_url": snapshot["source_url"],
                "collected_at": collected_at,
            }
        year_month = str(snapshot["year_month"])
        if is_complete_reporting_month(year_month, as_of):
            balances[year_month] = {
                "cb_code": code,
                "year_month": year_month,
                "balance_amount": snapshot["balance_amount"],
                "source": MOPS_SOURCE,
                "source_url": snapshot["source_url"],
                "collected_at": collected_at,
            }
        search_month = _previous_month(effective)
        prior: dict[str, object] | None = None
        while search_month >= issue_date[:7]:
            prior_url = _url_for_month(current_url, search_month)
            try:
                candidate = _get_mops_snapshot(session, prior_url, request_counts)
            except MopsNoDataError:
                search_month = _month_before(search_month)
                continue
            except MasterFormatError as exc:
                raise MasterFormatError(
                    f"MOPS history failed for {code} at {search_month}: {exc}"
                ) from exc
            _validate_snapshot(
                master,
                candidate,
                verified_issue_units=int(current["issue_units"]),
            )
            candidate_month = str(candidate["year_month"])
            if is_complete_reporting_month(candidate_month, as_of):
                balances[candidate_month] = {
                    "cb_code": code,
                    "year_month": candidate_month,
                    "balance_amount": candidate["balance_amount"],
                    "source": MOPS_SOURCE,
                    "source_url": candidate["source_url"],
                    "collected_at": collected_at,
                }
            candidate_effective = str(candidate["effective_date"])
            if candidate_effective < effective:
                prior = candidate
                break
            if candidate_effective > effective:
                raise MasterFormatError(
                    f"MOPS historical price moves forward unexpectedly for {code}"
                )
            search_month = _month_before(search_month)
        if prior is None:
            break
        snapshot = prior

    return list(events.values()), list(balances.values()), ambiguous_monthly_prices


def _validate_ambiguous_monthly_prices(
    cb_code: str,
    ambiguities: list[dict[str, object]],
    authoritative_events: list[dict[str, object]],
) -> None:
    authoritative_prices = {
        float(event["conversion_price"])
        for event in authoritative_events
        if event["source"] in {
            TPEX_ISSUE_SOURCE,
            MOPS_ANNOUNCEMENT_SOURCE,
            MOPS_RULES_SOURCE,
        }
    }
    for ambiguity in ambiguities:
        unresolved = set(ambiguity["prices"]) - authoritative_prices
        if unresolved:
            raise MasterFormatError(
                f"Ambiguous MOPS monthly conversion prices for {cb_code} at "
                f"{ambiguity['reported_effective_date']} are not resolved by an "
                f"official issue or price-change announcement: {sorted(unresolved)}"
            )


def _merge_conversion_events(
    cb_code: str,
    monthly_events: list[dict[str, object]],
    announcement_events: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    priority = {
        MOPS_SOURCE: 0,
        TPEX_ISSUE_SOURCE: 1,
        MOPS_RULES_SOURCE: 2,
        MOPS_ANNOUNCEMENT_SOURCE: 3,
    }
    event_by_date: dict[str, dict[str, object]] = {}
    for event in [*monthly_events, *announcement_events]:
        effective = str(event["effective_date"])
        existing = event_by_date.get(effective)
        if existing and existing["conversion_price"] != event["conversion_price"]:
            incoming_priority = priority[str(event["source"])]
            existing_priority = priority[str(existing["source"])]
            if incoming_priority > existing_priority:
                event_by_date[effective] = event
                continue
            if incoming_priority < existing_priority:
                continue
            raise MasterFormatError(
                f"Conflicting official conversion prices for {cb_code} on {effective}"
            )
        if existing is None or priority[str(event["source"])] >= priority[str(existing["source"])]:
            event_by_date[effective] = event
    return event_by_date


def _load_existing_master_state(db_path: Path | str) -> ExistingMasterState:
    """Read prior verified Phase 2 state without creating or changing the database."""
    path = Path(db_path)
    if not path.exists():
        return ExistingMasterState({}, {}, {})
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        masters = {
            str(row["cb_code"]): dict(row)
            for row in connection.execute("SELECT * FROM cb_master")
        }
        monthly_year_months: dict[str, set[str]] = {}
        for row in connection.execute("SELECT cb_code, year_month FROM cb_monthly_balance"):
            monthly_year_months.setdefault(str(row["cb_code"]), set()).add(
                str(row["year_month"])
            )
        events: dict[str, list[dict[str, object]]] = {}
        for row in connection.execute("SELECT * FROM conversion_price_events"):
            events.setdefault(str(row["cb_code"]), []).append(dict(row))
        return ExistingMasterState(masters, monthly_year_months, events)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        # Phase 1 databases do not yet have the Phase 2 tables.
        return ExistingMasterState({}, {}, {})
    finally:
        if "connection" in locals():
            connection.close()


def _latest_completed_year_month(as_of: date) -> str:
    return _month_before(as_of.strftime("%Y-%m"))


def _mops_event_from_snapshot(
    cb_code: str, snapshot: dict[str, object], collected_at: str
) -> dict[str, object]:
    return {
        "cb_code": cb_code,
        "effective_date": snapshot["effective_date"],
        "conversion_price": snapshot["conversion_price"],
        "source": MOPS_SOURCE,
        "source_url": snapshot["source_url"],
        "collected_at": collected_at,
    }


def _master_from_existing(
    tpex_master: dict[str, object],
    existing_master: dict[str, object],
    current_event: dict[str, object],
    tdcc_balance: dict[str, object],
    delisting_date: str | None,
    collected_at: str,
    as_of: date,
) -> dict[str, object]:
    """Apply daily TPEx state while retaining the prior MOPS-verified fields."""
    master = dict(tpex_master)
    master.update(
        {
            "issue_units": existing_master["issue_units"],
            "issue_amount": existing_master["issue_amount"],
            "current_conversion_price": current_event["conversion_price"],
            "current_conversion_price_effective_date": current_event["effective_date"],
            "delisting_date": delisting_date,
            "delisting_reason": None,
            "source": MASTER_SOURCE,
            "source_url": (
                f"{TPEX_CB_ISSUE_URL} | {TDCC_BOOK_ENTRY_URL} | "
                f"{current_event['source_url']}"
            ),
            "collected_at": collected_at,
        }
    )
    master["balance_amount"], master["balance_date"] = select_current_balance(
        tdcc_balance, as_of
    )
    for key in ("issue_conversion_price", "series_number", "tpex_reported_issue_amount"):
        master.pop(key, None)
    return master


def _existing_mops_detail_url(
    master: dict[str, object], events: list[dict[str, object]]
) -> str | None:
    """Find a previously verified MOPS detail URL without needing TPEx convSearch."""
    for event in events:
        source_url = str(event.get("source_url", ""))
        if event.get("source") == MOPS_SOURCE and "t120sg01" in source_url:
            return source_url
    for source_url in str(master.get("source_url", "")).split(" | "):
        if "t120sg01" in source_url:
            return source_url
    return None


def sync_mops_from_existing(
    db_path: Path | str,
    session: requests.Session,
    as_of: date,
    collected_at: str,
) -> tuple[int, int]:
    """Atomically sync MOPS updates using only previously verified master state."""
    state = _load_existing_master_state(db_path)
    latest_month = _latest_completed_year_month(as_of)
    events_to_write: list[dict[str, object]] = []
    balances_to_write: list[dict[str, object]] = []
    master_updates: list[tuple[float, str, str]] = []
    announcement_pages: dict[tuple[str, int], tuple[str, str]] = {}

    for code, master in state.masters.items():
        if not is_active_on(
            str(master["issue_date"]), master.get("delisting_date"), as_of
        ):
            continue
        existing_events = state.events.get(code, [])
        if not existing_events:
            continue
        monthly_events: list[dict[str, object]] = []
        if latest_month not in state.monthly_year_months.get(code, set()):
            detail_url = _existing_mops_detail_url(master, existing_events)
            if detail_url is not None:
                try:
                    snapshot = _get_mops_snapshot(
                        session, _url_for_month(detail_url, latest_month)
                    )
                except MopsNoDataError:
                    snapshot = None
                if snapshot is not None:
                    if (
                        snapshot["issue_date"] != master["issue_date"]
                        or snapshot["maturity_date"] != master["maturity_date"]
                    ):
                        raise MasterFormatError(f"MOPS existing-master identity mismatch: {code}")
                    balance_units_for_display(
                        int(master["issue_amount"]), int(master["issue_units"]),
                        int(snapshot["balance_amount"]),
                    )
                    balances_to_write.append(
                        {
                            "cb_code": code, "year_month": latest_month,
                            "balance_amount": snapshot["balance_amount"],
                            "source": MOPS_SOURCE, "source_url": snapshot["source_url"],
                            "collected_at": collected_at,
                        }
                    )
                    monthly_events.append(_mops_event_from_snapshot(code, snapshot, collected_at))
        announcements = _get_mops_announcements(
            session, str(master["stock_code"]), code, as_of.year, collected_at,
            announcement_pages,
        )
        merged = _merge_conversion_events(code, existing_events, [*monthly_events, *announcements])
        existing_by_date = {str(event["effective_date"]): event for event in existing_events}
        events_to_write.extend(
            event for effective, event in merged.items()
            if effective not in existing_by_date
            or float(event["conversion_price"]) != float(existing_by_date[effective]["conversion_price"])
            or event["source"] != existing_by_date[effective]["source"]
        )
        latest = latest_effective_event(list(merged.values()), as_of)
        master_updates.append((
            float(latest["conversion_price"]), str(latest["effective_date"]), code
        ))

    with connect(db_path) as connection:
        with connection:
            connection.executemany(
                """
                INSERT INTO conversion_price_events (
                    cb_code, effective_date, conversion_price, source, source_url, collected_at
                ) VALUES (:cb_code, :effective_date, :conversion_price, :source, :source_url, :collected_at)
                ON CONFLICT(cb_code, effective_date) DO UPDATE SET
                    conversion_price = excluded.conversion_price, source = excluded.source,
                    source_url = excluded.source_url, collected_at = excluded.collected_at
                """, events_to_write,
            )
            connection.executemany(
                """
                INSERT INTO cb_monthly_balance (
                    cb_code, year_month, balance_amount, source, source_url, collected_at
                ) VALUES (:cb_code, :year_month, :balance_amount, :source, :source_url, :collected_at)
                ON CONFLICT(cb_code, year_month) DO UPDATE SET
                    balance_amount = excluded.balance_amount, source = excluded.source,
                    source_url = excluded.source_url, collected_at = excluded.collected_at
                """, balances_to_write,
            )
            connection.executemany(
                """
                UPDATE cb_master
                SET current_conversion_price = ?, current_conversion_price_effective_date = ?
                WHERE cb_code = ?
                """, master_updates,
            )
    return len(events_to_write), len(balances_to_write)


def collect_master(
    db_path: Path | str = DEFAULT_DB_PATH,
    codes: set[str] | None = None,
    session: requests.Session | None = None,
    as_of_date: date | None = None,
    source_data: tuple[
        dict[str, dict[str, object]], dict[str, str], dict[str, dict[str, str]],
        dict[str, dict[str, object]],
    ] | None = None,
) -> dict[str, object]:
    http = session or requests.Session()
    if session is None:
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        http.mount("https://", HTTPAdapter(max_retries=retry))
    http.headers.update(
        {"User-Agent": "Mozilla/5.0 (compatible; cb-radar/0.2 official collector)"}
    )
    existing_state = _load_existing_master_state(db_path)
    request_counts = RequestCounts()
    if source_data is None:
        issues = parse_tpex_issues(_get_json(http, TPEX_CB_ISSUE_URL, request_counts))
        links = parse_tpex_mops_links(
            _get_json(http, TPEX_CB_LISTED_URL, request_counts)
        )
        delistings = parse_tpex_delistings(
            _get_json(http, TPEX_CB_DELISTED_URL, request_counts)
        )
        tdcc_balances = _get_tdcc_book_entries(http, request_counts)
    else:
        issues, links, delistings, tdcc_balances = source_data
    source_selected = sorted(codes if codes is not None else issues.keys())
    missing = [code for code in source_selected if code not in issues]
    if missing:
        raise MasterFormatError(f"Requested CBs are not active in TPEx issue data: {missing}")

    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    as_of = as_of_date or datetime.now(ZoneInfo("Asia/Taipei")).date()
    latest_completed_month = _latest_completed_year_month(as_of)
    not_yet_effective = [
        {
            "cb_code": code,
            "official_name": str(issues[code]["cb_name"]),
            "reason": f"TPEx issue date {issues[code]['issue_date']} is after {as_of}",
        }
        for code in source_selected
        if str(issues[code]["issue_date"]) > as_of.isoformat()
    ]
    delisted = [
        {
            "cb_code": code,
            "official_name": str(issues[code]["cb_name"]),
            "reason": (
                "TPEx official delisting date "
                f"{delistings[code]['delisting_date']} is not after {as_of}"
            ),
        }
        for code in source_selected
        if (
            str(issues[code]["issue_date"]) <= as_of.isoformat()
            and code in delistings
            and delistings[code]["delisting_date"] <= as_of.isoformat()
        )
    ]
    selected = [
        code
        for code in source_selected
        if is_active_on(
            str(issues[code]["issue_date"]),
            delistings.get(code, {}).get("delisting_date"),
            as_of,
        )
    ]
    lifecycle_updates = [
        {
            "cb_code": code,
            "delisting_date": row["delisting_date"],
            "delisting_reason": "已下市"
            if row["delisting_date"] <= as_of.isoformat() else None,
        }
        for code, row in delistings.items()
    ]
    redemption_dates = _announcement_redemption_dates(
        db_path, delistings, existing_state.masters, as_of
    )
    for row in lifecycle_updates:
        if row["cb_code"] in redemption_dates:
            row["delisting_date"] = redemption_dates[row["cb_code"]]
            row["delisting_reason"] = "已贖回"
    masters: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    balances: list[dict[str, object]] = []
    announcement_pages: dict[tuple[str, int], tuple[str, str]] = {}
    excluded_exchangeables: list[dict[str, str]] = []
    bootstrap_codes = {
        code for code in selected if code not in existing_state.masters
    }
    monthly_incremental_codes = {
        code
        for code in selected
        if (
            code in existing_state.masters
            and str(issues[code]["issue_date"])[:7] <= latest_completed_month
            and latest_completed_month
            not in existing_state.monthly_year_months.get(code, set())
        )
    }
    for code in selected:
        master = dict(issues[code])
        if code in bootstrap_codes:
            try:
                if code in links:
                    current_url = links[code]
                    snapshot = _get_mops_snapshot(http, current_url, request_counts)
                else:
                    current_url, snapshot = _latest_fallback_mops_snapshot(
                        http, master, as_of, request_counts
                    )
            except MasterFormatError as exc:
                raise MasterFormatError(
                    f"MOPS bootstrap snapshot failed for {code}: {exc}"
                ) from exc
            _validate_snapshot(master, snapshot)
            if snapshot["instrument_kind"] == "exchangeable":
                excluded_exchangeables.append(
                    {
                        "cb_code": code,
                        "official_name": str(snapshot["official_name"]),
                        "reason": "MOPS official name identifies an exchangeable bond",
                    }
                )
                continue
            if code not in tdcc_balances:
                raise MasterFormatError(
                    f"TDCC book-entry endpoint is missing active CB: {code}"
                )
            cb_events, cb_balances, ambiguities = _history_for_cb(
                http, master, current_url, snapshot, collected_at, as_of, request_counts
            )
            announcements: list[dict[str, object]] = []
            for year in range(int(str(master["issue_date"])[:4]), as_of.year + 1):
                announcements.extend(
                    _get_mops_announcements(
                        http, str(master["stock_code"]), code, year, collected_at,
                        announcement_pages, request_counts,
                    )
                )
            rules_events: list[dict[str, object]] = []
            try:
                _validate_ambiguous_monthly_prices(code, ambiguities, [*cb_events, *announcements])
            except MasterFormatError:
                rules_url = snapshot.get("rules_url")
                if not rules_url:
                    raise
                rules_events = _get_mops_rules_events(http, str(rules_url), code, collected_at)
                if any(
                    ambiguity["reported_effective_date"] == master["issue_date"]
                    for ambiguity in ambiguities
                ) and any(
                    str(event["effective_date"]) <= str(master["issue_date"])
                    for event in rules_events
                ):
                    cb_events = [event for event in cb_events if event["source"] != TPEX_ISSUE_SOURCE]
                _validate_ambiguous_monthly_prices(
                    code, ambiguities, [*cb_events, *announcements, *rules_events]
                )
            event_by_date = _merge_conversion_events(
                code, cb_events, [*rules_events, *announcements]
            )
            latest_event = latest_effective_event(list(event_by_date.values()), as_of)
            master["issue_amount"] = snapshot["issue_amount"]
            master["balance_amount"], master["balance_date"] = select_current_balance(
                tdcc_balances[code], as_of
            )
            master.update(
                {
                    "issue_units": snapshot["issue_units"],
                    "current_conversion_price": latest_event["conversion_price"],
                    "current_conversion_price_effective_date": latest_event["effective_date"],
                    "delisting_date": delistings.get(code, {}).get("delisting_date"),
                    "delisting_reason": None,
                    "source": MASTER_SOURCE,
                    "source_url": (
                        f"{TPEX_CB_ISSUE_URL} | {TDCC_BOOK_ENTRY_URL} | "
                        f"{snapshot['source_url']} | {latest_event['source_url']}"
                    ),
                    "collected_at": collected_at,
                }
            )
            for key in ("issue_conversion_price", "series_number", "tpex_reported_issue_amount"):
                master.pop(key, None)
            masters.append(master)
            events.extend(event_by_date.values())
            balances.extend(cb_balances)
            continue

        existing_master = existing_state.masters[code]
        if code not in tdcc_balances:
            raise MasterFormatError(
                f"TDCC book-entry endpoint is missing active CB: {code}"
            )
        existing_events = existing_state.events.get(code, [])
        if not existing_events:
            raise MasterFormatError(
                f"Existing CB {code} has no conversion-price event history; a full MOPS bootstrap is required"
            )
        monthly_events: list[dict[str, object]] = []
        if code in monthly_incremental_codes:
            monthly_url = (
                _url_for_month(links[code], latest_completed_month)
                if code in links
                else _fallback_mops_url(master, latest_completed_month)
            )
            try:
                snapshot = _get_mops_snapshot(http, monthly_url, request_counts)
            except MopsNoDataError:
                snapshot = None
            if snapshot is not None:
                _validate_snapshot(
                    master, snapshot, verified_issue_units=int(existing_master["issue_units"])
                )
                if str(snapshot["year_month"]) != latest_completed_month:
                    raise MasterFormatError(
                        f"MOPS incremental month mismatch for {code}: {snapshot['year_month']} != {latest_completed_month}"
                    )
                balances.append(
                    {
                        "cb_code": code,
                        "year_month": latest_completed_month,
                        "balance_amount": snapshot["balance_amount"],
                        "source": MOPS_SOURCE,
                        "source_url": snapshot["source_url"],
                        "collected_at": collected_at,
                    }
                )
                monthly_events.append(_mops_event_from_snapshot(code, snapshot, collected_at))
        announcements = _get_mops_announcements(
            http, str(master["stock_code"]), code, as_of.year, collected_at,
            announcement_pages, request_counts,
        )
        event_by_date = _merge_conversion_events(
            code, existing_events, [*announcements, *monthly_events]
        )
        existing_by_date = {str(event["effective_date"]): event for event in existing_events}
        events.extend(
            event
            for effective_date, event in event_by_date.items()
            if (
                effective_date not in existing_by_date
                or float(event["conversion_price"])
                != float(existing_by_date[effective_date]["conversion_price"])
                or event["source"] != existing_by_date[effective_date]["source"]
            )
        )
        latest_event = latest_effective_event(list(event_by_date.values()), as_of)
        masters.append(
            _master_from_existing(
                master, existing_master, latest_event,
                tdcc_balances[code],
                delistings.get(code, {}).get("delisting_date"), collected_at, as_of,
            )
        )

    with connect(db_path) as connection:
        master_count, event_count, balance_count = upsert_master_data(
            connection,
            masters,
            events,
            balances,
            [row["cb_code"] for row in excluded_exchangeables],
            lifecycle_updates,
            as_of,
        )
    return {
        "master_records": master_count,
        "conversion_price_events": event_count,
        "monthly_balance_records": balance_count,
        "tpex_requests": request_counts.tpex,
        "tdcc_requests": request_counts.tdcc,
        "mops_detail_requests": request_counts.mops_detail,
        "mops_announcement_requests": request_counts.mops_announcement,
        "bootstrap_cbs": len(bootstrap_codes),
        "monthly_incremental_cbs": len(monthly_incremental_codes),
        "database": str(db_path),
        "records": masters,
        "official_active_twd_bond_type_5": len(source_selected),
        "ordinary_convertible_bonds": len(masters),
        "exchangeable_bonds": len(excluded_exchangeables),
        "delisted_bonds": len(delisted),
        "other_excluded_types": len(not_yet_effective),
        "excluded_exchangeables": excluded_exchangeables,
        "excluded_delisted": delisted,
        "other_exclusions": not_yet_effective,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official TPEx/MOPS CB master data")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--codes",
        help="comma-separated active CB codes; omit to collect all active TWD CBs",
    )
    parser.add_argument(
        "--module",
        choices=("all", "tpex", "tdcc", "mops"),
        default="all",
        help="run one independently-atomic Phase 2 module, or all modules",
    )
    return parser.parse_args(argv)


def collect_phase2_modules(
    db_path: Path | str,
    codes: set[str] | None = None,
    session: requests.Session | None = None,
    as_of_date: date | None = None,
    module: str = "all",
) -> dict[str, dict[str, object]]:
    """Run Phase 2 modules independently and report every module outcome."""
    http = session or requests.Session()
    if session is None:
        retry = Retry(
            total=4, connect=4, read=4, backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504, 520),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        http.mount("https://", HTTPAdapter(max_retries=retry))
    http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; cb-radar/0.2 official collector)"})
    as_of = as_of_date or datetime.now(ZoneInfo("Asia/Taipei")).date()
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: dict[str, dict[str, object]] = {}

    if module in {"all", "tpex"}:
        try:
            issues = parse_tpex_issues(_get_json(http, TPEX_CB_ISSUE_URL))
            links = parse_tpex_mops_links(
                _get_json(http, TPEX_CB_LISTED_URL)
            )
            delistings = parse_tpex_delistings(_get_json(http, TPEX_CB_DELISTED_URL))
            existing_state = _load_existing_master_state(db_path)
            redemption_dates = _announcement_redemption_dates(
                db_path, delistings, existing_state.masters, as_of
            )
            created, events, monthly = bootstrap_new_tpex_masters(
                db_path, issues, links, delistings, http, as_of, collected_at, codes
            )
            sync_tpex_lifecycle(db_path, delistings, as_of, redemption_dates)
            results["tpex"] = {
                "status": "success",
                "updated": len(delistings),
                "bootstrap": created,
                "events": events,
                "monthly": monthly,
            }
        except (requests.RequestException, MasterFormatError, ValueError) as exc:
            results["tpex"] = {"status": "failed", "error": str(exc)}

    if module in {"all", "tdcc"}:
        try:
            tdcc_balances = _get_tdcc_book_entries(http)
            updated, warnings = sync_tdcc_balances(
                db_path, tdcc_balances, as_of, collected_at
            )
            results["tdcc"] = {
                "status": "success", "updated": updated, "warnings": warnings,
            }
        except (requests.RequestException, MasterFormatError, ValueError) as exc:
            results["tdcc"] = {"status": "failed", "error": str(exc)}

    if module in {"all", "mops"}:
        print("phase2_module_mops: start", flush=True)
        try:
            event_count, balance_count = sync_mops_from_existing(
                db_path, http, as_of, collected_at
            )
            results["mops"] = {
                "status": "success", "events": event_count, "monthly": balance_count,
            }
        except (requests.RequestException, MasterFormatError, ValueError) as exc:
            results["mops"] = {"status": "failed", "error": str(exc)}
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    codes = {code.strip() for code in args.codes.split(",") if code.strip()} if args.codes else None
    results = collect_phase2_modules(args.database, codes, module=args.module)
    for name in ("tpex", "tdcc", "mops"):
        if name in results:
            result = results[name]
            print(f"phase2_module_{name}: {result['status']}")
            if "updated" in result:
                print(f"phase2_module_{name}_updated: {result['updated']}")
            if "bootstrap" in result:
                print(f"phase2_module_{name}_bootstrap: {result['bootstrap']}")
            if "error" in result:
                print(f"phase2_module_{name}_error: {result['error']}")
            for warning in result.get("warnings", []):
                print(f"phase2_module_{name}_warning: {warning}")
            if name == "mops" and result["status"] == "success":
                print(f"phase2_module_mops_events: {result['events']}")
                print(f"phase2_module_mops_monthly: {result['monthly']}")
    if any(result["status"] != "success" for result in results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
