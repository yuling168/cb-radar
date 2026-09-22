"""Fetch auditable historical CB issuance terms from TPEx market notices.

The TPEx bulletin API is only an index.  Values are accepted solely from the
official notice HTML, after its exact CB code and all three required terms are
present.  The index date and notice number are retained/reconstruct the stable
TPEx storage URL, so a batch can be reproduced without a search engine.
"""
from __future__ import annotations

import html
import argparse
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from config import DEFAULT_DB_PATH
from db import connect
from tpex_tls import build_tpex_session

INDEX_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/announcement"
STORAGE_ROOT = "https://www.tpex.org.tw/storage/eb_data"
NOTICE_CATEGORY = "6"


class HistoricalIssuanceSourceError(RuntimeError):
    pass


def _roc_to_iso(value: str) -> str:
    m = re.fullmatch(r"\s*(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日\s*", value)
    if not m:
        raise HistoricalIssuanceSourceError(f"invalid ROC date: {value!r}")
    result = date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    return result.isoformat()


def _index_date_folder(value: str) -> str:
    m = re.fullmatch(r"(\d{2,3})/(\d{2})/(\d{2})", value)
    if not m:
        raise HistoricalIssuanceSourceError(f"invalid TPEx index date: {value!r}")
    # TPEx storage keeps the year width shown by the historical index: modern
    # notices use 10901, while the older archive uses e.g. 9401 (not 09401).
    return m.group(1) + m.group(2)


def notice_storage_url(index_date: str, notice_no: str) -> str:
    """Build the documented TPEx static notice URL from an index record."""
    match = re.search(r"(\d{9,11})", notice_no)
    if not match:
        raise HistoricalIssuanceSourceError(f"notice number lacks an identifier: {notice_no!r}")
    return f"{STORAGE_ROOT}/{_index_date_folder(index_date)}/{match.group(1)}.html"


def fetch_index(session: requests.Session, start: date, end: date) -> list[dict[str, str]]:
    """Fetch one official category-6 index range and validate every record."""
    response = session.post(INDEX_URL, data={
        "startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d"),
        "cate": NOTICE_CATEGORY, "page": "1", "pageSize": "10000",
    }, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "ok" or not isinstance(payload.get("tables"), list) or len(payload["tables"]) != 1:
        raise HistoricalIssuanceSourceError(f"invalid TPEx index payload: {payload!r}")
    table = payload["tables"][0]
    rows = table.get("data")
    total = table.get("totalCount")
    if not isinstance(rows, list) or not isinstance(total, int) or total != len(rows):
        raise HistoricalIssuanceSourceError("TPEx index is truncated or has invalid pagination")
    notices = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 5 or not all(isinstance(row[i], str) for i in (1, 2, 3, 4)):
            raise HistoricalIssuanceSourceError(f"invalid TPEx index row: {row!r}")
        # Category 6 includes occasional general-information rows without a
        # formal notice number.  They cannot be reconstructed into an
        # immutable notice document and are outside this issuance source.
        if not re.search(r"\d{9,11}", row[2]):
            continue
        notices.append({"index_date": row[1], "notice_no": row[2], "subject": row[3], "index_link": row[4],
                        "source_url": notice_storage_url(row[1], row[2])})
    return notices


def _text(raw_html: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_html))).strip()


