"""Versioned, append-only CB strategy evaluation and persistence."""

from __future__ import annotations

import argparse
import json
import sqlite3
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import DEFAULT_DB_PATH
from db import connect
from strategy_registry import get_strategy


STRATEGY = get_strategy("A")
STRATEGY_CODE = STRATEGY.strategy_code
STRATEGY_VERSION = STRATEGY.active_version
STRATEGY_NAME = STRATEGY.strategy_name


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _effective_dates(connection: sqlite3.Connection, trade_date: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date <= ? ORDER BY trade_date",
            (trade_date,),
        )
    ]


def _record_evaluation(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO strategy_evaluations (
            cb_code, trade_date, strategy_code, strategy_version, strategy_name,
            condition_results_json, condition_values_json, data_status,
            unavailable_reasons_json, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION,
            STRATEGY_NAME, _json(result["conditions"]), _json(result["values"]),
            result["data_status"], _json(result["unavailable_reasons"]),
            result["evaluated_at"],
        ),
    )


def _record_signal(connection: sqlite3.Connection, result: dict[str, Any]) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO strategy_signals (
            cb_code, trade_date, strategy_code, strategy_version, strategy_name,
            condition_results_json, condition_values_json, data_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?)
        """,
        (
            result["cb_code"], result["trade_date"], STRATEGY_CODE, STRATEGY_VERSION,
            STRATEGY_NAME, _json(result["conditions"]), _json(result["values"]),
            result["evaluated_at"],
        ),
    )
    return cursor.rowcount == 1


def _unavailable_result(cb_code: str, trade_date: str, reasons: list[str], values: dict[str, Any]) -> dict[str, Any]:
    return {
        "cb_code": cb_code,
        "trade_date": trade_date,
        "data_status": "UNAVAILABLE",
        "unavailable_reasons": reasons,
        "conditions": {},
        "values": values,
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signal_created": False,
    }


def evaluate_a_v2(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Pure read-only A-v2 evaluator for one trade date.

    A valid day is a day present in the official ``cb_daily`` calendar.  Every
    one of the required per-CB rows must exist on that calendar; a zero volume
    row is deliberately treated as an observed day, while an absent row is not.
    """
    calendar = _effective_dates(connection, trade_date)
    today_rows = connection.execute(
        "SELECT cb_code, cb_name, close_price, volume_lots FROM cb_daily WHERE trade_date = ? ORDER BY cb_code",
        (trade_date,),
    ).fetchall()
    if not today_rows:
        return [_unavailable_result("__RUN__", trade_date, ["target_trade_date_not_available"], {})]
    if trade_date not in calendar or len(calendar) < 10:
        return [
            _unavailable_result(str(row["cb_code"]), trade_date, ["insufficient_market_calendar_for_10_days"], {})
            for row in today_rows
        ]

    window_dates = calendar[calendar.index(trade_date) - 9 : calendar.index(trade_date) + 1]
    results: list[dict[str, Any]] = []
    for today in today_rows:
        cb_code = str(today["cb_code"])
        rows = connection.execute(
            f"""
            SELECT trade_date, close_price, volume_lots
            FROM cb_daily
            WHERE cb_code = ? AND trade_date IN ({','.join('?' for _ in window_dates)})
            ORDER BY trade_date
            """,
            (cb_code, *window_dates),
        ).fetchall()
        observed_by_date = {str(row["trade_date"]): row for row in rows}
        missing_dates = [day for day in window_dates if day not in observed_by_date]
        if missing_dates:
            results.append(_unavailable_result(cb_code, trade_date, ["missing_cb_daily_rows"], {"missing_trade_dates": missing_dates}))
            continue
        if today["close_price"] is None:
            results.append(_unavailable_result(cb_code, trade_date, ["missing_cb_close_price"], {}))
            continue

        master = connection.execute(
            "SELECT stock_code FROM cb_master WHERE cb_code = ?", (cb_code,)
        ).fetchone()
        if master is None:
            results.append(_unavailable_result(cb_code, trade_date, ["missing_cb_master"], {}))
            continue
        conversion = connection.execute(
            """SELECT conversion_price FROM conversion_price_events
               WHERE cb_code = ? AND effective_date <= ?
               ORDER BY effective_date DESC LIMIT 1""",
            (cb_code, trade_date),
        ).fetchone()
        if conversion is None or conversion[0] is None or float(conversion[0]) <= 0:
            results.append(_unavailable_result(cb_code, trade_date, ["missing_conversion_price_event"], {}))
            continue
        stock = connection.execute(
            "SELECT p_close_price FROM stock_daily_market WHERE trade_date = ? AND p_stock_code = ?",
            (trade_date, master["stock_code"]),
        ).fetchone()
        if stock is None or stock[0] is None:
            results.append(_unavailable_result(cb_code, trade_date, ["missing_parent_stock_close"], {"stock_code": master["stock_code"]}))
            continue

        volumes = [int(observed_by_date[day]["volume_lots"]) for day in window_dates]
        close_price = float(today["close_price"])
        conversion_value = float(stock[0]) / float(conversion[0]) * 100
        premium_rate_pct = (close_price / conversion_value - 1) * 100
        average_10_volume = sum(volumes) / 10
        # The five-day comparison deliberately excludes today's volume.
        average_5_volume = sum(volumes[-6:-1]) / 5
        conditions = {
            "volume_above_10_day_average": volumes[-1] > average_10_volume,
            "close_price_in_115_to_150": 115 <= close_price <= 150,
            "close_price_above_conversion_value": close_price > conversion_value,
            "premium_rate_above_1_pct": premium_rate_pct > 1,
            "ten_day_volume_above_300_lots": sum(volumes) > 300,
            "volume_above_prior_5_average_times_3": volumes[-1] > average_5_volume * 3,
        }
        values = {
            "window_trade_dates": window_dates,
            "today_volume_lots": volumes[-1],
            "average_10_volume_lots": average_10_volume,
            "average_5_volume_lots": average_5_volume,
            "ten_day_total_volume_lots": sum(volumes),
            "close_price": close_price,
            "conversion_price": float(conversion[0]),
            "parent_stock_close_price": float(stock[0]),
            "conversion_value": conversion_value,
            "premium_rate_pct": premium_rate_pct,
        }
        results.append({
            "cb_code": cb_code, "trade_date": trade_date, "data_status": "AVAILABLE",
            "unavailable_reasons": [], "conditions": conditions, "values": values,
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "signal_created": False,
        })
    return results


