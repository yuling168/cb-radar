"""Build the static GitHub Pages data file from the tracked SQLite database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "cb_history.db"
OUTPUT_PATH = ROOT / "docs" / "data.json"
TABLE_NAME = "cb_daily"
MASTER_TABLE_NAME = "cb_master"
STOCK_DAILY_TABLE_NAME = "stock_daily_market"
CONVERSION_EVENT_TABLE_NAME = "conversion_price_events"
DAILY_REQUIRED_COLUMNS = {
    "trade_date",
    "cb_code",
    "cb_name",
    "close_price",
    "volume_lots",
}
MASTER_REQUIRED_COLUMNS = {
    "cb_code",
    "stock_code",
    "issue_date",
    "maturity_date",
    "put_date",
    "issue_units",
    "issue_amount",
    "balance_amount",
    "balance_date",
    "current_conversion_price",
    "current_conversion_price_effective_date",
    "is_secured",
    "delisting_date",
    "delisting_reason",
}
STOCK_DAILY_REQUIRED_COLUMNS = {
    "trade_date",
    "p_stock_code",
    "p_close_price",
    "p_volume_shares",
}
CONVERSION_EVENT_REQUIRED_COLUMNS = {
    "cb_code",
    "effective_date",
    "conversion_price",
}


def balance_units_for_display(
    issue_amount: int | None, issue_units: int | None, balance_amount: int | None
) -> int | None:
    """Return whole CB units only when the official amounts define them exactly."""
    if balance_amount is None:
        return None
    if issue_amount is None or issue_units is None:
        raise RuntimeError("cb_master balance is missing its official issue basis")
    if issue_amount <= 0 or issue_units <= 0 or issue_amount % issue_units != 0:
        raise RuntimeError("cb_master issue amount/units cannot define a par value")
    par_value = issue_amount // issue_units
    if balance_amount < 0 or balance_amount % par_value != 0:
        raise RuntimeError("cb_master balance amount is not a whole CB unit")
    return balance_amount // par_value


def load_rows() -> list[dict[str, object]]:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"SQLite database not found: {DB_PATH}")

    database_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        for table_name, required_columns in (
            (TABLE_NAME, DAILY_REQUIRED_COLUMNS),
            (MASTER_TABLE_NAME, MASTER_REQUIRED_COLUMNS),
            (STOCK_DAILY_TABLE_NAME, STOCK_DAILY_REQUIRED_COLUMNS),
            (CONVERSION_EVENT_TABLE_NAME, CONVERSION_EVENT_REQUIRED_COLUMNS),
        ):
            if table_name not in tables:
                raise RuntimeError(f"Required SQLite table not found: {table_name}")
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            missing = required_columns - columns
            if missing:
                raise RuntimeError(
                    f"Required SQLite columns missing from {table_name}: {sorted(missing)}"
                )

        cursor = connection.execute(
            """
            SELECT
                daily.trade_date,
                daily.cb_code,
                daily.cb_name,
                daily.close_price,
                daily.volume_lots,
                stock.p_close_price,
                stock.p_volume_shares,
                (
                    SELECT event.conversion_price
                    FROM conversion_price_events AS event
                    WHERE event.cb_code = daily.cb_code
                      AND event.effective_date <= daily.trade_date
                    ORDER BY event.effective_date DESC
                    LIMIT 1
                ) AS conversion_price_on_trade_date,
                master.issue_date,
                master.maturity_date,
                master.put_date,
                master.issue_units,
                master.issue_amount,
                master.balance_amount,
                master.balance_date,
                master.current_conversion_price,
                master.current_conversion_price_effective_date,
                master.is_secured,
                master.delisting_date,
                master.delisting_reason
            FROM cb_daily AS daily
            LEFT JOIN cb_master AS master ON master.cb_code = daily.cb_code
            LEFT JOIN stock_daily_market AS stock
              ON stock.trade_date = daily.trade_date
             AND stock.p_stock_code = master.stock_code
            ORDER BY daily.trade_date DESC, daily.cb_code ASC
            """
        )
        records = []
        for row in cursor:
            record = dict(row)
            conversion_price = record.pop("conversion_price_on_trade_date")
            p_volume_shares = record.pop("p_volume_shares")
            issue_amount = record.pop("issue_amount")
            issue_units = record["issue_units"]
            balance_amount = record.pop("balance_amount")
            record["issue_amount_yi"] = (
                issue_amount / 100_000_000 if issue_amount is not None else None
            )
            record["balance_units"] = balance_units_for_display(
                issue_amount, issue_units, balance_amount
            )
            record["p_volume_lots"] = (
                p_volume_shares // 1_000 if p_volume_shares is not None else None
            )
            record["conversion_value"] = None
            record["premium_rate"] = None
            if conversion_price is not None and record["p_close_price"] is not None:
                if conversion_price <= 0:
                    raise RuntimeError(
                        "conversion_price_events conversion price must be positive"
                    )
                conversion_value = round(
                    record["p_close_price"] / conversion_price * 100, 8
                )
                record["conversion_value"] = conversion_value
                if record["close_price"] is not None and conversion_value != 0:
                    record["premium_rate"] = round(
                        (record["close_price"] / conversion_value - 1) * 100, 8
                    )
            record["is_secured"] = (
                "有" if record["is_secured"] == 1
                else "無" if record["is_secured"] == 0
                else "未知" if record["is_secured"] is None
                else _invalid_is_secured(record["is_secured"])
            )
            records.append(record)
        return records


def _invalid_is_secured(value: object) -> None:
    raise RuntimeError(f"Invalid cb_master.is_secured value: {value!r}")


def build_dashboard_data() -> tuple[int, int]:
    rows = load_rows()
    payload = {"records": rows}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return len(rows), OUTPUT_PATH.stat().st_size


def main() -> None:
    records, size = build_dashboard_data()
    print(f"database: {DB_PATH.relative_to(ROOT).as_posix()}")
    print(f"output: {OUTPUT_PATH.relative_to(ROOT).as_posix()}")
    print(f"records: {records}")
    print(f"bytes: {size}")


if __name__ == "__main__":
    main()

