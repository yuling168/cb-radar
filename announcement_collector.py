"""Collect and preserve official daily company material-information feeds.

This module deliberately stores announcements only.  Classification, alerts and
CB lifecycle handling are separate future consumers of this history layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS
from db import connect


SOURCES = {
    "TWSE": {
        "url": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
        "batch_date": "出表日期",
        "company_code": "公司代號",
        "company_name": "公司名稱",
        "subject": "主旨 ",
    },
    "TPEX": {
        "url": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
        "batch_date": "Date",
        "company_code": "SecuritiesCompanyCode",
        "company_name": "CompanyName",
        "subject": "主旨",
    },
}
COMMON_FIELDS = ("發言日期", "發言時間", "符合條款", "事實發生日", "說明")


class AnnouncementSourceError(RuntimeError):
    """An official source did not return a complete, valid daily feed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "CB-Radar/1.0"})
    return session


def normalize_text(value: object) -> str:
    if not isinstance(value, str):
        raise AnnouncementSourceError("announcement field must be a string")
    return "\n".join(
        line.strip() for line in unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


def roc_date(value: object, field: str, *, nullable: bool = False) -> tuple[str | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if nullable:
            return None, None
        raise AnnouncementSourceError(f"{field} is missing")
    raw = normalize_text(value).replace("/", "")
    if len(raw) != 7 or not raw.isdigit():
        raise AnnouncementSourceError(f"{field} is not a YYYYMMDD ROC date: {value!r}")
    try:
        normalized = f"{int(raw[:3]) + 1911:04d}-{raw[3:5]}-{raw[5:]}"
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise AnnouncementSourceError(f"{field} is invalid: {value!r}") from exc
    return raw, normalized


def normalize_time(value: object) -> str:
    raw = normalize_text(value)
    if not raw.isdigit() or len(raw) > 6:
        raise AnnouncementSourceError(f"發言時間 is invalid: {value!r}")
    raw = raw.zfill(6)
    try:
        datetime.strptime(raw, "%H%M%S")
    except ValueError as exc:
        raise AnnouncementSourceError(f"發言時間 is invalid: {value!r}") from exc
    return f"{raw[:2]}:{raw[2:4]}:{raw[4:]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_payload(source_market: str, payload: Any) -> tuple[str, str, list[dict[str, object]]]:
    if source_market not in SOURCES:
        raise ValueError(f"Unsupported source market: {source_market}")
    if not isinstance(payload, list):
        raise AnnouncementSourceError("official response is not a JSON array")
    source = SOURCES[source_market]
    batch_dates: set[tuple[str, str]] = set()
    rows: list[dict[str, object]] = []
    required = (source["batch_date"], source["company_code"], source["company_name"], source["subject"], *COMMON_FIELDS)
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise AnnouncementSourceError(f"row {index} is not an object")
        missing = [field for field in required if field not in row]
        if missing:
            raise AnnouncementSourceError(f"row {index} is missing fields: {', '.join(missing)}")
        batch_dates.add(roc_date(row[source["batch_date"]], source["batch_date"]))
        rows.append(dict(row))
    if len(batch_dates) != 1:
        raise AnnouncementSourceError("official response has zero or inconsistent batch dates")
    return (*batch_dates.pop(), rows)


def start_fetch(connection: sqlite3.Connection, source_market: str, requested_at: str) -> int:
    cursor = connection.execute(
        "INSERT INTO announcement_fetch (source_market, requested_at, status) VALUES (?, ?, 'started')",
        (source_market, requested_at),
    )
    connection.commit()
    return int(cursor.lastrowid)


def fail_fetch(connection: sqlite3.Connection, fetch_id: int, error: Exception, http_status: int | None = None) -> None:
    connection.execute(
        """UPDATE announcement_fetch
           SET status = 'failed', completed_at = ?, http_status = ?, error_message = ?
           WHERE fetch_id = ?""",
        (utc_now(), http_status, str(error), fetch_id),
    )
    connection.commit()


def persist_success(
    connection: sqlite3.Connection,
    fetch_id: int,
    source_market: str,
    raw_json: str,
    payload: Any,
    http_status: int,
) -> dict[str, object]:
    batch_date_roc, batch_date, rows = parse_payload(source_market, payload)
    now = utc_now()
    payload_sha256 = sha256_text(raw_json)
    source = SOURCES[source_market]
    with connection:
        connection.execute(
            """INSERT INTO announcement_snapshot
               (fetch_id, source_market, api_batch_date_roc, api_batch_date, payload_sha256, raw_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_market, api_batch_date, payload_sha256) DO NOTHING""",
            (fetch_id, source_market, batch_date_roc, batch_date, payload_sha256, raw_json, now),
        )
        snapshot = connection.execute(
            """SELECT snapshot_id FROM announcement_snapshot
               WHERE source_market = ? AND api_batch_date = ? AND payload_sha256 = ?""",
            (source_market, batch_date, payload_sha256),
        ).fetchone()
        snapshot_id = int(snapshot[0])
        inserts = updates = 0
        for row in rows:
            company_code = normalize_text(row[source["company_code"]])
            company_name = normalize_text(row[source["company_name"]])
            subject = normalize_text(row[source["subject"]])
            body = normalize_text(row["說明"])
            clause = normalize_text(row["符合條款"])
            if not all((company_code, company_name, subject, body, clause)):
                raise AnnouncementSourceError("required announcement content is blank")
            spoken_roc, spoken_date = roc_date(row["發言日期"], "發言日期")
            fact_roc, fact_date = roc_date(row["事實發生日"], "事實發生日", nullable=True)
            spoken_time = normalize_time(row["發言時間"])
            subject_sha256, body_sha256 = sha256_text(subject), sha256_text(body)
            logical_key = sha256_text("\x1f".join((source_market, company_code, spoken_date, spoken_time, subject_sha256)))
            event_key = sha256_text("\x1f".join((logical_key, body_sha256)))
            exists = connection.execute("SELECT 1 FROM company_announcements WHERE event_key = ?", (event_key,)).fetchone() is not None
            connection.execute(
                """INSERT INTO company_announcements (
                    source_market, company_code, company_name, api_batch_date_roc, api_batch_date,
                    spoken_date_roc, spoken_date, spoken_time, fact_date_roc, fact_date, clause,
                    subject, body, subject_sha256, body_sha256, logical_key, event_key,
                    first_seen_at, last_seen_at, first_snapshot_id, last_snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_snapshot_id = excluded.last_snapshot_id""",
                (source_market, company_code, company_name, batch_date_roc, batch_date,
                 spoken_roc, spoken_date, spoken_time, fact_roc, fact_date, clause,
                 subject, body, subject_sha256, body_sha256, logical_key, event_key,
                 now, now, snapshot_id, snapshot_id),
            )
            if exists:
                updates += 1
            else:
                inserts += 1
        connection.execute(
            """UPDATE announcement_fetch
               SET status = 'succeeded', completed_at = ?, http_status = ?,
                   api_batch_date_roc = ?, api_batch_date = ?, row_count = ?,
                   payload_sha256 = ?, snapshot_id = ?
               WHERE fetch_id = ?""",
            (now, http_status, batch_date_roc, batch_date, len(rows), payload_sha256, snapshot_id, fetch_id),
        )
    return {"source_market": source_market, "batch_date": batch_date, "rows": len(rows), "inserted": inserts, "updated": updates, "snapshot_id": snapshot_id}


def collect_market(source_market: str, db_path: Path | str = DEFAULT_DB_PATH, session: requests.Session | None = None, max_attempts: int = 3) -> dict[str, object]:
    """Collect one market independently; each failed retry remains auditable."""
    if source_market not in SOURCES:
        raise ValueError(f"Unsupported source market: {source_market}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    session = session or build_session()
    last_error: Exception | None = None
    with connect(db_path) as connection:
        for _ in range(max_attempts):
            fetch_id = start_fetch(connection, source_market, utc_now())
            response = None
            try:
                response = session.get(SOURCES[source_market]["url"], timeout=HTTP_TIMEOUT_SECONDS)
                response.raise_for_status()
                raw_json = response.text
                payload = response.json()
                result = persist_success(connection, fetch_id, source_market, raw_json, payload, int(response.status_code))
                result["fetch_id"] = fetch_id
                return result
            except (requests.RequestException, ValueError, json.JSONDecodeError, AnnouncementSourceError) as exc:
                last_error = exc
                fail_fetch(connection, fetch_id, exc, getattr(response, "status_code", None))
    raise AnnouncementSourceError(f"{source_market} failed after {max_attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preserve official daily material-information feeds")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--market", choices=("TWSE", "TPEX", "all"), default="all")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    markets = tuple(SOURCES) if args.market == "all" else (args.market,)
    results = []
    failures = []
    for market in markets:
        try:
            results.append(collect_market(market, args.db, max_attempts=args.max_attempts))
        except AnnouncementSourceError as exc:
            failures.append(str(exc))
    print(json.dumps({"results": results, "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
