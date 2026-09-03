# CB 母股法人籌碼追蹤（第一階段）

## 範圍與資料完整性

目標母股由 `cb_master` 依既有 active universe 規則取得：`issue_date <= 交易日 AND (delisting_date IS NULL OR delisting_date > 交易日)` 的 `DISTINCT stock_code`。一檔母股即使對應多檔 CB，也只收集一次；已下市、已完成回補 CB 的母股，以及官方回傳但不屬於該集合的任何股票，都不得寫入本階段資料表。所有原始股數均保存為「股」，不在 collector 轉換為張。資料來源缺漏、HTTP/格式失敗或市場端點未驗證時，collector 失敗且不寫入零值或部分資料。

## `institutional_daily`

每筆為 `(trade_date, stock_code)`：市場、名稱、外資及陸資／投信的買進、賣出、買賣超股數，以及來源 URL、收集時間。上市來源是 TWSE `rwd/zh/fund/T86`；欄位為外陸資（不含外資自營商）與投信的買進／賣出／買賣超。上櫃來源是 TPEx OpenAPI `openapi/v1/tpex_3insti_daily_trading`；其 `Date` 是民國年 `YYYMMDD`（例如 `1150903`），外資欄位採 API 的 `ForeignInvestorsIncludeMainlandAreaInvestors-*`，投信欄位採 `SecuritiesInvestmentTrustCompanies-*`。兩者皆為股數；TPEx 回應日期、完整必需欄位及全部目標母股必須都通過驗證，才會合併寫入。

## `active_etf_master`、`active_etf_holdings`

僅當基金公司官方來源可實測、有公告日期、股票代碼、名稱、股數時才在 master 啟用。已驗證野村投信 ETF 專區 PCF API `Fund/GetFundAssets`：`00980A`、`00985A`、`00999A`；回應的「股票」表以 `NavDate` 為日期，列為股票代號、股票名稱、股數、權重，股數為股。

群益已驗證正式 JSON：`POST https://www.capitalfund.com.tw/CFWeb/api/etf/buyback`，body 為 `{\"fundId\": \"399\"|\"500\", \"date\": \"YYYY-MM-DD\"}`，分別對應 `00982A`／`00992A`。回應的 `data.pcf.date1` 必須等於請求交易日，ETF 名稱取 `data.pcf.fundName`；`data.stocks` 的 `stocNo`、`stocName`、`share` 分別是股票代號、名稱及原始持股股數。`share` 保存為股，`shareFormat` 僅為呈現格式、不得作為數值來源；可指定歷史日期。統一 `00981A`、安聯 `00984A`／`00993A`、以及其他台股股票型主動式 ETF，截至本階段尚未驗證其非頁面擷取、可穩定使用的官方逐日 API，故不得入庫。

`active_etf_holdings` 保存原始快照與來源 URL／識別；跨日 `holding_shares` 差額的固定名稱是「持股增減」，不是精確買賣超。未出現在某日清單不自動產生 0。
