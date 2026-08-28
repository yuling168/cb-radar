# CB Radar Development Rules

開始任何工作前，先閱讀 `PROJECT_STATUS.md`、相關 `SPEC/`、現有程式、測試與 Git 狀態。若文件與實作不一致，以實際程式、SQLite schema、workflow 與測試為準，並指出差異。

## Data Integrity

- 官方資料優先；目前每日行情來源是 TPEx。
- 不可自行製造缺漏資料。
- 只有符合既定規則、已通過完整驗證的官方等價市場列，其 blank volume 才能轉成 0。
- `NULL` 與 `0` 必須區分：價格空白是 `NULL`，已確認無成交的成交量才是 0。
- 不可為了讓策略可計算而自行補假資料。

## Database

- `data/cb_history.db` 是正式歷史 DB，必須保留並謹慎處理。
- 不得未經明確需求自行修改 schema。
- schema 修改必須規劃 migration，並考慮既有歷史資料與向後相容性。
- 不得以刪除或重建歷史資料來解決程式問題。
- 不得在只讀、文件或前端任務中重跑 Collector 或改動正式 DB。

## Development

修改前：

1. 讀取 `PROJECT_STATUS.md`。
2. 讀取相關 `SPEC/`。
3. 檢查現有程式。
4. 檢查 tests。
5. 執行 `git status`，保留使用者既有變更。

修改後：

1. 執行與變更相關的 tests／驗證。
2. 執行 `git diff --check`。
3. 回報實際修改檔案。
4. 回報是否影響 schema、正式 DB 或 workflow。

## Git

除非使用者明確要求：

- 不自動 commit。
- 不自動 push。
- 不 force push。
- 不刪除或改寫歷史 commit。
- 不 stage 或提交無關檔案。

## Scope Control

- 使用者要求修改 Dashboard 時，不得順便修改 Collector、schema 或 strategy。
- 使用者要求修改 Collector 時，不得順便加入新策略。
- 一次只處理使用者明確要求的 scope；不可將未完成 phase 偷渡進目前工作。

## Security

- 不將 token、password、secret、private key、`.env` 或其他 credentials 寫入 repository。
- GitHub Actions 優先使用 GitHub Secrets 或內建 `GITHUB_TOKEN`，採最小必要權限。
- 不在程式輸出、workflow log、文件或回報中顯示 credentials。
