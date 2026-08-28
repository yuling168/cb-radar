# Dashboard Specification

本文件描述 Phase 1 Dashboard **目前已實作**的行為，不包含未完成的未來設計。

## Architecture

```text
data/cb_history.db (SQLite / cb_daily)
↓ scripts/build_dashboard.py
docs/data.json
↓ fetch("./data.json")
docs/index.html (HTML + CSS + vanilla JavaScript)
↓
GitHub Pages
```

瀏覽器不直接讀取 SQLite binary。`scripts/build_dashboard.py` 以 read-only SQLite URI 讀 DB，驗證資料表與必要欄位後，產生 UTF-8 JSON；不推算日期、不補資料，也不修改 DB。

## JSON Schema

`docs/data.json` 的頂層格式：

```json
{
  "records": [
    {
      "trade_date": "2026-08-28",
      "cb_code": "17172",
      "cb_name": "長興二",
      "close_price": 133.5,
      "volume_lots": 65
    }
  ]
}
```

每筆 record 包含：

| 欄位 | JSON 型別 | 說明 |
|---|---|---|
| `trade_date` | string | SQLite 中實際存在的 ISO 日期 |
| `cb_code` | string | CB 代號 |
| `cb_name` | string | CB 名稱，UTF-8 中文不做 ASCII escape |
| `close_price` | number 或 null | 官方收市價；缺值為 `null` |
| `volume_lots` | integer | 成交量（張），已確認無成交為 `0` |

輸出順序是 `trade_date DESC, cb_code ASC`。產生器會先驗證 `cb_daily` 及上述必要欄位存在。

## Filtering

- 日期選單使用 JSON 中實際存在的所有 `trade_date`，由新到舊排序，預設最新日期。
- 搜尋框對 `cb_code` 與 `cb_name` 做部分文字比對。
- 英文字母比對不區分大小寫。
- 日期與 CB 搜尋條件可以同時套用；清空搜尋會恢復該日期全部資料。
- 結果列及摘要都只使用載入的真實 JSON record。

## Sorting

表格可依以下欄位排序，重複點擊同一欄切換升冪／降冪：

- CB 代號
- CB 名稱
- 收盤價
- 成交量（張）

初始排序為 CB 代號升冪。`null` 收盤價排在非 null 值之後。

## Summary Cards

頁面顯示：

1. JSON 中的最新交易日。
2. 選定日期 CB 總數。
3. 選定日期 `volume_lots > 0` 的 CB 數。
4. 選定日期 `volume_lots === 0` 的 CB 數。
5. 選定日期 `close_price === null` 的 CB 數。

搜尋文字只篩選結果表格；摘要卡統計選定日期的全部 CB。

## Display Rules

- `close_price = null` 顯示「—」，不可顯示 0。
- `volume_lots = 0` 明確顯示 `0`，不可顯示空白。
- 顯示目前篩選結果筆數。
- 資料載入失敗或沒有符合資料時，顯示清楚的狀態訊息。

## Responsive Behavior

- 頁面主容器寬度 100%，桌機最大寬度 1180px。
- 桌機摘要卡多欄排列，日期與搜尋控制項橫向排列。
- `<= 880px` 摘要卡改為兩欄。
- `<= 768px` 日期與搜尋改為上下排列，控制項寬度 100%，字體至少 16px。
- `<= 480px` 摘要卡改為單欄，並縮小標題、卡片與表格間距。
- 頁面本身禁止水平溢出；表格由 `.table-wrap` 提供區域水平滑動。

## GitHub Pages Deployment

- 公開網址：<https://yuling168.github.io/cb-radar/>
- GitHub Pages 使用 `main` branch 的 `/docs` 目錄。
- Daily Collector workflow 在 Collector 成功後執行 `python scripts/build_dashboard.py`。
- `data/cb_history.db`、`docs/data.json` 或 `docs/index.html` 任一變更時，workflow 才 commit/push 回 `main`。

## Known Mobile Limitation

目前 mobile responsive 是基本版。CB 明細仍是有合理 `min-width` 的 table，因此窄螢幕查看右側欄位時，仍可能需要在 table wrapper 內水平滑動。

尚未實作 mobile card／stacked row layout。未來可在 `<= 768px` 將每列改為卡片，使日期、CB 代號、CB 名稱、收盤價與成交量不需水平滑動即可同時看到；本文件不將此功能標示為已完成。
