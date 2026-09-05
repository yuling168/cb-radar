# 策略 G-v1：時間發動策略

## 範圍與保存

- 策略代號為 `G`、版本為 `v1`、名稱為「時間發動策略」。
- 只讀保存的 `cb_daily`、`cb_master`、`conversion_price_events`、`stock_daily_market` 與 `cb_monthly_balance`；不收集、不補值或改寫行情。
- 每一評估 append 至 `strategy_evaluations`。符合任一事件時，訊號以 `(cb_code, trade_date, G, v1)` 使用 `INSERT OR IGNORE` 寫入 `strategy_signals`，不覆寫 A/B/C 或既有 G 訊號。
- CLI：`strategy_g.py --date YYYY-MM-DD --database PATH`，或 `strategy_g.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --database PATH`。

## 資料可用性與基本條件

只評估目標日在 `cb_daily` 有列、已發行且尚未於該日或之前下市的 CB。基本條件為：

1. 已轉換比例嚴格小於 10%；
2. 轉換價值大於或等於 90；
3. CB 當日收盤價小於或等於 130。

轉換價只取 `effective_date <= trade_date` 的最新官方事件；轉換價值為當日母股收盤／有效轉換價 × 100。已轉換比例為 `(1 - balance_amount / issue_amount) * 100`，餘額只取目標日以前（含）的最新 TPEx `balance_date`，或最新已完成月份的月底 MOPS 餘額，絕不使用未來資料。缺主檔、收盤價、轉換價、母股收盤、發行額或歷史餘額時寫入 `UNAVAILABLE` 與原因。

## 時間事件

- **G1 發行滿一年**：只有可驗證前一個有效交易日尚未進入事件、當日首次進入時，且基本條件成立才觸發一次。事件已開始但基本條件原為 false 時，只有可驗證前一日基本條件為 false、當日轉為 true 才可觸發一次。
- **G2 賣回日後發動**：`trade_date >= put_date`、基本條件成立，且當日收盤嚴格高於前 19 個有效交易日最高收盤；當日成交量嚴格大於前 5 個有效交易日平均成交量的 3 倍。兩個前置窗口都不含今日；同價不是新高；已保存的 `volume_lots = 0` 是有效觀測值。
- **G3 到期前一年**：到期日前一年起、且未逾到期日，並依 G1 相同的「可驗證事件跨越」或「基本條件 false→true」規則觸發一次。

若目標日已是可用歷史的第一個有效交易日，且 G1 或 G3 已處於事件期間，前一有效交易日無法驗證，寫入 `UNAVAILABLE` 與 `baseline_unknown`，不得建立該事件訊號。G2 所需前 19 日曆或該 CB 任何窗口行情／收盤價缺失時，也寫入 `UNAVAILABLE`，不可把缺列或缺價補成零或前值。

同一日符合多個事件時只建立一筆訊號，快照 `trigger_types` 按 `G1`、`G2`、`G3` 順序保存全部事件。完整快照亦保存基本條件、目標日收盤／量、轉換價值、轉換價、母股收盤、發行額、餘額與餘額日、已轉換比例、發行／賣回／到期事件日期、G2 前 19／前 5 日日期、前 19 日高點與前 5 日均量。

## 每日整合與 Dashboard

每日 workflow 在母股日行情完成後依序執行 A-v1、B-v1、C-v1、G-v1；每個策略各自容錯，任何一個失敗都不阻斷後續策略或 Dashboard 建置。Dashboard 只讀已保存的 G 訊號完整快照，並輸出每個交易日／版本／狀態／不可用原因的評估筆數彙總，不輸出逐檔 evaluation JSON。首頁與 `strategy-g.html` 分開顯示無訊號與資料不足；策略頁可選日期，顯示 G1／G2／G3、事件日期、基本條件及 G2 突破窗口。
