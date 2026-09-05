"""Strategy B-v1: CB breaks above its conversion value from saved history."""

from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from config import DEFAULT_DB_PATH
from db import connect


STRATEGY_CODE = "B"
STRATEGY_VERSION = "v1"
STRATEGY_NAME = "CB 突破轉換價"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _monthly_balance_date(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _balance_on(connection: sqlite3.Connection, cb_code: str, trade_date: str) -> tuple[int, str] | None:
    """Return the latest official balance whose as-of date is not after the target day."""
    candidates: list[tuple[str, int]] = []
    row = connection.execute(
        """SELECT year_month, balance_amount FROM cb_monthly_balance
           WHERE cb_code = ? AND year_month < ?
           ORDER BY year_month DESC LIMIT 1""",
        (cb_code, trade_date[:7]),
    ).fetchone()
    if row is not None:
        candidates.append((_monthly_balance_date(str(row["year_month"])), int(row["balance_amount"])))
    row = connection.execute(
        """SELECT balance_amount, balance_date FROM cb_master
           WHERE cb_code = ? AND balance_date <= ?""",
        (cb_code, trade_date),
    ).fetchone()
    if row is not None and row["balance_amount"] is not None and row["balance_date"] is not None:
        candidates.append((str(row["balance_date"]), int(row["balance_amount"])))
    if not candidates:
        return None
    balance_date, balance_amount = max(candidates, key=lambda item: item[0])
    return balance_amount, balance_date


def _unavailable(
    cb_code: str, trade_date: str, reasons: list[str], values: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "cb_code": cb_code,
        "trade_date": trade_date,
        "data_status": "UNAVAILABLE",
        "unavailable_reasons": reasons,
        "conditions": {},
        "values": values or {},
        "evaluated_at": _now(),
        "signal_created": False,
    }


def _record_evaluation(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO strategy_evaluations (
             cb_code, trade_date, strategy_code, strategy_version, strategy_name,
             condition_results_json, condition_values_json, data_status,
             unavailable_reasons_json, evaluated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION,
            STRATEGY_NAME, _json(result["conditions"]), _json(result["values"]),
            result["data_status"], _json(result["unavailable_reasons"]), result["evaluated_at"],
        ),
    )


def _record_signal(connection: sqlite3.Connection, result: dict[str, Any]) -> bool:
    cursor = connection.execute(
        """INSERT OR IGNORE INTO strategy_signals (
             cb_code, trade_date, strategy_code, strategy_version, strategy_name,
             condition_results_json, condition_values_json, data_status, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?)""",
        (
            result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION,
            STRATEGY_NAME, _json(result["conditions"]), _json(result["values"]),
            result["evaluated_at"],
        ),
    )
    return cursor.rowcount == 1


def _effective_dates(connection: sqlite3.Connection, trade_date: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date <= ? ORDER BY trade_date",
            (trade_date,),
        )
    ]


