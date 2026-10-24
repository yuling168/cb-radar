# CB Radar 外部準時排程

這個 Cloudflare Worker 只做一件事：在台灣時間平日 18:20 與 20:20 呼叫既有的 GitHub `CB Daily Collector` 手動流程。資料收集、SQLite 驗證、Dashboard 與 snapshot 發布仍完全由 GitHub Actions 執行。

## 安全設計

- GitHub token 只以 Cloudflare Worker Secret `GITHUB_WORKFLOW_TOKEN` 保存，絕不寫入 Git、Worker 原始碼或網站資料。
- 請建立 GitHub fine-grained personal access token，僅限 `yuling168/cb-radar`，只授予 **Actions: Read and write**；不要授予不必要的 repository 權限。
- Worker 每次觸發前先查詢相同 workflow 是否已在 queued 或 in-progress；若已有執行，就跳過，避免兩次排程重複寫入資料。
- Worker 的 HTTP health-check 不會觸發 workflow；只有 Cron Trigger 可觸發。

## 部署

1. 在 Cloudflare 建立 Worker，使用此資料夾的 `src/index.js` 與 `wrangler.toml` 設定。
2. 將 `GITHUB_WORKFLOW_TOKEN` 新增為 Worker Secret。
3. 確認 Cron Trigger 為 UTC `20 10 * * 1-5` 與 `20 12 * * 1-5`，也就是台灣時間 18:20、20:20。
4. 以 Cloudflare 的 Cron 測試功能觸發一次，並在 GitHub Actions 確認出現 `workflow_dispatch` 的 `CB Daily Collector` 執行。
5. 保留 GitHub 原有 schedule 作為備援；因 Worker 會略過 already-running workflow，不會與備援重複執行。

## 維運

- GitHub Actions 仍可能有短暫 runner 排隊，但不再依賴 GitHub 的 schedule 派發。
- 若 Worker 出現 401 或 403，先確認 token 未過期且仍有 Actions 的讀寫權限。
- 若 GitHub workflow 檔名改動，必須同步更新 `GITHUB_WORKFLOW`。
