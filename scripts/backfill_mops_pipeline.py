"""Stage 4 Pipeline: Backfill MOPS Monthly Balance & Conversion Price Events."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

# Add cb-radar root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS, MOPS_BASE_URL
from db import connect
from master_collector import parse_mops_snapshot, _fallback_mops_url


def ensure_cb_in_master(conn, cb_code: str, stock_code: str, snap: dict[str, object]) -> None:
    # Check if cb_code is in cb_master
    exists = conn.execute("SELECT 1 FROM cb_master WHERE cb_code = ?", (cb_code,)).fetchone()
    if exists:
        return

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO cb_master (
            cb_code, cb_name, stock_code, stock_name,
            issue_date, maturity_date, put_date, issue_units,
            issue_amount, balance_amount, balance_date,
            current_conversion_price, current_conversion_price_effective_date,
            is_secured, delisting_date, delisting_reason,
            source, source_url, collected_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?
        )
    """, (
        cb_code, str(snap.get("official_name", cb_code)), stock_code, str(snap.get("official_name", cb_code))[:4],
        str(snap.get("issue_date", "2024-01-01")), str(snap.get("maturity_date", "2027-01-01")), None, snap.get("issue_units"),
        snap.get("issue_amount") or 100000000, snap.get("balance_amount"), snap.get("year_month"),
        snap.get("conversion_price"), snap.get("effective_date"),
        1, None, None,
        "MOPS:t120sg01", str(snap.get("source_url", "")), now
    ))
    conn.commit()


def backfill_mops_for_cb(
    cb_code: str,
    stock_code: str,
    series_num: int,
    months: list[str],
    db_path: Path | str,
    session: requests.Session,
    delay_seconds: float = 1.0,
    max_retries: int = 3,
) -> dict[str, int]:
    master = {
        "cb_code": cb_code,
        "stock_code": stock_code,
        "series_number": series_num,
    }

    inserted_balances = 0
    inserted_prices = 0

    with connect(db_path) as conn:
        existing_months = {
            r[0] for r in conn.execute(
                "SELECT year_month FROM cb_monthly_balance WHERE cb_code = ?", (cb_code,)
            ).fetchall()
        }

    for ym in months:
        if ym in existing_months:
            continue

        url = _fallback_mops_url(master, ym)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        for attempt in range(1, max_retries + 1):
            try:
                resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
                if resp.status_code == 429 or resp.status_code == 403:
                    print(f"  [MOPS {cb_code} {ym}] Rate limited ({resp.status_code}). Backing off 30s...")
                    time.sleep(30)
                    continue

                if resp.status_code != 200:
                    break

                text = resp.text
                if "之轉(交)換公司債發行資料" not in text:
                    # Not yet issued or not available
                    break

                snap = parse_mops_snapshot(text, url)
                now = datetime.now(timezone.utc).isoformat()

                with connect(db_path) as conn:
                    ensure_cb_in_master(conn, cb_code, stock_code, snap)

                    # Insert balance
                    if snap.get("balance_amount") is not None:
                        conn.execute("""
                            INSERT INTO cb_monthly_balance (
                                cb_code, year_month, balance_amount, source, source_url, collected_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(cb_code, year_month) DO UPDATE SET
                                balance_amount = excluded.balance_amount,
                                collected_at = excluded.collected_at
                        """, (
                            cb_code, ym, snap["balance_amount"], "MOPS:t120sg01", url, now
                        ))
                        inserted_balances += 1

                    # Insert conversion price event
                    if snap.get("conversion_price") is not None and snap.get("effective_date") is not None:
                        conn.execute("""
                            INSERT INTO conversion_price_events (
                                cb_code, effective_date, conversion_price, source, source_url, collected_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(cb_code, effective_date) DO UPDATE SET
                                conversion_price = excluded.conversion_price,
                                collected_at = excluded.collected_at
                        """, (
                            cb_code, snap["effective_date"], snap["conversion_price"], "MOPS:t120sg01", url, now
                        ))
                        inserted_prices += 1

                    conn.commit()

                time.sleep(delay_seconds)
                break

            except Exception as e:
                if attempt == max_retries:
                    # print(f"  [MOPS {cb_code} {ym}] Failed after {max_retries} attempts: {e}")
                    pass
                else:
                    time.sleep(attempt * 2)

    return {"balances": inserted_balances, "prices": inserted_prices}


def run_mops_pipeline(
    db_path: Path | str = DEFAULT_DB_PATH,
    delay_seconds: float = 1.0,
    priority_cb: str = "64251",
) -> None:
    print(f"=== Stage 4: MOPS Monthly Balance & Conversion Price Backfill ===")

    # Generate months from 2024-01 to 2026-10
    months = []
    for y in range(2024, 2027):
        for m in range(1, 13):
            if y == 2026 and m > 10:
                break
            months.append(f"{y:04d}-{m:02d}")

    # 1. First priority: Priority CB (易發一 64251)
    session = requests.Session()
    print(f"\n>>> Priority Execution: {priority_cb} (易發一) <<<")
    res_priority = backfill_mops_for_cb(
        priority_cb, "6425", 1, months, db_path, session, delay_seconds=delay_seconds
    )
    print(f"Priority CB {priority_cb} done: {res_priority['balances']} balances, {res_priority['prices']} conversion price events saved.")

    # 2. Get all other CBs from cb_daily
    with connect(db_path) as conn:
        cb_rows = conn.execute("""
            SELECT DISTINCT cb_code, cb_name 
            FROM cb_daily 
            WHERE trade_date BETWEEN '2024-01-01' AND '2025-05-31'
            ORDER BY cb_code
        """).fetchall()

    print(f"\nTotal CBs in universe: {len(cb_rows)}")
    cb_list = []
    for r in cb_rows:
        code = str(r[0])
        if code == priority_cb:
            continue
        # Infer series number
        series_match = re.search(r"\d$", code)
        series_num = int(series_match.group(0)) if series_match else 1
        stock_code = code[:4]
        cb_list.append((code, stock_code, series_num))

    # Months for all other CBs: 2024-01 to 2025-05
    target_months = [m for m in months if m <= "2025-05"]

    total_cb_count = len(cb_list)
    total_balances = res_priority["balances"]
    total_prices = res_priority["prices"]

    for idx, (cb_code, stock_code, series_num) in enumerate(cb_list, 1):
        res = backfill_mops_for_cb(
            cb_code, stock_code, series_num, target_months, db_path, session, delay_seconds=delay_seconds
        )
        total_balances += res["balances"]
        total_prices += res["prices"]
        if res["balances"] > 0 or res["prices"] > 0 or idx % 10 == 0:
            print(f"[{idx}/{total_cb_count}] CB {cb_code}: +{res['balances']} balances, +{res['prices']} prices (Cumulative: {total_balances} balances, {total_prices} prices)")

    print(f"\n=== Stage 4 Finished: Total {total_balances} balances, {total_prices} conversion events upserted ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill MOPS balances and conversion prices.")
    parser.add_argument("--priority", default="64251", help="Priority CB code")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay in seconds")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DB path")
    args = parser.parse_args()

    run_mops_pipeline(db_path=args.db, delay_seconds=args.delay, priority_cb=args.priority)


if __name__ == "__main__":
    main()