def evaluate_b_v1_on(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Evaluate active CBs on one day without filling absent rows or values."""
    calendar_dates = _effective_dates(connection, trade_date)
    today_rows = connection.execute(
        "SELECT cb_code, close_price FROM cb_daily WHERE trade_date = ? ORDER BY cb_code",
        (trade_date,),
    ).fetchall()
    if not today_rows:
        return [_unavailable("__RUN__", trade_date, ["target_trade_date_not_available"])]
    if trade_date not in calendar_dates or len(calendar_dates) < 43:
        return [
            _unavailable(str(row["cb_code"]), trade_date, ["insufficient_market_calendar_for_43_days"])
            for row in today_rows
        ]

    position = calendar_dates.index(trade_date)
    window_43_dates = calendar_dates[position - 42 : position + 1]
    window_10_dates = window_43_dates[-10:]
    window_5_dates = window_43_dates[-5:]
    prior_19_dates = window_43_dates[-20:-1]
    results: list[dict[str, Any]] = []
    for today in today_rows:
        cb_code = str(today["cb_code"])
        master = connection.execute(
            """SELECT stock_code, issue_amount, issue_date, delisting_date
               FROM cb_master WHERE cb_code = ?""",
            (cb_code,),
        ).fetchone()
        if master is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_cb_master"]))
            continue
        if str(master["issue_date"]) > trade_date or (
            master["delisting_date"] and str(master["delisting_date"]) <= trade_date
        ):
            continue

        rows = connection.execute(
            f"""SELECT trade_date, close_price, volume_lots FROM cb_daily
                WHERE cb_code = ? AND trade_date IN ({','.join('?' for _ in window_43_dates)})
                ORDER BY trade_date""",
            (cb_code, *window_43_dates),
        ).fetchall()
        by_date = {str(row["trade_date"]): row for row in rows}
        missing_dates = [day for day in window_43_dates if day not in by_date]
        if missing_dates:
            results.append(_unavailable(
                cb_code, trade_date, ["missing_cb_daily_rows"], {"missing_trade_dates": missing_dates}
            ))
            continue
        missing_close_dates = [day for day in window_43_dates if by_date[day]["close_price"] is None]
        if missing_close_dates:
            results.append(_unavailable(
                cb_code, trade_date, ["missing_cb_close_price"], {"missing_close_trade_dates": missing_close_dates}
            ))
            continue
        conversion = connection.execute(
            """SELECT conversion_price FROM conversion_price_events
               WHERE cb_code = ? AND effective_date <= ?
               ORDER BY effective_date DESC LIMIT 1""",
            (cb_code, trade_date),
        ).fetchone()
        if conversion is None or conversion["conversion_price"] is None or float(conversion["conversion_price"]) <= 0:
            results.append(_unavailable(cb_code, trade_date, ["missing_conversion_price_event"]))
            continue
        stock = connection.execute(
            "SELECT p_close_price FROM stock_daily_market WHERE trade_date = ? AND p_stock_code = ?",
            (trade_date, master["stock_code"]),
        ).fetchone()
        if stock is None or stock["p_close_price"] is None:
            results.append(_unavailable(
                cb_code, trade_date, ["missing_parent_stock_close"], {"stock_code": master["stock_code"]}
            ))
            continue
        if master["issue_amount"] is None or int(master["issue_amount"]) <= 0:
            results.append(_unavailable(cb_code, trade_date, ["missing_issue_amount"]))
            continue
        balance = _balance_on(connection, cb_code, trade_date)
        if balance is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_historical_balance"]))
            continue

        balance_amount, balance_date = balance
        closes = [Decimal(str(by_date[day]["close_price"])) for day in window_43_dates]
        volumes = [int(by_date[day]["volume_lots"]) for day in window_43_dates]
        conversion_value = Decimal(str(stock["p_close_price"])) / Decimal(str(conversion["conversion_price"])) * 100
        premium_rate_pct = (closes[-1] / conversion_value - 1) * 100
        converted_ratio_pct = (1 - Decimal(balance_amount) / Decimal(int(master["issue_amount"]))) * 100
        average_43_close = sum(closes) / len(closes)
        average_10_volume = Decimal(sum(volumes[-10:])) / 10
        average_5_volume = Decimal(sum(volumes[-5:])) / 5
        prior_19_high_close = max(closes[-20:-1])
        conditions = {
            "close_price_above_43_day_average": closes[-1] > average_43_close,
            "volume_above_10_day_average": Decimal(volumes[-1]) > average_10_volume,
            "premium_rate_above_5_pct": premium_rate_pct > Decimal("5"),
            "conversion_value_in_90_to_110": Decimal("90") <= conversion_value <= Decimal("110"),
            "converted_ratio_at_most_20_pct": converted_ratio_pct <= Decimal("20"),
            "five_day_average_volume_above_50_lots": average_5_volume > Decimal("50"),
            "close_price_strictly_above_prior_19_high": closes[-1] > prior_19_high_close,
        }
        values = {
            "trigger_reason": "all_b_v1_conditions_met",
            "close_price": float(closes[-1]),
            "today_volume_lots": volumes[-1],
            "window_43_trade_dates": window_43_dates,
            "window_10_trade_dates": window_10_dates,
            "window_5_trade_dates": window_5_dates,
            "prior_19_trade_dates": prior_19_dates,
            "average_43_close_price": float(average_43_close),
            "average_10_volume_lots": float(average_10_volume),
            "average_5_volume_lots": float(average_5_volume),
            "prior_19_high_close_price": float(prior_19_high_close),
            "conversion_price": float(conversion["conversion_price"]),
            "parent_stock_close_price": float(stock["p_close_price"]),
            "conversion_value": float(conversion_value),
            "premium_rate_pct": float(premium_rate_pct),
            "issue_amount": int(master["issue_amount"]),
            "balance_amount": balance_amount,
            "balance_date": balance_date,
            "converted_ratio_pct": float(converted_ratio_pct),
        }
        results.append({
            "cb_code": cb_code, "trade_date": trade_date, "data_status": "AVAILABLE",
            "unavailable_reasons": [], "conditions": conditions, "values": values,
            "evaluated_at": _now(), "signal_created": False,
        })
    return results


def run_b_v1(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    """Append B-v1 evaluations and add only new B-v1 signals."""
    totals = {"evaluations": 0, "unavailable": 0, "matched": 0, "signals_inserted": 0, "signals_existing": 0}
    with connection:
        for trade_date in trade_dates:
            for result in evaluate_b_v1_on(connection, trade_date):
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
    parser = argparse.ArgumentParser(description="Run strategy B-v1 from saved CB data")
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
        trade_dates = [args.date.isoformat()] if args.date else [
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (args.start_date.isoformat(), args.end_date.isoformat()),
            )
        ]
        totals = run_b_v1(connection, trade_dates)
    print(f"strategy_code: {STRATEGY_CODE}")
    print(f"strategy_version: {STRATEGY_VERSION}")
    print(f"trade_dates: {len(trade_dates)}")
    for key, value in totals.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
