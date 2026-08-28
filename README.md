# CB Radar — 台股可轉債每日行情

目前 Phase 1 從 TPEx 官方來源抓取與保存台灣可轉換公司債每日行情，並提供 GitHub Pages 靜態 Dashboard。現階段仍不包含雷達策略、轉換價值、溢價率、通知或 Web API。

長期交接與目前進度請先閱讀 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)，開發規則見 [`AGENTS.md`](AGENTS.md)，Dashboard 現況規格見 [`SPEC/DASHBOARD_SPEC.md`](SPEC/DASHBOARD_SPEC.md)。

## 安裝

需 Python 3.11 以上：

```bash
cd cb-radar
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 執行

預設由今天往回尋找最近 14 天內 TPEx 已發布的有效交易日：

```bash
python collector.py
```

指定日期只抓該日；非交易日或尚未發布時會清楚回報、正常結束且不寫資料：

```bash
python collector.py --date 2026-08-28
```

預設資料庫為 `data/cb_history.db`，可用 `--database` 指定其他路徑。

## 官方資料來源與處理方式

- 報表索引：`https://www.tpex.org.tw/www/zh-tw/bond/cbDaily`
- 參數：`date=YYYY/MM/DD&fileCode=rsta0113`
- 每日 CSV：索引回傳的 TPEx `/storage/bond_zone/tradeinfo/cb/.../RSta0113.YYYYMMDD-C.csv`
- 官方頁面：`https://www.tpex.org.tw/zh-tw/bond/info/statistics-cb/day.html`
- 報表：每日轉(交)換公司債買賣斷交易行情表（含議價及電腦交易）

CSV 是 CP950 編碼。同一 CB 的「等價」與「議價」分列；本資料表的一列代表交易所電腦交易行情，因此只取「交易 = 等價」。欄位 mapping：

| TPEx 原始欄位 | 程式/SQLite 欄位 |
|---|---|
| `DATADATE` 的日期 | `trade_date` |
| `代號` | `cb_code` |
| `名稱` | `cb_name` |
| `收市` | `close_price` |
| `單位` | `volume_lots` |

TPEx 可轉債的一個交易單位為新台幣 10 萬元面額，即市場慣稱一張，因此 `單位` 數值以 1:1 保存為張。逗號只作千分位移除，不做倍率換算。

成交量以 TPEx 有效交易日計算。若某 CB 確實存在於該交易日官方 RSta0113 等價市場資料中，而官方成交量欄位為空白，視為當日 0 張成交並保存為 0；非交易日、來源取得失敗、資料缺漏或無法確認時，不得自行補 0。

- `0`：已確認有效交易日，且 CB 存在於格式驗證成功的官方檔，但沒有成交。
- 缺資料：來源或格式無法確認，不建立資料列，也不得自行製造 `0`。
- 收市價空白獨立處理為 SQLite `NULL`，不會轉成價格 0。

執行結果中的 `official_rows` 是 CSV 所有 `BODY` 列（包含等價及議價），`equivalent_market_rows` 則是實際保存的所有等價市場 CB 數。

## SQLite schema

```sql
CREATE TABLE IF NOT EXISTS cb_daily (
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

主鍵使同日同代號重跑時更新原列，不產生重複資料。

查詢某 CB 最近 20 筆：

```sql
SELECT *
FROM cb_daily
WHERE cb_code = '17172'
ORDER BY trade_date DESC
LIMIT 20;
```

## 測試

```bash
pytest -q
```

## 自動化與 Dashboard

`.github/workflows/daily-collector.yml` 於星期一至星期五台灣時間 20:30 執行 Collector，成功後以 `scripts/build_dashboard.py` 從既有 SQLite 產生 `docs/data.json`。有追蹤輸出變更時，GitHub Actions 才 commit/push 回 `main`。

Dashboard：<https://yuling168.github.io/cb-radar/>

GitHub Pages 的瀏覽器端只載入 `docs/data.json`，不直接開啟 SQLite binary。頁面支援日期篩選、CB 名稱／代號搜尋、摘要與欄位排序。
