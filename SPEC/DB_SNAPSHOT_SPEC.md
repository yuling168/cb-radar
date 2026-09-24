# 正式資料庫 Snapshot 與發布規格

## 目的

正式 SQLite `data/cb_history.db` 體積大且不納入 Git。GitHub Release 上不可變的壓縮 snapshot 是正式可還原資料本體；`data/cb_history.manifest.json` 是 Git 追蹤的指標與完整性證據。

## Manifest 合約

manifest 的 `schema_version` 為 1，`snapshot` 至少包含 Release tag、asset 名稱、交易日、建立時間、壓縮與解壓縮 SHA-256、兩種檔案大小；發布後另保存下載 URL。`strategy_a` 保存目前 A-v2 發布序列的定義、baseline 與統計，`provenance` 保存產生來源。

manifest 是 GitHub 上的正式資料版本指標。本機的 `data/cb_history.db` 為工作副本，可能比 manifest 新或舊；只有依 manifest 下載、驗證並還原後，才可宣稱與正式 snapshot 相同。

## 還原

`scripts/restore_db_snapshot.py` 必須在覆寫目的檔前完成下列檢查：

1. asset 名稱、壓縮大小與壓縮 SHA-256 符合 manifest；
2. 解壓後大小與 SHA-256 符合 manifest；
3. SQLite `integrity_check` 為 `ok`、`foreign_key_check` 無結果，且至少存在 `cb_daily`；
4. 只在所有檢查成功後，以原子替換更新目的檔。

任何失敗不得破壞既有目的 DB。

## 發布

`scripts/publish_db_snapshot.py` 對候選 DB 產生可重現 gzip，先建立 draft GitHub Release、上傳 asset、重新下載並以還原器驗證。驗證成功才發布 Release，並輸出候選 manifest；腳本本身不得直接替換正式 manifest。

GitHub Actions 的每日流程為：

1. 依現有 manifest 下載並還原正式 snapshot；
2. 執行 Collector、公告、master、exact parent mapping、母股與策略；
3. 驗證 DB 並建立 Dashboard 分片；
4. 發布並重新驗證候選 snapshot；
5. 安裝候選 manifest，將 manifest 與 Dashboard artifacts 一次 commit/push。

候選 Release 上傳或驗證失敗時，draft 可保留供稽核，但 manifest 不得前進、網站資料不得宣稱已發布新 DB。

## 併發與復原

workflow 使用單一 concurrency group，避免兩個 run 同時以不同 DB 寫入同一發布序列。手動還原可使用：

```bash
python scripts/restore_db_snapshot.py \
  --manifest data/cb_history.manifest.json \
  --asset path/to/cb_history.db.gz \
  --destination data/cb_history.db
```

不得手動把未驗證的 SQLite binary 加入 Git、覆寫 manifest，或把本機 DB 日期視為 GitHub 正式版本。
