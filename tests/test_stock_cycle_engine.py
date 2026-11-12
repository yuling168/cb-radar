import pandas as pd
import pytest

from stock_cycle_engine import (
    STAGE_BOTTOM,
    STAGE_EARLY_BEAR,
    STAGE_EARLY_BULL,
    STAGE_INSUFFICIENT,
    STAGE_MAIN_BEAR,
    STAGE_MAIN_BULL,
    STAGE_TOPPING,
    calculate_stock_cycles,
    determine_raw_stage,
)


def test_determine_raw_stage_insufficient():
    # When any critical MA or slope is missing
    assert determine_raw_stage(None, None, None, None, None) == STAGE_INSUFFICIENT
    assert determine_raw_stage(100.0, 100.0, None, 1.0, 5.0) == STAGE_INSUFFICIENT
    assert determine_raw_stage(100.0, 100.0, 90.0, None, 5.0) == STAGE_INSUFFICIENT
    assert determine_raw_stage(100.0, 100.0, 90.0, 1.0, None) == STAGE_INSUFFICIENT


def test_determine_raw_stage_bull_side():
    # 87MA >= 284MA (Bullish cross)
    ma43, ma87, ma284 = 110.0, 105.0, 100.0

    # 1. 主升: 87MA >= 284MA, 87MA slope up, 43MA slope up
    assert determine_raw_stage(ma43, ma87, ma284, slope43=1.0, deduct87=5.0) == STAGE_MAIN_BULL

    # 2. 做頭: 87MA >= 284MA, 87MA slope up, but 43MA starts sloping down
    assert determine_raw_stage(ma43, ma87, ma284, slope43=-0.5, deduct87=5.0) == STAGE_TOPPING

    # 3. 初跌: 87MA >= 284MA, but 87MA deduction turns down (deduct87 < 0)
    assert determine_raw_stage(ma43, ma87, ma284, slope43=-0.5, deduct87=-3.0) == STAGE_EARLY_BEAR
    assert determine_raw_stage(ma43, ma87, ma284, slope43=0.5, deduct87=-3.0) == STAGE_EARLY_BEAR


def test_determine_raw_stage_bear_side():
    # 87MA < 284MA (Bearish cross)
    ma43, ma87, ma284 = 90.0, 95.0, 100.0

    # 1. 主跌: 87MA < 284MA, 87MA slope down (deduct87 < 0), 43MA down
    assert determine_raw_stage(ma43, ma87, ma284, slope43=-1.0, deduct87=-5.0) == STAGE_MAIN_BEAR

    # 2. 築底: 87MA < 284MA, 87MA slope down, but 43MA turns up (slope43 > 0)
    assert determine_raw_stage(ma43, ma87, ma284, slope43=1.0, deduct87=-5.0) == STAGE_BOTTOM

    # 3. 初升: 87MA < 284MA, but 87MA deduction turns up (deduct87 > 0)
    assert determine_raw_stage(ma43, ma87, ma284, slope43=1.0, deduct87=3.0) == STAGE_EARLY_BULL


def test_three_day_confirmation_filter():
    # Build a dataframe with 300 trading days
    dates = [f"2025-{i//20+1:02d}-{i%20+1:02d}" for i in range(300)]
    # Constant price initially
    prices = [100.0] * 300
    df = pd.DataFrame({
        "trade_date": dates,
        "stock_code": ["2330"] * 300,
        "close": prices,
    })

    cycles = calculate_stock_cycles(df, confirmation_days=3)
    res = cycles["2330"]

    assert "confirmed_stage" in res.columns
    assert "is_new_entrant" in res.columns
    assert "consecutive_days" in res.columns
    assert len(res) == 300