def run_a_v2(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    """Append diagnostics; insert a signal once per strategy version and key."""
    totals = {"evaluations": 0, "unavailable": 0, "matched": 0, "signals_inserted": 0, "signals_existing": 0}
    with connection:
        for trade_date in trade_dates:
            for result in evaluate_a_v2(connection, trade_date):
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


def evaluate_a_active(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Evaluate the registry-selected active A version without writing results."""
    if get_strategy("A").active_version != "v2":
        raise RuntimeError("Strategy A active version has no registered evaluator")
    return evaluate_a_v2(connection, trade_date)


def run_a_active(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    """Run the registry-selected active A version using the legacy persistence path."""
    if get_strategy("A").active_version != "v2":
        raise RuntimeError("Strategy A active version has no registered writer")
    return run_a_v2(connection, trade_dates)


def evaluate_a_v1_on(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Deprecated compatibility alias; this does not reproduce historical A-v1."""
    warnings.warn(
        "evaluate_a_v1_on is deprecated; use evaluate_a_v2 or evaluate_a_active",
        DeprecationWarning,
        stacklevel=2,
    )
    return evaluate_a_active(connection, trade_date)


def run_a_v1(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    """Deprecated compatibility alias; this does not reproduce historical A-v1."""
    warnings.warn(
        "run_a_v1 is deprecated; use run_a_v2 or run_a_active",
        DeprecationWarning,
        stacklevel=2,
    )
    return run_a_active(connection, trade_dates)


def _dates_for_args(connection: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.date:
        return [args.date.isoformat()]
    return [
        str(row[0]) for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (args.start_date.isoformat(), args.end_date.isoformat()),
        )
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strategy A-v2 from saved CB data")
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date", type=date.fromisoformat, help="one trade date (YYYY-MM-DD)")
    dates.add_argument("--start-date", type=date.fromisoformat, help="inclusive range start (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="inclusive range end; required with --start-date")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    if args.start_date and not args.end_date:
        parser.error("--start-date requires --end-date")
    if args.end_date and not args.start_date:
        parser.error("--end-date requires --start-date")
    if args.start_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with connect(args.database) as connection:
        trade_dates = _dates_for_args(connection, args)
        totals = run_a_active(connection, trade_dates)
    print(f"strategy_code: {STRATEGY_CODE}")
    print(f"strategy_version: {STRATEGY_VERSION}")
    print(f"trade_dates: {len(trade_dates)}")
    for key, value in totals.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
