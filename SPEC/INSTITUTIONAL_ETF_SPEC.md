# CB 母股法人籌碼追蹤（第一階段）

## 範圍與資料完整性

目標母股由 `cb_master` 依既有 active universe 規則取得：`issue_date <= 交易日 AND (delisting_date IS NULL OR delisting_date > 交易日)` 的 `DISTINCT stock_code`。一檔母股即使對應多檔 CB，也只收集一次；已下市、已完成回補 CB 的母股，以及官方回傳但不屬於該集合的任何股票，都不得寫入本階段資料表。所有原始股數均保存為「股」，不在 collector 轉換為張。資料來源缺漏、HTTP/格式失敗或市場端點未驗證時，collector 失敗且不寫入零值或部分資料。

## `institutional_daily`

每筆為 `(trade_date, stock_code)`：市場、名稱、外資及陸資／投信的買進、賣出、買賣超股數，以及來源 URL、收集時間。上市來源是 TWSE `rwd/zh/fund/T86`；欄位為外陸資（不含外資自營商）與投信的買進／賣出／買賣超。上櫃來源是 TPEx OpenAPI `openapi/v1/tpex_3insti_daily_trading`；其 `Date` 是民國年 `YYYMMDD`（例如 `1150903`），外資欄位採 API 的 `ForeignInvestorsIncludeMainlandAreaInvestors-*`，投信欄位採 `SecuritiesInvestmentTrustCompanies-*`。兩者皆為股數；TPEx 回應日期、完整必需欄位及全部目標母股必須都通過驗證，才會合併寫入。

上櫃改採官方 `POST https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade`，form 為 `type=Daily&cate=EW&date=YYYY/MM/DD&response=json`；必須驗證 `stat=ok` 與 `date=YYYYMMDD`。`institutional_coverage` 逐母股保存 `COMPLETE`、`OFFICIAL_ZERO`、`UNAVAILABLE_MARKET`、`SOURCE_ERROR` 與原因。當已驗證完整涵蓋的市場官方明細未列母股，才以六欄股數 0 寫入並標記 `OFFICIAL_ZERO`；創新板 6645 標記 `UNAVAILABLE_MARKET`、原因為「資料未提供（創新板）」且不寫法人數字。來源失敗、日期不符或欄位／市場涵蓋無法驗證時，標記 `SOURCE_ERROR`，不補 0。每日 coverage 只要有不可寫入母股即為 `incomplete`，但仍繼續已追蹤主動式 ETF。

## `active_etf_master`、`active_etf_holdings`

僅當基金公司官方來源可實測、有公告日期、股票代碼、名稱、股數時才在 master 啟用。已驗證野村投信 ETF 專區 PCF API `Fund/GetFundAssets`：`00980A`、`00985A`、`00999A`；回應的「股票」表以 `NavDate` 為日期，列為股票代號、股票名稱、股數、權重，股數為股。

群益已驗證正式 JSON：`POST https://www.capitalfund.com.tw/CFWeb/api/etf/buyback`，body 為 `{\"fundId\": \"399\"|\"500\", \"date\": \"YYYY-MM-DD\"}`，分別對應 `00982A`／`00992A`。回應的 `data.pcf.date1` 必須等於請求交易日，ETF 名稱取 `data.pcf.fundName`；`data.stocks` 的 `stocNo`、`stocName`、`share` 分別是股票代號、名稱及原始持股股數。`share` 保存為股，`shareFormat` 僅為呈現格式、不得作為數值來源；可指定歷史日期。統一 `00981A`、安聯 `00984A`／`00993A`、以及其他台股股票型主動式 ETF，截至本階段尚未驗證其非頁面擷取、可穩定使用的官方逐日 API，故不得入庫。

`active_etf_holdings` 保存原始快照與來源 URL／識別；跨日 `holding_shares` 差額的固定名稱是「持股增減」，不是精確買賣超。未出現在某日清單不自動產生 0。

## 每日整合與近期回補

本機 `daily_pipeline.py --date YYYY-MM-DD --database PATH` 僅接受 `cb_daily` 已存在的有效交易日，依序先收集母股法人資料，再逐檔收集已追蹤主動式 ETF：`00980A`、`00985A`、`00999A`、`00982A`、`00992A`。法人資料須完成所有 active CB 母股才寫入當日資料；任一市場／欄位／目標股失敗即不寫入該日法人資料。ETF 則逐檔隔離：成功者可保存，失敗者寫入 `active_etf_master.last_status` 與 `active_etf_collection_status` 的原因，絕不產生零持股。`active_etf_collection_status` 使未來「已追蹤主動式 ETF 合計」能標示當日 coverage 為 `incomplete`，不得宣稱全市場主動式 ETF。

`institutional_etf_backfill.py --days 10 --database PATH` 預設從既有 `cb_daily` 取最近十個有效交易日；可改用 `--end-date YYYY-MM-DD` 限制最近日期，或使用 `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` 指定閉區間。此回補是手動入口，不會自動執行或修改 GitHub Actions。
