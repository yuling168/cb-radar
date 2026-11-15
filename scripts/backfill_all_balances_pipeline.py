"""Comprehensive MOPS Monthly Balance & Conversion Price Backfill Pipeline.

Backfills missing historical monthly balances (cb_monthly_balance) and conversion
price events (conversion_price_events) for all CBs across the entire active date range.
Enforces safe rate-limiting (0.8s default) and exponential backoff on HTTP 429/403.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_DB_PATH, HTTP_TIMEOUT_SECONDS, MOPS_BASE_URL
from db import connect
from master_collector import parse_mops_snapshot, _fallback_mops_url
from scripts.build_dashboard import build_dashboard_shards


def get_missing_pairs(
    db_path: Path | str,
    start_ym: str | None = None,
    end_ym: str | None = None,
) -> list[tuple[str, str, str, int]]:
    """Return distinct (cb_code, stock_code, year_month, series_num) missing from cb_monthly_balance."""
    with connect(db_path) as conn:
        conditions = ["1=1"]
        params: list[object] = []
        if start_ym:
            conditions.append("SUBSTR(d.trade_date, 1, 7) >= ?")
            params.append(start_ym)
        if end_ym:
            conditions.append("SUBSTR(d.trade_date, 1, 7) <= ?")
            params.append(end_ym)

        query = f"""
            SELECT DISTINCT d.cb_code, m.stock_code, SUBSTR(d.trade_date, 1, 7) AS ym
            FROM cb_daily AS d
            JOIN cb_master AS m ON m.cb_code = d.cb_code
            WHERE {' AND '.join(conditions)}
              AND NOT EXISTS (
                  SELECT 1 FROM cb_monthly_balance AS b
                  WHERE b.cb_code = d.cb_code AND b.year_month = SUBSTR(d.trade_date, 1, 7)
              )
            ORDER BY ym ASC, d.cb_code ASC
        """
        rows = conn.execute(query, params).fetchall()

    result = []
    for cb_code, stock_code, ym in rows:
        series_match = re.search(r"\d$", str(cb_code))
        series_num = int(series_match.group(0)) if series_match else 1
        result.append((str(cb_code), str(stock_code), str(ym), series_num))
    return result


def backfill_all_balances(
    db_path: Path | str = DEFAULT_DB_PATH,
    start_ym: str = "2024-01",
    end_ym: str = "2025-05",
    delay_seconds: float = 0.8,
    rebuild_interval: int = 500,
    max_items: int | None = None,
) -> dict[str, int]:
    print(f"=== Starting MOPS Historical Balance Backfill ({start_ym} ~ {end_ym}) ===")
    missing_pairs = get_missing_pairs(db_path, start_ym=start_ym, end_ym=end_ym)
    total_missing = len(missing_pairs)
    print(f"Total missing (CB, month) pairs to fetch: {total_missing}")

    if max_items:
        missing_pairs = missing_pairs[:max_items]
        print(f"Limited to first {max_items} pairs by parameter.")

    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    inserted_balances = 0
    inserted_prices = 0
    skipped_count = 0
    error_count = 0

    batch_balances: list[tuple] = []
    batch_prices: list[tuple] = []

    def flush_batches():
        nonlocal batch_balances, batch_prices
        if not batch_balances and not batch_prices:
            return
        with connect(db_path) as conn:
            if batch_balances:
                conn.executemany("""
                    INSERT INTO cb_monthly_balance (
                        cb_code, year_month, balance_amount, source, source_url, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cb_code, year_month) DO UPDATE SET
                        balance_amount = excluded.balance_amount,
                        collected_at = excluded.collected_at
                """, batch_balances)
                batch_balances = []
            if batch_prices:
                conn.executemany("""
                    INSERT INTO conversion_price_events (
                        cb_code, effective_date, conversion_price, source, source_url, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cb_code, effective_date) DO UPDATE SET
                        conversion_price = excluded.conversion_price,
                        collected_at = excluded.collected_at
                """, batch_prices)
                batch_prices = []
            conn.commit()

    start_time = time.time()

    for idx, (cb_code, stock_code, ym, series_num) in enumerate(missing_pairs, 1):
        master = {
            "cb_code": cb_code,
            "stock_code": stock_code,
            "series_number": series_num,
            "bond_subn": "$M00000001",
        }
        url = _fallback_mops_url(master, ym)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
                if resp.status_code in (429, 403):
                    print(f"  [MOPS {cb_code} {ym}] HTTP {resp.status_code}. Backing off 30s...")
                    time.sleep(30)
                    continue

                if resp.status_code != 200:
                    skipped_count += 1
                    break

                text = resp.text
                if "之轉(交)換公司債發行資料" not in text:
                    # CB not yet issued or not in MOPS database for this month
                    skipped_count += 1
                    break

                snap = parse_mops_snapshot(text, url)
                now = datetime.now(timezone.utc).isoformat()

                if snap.get("balance_amount") is not None:
                    batch_balances.append((
                        cb_code, ym, snap["balance_amount"], "MOPS:t120sg01", url, now
                    ))
                    inserted_balances += 1

                if snap.get("conversion_price") is not None and snap.get("effective_date") is not None:
                    batch_prices.append((
                        cb_code, snap["effective_date"], snap["conversion_price"], "MOPS:t120sg01", url, now
                    ))
                    inserted_prices += 1

                time.sleep(delay_seconds)
                break

            except Exception as exc:
                if attempt == max_retries:
                    error_count += 1
                    # print(f"  [Error {cb_code} {ym}]: {exc}")
                else:
                    time.sleep(attempt * 2)

        # Commit batch every 50 records
        if idx % 50 == 0:
            flush_batches()
            elapsed = time.time() - start_time
            rate = idx / elapsed if elapsed > 0 else 0
            eta_min = (total_missing - idx) / rate / 60 if rate > 0 else 0
            print(f"[{idx}/{total_missing}] ({(idx/total_missing)*100:.1f}%) "
                  f"+{inserted_balances} balances, +{inserted_prices} prices, "
                  f"{skipped_count} skipped. Rate: {rate:.1f} req/s, ETA: {eta_min:.1f}m", flush=True)

        # Periodically rebuild static shards
        if rebuild_interval and idx % rebuild_interval == 0:
            flush_batches()
            print(f"\n--- Periodic Dashboard Recompile at step {idx} ---", flush=True)
            try:
                build_dashboard_shards()
                print("Dashboard shards updated successfully.\n", flush=True)
            except Exception as e:
                print(f"Warning: Rebuild failed: {e}\n", flush=True)

    # Final flush
    flush_batches()
    print(f"\n=== Backfill Complete ===")
    print(f"Fetched: {len(missing_pairs)} requests")
    print(f"Saved: {inserted_balances} monthly balances, {inserted_prices} conversion events")
    print(f"Skipped (empty/unissued): {skipped_count}, Errors: {error_count}")

    print("\n--- Final Dashboard Recompile ---")
    build_dashboard_shards()
    print("Static shards fully refreshed and published.")

    return {
        "balances": inserted_balances,
        "prices": inserted_prices,
        "skipped": skipped_count,
        "errors": error_count,
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill missing MOPS balances.")
    parser.add_argument("--start-ym", default="2024-01", help="Start Year-Month (e.g. 2024-01)")
    parser.add_argument("--end-ym", default="2025-05", help="End Year-Month (e.g. 2025-05)")
    parser.add_argument("--delay", type=float, default=0.8, help="Delay between requests in seconds")
    parser.add_argument("--rebuild-interval", type=int, default=500, help="Rebuild shards every N items")
    parser.add_argument("--max-items", type=int, default=None, help="Limit number of items to crawl")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to cb_history.db")
    args = parser.parse_args()

    backfill_all_balances(
        db_path=args.db,
        start_ym=args.start_ym,
        end_ym=args.end_ym,
        delay_seconds=args.delay,
        rebuild_interval=args.rebuild_interval,
        max_items=args.max_items,
    )


if __name__ == "__main__":
    main()
