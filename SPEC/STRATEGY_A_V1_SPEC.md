# 策略 A-v1：CB 成交量創 10 日新高

## 範圍與資料來源

策略只讀 SQLite 中既有的 `cb_daily`、`cb_master`、`conversion_price_events` 與
`stock_daily_market`，不抓取資料、不補資料，也不改寫行情。成交量單位均為張。

「有效交易日」取自 `cb_daily` 中已由 TPEx 驗證並保存的交易日。某檔 CB 在該
有效交易日必須有一列資料；`volume_lots = 0` 是已驗證的觀測值，會納入計算；
缺列不是零量，會使該檔當日結果為 `UNAVAILABLE`。

## A-v1 條件

同時符合以下所有條件才建立訊號：

1. 當日成交量嚴格大於前 9 個有效交易日的最高成交量。
2. 當日收盤價在 115 至 150（含）之間。
3. 收盤價大於當日轉換價值。
4. 溢價率嚴格大於 1%。
5. 包含當日在內的最近 10 個有效交易日累計成交量嚴格大於 300 張。
6. 當日成交量嚴格大於前 5 個有效交易日平均成交量的 3 倍。

轉換價採 `effective_date <= trade_date` 的最新 `conversion_price_events`；轉換
價值為 `當日母股收盤價 / 轉換價 * 100`；溢價率為
`(CB 收盤價 / 轉換價值 - 1) * 100`。第 6 點明確使用「前」5 日，避免把今日量
同時放入比較基準。

## 可診斷性與不可變性

任一必要資料（10 日完整 CB 行情、收盤價、CB 主檔、有效轉換價、當日母股收盤）
不足時不建立訊號。每一評估都 append 至 `strategy_evaluations`，保存條件布林值、
計算數值、資料狀態與不可用原因。

符合條件時，`strategy_signals` 以
`(cb_code, trade_date, strategy_code, strategy_version)` 為主鍵保存策略名稱、條件
結果與數值快照。寫入使用 `INSERT OR IGNORE`：同一版本重跑不會覆蓋既有訊號；
規則有實質改動時必須提升 `strategy_version`。

## CLI

```bash
python strategy_engine.py --date 2026-08-28
python strategy_engine.py --start-date 2026-08-01 --end-date 2026-08-31
python strategy_engine.py --database path/to/history.db --date 2026-08-28
```

歷史區間只會挑選 DB 中已存在的交易日。指定單日即使沒有任何行情，仍會保存一筆
`__RUN__` 的不可用診斷。
