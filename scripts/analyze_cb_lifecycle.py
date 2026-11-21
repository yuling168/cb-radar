"""CB Full Lifecycle Replay & Backtest Analysis Engine."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "cb_history.db"
OUTPUT_DIR = BASE_DIR / "exports"


def generate_cb_lifecycle_report(
    db_path: Path = DB_PATH,
    output_csv: Path | None = None,
    min_date: str = "2019-01-01",
) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    
    print(f"Loading CB daily records since {min_date}...")
    cb_df = pd.read_sql_query("""
        SELECT trade_date, cb_code, cb_name, close_price, volume_lots
        FROM cb_daily
        WHERE trade_date >= ? AND close_price IS NOT NULL
        ORDER BY cb_code, trade_date
    """, con, params=(min_date,))

    print(f"Loading stock daily records since {min_date}...")
    stock_df = pd.read_sql_query("""
        SELECT trade_date, p_stock_code AS stock_code, p_close_price AS stock_close, p_volume_shares AS stock_volume
        FROM stock_daily_market
        WHERE trade_date >= ? AND p_close_price IS NOT NULL
        ORDER BY stock_code, trade_date
    """, con, params=(min_date,))

    print("Loading CB-to-Stock mappings...")
    mapping_df = pd.read_sql_query("""
        SELECT DISTINCT cb_code, stock_code, stock_name
        FROM cb_parent_stock_mapping
    """, con)
    con.close()

    mapping_dict = {
        row["cb_code"]: (row["stock_code"], row["stock_name"])
        for _, row in mapping_df.iterrows()
    }

    print("Pre-indexing mother stocks by stock_code...")
    stock_by_code = {
        str(k): v.sort_values("trade_date")
        for k, v in stock_df.groupby("stock_code")
    }

    print("Computing lifecycle metrics per CB...")
    results = []
    
    grouped = cb_df.groupby("cb_code")
    for cb_code, group in grouped:
        group = group.sort_values("trade_date")
        cb_name = group["cb_name"].iloc[-1]
        stock_code, stock_name = mapping_dict.get(cb_code, (cb_code[:4], ""))

        first_row = group.iloc[0]
        last_row = group.iloc[-1]
        
        first_date = str(first_row["trade_date"])
        last_date = str(last_row["trade_date"])
        first_price = float(first_row["close_price"])
        last_price = float(last_row["close_price"])
        
        max_price_idx = group["close_price"].idxmax()
        max_row = group.loc[max_price_idx]
        max_price = float(max_row["close_price"])
        max_date = str(max_row["trade_date"])
        
        min_price = float(group["close_price"].min())
        trading_days = len(group)
        total_volume = int(group["volume_lots"].sum())
        avg_daily_volume = round(total_volume / trading_days, 1) if trading_days else 0

        # Gain metrics
        max_gain_from_100_pct = round((max_price - 100.0) / 100.0 * 100, 2)
        max_gain_from_first_pct = round((max_price - first_price) / first_price * 100, 2)
        amplitude_pct = round((max_price - min_price) / min_price * 100, 2) if min_price > 0 else 0

        # Days to peak
        days_to_peak = (pd.to_datetime(max_date) - pd.to_datetime(first_date)).days
        trading_days_to_peak = int(group[group["trade_date"] <= max_date].shape[0])

        # Feature flags
        reached_130 = 1 if max_price >= 130.0 else 0
        reached_150 = 1 if max_price >= 150.0 else 0
        broken_100 = 1 if min_price < 100.0 else 0
        broken_95 = 1 if min_price < 95.0 else 0

        # Mother stock statistics during CB lifecycle
        full_stock = stock_by_code.get(str(stock_code))
        if full_stock is not None:
            sub_stock = full_stock[
                (full_stock["trade_date"] >= first_date) &
                (full_stock["trade_date"] <= last_date)
            ]
        else:
            sub_stock = None
        
        if sub_stock is not None and not sub_stock.empty:
            stock_first_price = float(sub_stock["stock_close"].iloc[0])
            stock_max_price = float(sub_stock["stock_close"].max())
            stock_min_price = float(sub_stock["stock_close"].min())
            stock_last_price = float(sub_stock["stock_close"].iloc[-1])
            stock_max_gain_pct = round((stock_max_price - stock_first_price) / stock_first_price * 100, 2)
        else:
            stock_first_price = None
            stock_max_price = None
            stock_min_price = None
            stock_last_price = None
            stock_max_gain_pct = None

        results.append({
            "CB代號": cb_code,
            "CB簡稱": cb_name,
            "母股代號": stock_code,
            "母股名稱": stock_name,
            "首見交易日": first_date,
            "末見交易日": last_date,
            "存續交易日數": trading_days,
            "首日收盤價": first_price,
            "最後收盤價": last_price,
            "最高價": max_price,
            "最高價日期": max_date,
            "最低價": min_price,
            "波段振幅%": amplitude_pct,
            "從100起算最大漲幅%": max_gain_from_100_pct,
            "從首日收盤最大漲幅%": max_gain_from_first_pct,
            "掛牌至最高價天數(日曆)": days_to_peak,
            "掛牌至最高價交易日數": trading_days_to_peak,
            "是否觸及130強贖門檻": reached_130,
            "是否突破150飆券": reached_150,
            "是否曾破百(100)": broken_100,
            "是否曾破95": broken_95,
            "日均成交量(張)": avg_daily_volume,
            "母股掛牌日收盤": stock_first_price,
            "母股存續期最高": stock_max_price,
            "母股存續期最低": stock_min_price,
            "母股存續期最後": stock_last_price,
            "母股存續期最大漲幅%": stock_max_gain_pct,
        })

    report_df = pd.DataFrame(results)
    report_df = report_df.sort_values("首見交易日", ascending=False)
    
    if output_csv is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_csv = OUTPUT_DIR / f"cb_lifecycle_replay_since_{min_date.replace('-', '')}.csv"
    
    try:
        report_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"Report generated successfully: {output_csv} ({len(report_df)} CBs)")
    except PermissionError:
        import datetime
        fallback_name = output_csv.stem + f"_{datetime.datetime.now().strftime('%H%M%S')}.csv"
        fallback_csv = output_csv.parent / fallback_name
        print(f"\n[警告] 檔案 {output_csv.name} 疑似被 Excel 或其他程式開啟中，無法直接覆蓋！")
        print(f"-> 已自動另存新檔至: {fallback_csv.name}")
        report_df.to_csv(fallback_csv, index=False, encoding="utf-8-sig")
        print(f"建議關閉 Excel 後再次執行，即可覆蓋原主檔案。\n")
        output_csv = fallback_csv

    return report_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CB lifecycle replay report.")
    parser.add_argument("--min-date", default="2019-01-01", help="Earliest date to include")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()
    
    out_path = Path(args.output) if args.output else None
    generate_cb_lifecycle_report(output_csv=out_path, min_date=args.min_date)
