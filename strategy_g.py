"""Strategy G-v1: time-based CB activation events from saved history."""

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


STRATEGY_CODE = "G"
STRATEGY_VERSION = "v1"
STRATEGY_NAME = "時間發動策略"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _one_year_after(value: str) -> str:
    source = date.fromisoformat(value)
    try:
        return source.replace(year=source.year + 1).isoformat()
    except ValueError:  # 29 February becomes the final day of February.
        return source.replace(year=source.year + 1, day=28).isoformat()


def _one_year_before(value: str) -> str:
    source = date.fromisoformat(value)
    try:
        return source.replace(year=source.year - 1).isoformat()
    except ValueError:
        return source.replace(year=source.year - 1, day=28).isoformat()


def _monthly_balance_date(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _balance_on(connection: sqlite3.Connection, cb_code: str, trade_date: str) -> tuple[int, str] | None:
    """Return the newest official balance that was available by ``trade_date``."""
    candidates: list[tuple[str, int]] = []
    row = connection.execute(
        """SELECT year_month, balance_amount FROM cb_monthly_balance
           WHERE cb_code = ? AND year_month < ? ORDER BY year_month DESC LIMIT 1""",
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


def _unavailable(cb_code: str, trade_date: str, reasons: list[str], values: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"cb_code": cb_code, "trade_date": trade_date, "data_status": "UNAVAILABLE",
            "unavailable_reasons": reasons, "conditions": {}, "values": values or {},
            "evaluated_at": _now(), "signal_created": False}


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


def _effective_dates(connection: sqlite3.Connection, trade_date: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date <= ? ORDER BY trade_date", (trade_date,)
    )]


def _first_effective_date_on_or_after(dates: list[str], event_date: str) -> str | None:
    return next((day for day in dates if day >= event_date), None)


def _basic_conditions_on(
    connection: sqlite3.Connection, cb_code: str, master: sqlite3.Row, trade_date: str
) -> dict[str, bool] | None:
    """Calculate the three time-invariant G basics as they were knowable on one day.

    ``None`` is deliberately distinct from false: it means the prior day's
    baseline cannot be established without inventing historical data.
    """
    daily = connection.execute(
        "SELECT close_price FROM cb_daily WHERE cb_code = ? AND trade_date = ?", (cb_code, trade_date)
    ).fetchone()
    if daily is None or daily["close_price"] is None:
        return None
    conversion = connection.execute(
        """SELECT conversion_price FROM conversion_price_events WHERE cb_code = ? AND effective_date <= ?
           ORDER BY effective_date DESC LIMIT 1""", (cb_code, trade_date)
    ).fetchone()
    stock = connection.execute(
        "SELECT p_close_price FROM stock_daily_market WHERE trade_date = ? AND p_stock_code = ?",
        (trade_date, master["stock_code"]),
    ).fetchone()
    balance = _balance_on(connection, cb_code, trade_date)
    if (conversion is None or conversion["conversion_price"] is None or float(conversion["conversion_price"]) <= 0
            or stock is None or stock["p_close_price"] is None or balance is None):
        return None
    balance_amount, _ = balance
    close = Decimal(str(daily["close_price"]))
    conversion_value = Decimal(str(stock["p_close_price"])) / Decimal(str(conversion["conversion_price"])) * 100
    converted_ratio = (1 - Decimal(balance_amount) / Decimal(int(master["issue_amount"]))) * 100
    return {
        "converted_ratio_below_10_pct": converted_ratio < Decimal("10"),
        "conversion_value_at_least_90": conversion_value >= Decimal("90"),
        "close_price_at_most_130": close <= Decimal("130"),
    }


def _already_signaled(connection: sqlite3.Connection, cb_code: str, trade_date: str, trigger_type: str) -> bool:
    """A G1/G3 event may be emitted once, even after a later false→true basic transition."""
    return connection.execute(
        """SELECT 1 FROM strategy_signals WHERE cb_code = ? AND trade_date < ?
           AND strategy_code = ? AND strategy_version = ? AND condition_values_json LIKE ? LIMIT 1""",
        (cb_code, trade_date, STRATEGY_CODE, STRATEGY_VERSION, f'%"{trigger_type}"%'),
    ).fetchone() is not None


def evaluate_g_v1_on(connection: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    """Evaluate G-v1 from historical rows; never substitute absent or future data."""
    dates = _effective_dates(connection, trade_date)
    todays = connection.execute(
        "SELECT cb_code, cb_name, close_price, volume_lots FROM cb_daily WHERE trade_date = ? ORDER BY cb_code",
        (trade_date,),
    ).fetchall()
    if not todays:
        return [_unavailable("__RUN__", trade_date, ["target_trade_date_not_available"])]
    position = dates.index(trade_date)
    results: list[dict[str, Any]] = []
    for today in todays:
        cb_code = str(today["cb_code"])
        master = connection.execute(
            """SELECT stock_code, issue_date, maturity_date, put_date, issue_amount, delisting_date
               FROM cb_master WHERE cb_code = ?""", (cb_code,)
        ).fetchone()
        if master is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_cb_master"]))
            continue
        if str(master["issue_date"]) > trade_date or (master["delisting_date"] and str(master["delisting_date"]) <= trade_date):
            continue
        if today["close_price"] is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_cb_close_price"]))
            continue
        if master["issue_amount"] is None or int(master["issue_amount"]) <= 0:
            results.append(_unavailable(cb_code, trade_date, ["missing_issue_amount"]))
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
        balance = _balance_on(connection, cb_code, trade_date)
        if balance is None:
            results.append(_unavailable(cb_code, trade_date, ["missing_historical_balance"]))
            continue

        balance_amount, balance_date = balance
        close = Decimal(str(today["close_price"]))
        conversion_value = Decimal(str(stock["p_close_price"])) / Decimal(str(conversion["conversion_price"])) * 100
        converted_ratio = (1 - Decimal(balance_amount) / Decimal(int(master["issue_amount"]))) * 100
        basic_conditions = {
            "converted_ratio_below_10_pct": converted_ratio < Decimal("10"),
            "conversion_value_at_least_90": conversion_value >= Decimal("90"),
            "close_price_at_most_130": close <= Decimal("130"),
        }
        basic_met = all(basic_conditions.values())
        issue_anniversary = _one_year_after(str(master["issue_date"]))
        maturity_window_start = _one_year_before(str(master["maturity_date"]))
        g1_entry_date = _first_effective_date_on_or_after(dates, issue_anniversary)
        g3_entry_date = _first_effective_date_on_or_after(dates, maturity_window_start)
        previous_trade_date = dates[position - 1] if position else None
        baseline_unknown: list[str] = []

        def event_trigger(trigger_type: str, event_start: str, active_today: bool) -> bool:
            if not active_today or not basic_met or _already_signaled(connection, cb_code, trade_date, trigger_type):
                return False
            if previous_trade_date is None:
                baseline_unknown.append(trigger_type)
                return False
            was_active = previous_trade_date >= event_start
            if not was_active:
                return True
            previous_basics = _basic_conditions_on(connection, cb_code, master, previous_trade_date)
            if previous_basics is None:
                baseline_unknown.append(trigger_type)
                return False
            return not all(previous_basics.values())

        g1 = event_trigger("G1", issue_anniversary, trade_date >= issue_anniversary)
        g3 = event_trigger(
            "G3", maturity_window_start,
            maturity_window_start <= trade_date <= str(master["maturity_date"]),
        )

        g2_active = bool(master["put_date"] and trade_date >= str(master["put_date"]))
        prior_19_dates: list[str] = []
        prior_5_dates: list[str] = []
        g2_price_breakout = False
        g2_volume_breakout = False
        prior_19_high: Decimal | None = None
        prior_5_average: Decimal | None = None
        if g2_active:
            if position < 19:
                results.append(_unavailable(cb_code, trade_date, ["insufficient_market_calendar_for_g2_19_days"], {
                    "trigger_types": [], "put_date": str(master["put_date"]), "basic_conditions": basic_conditions,
                }))
                continue
            prior_19_dates = dates[position - 19:position]
            prior_5_dates = dates[position - 5:position]
            rows = connection.execute(
                f"""SELECT trade_date, close_price, volume_lots FROM cb_daily WHERE cb_code = ?
                    AND trade_date IN ({','.join('?' for _ in prior_19_dates)}) ORDER BY trade_date""",
                (cb_code, *prior_19_dates),
            ).fetchall()
            by_date = {str(row["trade_date"]): row for row in rows}
            missing = [day for day in prior_19_dates if day not in by_date]
            if missing:
                results.append(_unavailable(cb_code, trade_date, ["missing_cb_daily_rows"], {"missing_trade_dates": missing}))
                continue
            missing_close = [day for day in prior_19_dates if by_date[day]["close_price"] is None]
            if missing_close:
                results.append(_unavailable(cb_code, trade_date, ["missing_cb_close_price"], {"missing_close_trade_dates": missing_close}))
                continue
            prior_19_high = max(Decimal(str(by_date[day]["close_price"])) for day in prior_19_dates)
            prior_5_average = sum((Decimal(int(by_date[day]["volume_lots"])) for day in prior_5_dates), Decimal()) / 5
            g2_price_breakout = close > prior_19_high
            g2_volume_breakout = Decimal(int(today["volume_lots"])) > prior_5_average * 3
        g2 = basic_met and g2_active and g2_price_breakout and g2_volume_breakout
        trigger_types = [name for name, matched in (("G1", g1), ("G2", g2), ("G3", g3)) if matched]
        conditions = {**basic_conditions,
                      "g1_first_effective_trade_date_after_issue_anniversary": g1,
                      "g2_after_put_date": g2_active,
                      "g2_close_strictly_above_prior_19_high": g2_price_breakout,
                      "g2_volume_above_prior_5_average_times_3": g2_volume_breakout,
                      "g3_first_effective_trade_date_in_maturity_final_year": g3}
        values = {
            "trigger_types": trigger_types, "close_price": float(close), "today_volume_lots": int(today["volume_lots"]),
            "conversion_price": float(conversion["conversion_price"]), "parent_stock_close_price": float(stock["p_close_price"]),
            "conversion_value": float(conversion_value), "issue_amount": int(master["issue_amount"]),
            "balance_amount": balance_amount, "balance_date": balance_date, "converted_ratio_pct": float(converted_ratio),
            "issue_date": str(master["issue_date"]), "issue_anniversary_date": issue_anniversary,
            "g1_entry_trade_date": g1_entry_date, "put_date": master["put_date"],
            "maturity_date": str(master["maturity_date"]), "maturity_final_year_start_date": maturity_window_start,
            "g3_entry_trade_date": g3_entry_date, "prior_19_trade_dates": prior_19_dates,
            "prior_5_trade_dates": prior_5_dates, "prior_19_high_close_price": float(prior_19_high) if prior_19_high is not None else None,
            "prior_5_average_volume_lots": float(prior_5_average) if prior_5_average is not None else None,
            "baseline_unknown_trigger_types": baseline_unknown,
        }
        if baseline_unknown and not trigger_types:
            results.append(_unavailable(cb_code, trade_date, ["baseline_unknown"], values))
            continue
        results.append({"cb_code": cb_code, "trade_date": trade_date, "data_status": "AVAILABLE",
                        "unavailable_reasons": [], "conditions": conditions, "values": values,
                        "evaluated_at": _now(), "signal_created": False})
    return results


def run_g_v1(connection: sqlite3.Connection, trade_dates: Iterable[str]) -> dict[str, int]:
    totals = {"evaluations": 0, "unavailable": 0, "matched": 0, "signals_inserted": 0, "signals_existing": 0}
    with connection:
        for trade_date in trade_dates:
            for result in evaluate_g_v1_on(connection, trade_date):
                _record_evaluation(connection, result)
                totals["evaluations"] += 1
                if result["data_status"] == "UNAVAILABLE":
                    totals["unavailable"] += 1
                elif result["values"]["trigger_types"]:
                    totals["matched"] += 1
                    if _record_signal(connection, result):
                        totals["signals_inserted"] += 1
                    else:
                        totals["signals_existing"] += 1
    return totals


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strategy G-v1 from saved CB data")
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
        trade_dates = [args.date.isoformat()] if args.date else [str(row[0]) for row in connection.execute(
            "SELECT DISTINCT trade_date FROM cb_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (args.start_date.isoformat(), args.end_date.isoformat()),
        )]
        totals = run_g_v1(connection, trade_dates)
    print(f"strategy_code: {STRATEGY_CODE}")
    print(f"strategy_version: {STRATEGY_VERSION}")
    print(f"trade_dates: {len(trade_dates)}")
    for key, value in totals.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
