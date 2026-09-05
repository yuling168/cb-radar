"""Strategy C-v1: rank fundamentally qualified CBs from saved historical data."""

from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
from decimal import Decimal
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import DEFAULT_DB_PATH
from db import connect


STRATEGY_CODE = "C"
STRATEGY_VERSION = "v1"
STRATEGY_NAME = "CB 資優生"
BUCKETS = ((100.0, 105.0), (105.0, 110.0), (110.0, 115.0), (115.0, 120.0))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bucket_for(conversion_value: Decimal) -> str | None:
    for lower, upper in BUCKETS:
        lower_decimal, upper_decimal = Decimal(str(lower)), Decimal(str(upper))
        if lower_decimal <= conversion_value and (
            conversion_value < upper_decimal or upper == 120.0 and conversion_value <= upper_decimal
        ):
            return f"{lower:g}-{upper:g}" if upper != 120.0 else "115-120"
    return None


def _monthly_balance_date(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _balance_on(connection: sqlite3.Connection, cb_code: str, trade_date: str) -> tuple[int, str] | None:
    """Return only a balance whose official as-of date is not in the future.

    Monthly MOPS balances represent completed reporting months, so the report
    month-end is their official as-of date.  The current TPEx snapshot is also
    usable when its explicitly supplied balance_date is no later than the day.
    """
    candidates: list[tuple[str, int]] = []
    month = trade_date[:7]
    row = connection.execute(
        """SELECT year_month, balance_amount FROM cb_monthly_balance
           WHERE cb_code = ? AND year_month < ?
           ORDER BY year_month DESC LIMIT 1""",
        (cb_code, month),
    ).fetchone()
    if row is not None:
        candidates.append((_monthly_balance_date(str(row["year_month"])), int(row["balance_amount"])))
    row = connection.execute(
        "SELECT balance_amount, balance_date FROM cb_master WHERE cb_code = ? AND balance_date <= ?",
        (cb_code, trade_date),
    ).fetchone()
    if row is not None and row["balance_amount"] is not None and row["balance_date"] is not None:
        candidates.append((str(row["balance_date"]), int(row["balance_amount"])))
    if not candidates:
        return None
    balance_date, balance_amount = max(candidates, key=lambda item: item[0])
    return balance_amount, balance_date


def _unavailable(cb_code: str, trade_date: str, reasons: list[str], values: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"cb_code": cb_code, "trade_date": trade_date, "data_status": "UNAVAILABLE", "unavailable_reasons": reasons,
            "conditions": {}, "values": values or {}, "evaluated_at": _now(), "signal_created": False}


def _record_evaluation(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO strategy_evaluations (cb_code, trade_date, strategy_code, strategy_version, strategy_name,
            condition_results_json, condition_values_json, data_status, unavailable_reasons_json, evaluated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION, STRATEGY_NAME,
         _json(result["conditions"]), _json(result["values"]), result["data_status"],
         _json(result["unavailable_reasons"]), result["evaluated_at"]),
    )


def _record_signal(connection: sqlite3.Connection, result: dict[str, Any]) -> bool:
    cursor = connection.execute(
        """INSERT OR IGNORE INTO strategy_signals (cb_code, trade_date, strategy_code, strategy_version, strategy_name,
            condition_results_json, condition_values_json, data_status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?)""",
        (result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION, STRATEGY_NAME,
         _json(result["conditions"]), _json(result["values"]), result["evaluated_at"]),
    )
    return cursor.rowcount == 1


def evaluate_c_v1_on(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Evaluate CBs observed and effective on one day; never fill historical gaps."""
    todays = connection.execute(
        "SELECT cb_code, close_price FROM cb_daily WHERE trade_date = ? ORDER BY cb_code", (trade_date,)
    ).fetchall()
    if not todays:
        return [_unavailable("__RUN__", trade_date, ["target_trade_date_not_available"])]
    results: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for daily in todays:
        cb_code = str(daily["cb_code"])
        master = connection.execute(
            """SELECT stock_code, issue_amount, issue_date, delisting_date FROM cb_master WHERE cb_code = ?""", (cb_code,)
        ).fetchone()
        if master is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_cb_master"]))
            continue
        if str(master["issue_date"]) > trade_date or (master["delisting_date"] and str(master["delisting_date"]) <= trade_date):
            continue
        if daily["close_price"] is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_cb_close_price"]))
            continue
        conversion = connection.execute(
            """SELECT conversion_price FROM conversion_price_events WHERE cb_code = ? AND effective_date <= ?
               ORDER BY effective_date DESC LIMIT 1""", (cb_code, trade_date)
        ).fetchone()
        if conversion is None or conversion["conversion_price"] is None or float(conversion["conversion_price"]) <= 0:
            results.append(_unavailable(cb_code, trade_date, ["missing_conversion_price_event"]))
            continue
        stock = connection.execute(
            "SELECT p_close_price FROM stock_daily_market WHERE trade_date = ? AND p_stock_code = ?",
            (trade_date, master["stock_code"]),
        ).fetchone()
        if stock is None or stock["p_close_price"] is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_parent_stock_close"], {"stock_code": master["stock_code"]}))
            continue
        if master["issue_amount"] is None or int(master["issue_amount"]) <= 0:
            results.append(_unavailable(cb_code, trade_date, ["missing_issue_amount"]))
            continue
        balance = _balance_on(connection, cb_code, trade_date)
        if balance is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_historical_balance"]))
            continue
        balance_amount, balance_date = balance
        conversion_value_decimal = Decimal(str(stock["p_close_price"])) / Decimal(str(conversion["conversion_price"])) * 100
        premium_rate_decimal = (Decimal(str(daily["close_price"])) / conversion_value_decimal - 1) * 100
        converted_ratio_decimal = (1 - Decimal(balance_amount) / Decimal(int(master["issue_amount"]))) * 100
        conversion_value = float(conversion_value_decimal)
        premium_rate_pct = float(premium_rate_decimal)
        converted_ratio_pct = float(converted_ratio_decimal)
        bucket = _bucket_for(conversion_value_decimal)
        conditions = {
            "conversion_value_in_100_to_120": bucket is not None,
            "converted_ratio_at_most_20_pct": converted_ratio_decimal <= Decimal("20"),
            "premium_rate_above_5_pct": premium_rate_decimal > Decimal("5"),
            "within_bucket_top_two": False,
        }
        result = {"cb_code": cb_code, "trade_date": trade_date, "data_status": "AVAILABLE", "unavailable_reasons": [],
                  "conditions": conditions, "values": {"close_price": float(daily["close_price"]), "conversion_price": float(conversion["conversion_price"]),
                  "parent_stock_close_price": float(stock["p_close_price"]), "conversion_value": conversion_value,
                  "premium_rate_pct": premium_rate_pct, "issue_amount": int(master["issue_amount"]), "balance_amount": balance_amount,
                  "balance_date": balance_date, "converted_ratio_pct": converted_ratio_pct, "conversion_value_bucket": bucket,
                  "bucket_rank": None, "bucket_candidate_count": 0}, "evaluated_at": _now(), "signal_created": False}
        results.append(result)
        if bucket is not None and all(conditions[key] for key in ("conversion_value_in_100_to_120", "converted_ratio_at_most_20_pct", "premium_rate_above_5_pct")):
            candidates.append(result)
    for bucket in {candidate["values"]["conversion_value_bucket"] for candidate in candidates}:
        ranked = sorted((item for item in candidates if item["values"]["conversion_value_bucket"] == bucket),
                        key=lambda item: (-float(item["values"]["premium_rate_pct"]), item["cb_code"]))
        for rank, item in enumerate(ranked, start=1):
            item["values"]["bucket_rank"] = rank
            item["values"]["bucket_candidate_count"] = len(ranked)
            item["conditions"]["within_bucket_top_two"] = rank <= 2
    return results


def run_c_v1(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    totals = {"evaluations": 0, "unavailable": 0, "matched": 0, "signals_inserted": 0, "signals_existing": 0}
    with connection:
        for trade_date in trade_dates:
            for result in evaluate_c_v1_on(connection, trade_date):
                _record_evaluation(connection, result)
                totals["evaluations"] += 1
                if result["data_status"] == "UNAVAILABLE":
                    totals["unavailable"] += 1
                elif all(result["conditions"].values()):
                    totals["matched"] += 1
                    if _record_signal(connection, result):
                        totals["signals_inserted"] += 1
                    else:
                        totals["signals_existing"] += 1
    return totals


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strategy C-v1 from saved CB data")
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date", type=date.fromisoformat)
    dates.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be supplied together")
    if args.start_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with connect(args.database) as connection:
        dates = [args.date.isoformat()] if args.date else [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (args.start_date.isoformat(), args.end_date.isoformat()))]
        totals = run_c_v1(connection, dates)
    print(f"strategy_code: {STRATEGY_CODE}")
    print(f"strategy_version: {STRATEGY_VERSION}")
    print(f"trade_dates: {len(dates)}")
    for key, value in totals.items(): print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
