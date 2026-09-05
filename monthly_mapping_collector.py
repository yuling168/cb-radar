"""Verify month-level CB-to-parent mappings from official MOPS detail pages."""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS, MOPS_BASE_URL
from db import connect, upsert_parent_stock_monthly_mappings


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


def collect_monthly_verified_mappings(
    year_month: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    session: requests.Session | None = None,
    cb_codes: set[str] | None = None,
) -> dict[str, object]:
    """Use current master only as query candidates; MOPS must prove each mapping."""
    try:
        datetime.strptime(year_month, "%Y-%m")
    except ValueError as exc:
        raise ValueError("year_month must be YYYY-MM") from exc
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT cb_code, stock_code, stock_name, source_url FROM cb_master ORDER BY cb_code"
        ).fetchall()
    if cb_codes is not None:
        rows = [row for row in rows if str(row["cb_code"]) in cb_codes]
    if not rows:
        raise MonthlyMappingError("no CB master candidates for monthly verification")

    http = session or requests.Session()
    mappings = []
    for row in rows:
        cb_code = str(row["cb_code"])
        url = _month_url(_mops_detail_url(str(row["source_url"])), year_month)
        response = http.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        mappings.append(parse_mops_monthly_mapping(
            response.text, url, cb_code=cb_code, stock_code=str(row["stock_code"]),
            stock_name=str(row["stock_name"]),
        ))

    with connect(db_path) as connection:
        upsert_parent_stock_monthly_mappings(connection, mappings)
    return {"year_month": year_month, "verified": len(mappings), "database": str(db_path)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify monthly CB parent mappings from MOPS")
    parser.add_argument("--year-month", required=True, help="YYYY-MM")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cb-code", action="append", dest="cb_codes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = collect_monthly_verified_mappings(
            args.year_month, args.database, cb_codes=set(args.cb_codes or []) or None
        )
    except (MonthlyMappingError, requests.RequestException, ValueError) as exc:
        print(f"monthly_mapping_collector_error: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
