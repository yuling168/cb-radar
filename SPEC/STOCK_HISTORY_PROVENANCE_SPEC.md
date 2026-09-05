# 母股歷史行情可追溯資料層

## 範圍

本資料層為策略 F 的母股 87MA、284MA、43 日乖離提供可追溯的行情輸入；本規格不建立或執行策略 F。43 日乖離定義為 `(close / MA43 - 1) * 100`。未來 F 僅在前一有效日乖離率大於 0、當日小於或等於 0 時觸發。

## 日期化 CB 母股 mapping

`cb_parent_stock_mapping` 以 `(cb_code, mapping_date)` 保存官方 CB→母股觀測：母股代號／名稱、市場（`TWSE`、`TPEX`、`TIB` 或尚未分類的 `UNKNOWN`）、官方來源、來源 URL 及驗證時間。

- `mapping_date` 是該 mapping 被官方來源驗證的日期，不是可向過去延伸套用的開始日。
- 回補與 collector 只可使用 `mapping_date = trade_date` 的 exact-date mapping；不得以今日 `cb_master` 補寫或推論歷史母股。
- master collector 以其 `as_of_date` 保存同日官方觀測；缺少某日 mapping 時，回補在任何網路請求前以 `Unverified parent-stock mapping` 拒絕該區間。

## MOPS 月度 verified mapping

`cb_parent_stock_monthly_mapping` 是獨立的月度表，主鍵為 `(cb_code, year_month)`，只允許來源 `MOPS:t120sg01`。它絕不寫入或覆蓋 `cb_parent_stock_mapping` 的日級列。

- `monthly_mapping_collector.py --year-month YYYY-MM` 只把今日 `cb_master` 用作「向 MOPS 查哪些已知 CB」的候選清單；每一列都必須由回傳的 MOPS `t120sg01` 文件驗證 CB 代號、母股代號與母股名稱，並保存 MOPS URL 與驗證時間。
- MOPS 文件缺失、非轉換債、CB／母股代號或名稱不符時，該月度 mapping 不得寫入。
- 日級 mapping 永遠優先。`stock_backfill.py` 預設 strict exact；只有明確傳入 `--allow-monthly-verified`，才可在同月且日級 mapping 缺失時使用月度 mapping。
- 使用月度 mapping 的 `stock_daily_coverage.mapping_level` 必須為 `MONTHLY_VERIFIED`；coverage 直接保存該日行情的官方 URL／回應日，以及 `mapping_source_url`、`mapping_year_month`、`mapping_verified_at`。日級列使用 `EXACT`。

## 行情 coverage 與 provenance

`stock_daily_coverage` 以 `(trade_date, stock_code)` 保存市場、狀態、原因、官方 URL、官方回應日期與 `checked_at`。狀態為：

- `COMPLETE`：官方列與收盤價可用；
- `OFFICIAL_ZERO`：官方列收盤價可用且成交量為零；零量仍是有效交易日；
- `MISSING_CLOSE`：官方列存在但收盤價缺失；不得用零或前值補齊；
- `MISSING_OFFICIAL_ROW`：已驗證的官方回應未出現目標母股；
- `SOURCE_ERROR`：下載、格式或官方回應日期驗證失敗。

TWSE 與 TPEx 官方回應都在寫入行情前驗證指定交易日。創新板可由 TWSE 日行情回應取得列資料，但 coverage 保留已驗證 mapping 的 `TIB` 市場別。來源錯誤及缺列只保存 coverage；不產生合成行情。

## 歷史回補 CLI

`stock_backfill.py` 可使用既有 `--days`，或使用 `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` 回補 DB 中已驗證的 `cb_daily` 交易日。所有日期都必須先通過 exact-date mapping preflight；任何日期未驗證時，整個呼叫在網路 I/O 前失敗。

本工具只提供可驗證資料層，不授權對正式 DB 執行回補。策略計算時，MA43／MA87／MA284 只計入收盤價非空的官方交易日；資料不足時必須回報不可用原因。
