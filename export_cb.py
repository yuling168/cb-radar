"""Export one CB's recent history as a Markdown file for LLM analysis (read-only).

Usage:
    python export_cb.py 11011 [--days 120] [--database data/cb_history.db] [--output-dir exports]
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path


def _fmt(value, nd=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{nd}f}"
    return str(value)


def export_cb(database: str, cb_code: str, days: int, output_dir: str, since: str | None = None) -> Path:
    conn = sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    master = conn.execute("SELECT * FROM cb_master WHERE cb_code = ?", (cb_code,)).fetchone()
    if master is None:
        # CB present in cb_daily but absent from cb_master: keep it, with unknown metadata.
        name_row = conn.execute("SELECT cb_name FROM cb_daily WHERE cb_code = ? LIMIT 1", (cb_code,)).fetchone()
        master = {k: None for k in (
            "stock_name", "stock_code", "issue_date", "maturity_date", "put_date", "issue_amount",
            "balance_amount", "balance_date", "current_conversion_price",
            "current_conversion_price_effective_date", "delisting_date", "delisting_reason")}
        master["cb_name"] = name_row[0] if name_row else cb_code
        master["stock_name"] = master["stock_code"] = "未知(cb_master 无此 CB)"

    rows = conn.execute(
        "SELECT trade_date, close_price, reference_price, volume_lots FROM cb_daily "
        "WHERE cb_code = ? AND trade_date >= ? ORDER BY trade_date DESC LIMIT ?",
        (cb_code, since or "0000-00-00", days),
    ).fetchall()
    rows = list(reversed(rows))
    if not rows:
        raise SystemExit(f"cb_daily 没有 {cb_code} 的交易记录")
    start, end = rows[0]["trade_date"], rows[-1]["trade_date"]

    stock = {
        r["trade_date"]: r
        for r in conn.execute(
            "SELECT trade_date, p_close_price, p_volume_shares FROM stock_daily_market "
            "WHERE p_stock_code = ? AND trade_date BETWEEN ? AND ?",
            (master["stock_code"], start, end),
        )
    }
    events = conn.execute(
        "SELECT effective_date, conversion_price FROM conversion_price_events "
        "WHERE cb_code = ? ORDER BY effective_date",
        (cb_code,),
    ).fetchall()
    balances = conn.execute(
        "SELECT year_month, balance_amount FROM cb_monthly_balance "
        "WHERE cb_code = ? ORDER BY year_month DESC LIMIT 6",
        (cb_code,),
    ).fetchall()
    market_dates = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ?", (start, end)
        )
    }
    own_dates = {r["trade_date"] for r in rows}
    no_record = sorted(market_dates - own_dates)
    stock_missing = sorted(own_dates - set(stock))

    def conv_price(trade_date: str):
        price = None
        for ev in events:
            if ev["effective_date"] <= trade_date:
                price = ev["conversion_price"]
        return price if price is not None else master["current_conversion_price"]

    lines = [
        f"# {master['cb_name']} ({cb_code}) 近 {len(rows)} 个有记录交易日",
        "",
        "> **分析规则**:只能根据本文件的数据回答。文件没有的数据请直接说「没有数据」,不要猜测或引用记忆。",
        "",
        f"- 导出日期: {date.today().isoformat()}",
        f"- 数据区间: {start} ~ {end}(本档共 {len(rows)} 个有记录交易日)",
        "",
        "## 基本资料",
        f"- 对应股票: {master['stock_name']} ({master['stock_code']})",
        f"- 发行日: {master['issue_date']} / 到期日: {master['maturity_date']} / 回售日: {_fmt(master['put_date'])}",
        f"- 发行金额: {_fmt(master['issue_amount'])} / 最新余额: {_fmt(master['balance_amount'])}(截至 {_fmt(master['balance_date'])})",
        f"- 最新转换价: {_fmt(master['current_conversion_price'])}(生效 {_fmt(master['current_conversion_price_effective_date'])})",
        f"- 下市日: {_fmt(master['delisting_date'])} / 原因: {_fmt(master['delisting_reason'])}",
        "",
    ]
    if events:
        lines += ["## 转换价调整记录", "| 生效日 | 转换价 |", "|---|---|"]
        lines += [f"| {e['effective_date']} | {_fmt(e['conversion_price'])} |" for e in events]
        lines.append("")
    if balances:
        lines += ["## 近期月底余额", "| 年月 | 余额 |", "|---|---|"]
        lines += [f"| {b['year_month']} | {b['balance_amount']} |" for b in balances]
        lines.append("")

    lines += [
        "## 数据质量",
        f"- 同期其他 CB 有交易、但本档无记录的日期(可能是当日无成交,也可能是缺漏,无法区分): "
        f"{', '.join(no_record) if no_record else '无'}",
        f"- 本档有交易但缺少股票行情的日期: {', '.join(stock_missing) if stock_missing else '无'}",
        "",
        "## 每日数据",
        "字段说明: CB收盘、参考价为 TPEx 数据;成交量单位为「张」;股票收盘为对应标的收盘价;"
        "转换价值 = 股价 ÷ 当日转换价 × 100(程序估算);溢价率 = CB收盘 ÷ 转换价值 − 1(程序估算)。",
        "",
        "| 日期 | CB收盘 | 参考价 | 成交量(张) | 股票收盘 | 转换价 | 转换价值 | 溢价率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in reversed(rows):
        d = r["trade_date"]
        s = stock.get(d)
        sp = s["p_close_price"] if s else None
        cp = conv_price(d)
        value = sp / cp * 100 if sp and cp else None
        prem = (r["close_price"] / value - 1) * 100 if value and r["close_price"] else None
        lines.append(
            f"| {d} | {_fmt(r['close_price'])} | {_fmt(r['reference_price'])} | {r['volume_lots']} | "
            f"{_fmt(sp)} | {_fmt(cp)} | {_fmt(value)} | {_fmt(prem)}% |"
            if prem is not None
            else f"| {d} | {_fmt(r['close_price'])} | {_fmt(r['reference_price'])} | {r['volume_lots']} | "
            f"{_fmt(sp)} | {_fmt(cp)} | {_fmt(value)} | - |"
        )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"cb_{cb_code}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.close()
    return path


def _write_xlsx(conn: sqlite3.Connection, path: Path, since: str | None) -> None:
    """Same rows as all_cb_daily.csv, as a real spreadsheet with numeric columns."""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping all_cb_daily.xlsx")
        return
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("all_cb_daily")
    ws.append(["trade_date", "cb_code", "cb_name", "close_price", "reference_price", "volume_lots"])
    for r in conn.execute(
        "SELECT trade_date, cb_code, cb_name, close_price, reference_price, volume_lots "
        "FROM cb_daily WHERE trade_date >= ? ORDER BY cb_code, trade_date",
        (since or "0000-00-00",),
    ):
        ws.append(list(r))
    wb.save(path)


def export_all(database: str, days: int, output_dir: str, since: str | None) -> None:
    """Write one Markdown per CB, a combined daily CSV and an index (all read-only)."""
    conn = sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)
    codes = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT cb_code FROM cb_daily WHERE trade_date >= ? ORDER BY cb_code",
            (since or "0000-00-00",),
        )
    ]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_lines = ["trade_date,cb_code,cb_name,close_price,reference_price,volume_lots"]
    for r in conn.execute(
        "SELECT trade_date, cb_code, cb_name, close_price, reference_price, volume_lots "
        "FROM cb_daily WHERE trade_date >= ? ORDER BY cb_code, trade_date",
        (since or "0000-00-00",),
    ):
        csv_lines.append(",".join("" if v is None else str(v).replace(",", " ") for v in r))
    (out / "all_cb_daily.csv").write_text("\n".join(csv_lines) + "\n", encoding="utf-8-sig")
    _write_xlsx(conn, out / "all_cb_daily.xlsx", since)
    index = ["# CB 清单", "", f"导出日期 {date.today().isoformat()};区间 {since or '全部'} 起", "",
             "| CB代码 | 名称 | 股票 | 到期日 |", "|---|---|---|---|"]
    done = 0
    for code in codes:
        m = conn.execute("SELECT cb_name, stock_name, stock_code, maturity_date FROM cb_master WHERE cb_code=?", (code,)).fetchone()
        try:
            export_cb(database, code, days, output_dir, since)
        except SystemExit as exc:
            print(f"skip {code}: {exc}")
            continue
        done += 1
        index.append(f"| {code} | {m[0] if m else '-'} | {(m[1] + ' ' + m[2]) if m else '-'} | {m[3] if m else '-'} |")
    (out / "_index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"exported {done}/{len(codes)} CBs to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cb_code", nargs="?", help="CB code; omit with --all")
    parser.add_argument("--all", action="store_true", help="export every CB with data since --since, plus a CSV and index")
    parser.add_argument("--since", help="only include trade dates >= YYYY-MM-DD (e.g. 2026-01-01)")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--database", default="data/cb_history.db")
    parser.add_argument("--output-dir", default="exports")
    args = parser.parse_args()
    if args.all:
        export_all(args.database, args.days, args.output_dir, args.since)
    elif args.cb_code:
        print(export_cb(args.database, args.cb_code, args.days, args.output_dir, args.since))
    else:
        parser.error("provide cb_code or --all")


if __name__ == "__main__":
    main()



