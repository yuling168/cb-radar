# 策略 C-v1：CB 資優生

策略 C-v1 只讀 SQLite 已保存的 CB、母股、轉換價與官方餘額資料，並以既有 append-only
`strategy_evaluations`／`strategy_signals` 分別保存診斷與不可變訊號快照。策略代號為 `C`、
版本為 `v1`，不讀取、更不覆寫 A-v1 記錄。

## 每日資料與歷史性

只評估目標日有 `cb_daily` 行情且主檔於該日已發行、尚未下市的 CB。轉換價使用
`effective_date <= trade_date` 的最新事件；母股收盤也必須是目標日資料。

已轉換比例為 `(1 - balance_amount / issue_amount) * 100`。餘額優先使用日期不晚於目標日
的 TPEx `cb_master.balance_date` 快照；否則使用 `cb_monthly_balance` 的最新已完整月份，
其官方餘額日為該報表月份月底。絕不使用未來餘額、今日資料回填歷史，或未完成月份。
缺少任一必要資料時寫入 `UNAVAILABLE` 與原因，不建立訊號。

## C-v1 條件與排名

候選須同時符合：轉換價值在 100～120（含）、已轉換比例不超過 20%、轉換溢價率嚴格大於
5%。轉換價值為 `母股收盤 / 轉換價 * 100`，溢價率為 `(CB 收盤 / 轉換價值 - 1) * 100`。

候選按轉換價值分為 `[100,105)`、`[105,110)`、`[110,115)`、`[115,120]` 四區。每區按
溢價率降冪、CB 代號升冪排序，前兩名才建立訊號。快照保存所有條件、轉換價值、溢價率、
已轉換比例、使用餘額日、區間、區間排名與同區候選數。

## CLI

```bash
python strategy_c.py --date 2026-08-28
python strategy_c.py --start-date 2026-08-01 --end-date 2026-08-31
python strategy_c.py --database path/to/history.db --date 2026-08-28
```

重跑只 append 評估；同一 `(cb_code, trade_date, C, v1)` 訊號使用 `INSERT OR IGNORE`，
不會覆蓋既有 C-v1 或 A-v1 訊號。

## 每日整合與 Dashboard

每日 workflow 在母股日行情完成後，先執行 A-v1 再執行 C-v1；兩個 CLI 分開執行，C 的
`UNAVAILABLE` 診斷不會阻斷 A，反之亦然。Dashboard 讀取已保存快照，不重新計算策略。
首頁以 A／C 標籤顯示訊號；`strategy-c.html` 可依日期檢視各區前二名、完整條件與資料不足原因，
並將「無訊號」與「資料不足無法評估」分開。
