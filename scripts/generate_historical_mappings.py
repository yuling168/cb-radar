"""Generate exact-date parent stock mappings for historical CB trading days."""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "cb_history.db"
TARGETS_PATH = BASE_DIR / "data" / "target_cbs.csv"


def clean_stock_name(cb_name: str) -> str:
    name = re.sub(r"(一|二|三|四|五|六|七|八|九|十|\d+A|\d+B|E\d+|\d+)(永|KY|創)?$", "", cb_name)
    name = re.sub(r"(永|KY|創)$", "", name)
    if "KY" in cb_name and not name.endswith("-KY"):
        name = name + "-KY"
    return name


def build_historical_mappings(db_path: Path = DB_PATH) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1. Base resolution dictionaries
    master = {
        row["cb_code"]: (row["stock_code"], row["stock_name"])
        for row in cur.execute("SELECT cb_code, stock_code, stock_name FROM cb_master")
    }

    targets = {}
    if TARGETS_PATH.exists():
        with open(TARGETS_PATH, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                targets[r["代稱"]] = r["名稱"]

    # 2. Get all distinct (cb_code, cb_name) in cb_daily
    daily_cbs = dict(cur.execute("SELECT cb_code, cb_name FROM cb_daily GROUP BY cb_code").fetchall())

    def resolve_cb(cb_code: str) -> tuple[str, str]:
        if cb_code in master:
            return master[cb_code]
        cb_name = targets.get(cb_code) or daily_cbs.get(cb_code) or ""
        stock_code = cb_code[:4]
        stock_name = clean_stock_name(cb_name) if cb_name else stock_code
        return stock_code, stock_name

    # 3. Find missing (cb_code, trade_date) pairs in cb_parent_stock_mapping
    print("Scanning cb_daily for missing parent stock mappings...")
    missing_rows = cur.execute("""
        SELECT daily.cb_code, daily.trade_date
        FROM cb_daily AS daily
        LEFT JOIN cb_parent_stock_mapping AS mapping
          ON mapping.cb_code = daily.cb_code
         AND mapping.mapping_date = daily.trade_date
        WHERE mapping.stock_code IS NULL
    """).fetchall()

    print(f"Found {len(missing_rows)} missing daily mapping records.")
    if not missing_rows:
        con.close()
        return 0

    verified_at = datetime.now(timezone.utc).isoformat()
    mappings_to_insert = []
    for row in missing_rows:
        cb_code = row["cb_code"]
        trade_date = row["trade_date"]
        stock_code, stock_name = resolve_cb(cb_code)
        mappings_to_insert.append((
            cb_code,
            trade_date,
            stock_code,
            stock_name,
            "UNKNOWN",
            "TPEx:historical_mapping",
            "https://www.tpex.org.tw/www/zh-tw/bond/cbDaily",
            verified_at,
        ))

    print(f"Inserting {len(mappings_to_insert)} mapping records into cb_parent_stock_mapping...")
    cur.executemany("""
        INSERT OR IGNORE INTO cb_parent_stock_mapping (
            cb_code, mapping_date, stock_code, stock_name, market, source, source_url, verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, mappings_to_insert)
    con.commit()
    inserted = cur.rowcount
    con.close()
    print("Mappings generated successfully.")
    return len(mappings_to_insert)


if __name__ == "__main__":
    build_historical_mappings()
