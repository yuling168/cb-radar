import argparse
import csv
import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from config import (
    DEFAULT_DB_PATH,
    HTTP_TIMEOUT_SECONDS,
    LOOKBACK_DAYS,
    TPEX_BASE_URL,
    TPEX_REPORT_CODE,
    TPEX_REPORT_INDEX_URL,
    TPEX_SOURCE,
)
from db import connect, upsert_daily


EXPECTED_HEADER = [
    "代號",
    "名稱",
    "交易",
    "收市",
    "漲跌",
    "開市",
    "最高",
    "最低",
    "筆數",
    "單位",
    "金額",
    "均價",
    "明日參價",
    "明日漲停",
    "明日跌停",
]
EXCLUDED_VERIFICATION_CODES = {"16095", "17172", "62236"}


class DataNotPublished(Exception):
    """The requested official daily report is not listed yet."""


class TpexFormatError(RuntimeError):
    """TPEx returned a response whose required structure has changed."""


def _parse_number(value: Any, *, integer: bool = False) -> float | int | None:
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise TpexFormatError(f"Invalid numeric value: {value!r}") from exc
    if integer:
        if not number.is_integer():
            raise TpexFormatError(f"Expected an integer value: {value!r}")
        return int(number)
    return number


def volume_to_lots(value: Any, source_unit: str = "交易單位") -> int | None:
    """Convert a nonblank TPEx volume value to lots."""
    number = _parse_number(value, integer=True)
    if number is None:
        return None
    if number < 0:
        raise TpexFormatError("Volume cannot be negative")
    if source_unit in {"交易單位", "單位", "張"}:
        return number
    if source_unit in {"面額(元)", "元面額"}:
        if number % 100_000:
            raise TpexFormatError("CB par value is not divisible by NT$100,000")
        return number // 100_000
    raise TpexFormatError(f"Unsupported TPEx volume unit: {source_unit}")


def parse_tpex_csv(content: bytes, requested_date: date) -> list[dict[str, object]]:
    frame = _read_validated_csv(content, requested_date)
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[dict[str, object]] = []
    body = frame[frame[0] == "BODY"]
    for _, raw in body.iterrows():
        values = [str(value).strip() for value in raw.iloc[1:16]]
        cb_code, cb_name, trading_mode = values[:3]
        if trading_mode != "等價":
            continue
        if not cb_code or not cb_name:
            raise TpexFormatError("An equal-price row is missing CB code or name")
        # This conversion is valid only after the report, date, header and row
        # membership have all been verified by _read_validated_csv above.
        volume_lots = (
            0 if values[9] == "" else volume_to_lots(values[9], "交易單位")
        )
        records.append(
            {
                "trade_date": requested_date.isoformat(),
                "cb_code": cb_code,
                "cb_name": cb_name,
                "close_price": _parse_number(values[3]),
                "volume_lots": volume_lots,
                "source": TPEX_SOURCE,
                "collected_at": collected_at,
            }
        )
    return records


def _read_validated_csv(content: bytes, requested_date: date) -> pd.DataFrame:
    try:
        text = content.decode("cp950")
    except UnicodeDecodeError as exc:
        raise TpexFormatError("TPEx CSV is no longer valid CP950") from exc

    raw_rows = list(csv.reader(io.StringIO(text)))
    if not raw_rows or max(len(row) for row in raw_rows) > 16:
        raise TpexFormatError("TPEx CSV contains an invalid column count")
    # Metadata and data rows have different widths. Normalize first, then use a
    # DataFrame for explicit filtering without letting pandas infer nulls/types.
    frame = pd.DataFrame([row + [""] * (16 - len(row)) for row in raw_rows])
    header_rows = frame[frame[0] == "HEADER"]
    if len(header_rows) != 1:
        raise TpexFormatError("TPEx CSV must contain exactly one HEADER row")
    actual_header = [str(value).strip() for value in header_rows.iloc[0, 1:16]]
    if actual_header != EXPECTED_HEADER:
        raise TpexFormatError(
            f"TPEx required fields changed: expected {EXPECTED_HEADER}, got {actual_header}"
        )

    date_rows = frame[frame[0] == "DATADATE"]
    expected_roc = f"日期:{requested_date.year - 1911}年{requested_date:%m月%d日}"
    if len(date_rows) != 1 or str(date_rows.iloc[0, 1]).strip() != expected_roc:
        raise TpexFormatError("TPEx CSV date does not match the requested trade date")

    return frame


def select_verification_rows(
    content: bytes, requested_date: date
) -> list[dict[str, object]]:
    frame = _read_validated_csv(content, requested_date)
    positive: list[dict[str, object]] = []
    blank: list[dict[str, object]] = []
    seen_positive_volumes: set[int] = set()
    for _, raw in frame[frame[0] == "BODY"].iterrows():
        values = [str(value).strip() for value in raw.iloc[1:16]]
        cb_code, cb_name, trading_mode = values[:3]
        if trading_mode != "等價" or cb_code in EXCLUDED_VERIFICATION_CODES:
            continue
        raw_volume = values[9]
        saved_volume = 0 if raw_volume == "" else volume_to_lots(raw_volume)
        row = {
            "trade_date": requested_date.isoformat(),
            "cb_code": cb_code,
            "cb_name": cb_name,
            "close_price": _parse_number(values[3]),
            "volume_lots": saved_volume,
            "raw_volume_value": raw_volume,
        }
        if saved_volume and saved_volume not in seen_positive_volumes and len(positive) < 3:
            positive.append(row)
            seen_positive_volumes.add(saved_volume)
        elif raw_volume == "" and len(blank) < 2:
            blank.append(row)
        if len(positive) == 3 and len(blank) == 2:
            break
    if len(positive) != 3 or len(blank) != 2:
        raise TpexFormatError("Unable to select the required five verification CBs")
    return positive + blank


