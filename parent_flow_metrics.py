"""Derived CB-parent institutional and tracked-active-ETF flow metrics.

Raw sources remain shares.  This module is the single place that converts the
derived display statistics to lots, and it never turns missing observations into
zeroes.
"""

import argparse
import sqlite3
from pathlib import Path

from config import DEFAULT_DB_PATH
from db import active_parent_stock_codes_on, connect


GOOD_INSTITUTIONAL = ("COMPLETE", "OFFICIAL_ZERO")
# Keep the derived-query entry point usable without collector HTTP dependencies.
TRACKED_ACTIVE_ETF_CODES = ("00980A", "00985A", "00999A", "00982A", "00992A")


def _market_dates(connection: sqlite3.Connection, stock_code: str, as_of: str) -> list[str]:
    return [row[0] for row in connection.execute(
        """SELECT trade_date FROM stock_daily_market
           WHERE p_stock_code = ? AND trade_date <= ? ORDER BY trade_date DESC""",
        (stock_code, as_of),
    )]


def _institutional_value(connection, stock_code: str, trade_date: str, column: str):
    row = connection.execute(
        """SELECT coverage.status, daily.%s, market.p_volume_shares
           FROM institutional_coverage AS coverage
           LEFT JOIN institutional_daily AS daily
             ON daily.trade_date=coverage.trade_date AND daily.stock_code=coverage.stock_code
           LEFT JOIN stock_daily_market AS market
             ON market.trade_date=coverage.trade_date AND market.p_stock_code=coverage.stock_code
           WHERE coverage.trade_date=? AND coverage.stock_code=?""" % column,
        (trade_date, stock_code),
    ).fetchone()
    if row is None or row["status"] not in GOOD_INSTITUTIONAL or row[column] is None or row["p_volume_shares"] is None:
        return None
    return int(row[column]), int(row["p_volume_shares"])


def _institutional_metrics(connection, stock_code: str, dates: list[str], column: str):
    if not dates:
        return "UNAVAILABLE", None, None, None, None
    latest = _institutional_value(connection, stock_code, dates[0], column)
    if latest is None:
        return "UNAVAILABLE", None, None, None, None
    net, volume = latest
    percent = None if volume == 0 else net / volume * 100
    sign = (net > 0) - (net < 0)
    if sign == 0:
        return "AVAILABLE", net / 1000, percent, 0, 0.0
    streak_days, cumulative = 0, 0
    for trade_date in dates:
        value = _institutional_value(connection, stock_code, trade_date, column)
        if value is None:
            # A gap before the natural zero/sign boundary makes the run unknowable.
            return "UNAVAILABLE", None, None, None, None
        old_net, _ = value
        old_sign = (old_net > 0) - (old_net < 0)
        if old_sign != sign:
            break
        streak_days += 1
        cumulative += old_net
    return "AVAILABLE", net / 1000, percent, streak_days, cumulative / 1000


def _etf_total_if_complete(connection, stock_code: str, trade_date: str):
    status_rows = connection.execute(
        """SELECT etf_code, status FROM active_etf_collection_status
           WHERE trade_date=? AND etf_code IN (%s)""" % ",".join("?" * len(TRACKED_ACTIVE_ETF_CODES)),
        (trade_date, *TRACKED_ACTIVE_ETF_CODES),
    ).fetchall()
    if {row["etf_code"] for row in status_rows} != set(TRACKED_ACTIVE_ETF_CODES) or any(row["status"] != "succeeded" for row in status_rows):
        return None
    rows = connection.execute(
        """SELECT etf_code, holding_shares FROM active_etf_holdings
           WHERE trade_date=? AND stock_code=? AND etf_code IN (%s)""" % ",".join("?" * len(TRACKED_ACTIVE_ETF_CODES)),
        (trade_date, stock_code, *TRACKED_ACTIVE_ETF_CODES),
    ).fetchall()
    # A succeeded coverage record means this ETF's official *complete* holdings
    # snapshot was obtained.  A parent absent from that snapshot is therefore a
    # real zero for this derived total, not a missing raw observation.  Do not
    # synthesize rows in active_etf_holdings.
    return sum(int(row["holding_shares"]) for row in rows)


def _etf_change(connection, stock_code: str, dates: list[str], index: int):
    if index + 1 >= len(dates):
        return None
    current = _etf_total_if_complete(connection, stock_code, dates[index])
    previous = _etf_total_if_complete(connection, stock_code, dates[index + 1])
    if current is None or previous is None:
        return None
    return current - previous


