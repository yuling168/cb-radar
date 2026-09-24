# 已下市 CB 歷史發行條款規格

## 目的與範圍

`cb_master` 的官方 active-universe 來源不能完整覆蓋已下市 CB。本規格定義狹義、可重跑且可稽核的補充流程：只為已驗證的歷史 CB 保存發行日、到期日與發行總額。它不重建完整 master、不推論母股 mapping，也不補行情、轉換價或餘額。

## 唯一可寫入來源

- 擷取器為 `historical_issuance_collector.py`。
- 先掃描 TPEx 歷史上櫃公告索引第 6 類，再依公告日期與文號取得 TPEx 靜態公告原文。
- 公告原文必須精確且唯一對應 CB 代碼，並能同時解析 `issue_date`、`maturity_date` 與正數 `issue_amount`，才可寫入。
- 第三方網站、搜尋摘要、名稱近似、推算年期、面額乘法猜測與空值／零值都不是可寫入證據。

## 資料表與稽核

### `cb_historical_issuance_terms`

主鍵為 `cb_code`，欄位包含三個必要條款、固定來源 `TPEx:historical_listing_announcement`、公告 URL 與收集時間。此表是 `cb_master` 的補充，不覆蓋或取代現行 master。

### `cb_historical_issuance_backfill_status`

每個目標 CB 保存 `SUCCEEDED`、`UNAVAILABLE` 或 `SOURCE_ERROR`、嘗試次數、最後錯誤、可能命中的公告 URL 與檢查時間。`UNAVAILABLE` 只代表尚未找到足以安全寫入的完整證據，不代表條款為零或回補完成；`SOURCE_ERROR` 可以於後續批次重試。

## 寫入與重跑規則

- 每次批次都可依 status 續跑；已成功資料保留來源鏈，不得因未命中而清空。
- 同一代碼若再取得不同條款，必須人工比對官方公告後才可覆寫；不得以後到資料直接取代。
- 已完成 2005–2026 索引掃描的結果是：6 檔具有完整可驗證條款，279 檔尚無可安全寫入結果。此數字是工作進度，不得宣稱所有已下市 CB 已補齊。

## Dashboard

Dashboard 對發行日、到期日與發行額以 `COALESCE(cb_master, cb_historical_issuance_terms)` 唯讀呈現。兩表皆無可靠資料時顯示缺值；前端不得自行補值或將缺值轉為零。

