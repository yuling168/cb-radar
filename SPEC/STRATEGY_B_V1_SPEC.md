# 策略 B-v1：CB 突破轉換價

## 範圍與版本

- 策略代號：`B`；版本：`v1`；名稱：`CB 突破轉換價`。
- 寫入既有版本化 `strategy_evaluations` 與 `strategy_signals`；不得覆寫 A-v1、C-v1 或其他 B 版本。
- `strategy_evaluations` 為 append-only 診斷記錄；`strategy_signals` 以 `(cb_code, trade_date, strategy_code, strategy_version)` 冪等新增。
- CLI：`strategy_b.py --date YYYY-MM-DD --database PATH`，或
  `strategy_b.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --database PATH`。

## 有效範圍與資料完整性

- 只評估目標日 `cb_daily` 中存在，且 `issue_date <= target_date`、未於當日或之前下市的 CB。
- 有效交易日是 `cb_daily` 中存在的實際交易日；窗口不可用日曆日、不可補交易日。
- 43 日窗口中的每個 CB 日行情列、收盤價都必須存在。官方已確認的 `volume_lots = 0` 是有效觀測值，不可當成缺值。
- 轉換價僅可取 `effective_date <= target_date` 的最新事件；母股收盤只可取目標日的保存值。
- 已轉換比例為 `(1 - balance_amount / issue_amount) * 100`。餘額僅可取目標日前的最新官方月餘額（完整月份月底）或 `balance_date <= target_date` 的 TPEx 官方快照；不得使用未來或目前餘額回填歷史。
- 缺少窗口日行情、收盤價、轉換價、目標日母股收盤、發行額或歷史餘額時，只寫 `UNAVAILABLE` 與原因；不補零、不前值填補、不使用未來資料。

## 條件

以下所有條件均須成立才建立訊號：

1. 目標日 CB 收盤價嚴格大於包含當日在內 43 個有效交易日的平均收盤價。
2. 目標日 CB 成交量嚴格大於包含當日在內 10 個有效交易日的平均成交量。
3. 轉換溢價率嚴格大於 5%。
4. 轉換價值介於 90 至 110，含兩端。
5. 已轉換比例小於或等於 20%。
6. 包含當日在內 5 個有效交易日平均成交量嚴格大於 50 張。
7. 目標日 CB 收盤價嚴格大於前 19 個有效交易日的最高收盤價；同價不算新高。

轉換價值為 `目標日母股收盤價 / 當日有效轉換價 * 100`；轉換溢價率為
`(CB 收盤價 / 轉換價值 - 1) * 100`。

## 快照

每筆 `AVAILABLE` 評估與訊號保存：四個窗口日期、目標日收盤／成交量、43 日收盤均價、10／5 日成交量均值、前 19 日最高收盤、轉換價、母股收盤、轉換價值、溢價率、發行額、餘額／餘額日期、已轉換比例、全部條件結果與 `trigger_reason=all_b_v1_conditions_met`。`UNAVAILABLE` 保存不可用原因與可安全保存的缺值定位資訊。
