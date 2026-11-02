"""Collect TWSE auction announcement records and store in SQLite cb_auction table."""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "cb_history.db"
AUCTIONS_JSON_PATH = ROOT / "docs" / "data" / "v2" / "auctions.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_twse_auction_year(year: int) -> list[list[str]]:
    url = f"https://www.twse.com.tw/rwd/zh/announcement/auction?date={year}0101&response=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        print(f"Warning: Failed to fetch TWSE auction for {year}: {e}")
        return []


def init_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cb_auction (
            cb_code TEXT PRIMARY KEY,
            cb_name TEXT,
            underwriting_method TEXT NOT NULL,
            auction_date TEXT,
            bid_method TEXT,
            avg_price REAL,
            min_price REAL,
            max_price REAL,
            underwrite_price REAL,
            source_url TEXT,
            collected_at TEXT
        );
        """
    )


def safe_float(val: object) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def collect_auctions(start_year: int = 2018, end_year: int = 2026, db_path: Path = DB_PATH):
    sys.stdout.reconfigure(encoding="utf-8")
    now_str = utc_now()
    source_url = "https://www.twse.com.tw/zh/announcement/auction.html"

    print(f"Fetching TWSE auction announcements ({start_year}-{end_year})...")
    auction_rows_by_code: dict[str, dict] = {}

    for y in range(start_year, end_year + 1):
        rows = fetch_twse_auction_year(y)
        for r in rows:
            if len(r) < 25:
                continue
            code = str(r[3]).strip()
            name = str(r[2]).strip()
            issue_type = str(r[5]).strip()
            
            # Check if this is a convertible bond
            if len(code) == 5 or "債" in issue_type or "轉" in issue_type:
                auction_date = str(r[1]).strip()
                bid_method = str(r[6]).strip()
                min_p = safe_float(r[21])
                max_p = safe_float(r[22])
                avg_p = safe_float(r[23])
                und_p = safe_float(r[24])

                auction_rows_by_code[code] = {
                    "cb_code": code,
                    "cb_name": name,
                    "underwriting_method": "競拍",
                    "auction_date": auction_date,
                    "bid_method": bid_method,
                    "avg_price": avg_p,
                    "min_price": min_p,
                    "max_price": max_p,
                    "underwrite_price": und_p,
                    "source_url": source_url,
                    "collected_at": now_str,
                }

    print(f"Fetched {len(auction_rows_by_code)} CB auction records from TWSE.")

    with sqlite3.connect(db_path) as conn:
        init_table(conn)

        # Get all CB codes from cb_master
        master_rows = conn.execute("SELECT cb_code, cb_name FROM cb_master").fetchall()
        print(f"Processing {len(master_rows)} CBs from cb_master...")

        all_entries: dict[str, dict] = {}
        for code, name in master_rows:
            code = str(code).strip()
            name = str(name).strip() if name else ""

            if code in auction_rows_by_code:
                entry = auction_rows_by_code[code]
                entry["cb_name"] = name or entry["cb_name"]
            else:
                entry = {
                    "cb_code": code,
                    "cb_name": name,
                    "underwriting_method": "詢圈",
                    "auction_date": None,
                    "bid_method": None,
                    "avg_price": None,
                    "min_price": None,
                    "max_price": None,
                    "underwrite_price": None,
                    "source_url": source_url,
                    "collected_at": now_str,
                }
            all_entries[code] = entry

        # Also add any CB from auction_rows_by_code that might not be in cb_master
        for code, entry in auction_rows_by_code.items():
            if code not in all_entries:
                all_entries[code] = entry

        # Upsert into sqlite
        for entry in all_entries.values():
            conn.execute(
                """
                INSERT INTO cb_auction (
                    cb_code, cb_name, underwriting_method, auction_date,
                    bid_method, avg_price, min_price, max_price, underwrite_price,
                    source_url, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cb_code) DO UPDATE SET
                    cb_name = excluded.cb_name,
                    underwriting_method = excluded.underwriting_method,
                    auction_date = excluded.auction_date,
                    bid_method = excluded.bid_method,
                    avg_price = excluded.avg_price,
                    min_price = excluded.min_price,
                    max_price = excluded.max_price,
                    underwrite_price = excluded.underwrite_price,
                    source_url = excluded.source_url,
                    collected_at = excluded.collected_at
                """,
                (
                    entry["cb_code"],
                    entry["cb_name"],
                    entry["underwriting_method"],
                    entry["auction_date"],
                    entry["bid_method"],
                    entry["avg_price"],
                    entry["min_price"],
                    entry["max_price"],
                    entry["underwrite_price"],
                    entry["source_url"],
                    entry["collected_at"],
                ),
            )
        conn.commit()

        # Query summary
        counts = conn.execute(
            "SELECT underwriting_method, count(*), count(avg_price) FROM cb_auction GROUP BY underwriting_method"
        ).fetchall()
        print(f"Stored in SQLite cb_auction: {counts}")

    # Write auctions.json for frontend
    export_payload = {
        "schema_version": 2,
        "collected_at": now_str,
        "source": source_url,
        "records": all_entries,
    }
    AUCTIONS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUCTIONS_JSON_PATH.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(all_entries)} auction records to {AUCTIONS_JSON_PATH}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collect TWSE CB auctions")
    parser.add_argument("--database", type=Path, default=DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()
    collect_auctions(db_path=args.database)
