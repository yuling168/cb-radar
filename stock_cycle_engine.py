"""Stock Cycle Engine: 43MA, 87MA, 284MA Magic Moving Averages & 6 Cycle Stages.

Classifies CBs into 6 market cycle stages based on underlying mother stock MAs:
1. 築底 (STAGE_BOTTOM): 43MA 上彎 (操作: 找轉折契機)
2. 初升 (STAGE_EARLY_BULL): 87MA 扣抵往上 (操作: 拉高波段部位)
3. 主升 (STAGE_MAIN_BULL): 神奇均線黃金交叉 (87MA >= 284MA 且向上) (操作: 階梯停利)
4. 做頭 (STAGE_TOPPING): 43MA 下彎 (操作: 降部位保留彈性)
5. 初跌 (STAGE_EARLY_BEAR): 87MA 扣抵往下 (操作: 減碼)
6. 主跌 (STAGE_MAIN_BEAR): 神奇均線死亡交叉 (87MA < 284MA 且向下) (操作: 觀望)

Includes 3-day smoothing confirmation filter & 'new entrant' (新進) detection.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from config import DEFAULT_DB_PATH
from db import connect

# Stage Definitions
STAGE_BOTTOM = "BOTTOM"
STAGE_EARLY_BULL = "EARLY_BULL"
STAGE_MAIN_BULL = "MAIN_BULL"
STAGE_TOPPING = "TOPPING"
STAGE_EARLY_BEAR = "EARLY_BEAR"
STAGE_MAIN_BEAR = "MAIN_BEAR"
STAGE_INSUFFICIENT = "INSUFFICIENT"

STAGE_METADATA = {
    STAGE_BOTTOM: {
        "code": STAGE_BOTTOM,
        "name": "築底",
        "side": "空頭",
        "node_type": "bottom",
        "point_color": "#4ade80",  # Green
        "description": "43MA 開始上彎，顯示短期趨勢有止穩跡象。",
        "action": "找轉折契機",
    },
    STAGE_EARLY_BULL: {
        "code": STAGE_EARLY_BULL,
        "name": "初升",
        "side": "多頭",
        "node_type": "bull",
        "point_color": "#f87171",  # Red
        "description": "87MA 扣抵往上，代表長均線開始轉強。",
        "action": "拉高波段部位",
    },
    STAGE_MAIN_BULL: {
        "code": STAGE_MAIN_BULL,
        "name": "主升",
        "side": "多頭",
        "node_type": "bull",
        "point_color": "#f87171",  # Red
        "description": "神奇均線出現黃金交叉（87MA 位於 284MA 之上且向上）。",
        "action": "階梯停利",
    },
    STAGE_TOPPING: {
        "code": STAGE_TOPPING,
        "name": "做頭",
        "side": "多頭",
        "node_type": "topping",
        "point_color": "#f87171",  # Red
        "description": "43MA 開始下彎，代表短期漲勢動能趨緩。",
        "action": "降部位保留彈性",
    },
    STAGE_EARLY_BEAR: {
        "code": STAGE_EARLY_BEAR,
        "name": "初跌",
        "side": "空頭",
        "node_type": "bear",
        "point_color": "#4ade80",  # Green
        "description": "87MA 扣抵往下，代表原本的多頭排列中長均線開始轉弱。",
        "action": "減碼",
    },
    STAGE_MAIN_BEAR: {
        "code": STAGE_MAIN_BEAR,
        "name": "主跌",
        "side": "空頭",
        "node_type": "bear",
        "point_color": "#4ade80",  # Green
        "description": "神奇均線出現死亡交叉（87MA 位於 284MA 之下且向下）。",
        "action": "觀望",
    },
    STAGE_INSUFFICIENT: {
        "code": STAGE_INSUFFICIENT,
        "name": "資料不足",
        "side": "其他",
        "node_type": "insufficient",
        "point_color": "#9ca3af",
        "description": "母股掛牌未滿 284 個交易日，無法精確計算神奇均線。",
        "action": "無建議",
    },
}

DISPLAY_ORDER = [
    STAGE_EARLY_BEAR,
    STAGE_MAIN_BEAR,
    STAGE_BOTTOM,
    STAGE_EARLY_BULL,
    STAGE_MAIN_BULL,
    STAGE_TOPPING,
]


def clean_float(val: Any, decimals: int = 2) -> float | None:
    """Safely convert any numeric / NaN / None value to a clean JSON-compliant float or None."""
    if val is None:
        return None
    if isinstance(val, (float, np.floating)):
        if math.isnan(val) or math.isinf(val):
            return None
        return round(float(val), decimals)
    if isinstance(val, (int, np.integer)):
        return round(float(val), decimals)
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, decimals)
    except (ValueError, TypeError):
        return None


def determine_raw_stage(
    ma43: float | None,
    ma87: float | None,
    ma284: float | None,
    slope43: float | None,
    deduct87: float | None,
) -> str:
    """Determine the raw cycle stage from moving averages for a single day."""
    if (
        ma284 is None
        or pd.isna(ma284)
        or slope43 is None
        or pd.isna(slope43)
        or deduct87 is None
        or pd.isna(deduct87)
    ):
        return STAGE_INSUFFICIENT

    is_43_up = slope43 > 0
    is_43_down = slope43 < 0
    is_87_up = deduct87 > 0
    is_87_down = deduct87 < 0
    is_bull_cross = ma87 >= ma284

    if is_bull_cross:
        # Bullish cross region (87MA >= 284MA)
        if is_87_down:
            # 87MA 扣抵往下 -> 初跌 (長均線轉弱)
            return STAGE_EARLY_BEAR
        elif is_43_down:
            # 43MA 開始下彎 -> 做頭 (短期漲勢趨緩)
            return STAGE_TOPPING
        else:
            # 87MA 向上 -> 主升 (黃金交叉且向上)
            return STAGE_MAIN_BULL
    else:
        # Bearish cross region (87MA < 284MA)
        if is_87_up:
            # 87MA 扣抵往上 -> 初升 (長均線開始轉強)
            return STAGE_EARLY_BULL
        elif is_43_up:
            # 43MA 開始上彎 -> 築底 (短線趨勢止穩)
            return STAGE_BOTTOM
        else:
            # 87MA 向下 -> 主跌 (死亡交叉且向下)
            return STAGE_MAIN_BEAR


def calculate_stock_cycles(
    stocks_df: pd.DataFrame, confirmation_days: int = 3
) -> dict[str, pd.DataFrame]:
    """Calculate moving averages, deductions, and 3-day confirmed stages for each stock."""
    stock_results: dict[str, pd.DataFrame] = {}

    for code, group in stocks_df.groupby("stock_code"):
        g = group.copy().sort_values("trade_date").reset_index(drop=True)
        g["ma43"] = g["close"].rolling(43).mean()
        g["ma87"] = g["close"].rolling(87).mean()
        g["ma284"] = g["close"].rolling(284).mean()
        g["slope43"] = g["ma43"].diff()
        g["deduct87"] = g["close"] - g["close"].shift(87)

        raw_stages: list[str] = [
            determine_raw_stage(
                row.ma43, row.ma87, row.ma284, row.slope43, row.deduct87
            )
            for row in g.itertuples()
        ]
        g["raw_stage"] = raw_stages

        confirmed_stages: list[str] = []
        is_new_entrant: list[bool] = []
        consecutive_days: list[int] = []

        curr_confirmed = STAGE_INSUFFICIENT
        curr_consec = 0

        for i in range(len(raw_stages)):
            raw = raw_stages[i]
            # Check if last `confirmation_days` are all identical and valid
            is_consistent = (
                i >= (confirmation_days - 1)
                and all(raw_stages[i - j] == raw for j in range(confirmation_days))
                and raw != STAGE_INSUFFICIENT
            )

            if is_consistent:
                if raw != curr_confirmed:
                    # Confirmed switch to a new stage on this day
                    curr_confirmed = raw
                    is_new_entrant.append(True)
                    curr_consec = confirmation_days
                    consecutive_days.append(curr_consec)
                else:
                    is_new_entrant.append(False)
                    curr_consec += 1
                    consecutive_days.append(curr_consec)
            else:
                if curr_confirmed == STAGE_INSUFFICIENT and raw != STAGE_INSUFFICIENT:
                    # Initial valid bootstrap
                    curr_confirmed = raw
                    is_new_entrant.append(False)
                    curr_consec = 1
                    consecutive_days.append(curr_consec)
                else:
                    is_new_entrant.append(False)
                    if curr_confirmed != STAGE_INSUFFICIENT:
                        curr_consec += 1
                    consecutive_days.append(curr_consec)

            confirmed_stages.append(curr_confirmed)

        g["confirmed_stage"] = confirmed_stages
        g["is_new_entrant"] = is_new_entrant
        g["consecutive_days"] = consecutive_days
        stock_results[str(code)] = g

    return stock_results


def evaluate_stock_cycles_for_date(
    db_path: Path | str = DEFAULT_DB_PATH,
    target_date: str | None = None,
) -> dict[str, Any]:
    """Evaluate and package stock cycle stages for all active CBs on a given date."""
    with connect(db_path) as conn:
        if target_date is None:
            latest_row = conn.execute("SELECT MAX(trade_date) FROM cb_daily").fetchone()
            if not latest_row or not latest_row[0]:
                raise ValueError("No trade dates found in cb_daily")
            target_date = str(latest_row[0])

        # Get previous trading date to compute CB daily price change
        prev_row = conn.execute(
            "SELECT MAX(trade_date) FROM cb_daily WHERE trade_date < ?", (target_date,)
        ).fetchone()
        prev_date = str(prev_row[0]) if prev_row and prev_row[0] else None

        prev_cbs_map: dict[str, float] = {}
        if prev_date:
            prev_rows = conn.execute(
                "SELECT cb_code, COALESCE(close_price, reference_price) FROM cb_daily WHERE trade_date = ?",
                (prev_date,),
            ).fetchall()
            prev_cbs_map = {str(r[0]): float(r[1]) for r in prev_rows if r[1] is not None}

        # Load active CBs and prices for target_date
        cb_query = """
            SELECT 
                m.cb_code, m.cb_name, m.stock_code, m.stock_name,
                m.current_conversion_price, m.issue_date, m.maturity_date,
                d.close_price as cb_close, d.volume_lots as cb_volume,
                d.reference_price as cb_reference_price
            FROM cb_master m
            JOIN cb_daily d ON d.cb_code = m.cb_code AND d.trade_date = ?
            WHERE m.delisting_date IS NULL
            ORDER BY m.cb_code
        """
        cbs_df = pd.read_sql_query(cb_query, conn, params=(target_date,))

        # Load all stock daily price history up to target_date
        stock_query = """
            SELECT trade_date, p_stock_code as stock_code, p_close_price as close, p_volume_shares as volume
            FROM stock_daily_market
            WHERE trade_date <= ?
            ORDER BY p_stock_code, trade_date
        """
        stocks_df = pd.read_sql_query(stock_query, conn, params=(target_date,))

    if cbs_df.empty or stocks_df.empty:
        return {
            "date": target_date,
            "prev_date": prev_date,
            "total_cbs": 0,
            "bull_total": 0,
            "bear_total": 0,
            "stages": {},
        }

    # Calculate MAs and stages
    stock_cycles = calculate_stock_cycles(stocks_df)

    # Extract target date metrics for each stock
    stock_latest_map: dict[str, dict[str, Any]] = {}
    for code, g in stock_cycles.items():
        sub = g[g["trade_date"] == target_date]
        if not sub.empty:
            r = sub.iloc[0]
            stock_latest_map[code] = {
                "stock_code": code,
                "stock_close": clean_float(r["close"]),
                "ma43": clean_float(r["ma43"]),
                "ma87": clean_float(r["ma87"]),
                "ma284": clean_float(r["ma284"]),
                "slope43": clean_float(r["slope43"], 3),
                "deduct87": clean_float(r["deduct87"]),
                "stage": r["confirmed_stage"],
                "is_new_entrant": bool(r["is_new_entrant"]),
                "consecutive_days": int(r["consecutive_days"]),
            }

    # Group CBs by stage
    stages_data: dict[str, dict[str, Any]] = {}
    for stage_code in [*DISPLAY_ORDER, STAGE_INSUFFICIENT]:
        meta = STAGE_METADATA[stage_code]
        stages_data[stage_code] = {
            "code": meta["code"],
            "name": meta["name"],
            "side": meta["side"],
            "point_color": meta["point_color"],
            "description": meta["description"],
            "action": meta["action"],
            "count": 0,
            "new_count": 0,
            "cbs": [],
            "volatile_cbs": [],  # CBs with |change_pct| >= 5% for hover popover
        }

    bull_total = 0
    bear_total = 0

    for cb in cbs_df.itertuples():
        stk_code = str(cb.stock_code)
        stk_data = stock_latest_map.get(stk_code)

        if stk_data is None:
            stage_code = STAGE_INSUFFICIENT
            is_new = False
            consec = 0
            stk_close = None
            ma43 = ma87 = ma284 = slope43 = deduct87 = None
        else:
            stage_code = stk_data["stage"]
            is_new = stk_data["is_new_entrant"]
            consec = stk_data["consecutive_days"]
            stk_close = stk_data["stock_close"]
            ma43 = stk_data["ma43"]
            ma87 = stk_data["ma87"]
            ma284 = stk_data["ma284"]
            slope43 = stk_data["slope43"]
            deduct87 = stk_data["deduct87"]

        # Calculate conversion value & premium rate cleanly
        conv_price = clean_float(cb.current_conversion_price)
        cb_close = clean_float(cb.cb_close)
        cb_ref = clean_float(cb.cb_reference_price)
        cb_volume = int(cb.cb_volume) if cb.cb_volume is not None and not pd.isna(cb.cb_volume) else 0

        # Calculate daily change and change %
        eff_cb_price = cb_close if cb_close is not None else cb_ref
        p0 = prev_cbs_map.get(str(cb.cb_code))
        cb_change = None
        cb_change_pct = None
        if eff_cb_price is not None and p0 is not None and p0 > 0:
            cb_change = clean_float(eff_cb_price - p0, 2)
            cb_change_pct = clean_float(((eff_cb_price - p0) / p0) * 100.0, 2)

        premium_rate = None
        conversion_value = None

        if conv_price and conv_price > 0 and stk_close is not None:
            raw_conv_val = (100.0 / conv_price) * stk_close
            conversion_value = clean_float(raw_conv_val)
            if cb_close is not None and conversion_value and conversion_value > 0:
                raw_prem = ((cb_close - conversion_value) / conversion_value) * 100.0
                premium_rate = clean_float(raw_prem)

        cb_item = {
            "cb_code": str(cb.cb_code),
            "cb_name": str(cb.cb_name),
            "cb_close": cb_close,
            "cb_change": cb_change,
            "cb_change_pct": cb_change_pct,
            "cb_volume": cb_volume,
            "stock_code": str(cb.stock_code),
            "stock_name": str(cb.stock_name),
            "stock_close": stk_close,
            "conversion_price": conv_price,
            "conversion_value": conversion_value,
            "premium_rate": premium_rate,
            "stage": stage_code,
            "is_new_entrant": is_new,
            "consecutive_days": consec,
            "ma43": ma43,
            "ma87": ma87,
            "ma284": ma284,
            "slope43": slope43,
            "deduct87": deduct87,
        }

        if stage_code in stages_data:
            stages_data[stage_code]["count"] += 1
            if is_new:
                stages_data[stage_code]["new_count"] += 1
            stages_data[stage_code]["cbs"].append(cb_item)

            # Check if volatility exceeds 5%
            if cb_change_pct is not None and abs(cb_change_pct) >= 5.0:
                stages_data[stage_code]["volatile_cbs"].append({
                    "cb_code": cb_item["cb_code"],
                    "cb_name": cb_item["cb_name"],
                    "cb_close": cb_item["cb_close"],
                    "cb_change": cb_item["cb_change"],
                    "cb_change_pct": cb_item["cb_change_pct"],
                    "conversion_value": cb_item["conversion_value"],
                    "premium_rate": cb_item["premium_rate"],
                    "cb_volume": cb_item["cb_volume"],
                })

            if STAGE_METADATA.get(stage_code, {}).get("side") == "多頭":
                bull_total += 1
            elif STAGE_METADATA.get(stage_code, {}).get("side") == "空頭":
                bear_total += 1

    # Default sort CBs in each stage by cb_change_pct descending (highest first)
    for s_info in stages_data.values():
        s_info["cbs"].sort(
            key=lambda x: (
                -(x["cb_change_pct"] if x["cb_change_pct"] is not None else -999999),
                -(x["cb_volume"] or 0),
                x["cb_code"],
            )
        )
        s_info["volatile_cbs"].sort(
            key=lambda x: -(x["cb_change_pct"] if x["cb_change_pct"] is not None else -999999)
        )

    return {
        "date": target_date,
        "prev_date": prev_date,
        "total_cbs": len(cbs_df),
        "bull_total": bull_total,
        "bear_total": bear_total,
        "stages": stages_data,
        "display_order": DISPLAY_ORDER,
    }
