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

ETF 官方 HTTP POST 對網路逾時、連線錯誤、HTTP 429 與 HTTP 5xx 最多嘗試三次；兩次間的退避等待固定為 0.5 秒、1 秒。HTTP 403／404，以及回應日期不符、欄位缺漏、持股格式錯誤等資料驗證失敗均不可重試，直接保留該 ETF 的 failed coverage 與原因。

`institutional_etf_backfill.py --days 10 --database PATH` 預設從既有 `cb_daily` 取最近十個有效交易日；可改用 `--end-date YYYY-MM-DD` 限制最近日期，或使用 `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` 指定閉區間。此回補是手動入口，不會自動執行或修改 GitHub Actions。

## 第三階段 B：`parent_flow_metrics`

每日 pipeline 在法人與五檔 ETF 收集結束後，為當日的現行 CB 母股（依同一 active universe 規則 `DISTINCT stock_code`）重算一筆 `(trade_date, stock_code)` 衍生統計。它不改寫任何原始資料；原始資料持續以股保存。查詢入口為 `python parent_flow_metrics.py --date YYYY-MM-DD --database PATH [--stock-code CODE]`，加上 `--recompute` 可本機重算指定日。

`foreign_*` 與 `trust_*` 分別保存買賣超張數（`net_shares / 1000`）、占當日母股成交量比例（`net_shares / p_volume_shares * 100`）、目前同方向連續日數與該期間累計張數。只有 `institutional_coverage` 是 `COMPLETE` 或 `OFFICIAL_ZERO` 且相應原始列、母股行情存在時可用；成交量為 0 時比例為 `NULL`。正值為連買、負值為連賣、0 立即中斷。從當日向前略過非交易日，但遇到 coverage 不可用或原始資料缺漏即整個法人欄位標為 `UNAVAILABLE`，絕不補零或假定連續。

`active_etf_*` 固定代表「已追蹤主動式 ETF」的持股增減，僅合計 `00980A`、`00985A`、`00999A`、`00982A`、`00992A`，絕非全市場 ETF 買賣超。當日與前一有效交易日都必須五檔 coverage 為 `succeeded`，才計算 `change_lots = (今日合計股數 - 前日合計股數) / 1000` 與 `change_value_twd = 差額股數 * 當日 p_close_price`。`succeeded` 代表該 ETF 的官方完整持股清單已取得，因此在原始 `active_etf_holdings` 中缺少母股列時，僅於衍生合計中視為 0；絕不回寫或合成原始 0 持股列。收盤價為 `NULL` 也不可用。連續增減沿相同規則往前計算；任一必要日期 coverage 不完整或來源失敗即標為 `UNAVAILABLE`。正值為連續增持、負值為連續減持、0 中斷。金額保存新台幣元，未來 UI 才轉萬元。