def _amount_to_twd(raw: str) -> int:
    raw = raw.replace(",", "").replace("，", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*億", raw)
    if m:
        return int(float(m.group(1)) * 100_000_000)
    m = re.search(r"(\d+)\s*元", raw)
    if m:
        return int(m.group(1))
    raise HistoricalIssuanceSourceError(f"unparseable issue amount: {raw!r}")


def parse_notice(cb_code: str, raw_html: str) -> dict[str, object]:
    """Extract exactly one complete issuance record; ambiguity fails closed."""
    text = _text(raw_html)
    code_hits = re.findall(r"代碼\s*[：:]\s*(\d{5,6})", text)
    if code_hits.count(cb_code) != 1 or len(set(code_hits)) != 1:
        raise HistoricalIssuanceSourceError(f"notice code does not uniquely match {cb_code}")
    def one(pattern: str, label: str) -> str:
        hits = re.findall(pattern, text)
        if len(hits) != 1:
            raise HistoricalIssuanceSourceError(f"{label} is missing or ambiguous for {cb_code}")
        return hits[0]
    amount = _amount_to_twd(one(r"發行總面額\s*[：:]\s*([^。；;]+)", "issue amount"))
    issue = _roc_to_iso(one(r"發行日\s*[：:]\s*(\d{2,3}年\s*\d{1,2}月\s*\d{1,2}日)", "issue date"))
    maturity = _roc_to_iso(one(r"到期日\s*[：:]\s*(\d{2,3}年\s*\d{1,2}月\s*\d{1,2}日)", "maturity date"))
    if maturity <= issue or amount <= 0:
        raise HistoricalIssuanceSourceError(f"invalid issuance terms for {cb_code}")
    return {"cb_code": cb_code, "issue_date": issue, "maturity_date": maturity, "issue_amount": amount}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def candidate_codes(connection: sqlite3.Connection) -> set[str]:
    """Daily-market CBs which lack both full master and verified historic terms."""
    return {row[0] for row in connection.execute("""
        SELECT DISTINCT d.cb_code FROM cb_daily d
        LEFT JOIN cb_master m ON m.cb_code = d.cb_code
        LEFT JOIN cb_historical_issuance_terms h ON h.cb_code = d.cb_code
        WHERE m.cb_code IS NULL AND h.cb_code IS NULL
    """)}


def _save_status(connection: sqlite3.Connection, code: str, status: str, *, url: str | None, error: str | None) -> None:
    with connection:
        connection.execute("""
            INSERT INTO cb_historical_issuance_backfill_status
              (cb_code,status,attempt_count,matched_notice_url,last_error,checked_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(cb_code) DO UPDATE SET status=excluded.status,
              attempt_count=cb_historical_issuance_backfill_status.attempt_count+1,
              matched_notice_url=excluded.matched_notice_url,last_error=excluded.last_error,
              checked_at=excluded.checked_at
        """, (code, status, 1, url, error, utc_now()))


def scan_range(db_path: Path | str, start: date, end: date) -> dict[str, int]:
    """One repeatable index slice.  Network/index failures write no false unavailable states."""
    with connect(db_path) as connection:
        candidates = candidate_codes(connection)
    session = build_tpex_session()
    try:
        notices = fetch_index(session, start, end)
        hits = 0
        with connect(db_path) as connection:
            for notice in notices:
                # The subject is merely a bandwidth filter; the notice body is authoritative.
                if "轉換公司債" not in notice["subject"]:
                    continue
                response = session.get(notice["source_url"], timeout=30)
                # A few historic index records no longer have their static
                # document.  They are not evidence for any CB, so retain the
                # batch's ability to continue; no unavailable conclusion is
                # made for a candidate from this missing document.
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                for code in tuple(candidates):
                    try:
                        terms = parse_notice(code, response.text)
                    except HistoricalIssuanceSourceError:
                        continue
                    with connection:
                        connection.execute("""
                            INSERT INTO cb_historical_issuance_terms
                              (cb_code,issue_date,maturity_date,issue_amount,source,source_url,collected_at)
                            VALUES (?,?,?,?, 'TPEx:historical_listing_announcement', ?, ?)
                            ON CONFLICT(cb_code) DO UPDATE SET issue_date=excluded.issue_date,
                              maturity_date=excluded.maturity_date,issue_amount=excluded.issue_amount,
                              source_url=excluded.source_url,collected_at=excluded.collected_at
                        """, (terms["cb_code"], terms["issue_date"], terms["maturity_date"], terms["issue_amount"], notice["source_url"], utc_now()))
                    _save_status(connection, code, "SUCCEEDED", url=notice["source_url"], error=None)
                    candidates.remove(code); hits += 1
        return {"notices": len(notices), "matched": hits, "remaining": len(candidates)}
    finally:
        session.close()


def _month_starts(start: date, end: date):
    current = date(start.year, start.month, 1)
    while current <= end:
        next_month = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
        yield current, min(end, date.fromordinal(next_month.toordinal() - 1))
        current = next_month


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill verified historical CB issuance terms from TPEx notices")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must not precede --start")
    total = {"notices": 0, "matched": 0}
    for first, last in _month_starts(args.start, args.end):
        result = scan_range(args.database, first, last)
        total["notices"] += result["notices"]; total["matched"] += result["matched"]
        print({"range": f"{first}/{last}", **result}, flush=True)
    print({**total, "remaining": len(candidate_codes(connect(args.database)))})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
