"""Integrated Stage 2 Backfill Pipeline: Parent Stock Quotes, Institutional Flows & Margin Trading."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import requests

# Add cb-radar root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS
from db import connect, active_parent_stock_codes_on
from stock_collector import collect_stock_daily_market
from institutional_collector import collect_institutional_daily

TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"


def ensure_margin_table(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_margin_daily (
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                market TEXT NOT NULL,
                margin_buy INTEGER,
                margin_sell INTEGER,
                margin_cash_repay INTEGER,
                margin_balance INTEGER,
                short_buy INTEGER,
                short_sell INTEGER,
                short_cash_repay INTEGER,
                short_balance INTEGER,
                source_url TEXT,
                collected_at TEXT,
                PRIMARY KEY (trade_date, stock_code)
            );
        """)
        conn.commit()


def _parse_int(val: object) -> int | None:
    if val is None:
        return None
    text = str(val).strip().replace(",", "")
    if not text or text in {"--", "---", "N/A"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def collect_margin_daily(trade_date: date, db_path: Path | str, target_stocks: set[str], session: requests.Session | None = None) -> int:
    http = session or requests.Session()
    collected_at = datetime.now(timezone.utc).isoformat()
    rows_to_insert = []

    # 1. TWSE Margin
    twse_date = trade_date.strftime("%Y%m%d")
    try:
        resp = http.get(TWSE_MARGIN_URL, params={"date": twse_date, "selectType": "ALL", "response": "json"}, timeout=HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("stat") == "OK":
                tables = payload.get("tables", [])
                for t in tables:
                    fields = t.get("fields", [])
                    data = t.get("data", [])
                    # Look for 融資融券彙總 table
                    if len(fields) >= 15 and "融資" in t.get("title", "") or "代號" in fields[0]:
                        for r in data:
                            code = str(r[0]).strip()
                            if code in target_stocks:
                                rows_to_insert.append((
                                    trade_date.isoformat(), code, str(r[1]).strip(), "TWSE",
                                    _parse_int(r[2]), _parse_int(r[3]), _parse_int(r[4]), _parse_int(r[6]),
                                    _parse_int(r[8]), _parse_int(r[9]), _parse_int(r[10]), _parse_int(r[12]),
                                    f"{TWSE_MARGIN_URL}?date={twse_date}", collected_at
                                ))
    except Exception as e:
        print(f"  [Margin TWSE {trade_date}] Error: {e}")

    # 2. TPEx Margin
    tpex_date = trade_date.strftime("%Y/%m/%d")
    try:
        resp = http.post(TPEX_MARGIN_URL, data={"date": tpex_date, "response": "json"}, timeout=HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("stat") == "ok":
                tables = payload.get("tables", [])
                if tables:
                    data = tables[0].get("data", [])
                    for r in data:
                        code = str(r[0]).strip()
                        if code in target_stocks and len(r) >= 16:
                            rows_to_insert.append((
                                trade_date.isoformat(), code, str(r[1]).strip(), "TPEX",
                                _parse_int(r[3]), _parse_int(r[4]), _parse_int(r[5]), _parse_int(r[6]),
                                _parse_int(r[11]), _parse_int(r[12]), _parse_int(r[13]), _parse_int(r[14]),
                                f"{TPEX_MARGIN_URL}?date={tpex_date}", collected_at
                            ))
    except Exception as e:
        print(f"  [Margin TPEx {trade_date}] Error: {e}")

    if rows_to_insert:
        with connect(db_path) as conn:
            conn.executemany("""
                INSERT INTO stock_margin_daily (
                    trade_date, stock_code, stock_name, market,
                    margin_buy, margin_sell, margin_cash_repay, margin_balance,
                    short_buy, short_sell, short_cash_repay, short_balance,
                    source_url, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                    margin_buy = excluded.margin_buy,
                    margin_sell = excluded.margin_sell,
                    margin_cash_repay = excluded.margin_cash_repay,
                    margin_balance = excluded.margin_balance,
                    short_buy = excluded.short_buy,
                    short_sell = excluded.short_sell,
                    short_cash_repay = excluded.short_cash_repay,
                    short_balance = excluded.short_balance,
                    collected_at = excluded.collected_at
            """, rows_to_insert)
            conn.commit()

    return len(rows_to_insert)


def get_all_target_stocks(db_path: Path | str, start_date: date, end_date: date) -> set[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock_code FROM cb_parent_stock_mapping WHERE mapping_date BETWEEN ? AND ?",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()
    return {str(r[0]) for r in rows if r[0]}


def run_stage2_pipeline(
    start_date: date,
    end_date: date,
    db_path: Path | str = DEFAULT_DB_PATH,
    delay_seconds: float = 1.2,
) -> None:
    print(f"=== Stage 2: Parent Stock Quotes, Institutional & Margin [{start_date} to {end_date}] ===")
    ensure_margin_table(db_path)

    # Get verified trading days from cb_daily
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()
        trading_dates = [date.fromisoformat(r[0]) for r in rows]

        # Check existing completion status
        completed_quotes = {
            date.fromisoformat(r[0]) for r in conn.execute(
                "SELECT trade_date, COUNT(*) FROM stock_daily_market WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date HAVING COUNT(*) > 100",
                (start_date.isoformat(), end_date.isoformat())
            ).fetchall()
        }
        completed_inst = {
            date.fromisoformat(r[0]) for r in conn.execute(
                "SELECT trade_date, COUNT(*) FROM institutional_daily WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date HAVING COUNT(*) > 50",
                (start_date.isoformat(), end_date.isoformat())
            ).fetchall()
        }
        completed_margin = {
            date.fromisoformat(r[0]) for r in conn.execute(
                "SELECT trade_date, COUNT(*) FROM stock_margin_daily WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date HAVING COUNT(*) > 50",
                (start_date.isoformat(), end_date.isoformat())
            ).fetchall()
        }

    print(f"Total CB trading dates to process: {len(trading_dates)}")
    print(f"Already completed Quotes: {len(completed_quotes)}, Inst: {len(completed_inst)}, Margin: {len(completed_margin)}")

    target_stocks = get_all_target_stocks(db_path, start_date, end_date)
    print(f"Total unique target stocks: {len(target_stocks)}")

    session = requests.Session()
    success_days = 0

    try:
        for idx, trade_date in enumerate(trading_dates, 1):
            needs_quote = trade_date not in completed_quotes
            needs_inst = trade_date not in completed_inst
            needs_margin = trade_date not in completed_margin

            if not (needs_quote or needs_inst or needs_margin):
                continue

            msg_parts = []
            # 1. Daily Market Quotes
            if needs_quote:
                try:
                    q_res = collect_stock_daily_market(trade_date, db_path=db_path, session=session)
                    msg_parts.append(f"Q:{q_res.get('records_inserted', 0)+q_res.get('records_updated', 0)}")
                except Exception as e:
                    msg_parts.append(f"Q:ERR({e})")

            # 2. Institutional Flows
            if needs_inst:
                try:
                    i_res = collect_institutional_daily(trade_date, db_path=db_path, session=session)
                    msg_parts.append(f"I:{i_res.get('records_inserted', 0)+i_res.get('records_updated', 0)}")
                except Exception as e:
                    msg_parts.append(f"I:ERR({e})")

            # 3. Margin Trading
            if needs_margin:
                try:
                    m_cnt = collect_margin_daily(trade_date, db_path=db_path, target_stocks=target_stocks, session=session)
                    msg_parts.append(f"M:{m_cnt}")
                except Exception as e:
                    msg_parts.append(f"M:ERR({e})")

            success_days += 1
            print(f"[{idx}/{len(trading_dates)}] {trade_date} -> {', '.join(msg_parts)}")
            time.sleep(delay_seconds)

    except KeyboardInterrupt:
        print("\nStage 2 interrupted by user. Saved checkpoint safely.")

    print(f"\n=== Stage 2 Finished: {success_days} trading dates processed ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill parent stock quotes, institutional and margin data.")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2025-05-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--delay", type=float, default=1.2, help="Delay between trading dates in seconds")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Database path")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    run_stage2_pipeline(start_date, end_date, db_path=args.db, delay_seconds=args.delay)


if __name__ == "__main__":
    main()
