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

## Current Operational Baseline

- 修改前先讀 `PROJECT_STATUS.md`；其中記錄目前資料發布進度、已知排程問題與下一步限制。
- 目前已驗證的網站資料與策略 A／B／C／G 發布基準是 2026-09-22。發布 Dashboard 時必須同時更新 `docs/data/v2/` 與 `docs/data.json`，因為 B／C／G 的舊頁仍依賴後者。
- GitHub Actions 的排程只保證 best-effort。單一已映射母股在官方日行情缺失時，現行流程會中斷後續策略與發布；查看 Actions 執行結果，不可只用網站日期判定排程是否執行。
- 已下市 CB 的發行日、到期日與發行額，只能寫入已精確驗證的 TPEx 歷史公告結果；沒有可靠公告時必須維持缺值，不能推測或補零。
- 專案根目錄只保留正式程式、規格、文件、正式資料、測試、Git 設定與目前使用中的 Python 環境。暫存資料庫、掃描輸出、log、cache 與一次性測試環境必須放在忽略路徑或系統暫存區，完成後清除，不得混入正式資料或提交。

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