def _etf_metrics(connection, stock_code: str, dates: list[str]):
    if not dates:
        return "UNAVAILABLE", None, None, None, None
    change = _etf_change(connection, stock_code, dates, 0)
    price = connection.execute("SELECT p_close_price FROM stock_daily_market WHERE trade_date=? AND p_stock_code=?", (dates[0], stock_code)).fetchone()
    if change is None or price is None or price[0] is None:
        return "UNAVAILABLE", None, None, None, None
    sign = (change > 0) - (change < 0)
    if sign == 0:
        return "AVAILABLE", 0.0, 0.0, 0, 0.0
    streak_days, cumulative = 0, 0
    # The earliest stored market day has no predecessor from which to derive a
    # holding change.  That is a history boundary, not failed ETF coverage.
    for index in range(len(dates) - 1):
        old_change = _etf_change(connection, stock_code, dates, index)
        if old_change is None:
            return "UNAVAILABLE", None, None, None, None
        if (old_change > 0) - (old_change < 0) != sign:
            break
        streak_days += 1
        cumulative += old_change
    return "AVAILABLE", change / 1000, change * float(price[0]), streak_days, cumulative / 1000


def recompute_parent_flow_metrics(connection: sqlite3.Connection, trade_date: str) -> int:
    """Rebuild one daily snapshot for all *currently active* distinct parent stocks."""
    rows = []
    for stock_code in sorted(active_parent_stock_codes_on(connection, trade_date)):
        dates = _market_dates(connection, stock_code, trade_date)
        foreign = _institutional_metrics(connection, stock_code, dates, "foreign_net_shares")
        trust = _institutional_metrics(connection, stock_code, dates, "trust_net_shares")
        etf = _etf_metrics(connection, stock_code, dates)
        rows.append({"trade_date": trade_date, "stock_code": stock_code,
            "foreign_status": foreign[0], "foreign_net_lots": foreign[1], "foreign_volume_pct": foreign[2], "foreign_streak_days": foreign[3], "foreign_streak_lots": foreign[4],
            "trust_status": trust[0], "trust_net_lots": trust[1], "trust_volume_pct": trust[2], "trust_streak_days": trust[3], "trust_streak_lots": trust[4],
            "active_etf_status": etf[0], "active_etf_change_lots": etf[1], "active_etf_change_value_twd": etf[2], "active_etf_streak_days": etf[3], "active_etf_streak_lots": etf[4]})
    with connection:
        connection.executemany("""INSERT INTO parent_flow_metrics VALUES
          (:trade_date,:stock_code,:foreign_status,:foreign_net_lots,:foreign_volume_pct,:foreign_streak_days,:foreign_streak_lots,
           :trust_status,:trust_net_lots,:trust_volume_pct,:trust_streak_days,:trust_streak_lots,
           :active_etf_status,:active_etf_change_lots,:active_etf_change_value_twd,:active_etf_streak_days,:active_etf_streak_lots)
          ON CONFLICT(trade_date,stock_code) DO UPDATE SET
           foreign_status=excluded.foreign_status,foreign_net_lots=excluded.foreign_net_lots,foreign_volume_pct=excluded.foreign_volume_pct,foreign_streak_days=excluded.foreign_streak_days,foreign_streak_lots=excluded.foreign_streak_lots,
           trust_status=excluded.trust_status,trust_net_lots=excluded.trust_net_lots,trust_volume_pct=excluded.trust_volume_pct,trust_streak_days=excluded.trust_streak_days,trust_streak_lots=excluded.trust_streak_lots,
           active_etf_status=excluded.active_etf_status,active_etf_change_lots=excluded.active_etf_change_lots,active_etf_change_value_twd=excluded.active_etf_change_value_twd,active_etf_streak_days=excluded.active_etf_streak_days,active_etf_streak_lots=excluded.active_etf_streak_lots""", rows)
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Recompute/query CB-parent flow metrics")
    parser.add_argument("--date", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--stock-code")
    parser.add_argument("--recompute", action="store_true")
    args = parser.parse_args(argv)
    with connect(args.database) as connection:
        if args.recompute:
            recompute_parent_flow_metrics(connection, args.date)
        query = "SELECT * FROM parent_flow_metrics WHERE trade_date=?"
        values = [args.date]
        if args.stock_code:
            query += " AND stock_code=?"; values.append(args.stock_code)
        for row in connection.execute(query + " ORDER BY stock_code", values):
            print(dict(row))


if __name__ == "__main__":
    main()