def _roc_date_to_date(value: str) -> date:
    try:
        year, month, day = (int(part) for part in value.split("/"))
        return date(year + 1911, month, day)
    except (TypeError, ValueError) as exc:
        raise TpexFormatError(f"Invalid ROC date in TPEx response: {value!r}") from exc


def fetch_report_listing(session: requests.Session, query_date: date) -> dict[date, str]:
    response = session.get(
        TPEX_REPORT_INDEX_URL,
        params={"date": query_date.strftime("%Y/%m/%d"), "fileCode": TPEX_REPORT_CODE},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        payload = response.json()
        table = payload["tables"][0]
        rows = table["data"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise TpexFormatError("TPEx report index structure changed") from exc
    if payload.get("stat") != "ok" or table.get("fields") != ["資料日期", "檔案下載"]:
        raise TpexFormatError("TPEx report index required fields changed")
    listing: dict[date, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            raise TpexFormatError("TPEx report index contains a malformed row")
        listing[_roc_date_to_date(row[0])] = row[1]
    return listing


def resolve_report(
    session: requests.Session, requested_date: date, latest_available: bool
) -> tuple[date, str]:
    month_cache: dict[tuple[int, int], dict[date, str]] = {}
    days = range(LOOKBACK_DAYS + 1) if latest_available else range(1)
    for offset in days:
        candidate = requested_date - timedelta(days=offset)
        month = (candidate.year, candidate.month)
        if month not in month_cache:
            month_cache[month] = fetch_report_listing(session, candidate)
        path = month_cache[month].get(candidate)
        if path:
            return candidate, path
    raise DataNotPublished(
        f"No TPEx RSta0113 report published for {requested_date.isoformat()}"
    )


def collect(
    requested_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    latest_available: bool = False,
    session: requests.Session | None = None,
    write: bool = True,
) -> dict[str, object]:
    http = session or requests.Session()
    http.headers.update({"User-Agent": "cb-radar/0.1 (TPEx daily collector)"})
    trade_date, report_path = resolve_report(http, requested_date, latest_available)
    response = http.get(f"{TPEX_BASE_URL}{report_path}", timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    frame = _read_validated_csv(response.content, trade_date)
    body = frame[frame[0] == "BODY"]
    equivalent = body[body[3].astype(str).str.strip() == "等價"]
    records = parse_tpex_csv(response.content, trade_date)
    verification = select_verification_rows(response.content, trade_date)
    if not records:
        raise TpexFormatError("Published TPEx report contained no equivalent-market CB rows")
    if not write:
        return {
            "trade_date": trade_date.isoformat(),
            "official_rows": len(body),
            "equivalent_market_rows": len(records),
            "records_inserted": 0,
            "records_updated": 0,
            "volume_positive_count": sum(row["volume_lots"] > 0 for row in records),
            "volume_zero_count": sum(row["volume_lots"] == 0 for row in records),
            "close_price_null_count": sum(row["close_price"] is None for row in records),
            "database": str(db_path),
            "verification": verification,
        }
    with connect(db_path) as connection:
        inserted, updated = upsert_daily(connection, records)
        for row in verification:
            saved = connection.execute(
                """
                SELECT close_price, volume_lots FROM cb_daily
                WHERE trade_date = ? AND cb_code = ?
                """,
                (row["trade_date"], row["cb_code"]),
            ).fetchone()
            if saved is None:
                raise TpexFormatError("A verification CB was not saved to SQLite")
            row["close_price"] = saved["close_price"]
            row["volume_lots"] = saved["volume_lots"]
    return {
        "trade_date": trade_date.isoformat(),
        "official_rows": len(body),
        "equivalent_market_rows": len(records),
        "records_inserted": inserted,
        "records_updated": updated,
        "volume_positive_count": sum(row["volume_lots"] > 0 for row in records),
        "volume_zero_count": sum(row["volume_lots"] == 0 for row in records),
        "close_price_null_count": sum(row["close_price"] is None for row in records),
        "database": str(db_path),
        "verification": verification,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect TPEx daily CB quotes into SQLite")
    parser.add_argument("--date", type=date.fromisoformat, help="trade date (YYYY-MM-DD)")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested = args.date or date.today()
    try:
        result = collect(
            requested, args.database, latest_available=args.date is None
        )
    except DataNotPublished as exc:
        print(f"data_not_published: {exc}")
        return 0
    except (requests.RequestException, TpexFormatError) as exc:
        print(f"collector_error: {exc}", file=sys.stderr)
        return 1

    for key in (
        "trade_date",
        "official_rows",
        "equivalent_market_rows",
        "records_inserted",
        "records_updated",
        "volume_positive_count",
        "volume_zero_count",
        "close_price_null_count",
        "database",
    ):
        print(f"{key}: {result[key]}")
    print("verification:")
    for row in result["verification"]:
        close = "NULL" if row["close_price"] is None else row["close_price"]
        raw_volume = row["raw_volume_value"] or "空白"
        print(
            f"  trade_date={row['trade_date']}, cb_code={row['cb_code']}, "
            f"cb_name={row['cb_name']}, close_price={close}, "
            f"volume_lots={row['volume_lots']}, raw_volume_value={raw_volume}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
