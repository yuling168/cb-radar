# CB Radar Project Status

## 1. Project Goal

CB Radar 的目標是建立台灣可轉換公司債（CB）資料與分析系統。專案先逐日累積官方市場資料，後續再逐步加入 CB 基本資料、母股行情、衍生指標、策略雷達與通知。

## 2. Current Phase

目前是 **Phase 1：Daily CB Market Data Collection + Dashboard**，第一階段已完成並上線。第二階段及策略功能尚未實作。

## 3. Current Data Flow

```text
TPEx 官方每日 CB 資料
↓
GitHub Actions
↓
collector.py
↓
data/cb_history.db
↓
scripts/build_dashboard.py
↓
docs/data.json
↓
docs/index.html
↓
GitHub Pages
```

GitHub Actions 在 GitHub-hosted Linux runner 執行，使用者電腦不需要保持開機。

## 4. Database

正式歷史資料庫是 `data/cb_history.db`，正式行情資料表是 `cb_daily`。2026-08-29 檢查到的實際 schema：

```sql
CREATE TABLE cb_daily (
    trade_date TEXT NOT NULL,
    cb_code TEXT NOT NULL,
    cb_name TEXT NOT NULL,
    close_price REAL,
    volume_lots INTEGER NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, cb_code)
);
```

`volume_lots` 的單位是「張」，且不得為 `NULL`。`close_price` 可以是 `NULL`。

## 5. Collector Rules

- 官方來源是 TPEx 每日可轉債 RSta0113 CSV，編碼為 CP950。
- 下載前先從 TPEx `cbDaily` index 解析指定日期的官方檔案路徑。
- CSV 必須通過編碼、欄數、唯一 HEADER、完整欄位及 DATADATE 日期驗證。
- 只處理 `交易 = 等價` 的市場列。
- 已確認有效、格式完整的官方等價市場列，其 `單位`空白才轉為 `volume_lots = 0`。
- 官方 `收市`空白保存為 `close_price = NULL`，不可轉成 0。
- 成交量 0 必須保留，因為它代表該 CB 當日存在於有效官方資料但無成交。
- 非交易日、CB 不存在於官方資料、來源失敗或資料無法驗證時，不可自行建立 0 或假資料。
- HTTP、CSV、HEADER 或日期驗證失敗會明確失敗；指定日期沒有官方報表時正常回報未發布且不寫資料。
- `(trade_date, cb_code)` 是主鍵；同日期、同 CB 使用 upsert，避免重複列。
- TPEx `單位`對 CB 是新台幣 10 萬元面額，等同市場慣稱 1 張，目前以 1:1 保存。

## 6. Automation

Workflow：`.github/workflows/daily-collector.yml`

- cron：`30 12 * * 1-5`，即星期一至星期五 UTC 12:30／台灣時間 20:30。
- 支援 `workflow_dispatch` 手動執行。
- Runner：`ubuntu-latest`；Python：3.11。
- 安裝：`python -m pip install -r requirements.txt`。
- Collector：`python collector.py`，未指定日期時向前尋找最近 14 天內已發布的有效交易日。
- Dashboard build：`python scripts/build_dashboard.py`。
- 監看 `data/cb_history.db`、`docs/data.json`、`docs/index.html`；任一變更才 commit/push 回 `main`。
- 自動 commit 使用 `github-actions[bot]`，push 使用內建 `GITHUB_TOKEN`，workflow 權限是 `contents: write`，不需要 PAT。
- concurrency group 是 `cb-daily-collector-main`，`cancel-in-progress: false`，避免排程與手動執行同時寫 DB。
- Collector 若失敗，後續 build/commit 不會執行；沒有資料變更時 workflow 正常結束。

## 7. Dashboard

- GitHub Pages：<https://yuling168.github.io/cb-radar/>
- 資料來源：`docs/data.json`。
- 頁面：`docs/index.html`。
- 產生器：`scripts/build_dashboard.py`，以 SQLite read-only URI 讀取 `cb_daily`。
- 日期選單列出所有實際交易日並預設最新交易日。
- 支援 CB 名稱或代號的部分文字搜尋，並可與日期篩選同時使用。
- 摘要卡顯示最新日期、當日總數、有成交數、0 張數與收盤價 NULL 數。
- 表格可依 CB 代號、名稱、收盤價或成交量排序。
- `close_price = NULL` 顯示「—」；`volume_lots = 0` 顯示 `0`。
- 已有 desktop responsive 與 mobile responsive 基本版。

## 8. Known Issues / Technical Debt

### Mobile table

手機上方控制區與摘要卡已 responsive，但 CB 明細仍是橫向 table。窄螢幕查看成交量等右側欄位時，可能需要在 table 區域內左右滑動；這是目前已知限制。

未來可保留桌機 table，並在 `<= 768px` 改為 card／stacked row layout，使日期、CB 代號、CB 名稱、收盤價與成交量不需水平滑動即可同時看到。本階段尚未實作。

### SQLite rerun

`parse_tpex_csv()` 每次執行都產生新的 `collected_at`，而 `upsert_daily()` 在衝突時會更新該欄位。因此同一天重跑時，即使行情 business data 沒有改變，SQLite binary 仍可能改變並造成新的資料 commit。這是待處理的 technical debt，本次未修改。

## 9. Tests / Validation

`tests/test_collector.py` 目前有 7 個測試，驗證：

- 正常成交量以張保存，並驗證面額換算。
- 有效官方等價市場列的 blank volume 轉為 0 且確實入庫。
- blank close price 保存為 `NULL`。
- TPEx 整體來源失敗時不建立 DB 或大量假 0。
- 必要 HEADER 欄位消失時明確失敗。
- 同日期、同 CB 重跑只保留一列，回報 update 而非重複 insert。
- 非交易日沒有官方 index 資料時不寫假資料。

## 10. Git History Milestones

依 2026-08-29 的實際 `git log`：

- `6ab0bdf` — Initialize CB radar collector
- `cc891b3` — Add daily CB collector workflow
- `c07720e` — Update CB history 2026-08-28（GitHub Actions 資料更新 commit）
- `0f5292a` — Add CB history dashboard
- `64daf2e` — Improve dashboard mobile layout

GitHub Actions 後續產生的 `Update CB history YYYY-MM-DD` commit 屬於每日資料更新，不是功能里程碑。

## 11. Not Implemented Yet

- CB 基本資料層
- 母股每日行情
- 轉換價值
- 溢價率
- 剩餘比率
- 5MA
- 20MA
- A/B/C 策略雷達
- 通知系統
- AI 分析

## 12. Planned Next Phase

下一階段暫定為 **Phase 2：CB Basic Data Layer**。尚未開始開發。

## 13. How Future Codex Sessions Should Start

新的 Codex session 在修改專案前，應依序閱讀：

1. `AGENTS.md`
2. `PROJECT_STATUS.md`
3. `README.md`
4. `SPEC/`
5. `CHANGELOG.md`
6. 與任務相關的 source code
7. tests
8. git history

不得只根據使用者口述直接大幅修改架構；描述與 repository 不一致時，以實際程式、schema、workflow 與測試為準並回報差異。
