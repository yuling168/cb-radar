# CB Radar Project Status

## 1. Project Goal

CB Radar 的目標是建立台灣可轉換公司債（CB）資料與分析系統。專案先逐日累積官方市場資料，後續再逐步加入 CB 基本資料、母股行情、衍生指標、策略雷達與通知。

## 2. Current Phase

Phase 1～3、公告歷史層，以及策略 A／B／C／G 的計算與靜態發布均已納入正式流程。通知與公告事件分類仍未實作。

## 3. Current Data Flow

```text
GitHub Actions（週一～週五台灣 18:10、20:10；GitHub 排程可能延遲）
↓
Phase 1：TPEx CB 行情 → announcement collector（TWSE／TPEx）
↓
Phase 2：CB Master／lifecycle → Phase 3：母股映射與母股行情
↓
策略 A／B／C／G
↓
data/cb_history.db
↓
scripts/build_dashboard.py
↓
docs/data/v2/（新版分片）＋ docs/data.json（舊策略頁相容資料）
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

- cron：`10 10 * * 1-5`、`10 12 * * 1-5`，即星期一至星期五台灣時間 18:10、20:10。
- 支援 `workflow_dispatch` 手動執行。
- Runner：`ubuntu-latest`；Python：3.11。
- 安裝：`python -m pip install -r requirements.txt`。
- 順序：Phase 1 `collector.py` → `announcement_collector.py` → Phase 2 `master_collector.py` → 母股映射與 `stock_collector.py` → 策略 A／B／C／G → DB validation → Dashboard → 單一 commit/push。
- 公告 collector 每日各抓一次 TWSE、TPEx；任一市場最終失敗會使 workflow failed，但已成功市場與 failed fetch 都保留。
- `workflow_dispatch` 與排程使用相同流程；手動執行適合用於驗證當日完整鏈路或指定交易日的窄範圍復原。
- Dashboard build：`python scripts/build_dashboard.py`。
- 監看 snapshot manifest、`docs/data/v2/` 與 `docs/data.json`；任一發布內容變更才 commit/push 回 `main`。
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

### GitHub Actions 的單檔母股缺失

若 `stock_collector.py` 無法在官方日行情取得某個已映射母股（2026-09 曾發生代號 3591），目前 workflow 會失敗，後續策略、Dashboard 與自動提交都會跳過。這是排程「有觸發但網站沒有更新」的主要已知原因；修復方向是將單一缺失保存為明確不可用狀態，而非中斷整個日期流程。

GitHub 的 `schedule` 為 best-effort，可能較設定時間延後數小時；排查時應先看 Actions 執行紀錄，不應只以網站日期判定排程未執行。

## 9. Git History Milestones

依 2026-08-29 的實際 `git log`：

- `6ab0bdf` — Initialize CB radar collector
- `cc891b3` — Add daily CB collector workflow
- `c07720e` — Update CB history 2026-08-28（GitHub Actions 資料更新 commit）
- `0f5292a` — Add CB history dashboard
- `64daf2e` — Improve dashboard mobile layout

GitHub Actions 後續產生的 `Update CB history YYYY-MM-DD` commit 屬於每日資料更新，不是功能里程碑。

## 10. Announcement History and Lifecycle

- `announcement_fetch`：每次 TWSE／TPEx API 抓取結果；失敗不可當作零公告。
- `announcement_snapshot`：完整官方 JSON raw snapshot，不覆寫。
- `company_announcements`：正規化每日公告，以 logical/event key 冪等保存。
- `historical_company_announcements`：公告歷史層建立前的官方歷史補洞；目前只接受 `MOPS_HISTORICAL_DETAIL`，不偽造 TWSE／TPEx snapshot，也不是 daily dependency。
- 強制贖回優先讀每日公告，歷史補洞再讀 historical 表；精確 CB 代碼與「行使債券贖回權」後，唯一收回基準日覆寫 lifecycle。缺日期或衝突日期必須失敗。
- 15601 中砂一：MOPS 2026-07-15 公告、收回基準日 2026-09-02、TPEx 終止交易日 2026-09-03；正式 lifecycle 為 `2026-09-02 / 已贖回`。

## 11. Historical delisted-CB issuance terms

- 現行 `cb_master` 的官方來源只涵蓋仍掛牌 CB；已下市 CB 的基本發行條款不可假設會存在於該表。
- `cb_historical_issuance_terms` 是獨立、較窄的補充表，只保存 TPEx 歷史上櫃公告已精確驗證的 `issue_date`、`maturity_date`、`issue_amount` 與公告 URL；它不替代完整 `cb_master`。
- 擷取器：`historical_issuance_collector.py`。先查 TPEx 公告索引第 6 類，再依公告日期與文號取得 TPEx 靜態原文；必須精確且唯一匹配 CB 代碼，並同時解析三個必要欄位才可寫入。
- 已完成 2005–2026 的索引掃描。已驗證 6 檔；另有 279 檔尚未取得可安全寫入的完整公告條款。不得把「沒有命中」當成 0、空值推測或已完成回補。
- Dashboard 會以 `COALESCE(cb_master, cb_historical_issuance_terms)` 顯示這三個歷史條款，因此缺少完整 master 的已下市 CB 仍可顯示經驗證的發行／到期日與發行額。

## 12. Strategy and dashboard publication

- 策略 A 由 `strategy_runs.py --run-published-date YYYY-MM-DD` 發布；B／C／G 分別由 `strategy_b.py`、`strategy_c.py`、`strategy_g.py` 計算。
- 策略前提是當日 `cb_daily`、`cb_parent_stock_mapping` 與 `stock_daily_market` 都完整。補每日行情後，必須依序補映射、母股行情、策略，再 build Dashboard。
- `scripts/build_dashboard.py` 同時輸出 `docs/data/v2/` 與 `docs/data.json`。新版頁使用分片；`strategy-b.html`、`strategy-c.html`、`strategy-g.html` 仍讀取 `docs/data.json`。發布策略結果時兩者都必須提交，否則舊策略頁會停在舊日期。
- 2026-09-22 已完整回補並發布市場資料與 A／B／C／G 策略。9/21、9/22 的母股映射與母股行情均已補齊。

## 13. Not Implemented Yet

- 5MA
- 20MA
- 通知系統
- AI 分析

## 14. Planned Next Phase

下一階段為公告事件分類、通知與策略功能；不得將它們混入每日保存／lifecycle 流程。

## 15. Workspace Hygiene

- 專案已清理一次性 pytest 環境、cache、log、暫存掃描輸出與測試資料庫；目前根目錄只保留正式程式、`data/` 正式 DB、`docs/` 網站發布內容、`SPEC/`、`scripts/`、`tests/`、GitHub workflow 與目前使用中的 `.venv311/`。
- `data/cb_history.db`、`docs/`、`.github/`、`SPEC/`、`scripts/`、`tests/`、`.git/` 及 `.venv311/` 均是目前需要保留的內容；`.venv311/` 雖可重建，但正被本機工作流程使用，不應在未重建環境前刪除。
- 新的一次性輸出不得放在專案根目錄；應使用已忽略的暫存路徑或系統暫存區，並於工作完成後移除。

## 16. How Future Codex Sessions Should Start

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

## 17. Phase 2 CB Master Data

- Collector：`master_collector.py`。
- 規格：`SPEC/CB_MASTER_SPEC.md`。
- 官方發行資料：TPEx OpenAPI `bond_ISSBD5_data`。
- 現行債與 MOPS link：TPEx `bond/convSearch`。
- 下市 lifecycle：TPEx `bond/convDelist`；正式下市日到達後不再列入 active universe，但歷史 master、價格與餘額資料保留。
- 發行張數、歷史轉換價與月餘額：MOPS `t120sg01` 月申報頁。
- `cb_master.balance_amount`／`balance_date`：固定採同一筆 TPEx `OutstandingAmount`／`Date`，代表最新官方狀態；不得以 MOPS 月底或 Collector 執行日取代。MOPS `monyr_reg` 僅為報表月份，只有已完整結束月份可使用月底並寫入 `cb_monthly_balance`；未完成月份不得推定月底。
- 最新有效轉換價補強：MOPS `t108sb08_1` 轉換價格變更公告；僅套用執行日已生效事件。
- 是否有擔保：TPEx `Guaranteed` 與 `GuaranteeDescription` 結構化欄位。
- 普通 CB 篩選：以 MOPS 官方債券中文名稱區分「轉換公司債」與「交換公司債」，後者排除。
- `cb_master.current_conversion_price_effective_date` 與最新有效價格由同一事件同步更新。
- 新增資料表：`cb_master`、`conversion_price_events`、`cb_monthly_balance`。
- 正式 DB 目前先保存 5 檔端到端驗證資料；執行時不帶 `--codes` 才會處理官方來源中的全部現行新台幣 CB。
- Phase 2 已在 daily workflow 中於 announcement collector 後執行；公告可在同次 run 供 lifecycle 讀取。
- 月申報可能落後已生效公告；Collector 會以 MOPS 官方轉換價格變更公告補強，並以生效日決定目前價格。
