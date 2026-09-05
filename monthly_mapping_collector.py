"""Verify month-level CB-to-parent mappings from official MOPS detail pages."""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS, MOPS_BASE_URL
from db import (
    connect, monthly_mapping_statuses, record_monthly_mapping_status,
    upsert_parent_stock_monthly_mappings,
)


class MonthlyMappingError(RuntimeError):
    """MOPS cannot verify a requested monthly CB parent mapping."""


def _month_url(source_url: str, year_month: str) -> str:
    """Set MOPS's reporting-month parameter on an existing official detail URL."""
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.netloc != urlparse(MOPS_BASE_URL).netloc:
        raise MonthlyMappingError("monthly mapping source is not an official MOPS URL")
    if not parsed.path.endswith("/t120sg01"):
        raise MonthlyMappingError("monthly mapping source is not MOPS t120sg01")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not query.get("bond_id") or not query.get("issuer_stock_code"):
        raise MonthlyMappingError("MOPS detail URL is missing bond or issuer code")
    query["monyr_reg"] = [year_month.replace("-", "")]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _compact(content: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", content))


def parse_mops_monthly_mapping(
    content: str,
    source_url: str,
    *,
    cb_code: str,
    stock_code: str,
    stock_name: str,
) -> dict[str, str]:
    """Verify all identity fields are present in MOPS's returned detail document."""
    text = _compact(content)
    if "之轉(交)換公司債發行資料" not in text:
        raise MonthlyMappingError("MOPS response is not a CB issue detail page")
    if "交換公司債" in text:
        raise MonthlyMappingError(f"MOPS response is an exchangeable bond for {cb_code}")
    for label, value in (("CB code", cb_code), ("parent stock code", stock_code), ("parent stock name", stock_name)):
        if not value or value not in text:
            raise MonthlyMappingError(f"MOPS response cannot verify {label} for {cb_code}")
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    if query.get("bond_id", [""])[0] != cb_code:
        raise MonthlyMappingError(f"MOPS URL CB code does not match {cb_code}")
    if query.get("issuer_stock_code", [""])[0] != stock_code:
        raise MonthlyMappingError(f"MOPS URL parent stock code does not match {stock_code}")
    month = query.get("monyr_reg", [""])[0]
    if not re.fullmatch(r"\d{6}", month):
        raise MonthlyMappingError("MOPS detail URL has no verified reporting month")
    return {
        "cb_code": cb_code,
        "year_month": f"{month[:4]}-{month[4:]}",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "market": "UNKNOWN",
        "source": "MOPS:t120sg01",
        "source_url": source_url,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def _mops_detail_url(source_urls: str) -> str:
    for value in source_urls.split(" | "):
        if "/mops/web/t120sg01" in value:
            return value
    raise MonthlyMappingError("CB master has no official MOPS t120sg01 candidate URL")


def _months(start_month: str, end_month: str) -> list[str]:
    start = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    if start > end:
        raise ValueError("start_month must not be after end_month")
    result = []
    current = start
    while current <= end:
        result.append(current.strftime("%Y-%m"))
        current = datetime(current.year + (current.month == 12), (current.month % 12) + 1, 1)
    return result


def collect_monthly_verified_mappings(
    year_month: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    session: requests.Session | None = None,
    cb_codes: set[str] | None = None,
    *,
    start_month: str | None = None,
    end_month: str | None = None,
    batch_size: int = 50,
    delay_seconds: float = 0.2,
    retry_unavailable: bool = False,
) -> dict[str, object]:
    """Use current master only as query candidates; MOPS must prove each mapping."""
    if batch_size <= 0 or delay_seconds < 0:
        raise ValueError("batch_size must be positive and delay_seconds must be non-negative")
    if year_month:
        if start_month or end_month:
            raise ValueError("year_month cannot be combined with a month range")
        months = _months(year_month, year_month)
    elif start_month and end_month:
        months = _months(start_month, end_month)
    else:
        raise ValueError("supply year_month or both start_month and end_month")
    http = session or requests.Session()
    result = {"months": months, "verified": 0, "unavailable": 0, "source_errors": 0,
              "skipped_succeeded": 0, "skipped_unavailable": 0, "processed": 0,
              "database": str(db_path)}
    for month in months:
        with connect(db_path) as connection:
            rows = connection.execute(
                """
                SELECT cb_code, stock_code, stock_name, source_url FROM cb_master
                WHERE issue_date <= ? AND (delisting_date IS NULL OR delisting_date > ?)
                ORDER BY cb_code
                """,
                (f"{month}-31", f"{month}-01"),
            ).fetchall()
            statuses = monthly_mapping_statuses(connection, month)
        if cb_codes is not None:
            rows = [row for row in rows if str(row["cb_code"]) in cb_codes]
        for row in rows:
            if result["processed"] >= batch_size:
                return result
            cb_code = str(row["cb_code"])
            prior_status = statuses.get(cb_code)
            if prior_status == "SUCCEEDED":
                result["skipped_succeeded"] += 1
                continue
            if prior_status == "UNAVAILABLE" and not retry_unavailable:
                result["skipped_unavailable"] += 1
                continue
            url = None
            try:
                url = _month_url(_mops_detail_url(str(row["source_url"])), month)
                response = http.get(url, timeout=HTTP_TIMEOUT_SECONDS)
                response.raise_for_status()
                mapping = parse_mops_monthly_mapping(
                    response.text, url, cb_code=cb_code, stock_code=str(row["stock_code"]),
                    stock_name=str(row["stock_name"]),
                )
            except requests.RequestException as exc:
                status, error, mapping = "SOURCE_ERROR", str(exc), None
                result["source_errors"] += 1
            except MonthlyMappingError as exc:
                status, error, mapping = "UNAVAILABLE", str(exc), None
                result["unavailable"] += 1
            else:
                status, error = "SUCCEEDED", None
                result["verified"] += 1
            with connect(db_path) as connection:
                if mapping is not None:
                    upsert_parent_stock_monthly_mappings(connection, [mapping])
                record_monthly_mapping_status(connection, {
                    "cb_code": cb_code, "year_month": month, "status": status,
                    "last_error": error, "source_url": url,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })
            result["processed"] += 1
            if delay_seconds:
                time.sleep(delay_seconds)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify monthly CB parent mappings from MOPS")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--year-month", help="YYYY-MM")
    selection.add_argument("--start-month", help="YYYY-MM; requires --end-month")
    parser.add_argument("--end-month", help="YYYY-MM")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cb-code", action="append", dest="cb_codes")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    parser.add_argument(
        "--retry-unavailable", action="store_true",
        help="Re-query prior UNAVAILABLE mappings; default is to skip them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = collect_monthly_verified_mappings(
            args.year_month, args.database, cb_codes=set(args.cb_codes or []) or None,
            start_month=args.start_month, end_month=args.end_month,
            batch_size=args.batch_size, delay_seconds=args.delay_seconds,
            retry_unavailable=args.retry_unavailable,
        )
    except (MonthlyMappingError, requests.RequestException, ValueError) as exc:
        print(f"monthly_mapping_collector_error: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
