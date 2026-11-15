"""Stage 3: Collect Monthly Revenue (營業收入彙總表) from MOPS for CB Parent Stocks."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import requests

# Add cb-radar root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS
from db import connect

MOPS_URLS = [
    ("https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{roc_year}_{month}_0.html", "TWSE"),
    ("https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{roc_year}_{month}_1.html", "TWSE"),
    ("https://mopsov.twse.com.tw/nas/t21/otc/t21sc03_{roc_year}_{month}_0.html", "TPEx"),
    ("https://mopsov.twse.com.tw/nas/t21/otc/t21sc03_{roc_year}_{month}_1.html", "TPEx"),
]


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_td = False
        self.current_row = []
        self.rows = []
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_td = True
            self.current_text = []
        elif tag == "tr":
            self.current_row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.in_td = False
            self.current_row.append("".join(self.current_text).strip().replace(",", ""))
        elif tag == "tr":
            if self.current_row:
                self.rows.append(self.current_row)

    def handle_data(self, data):
        if self.in_td:
            self.current_text.append(data)


def init_revenue_table(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_monthly_revenue (
                year_month TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                market TEXT NOT NULL,
                revenue_curr_month INTEGER,
                revenue_prev_month INTEGER,
                revenue_prev_year INTEGER,
                mom_growth_pct REAL,
                yoy_growth_pct REAL,
                cum_curr_year INTEGER,
                cum_prev_year INTEGER,
                cum_yoy_growth_pct REAL,
                comment TEXT,
                source_url TEXT,
                collected_at TEXT,
                PRIMARY KEY (year_month, stock_code)
            );
        """)
        conn.commit()


def _parse_int(val: str) -> int | None:
    text = str(val).strip().replace(",", "")
    if not text or text in {"--", "---", "不適用", "N/A"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_float(val: str) -> float | None:
    text = str(val).strip().replace(",", "")
    if not text or text in {"--", "---", "不適用", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_and_parse_revenue_market(
    url: str,
    market: str,
    year_month: str,
    target_stocks: set[str],
    session: requests.Session,
) -> list[tuple]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        return []

    content = resp.content.decode("cp950", errors="ignore")
    parser = TableParser()
    parser.feed(content)

    now = datetime.now(timezone.utc).isoformat()
    records = []

    for r in parser.rows:
        if len(r) >= 10 and re.match(r"^\d{4,5}$", r[0]):
            code = r[0]
            if target_stocks and code not in target_stocks:
                continue

            name = r[1]
            rev_curr = _parse_int(r[2])
            rev_prev_m = _parse_int(r[3])
            rev_prev_y = _parse_int(r[4])
            mom = _parse_float(r[5])
            yoy = _parse_float(r[6])
            cum_curr = _parse_int(r[7])
            cum_prev = _parse_int(r[8])
            cum_yoy = _parse_float(r[9])
            comment = r[10] if len(r) > 10 else None

            records.append((
                year_month, code, name, market,
                rev_curr, rev_prev_m, rev_prev_y, mom, yoy,
                cum_curr, cum_prev, cum_yoy, comment, url, now
            ))

    return records


def run_revenue_pipeline(
    start_ym: str = "2024-01",
    end_ym: str = "2025-05",
    db_path: Path | str = DEFAULT_DB_PATH,
    delay_seconds: float = 1.5,
) -> None:
    print(f"=== Stage 3: Monthly Revenue Backfill [{start_ym} to {end_ym}] ===")
    init_revenue_table(db_path)

    # Get target stocks from both mapping and master
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT DISTINCT stock_code FROM cb_parent_stock_mapping WHERE stock_code IS NOT NULL
            UNION
            SELECT DISTINCT stock_code FROM cb_master WHERE stock_code IS NOT NULL
        """).fetchall()
        target_stocks = {str(r[0]) for r in rows if r[0]}

    print(f"Target stocks count: {len(target_stocks)}")

    # Generate year-month list
    start_y, start_m = map(int, start_ym.split("-"))
    end_y, end_m = map(int, end_ym.split("-"))

    ym_list = []
    cy, cm = start_y, start_m
    while (cy, cm) <= (end_y, end_m):
        ym_list.append(f"{cy:04d}-{cm:02d}")
        cm += 1
        if cm > 12:
            cm = 1
            cy += 1

    session = requests.Session()
    total_saved = 0

    for idx, ym in enumerate(ym_list, 1):
        y, m = map(int, ym.split("-"))
        roc_year = y - 1911

        # Check existing count for this month (410+ domestic + KY)
        with connect(db_path) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM stock_monthly_revenue WHERE year_month=?", (ym,)).fetchone()[0]
        if cnt >= 420:
            print(f"[{idx}/{len(ym_list)}] {ym} already completed ({cnt} records). Skipping.")
            continue

        try:
            all_records = []
            for tmpl, mkt in MOPS_URLS:
                u = tmpl.format(roc_year=roc_year, month=m)
                recs = fetch_and_parse_revenue_market(u, mkt, ym, target_stocks, session)
                all_records.extend(recs)
                time.sleep(delay_seconds)

            if all_records:
                with connect(db_path) as conn:
                    conn.executemany("""
                        INSERT INTO stock_monthly_revenue (
                            year_month, stock_code, stock_name, market,
                            revenue_curr_month, revenue_prev_month, revenue_prev_year,
                            mom_growth_pct, yoy_growth_pct,
                            cum_curr_year, cum_prev_year, cum_yoy_growth_pct,
                            comment, source_url, collected_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(year_month, stock_code) DO UPDATE SET
                            revenue_curr_month = excluded.revenue_curr_month,
                            revenue_prev_month = excluded.revenue_prev_month,
                            revenue_prev_year = excluded.revenue_prev_year,
                            mom_growth_pct = excluded.mom_growth_pct,
                            yoy_growth_pct = excluded.yoy_growth_pct,
                            cum_curr_year = excluded.cum_curr_year,
                            cum_prev_year = excluded.cum_prev_year,
                            cum_yoy_growth_pct = excluded.cum_yoy_growth_pct,
                            comment = excluded.comment,
                            collected_at = excluded.collected_at
                    """, all_records)
                    conn.commit()

                total_saved += len(all_records)
                print(f"[{idx}/{len(ym_list)}] {ym} -> Saved {len(all_records)} revenue records across domestic + KY")
            else:
                print(f"[{idx}/{len(ym_list)}] {ym} -> No records found.")

        except Exception as e:
            print(f"[{idx}/{len(ym_list)}] {ym} -> ERROR: {e}")

    print(f"\n=== Stage 3 Finished: Total {total_saved} monthly revenue records upserted ===")


def main():
    parser = argparse.ArgumentParser(description="Backfill monthly revenue from MOPS.")
    parser.add_argument("--start", default="2024-01", help="Start YYYY-MM")
    parser.add_argument("--end", default="2025-05", help="End YYYY-MM")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay in seconds")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DB path")
    args = parser.parse_args()

    run_revenue_pipeline(args.start, args.end, db_path=args.db, delay_seconds=args.delay)


if __name__ == "__main__":
    main()
