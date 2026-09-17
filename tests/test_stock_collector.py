from datetime import date
from http.client import IncompleteRead
import sqlite3
import time

import pytest
import requests
import stock_collector
from config import (
    TWSE_FIXED_PRICE_URL, TWSE_INTRADAY_ODD_LOT_URL, TWSE_POST_ODD_LOT_URL,
)

from db import (
    connect, upsert_daily, upsert_parent_stock_mappings,
    upsert_parent_stock_monthly_mappings, upsert_stock_daily_market,
)
from stock_backfill import enrich_existing_stock_daily_market_v2
from stock_collector import (
    ParentStockMappingError,
    StockMarketFormatError,
    build_session,
    collect_stock_daily_market,
    parse_tpex_block_trade,
    parse_tpex_market,
    parse_tpex_volume_component,
    parse_twse_market,
    parse_twse_volume_component,
    verify_twse_suspensions,
)


TRADE_DATE = date(2026, 8, 28)
TWSE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]
TPEX_FIELDS = ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  "]
TWSE_COMPONENT_FIELDS = ["證券代號", "證券名稱", "成交股數"]
TWSE_FIXED_FIELDS = ["證券代號", "證券名稱", "成交數量"]
TPEX_COMPONENT_FIELDS = ["代號", "名稱", "成交股數"]
TPEX_FIXED_FIELDS = ["代號", "名稱", "成交張數"]


def test_stock_volume_v2_migration_marks_legacy_rows_regular_only(tmp_path):
    db_path = tmp_path / "legacy-history.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """CREATE TABLE stock_daily_market (
            trade_date TEXT NOT NULL,
            p_stock_code TEXT NOT NULL,
            p_open_price REAL,
            p_high_price REAL,
            p_low_price REAL,
            p_close_price REAL,
            p_volume_shares INTEGER NOT NULL,
            PRIMARY KEY (trade_date, p_stock_code)
        )"""
    )
    legacy.execute(
        "INSERT INTO stock_daily_market VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
        ("2026-09-08", "1101", 42.5, 12_345),
    )
    legacy.commit()
    legacy.close()

    with connect(db_path) as connection:
        row = connection.execute(
            """SELECT p_volume_shares, p_regular_volume_shares,
                      p_intraday_odd_lot_shares, p_post_odd_lot_shares,
                      p_fixed_price_volume_shares, p_block_trade_volume_shares, p_total_volume_shares,
                      p_market_volume_shares, p_all_execution_volume_shares,
                      p_volume_definition, p_volume_component_status
               FROM stock_daily_market"""
        ).fetchone()

    assert tuple(row) == (
        12_345, 12_345, None, None, None, None, 12_345, None, None,
        "REGULAR_ONLY_V1", "LEGACY_REGULAR_ONLY",
    )


def twse_payload(*rows):
    return {
        "stat": "OK",
        "date": "20260828",
        "tables": [{"fields": [], "data": []} for _ in range(8)]
        + [{"fields": TWSE_FIELDS, "data": list(rows)}],
    }


def tpex_payload(*rows):
    return {
        "stat": "ok",
        "date": "20260828",
        "tables": [{"fields": TPEX_FIELDS, "data": list(rows)}],
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, twse, tpex, *, twse_intraday=None, twse_post_odd=None,
                 twse_fixed=None, tpex_daily_quotes=None, tpex_regular=None,
                 tpex_intraday=None, tpex_post_odd=None, tpex_fixed=None, tpex_block=None):
        self.twse = twse
        self.tpex = tpex
        self.twse_intraday = twse_component_payload() if twse_intraday is None else twse_intraday
        self.twse_post_odd = twse_component_payload() if twse_post_odd is None else twse_post_odd
        self.twse_fixed = twse_component_payload(fixed_price=True) if twse_fixed is None else twse_fixed
        self.tpex_daily_quotes = tpex if tpex_daily_quotes is None else tpex_daily_quotes
        self.tpex_regular = tpex if tpex_regular is None else tpex_regular
        self.tpex_intraday = tpex_component_payload() if tpex_intraday is None else tpex_intraday
        self.tpex_post_odd = tpex_component_payload() if tpex_post_odd is None else tpex_post_odd
        self.tpex_fixed = tpex_component_payload(fixed_price=True) if tpex_fixed is None else tpex_fixed
        self.tpex_block = tpex_block_payload() if tpex_block is None else tpex_block
        self.post_calls = []

    def get(self, url, *args, **kwargs):
        if url.endswith("TWTC7U"):
            return Response(self.twse_intraday)
        if url.endswith("TWT53U"):
            return Response(self.twse_post_odd)
        if url.endswith("BFT41U"):
            return Response(self.twse_fixed)
        return Response(self.twse)

    def post(self, url, *args, **kwargs):
        self.post_calls.append((url, kwargs.get("data")))
        if url.endswith("dailyQuotes"):
            return Response(self.tpex_daily_quotes)
        if url.endswith("/otc"):
            return Response(self.tpex_regular)
        if url.endswith("oddQuote"):
            return Response(self.tpex_intraday)
        if url.endswith("/odd"):
            return Response(self.tpex_post_odd)
        if url.endswith("fixPricing"):
            return Response(self.tpex_fixed)
        if url.endswith("blockTrade/quote"):
            return Response(self.tpex_block)
        return Response(self.tpex)


class ClosableSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def no_suspensions(*_args, **_kwargs):
    return {}


def suspended_3591(*_args, **_kwargs):
    return {
        "3591": {
            "source_url": "https://www.twse.com.tw/exchangeReport/TWTAUU",
            "last_trade_date": "2026-09-09",
            "recovery_date": "2026-09-21",
            "pre_suspension_close": "23.65",
            "corporate_action_reason": "退還股款",
        }
    }


def twse_component_payload(*rows, fixed_price=False):
    return {
        "stat": "OK", "date": "20260828",
        "fields": TWSE_FIXED_FIELDS if fixed_price else TWSE_COMPONENT_FIELDS,
        "data": list(rows),
    }


def tpex_component_payload(*rows, fixed_price=False):
    return {
        "stat": "ok", "date": "20260828",
        "tables": [{
            "fields": TPEX_FIXED_FIELDS if fixed_price else TPEX_COMPONENT_FIELDS,
            "data": list(rows),
        }],
    }


def tpex_block_payload(*rows):
    return {
        "stat": "ok", "date": "20260828",
        "tables": [{
            "fields": ["交易型態", "交割期別", "代號", "名稱", "成交價格(元)", "成交股數"],
            "data": list(rows),
        }],
    }


def with_trade_date(payload, day):
    payload["date"] = day.strftime("%Y%m%d")
    return payload


def verified_tpex_mappings(*codes):
    return {
        f"test-{code}": {
            "stock_code": code, "stock_name": code, "market": "TPEX",
            "source_url": "https://example.test/tpex", "verified_at": "2026-09-09T00:00:00+00:00",
            "mapping_level": "EXACT", "mapping_year_month": "2026-09",
        }
        for code in codes
    }


def seed_phase1_and_master(db_path):
    with connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO cb_master (
                cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
                issue_amount, source, source_url, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("11111", "甲一", "1101", "台泥", "2024-01-01", "2027-01-01", 100000000, "test", "test", "2026-08-28T00:00:00+00:00"),
                ("22221", "乙一", "3131", "弘塑", "2024-01-01", "2027-01-01", 100000000, "test", "test", "2026-08-28T00:00:00+00:00"),
            ],
        )
        upsert_daily(
            connection,
            [
                {"trade_date": "2026-08-28", "cb_code": "11111", "cb_name": "甲一", "close_price": 100.0, "volume_lots": 1, "source": "test", "collected_at": "2026-08-28T00:00:00+00:00"},
                {"trade_date": "2026-08-28", "cb_code": "22221", "cb_name": "乙一", "close_price": 100.0, "volume_lots": 1, "source": "test", "collected_at": "2026-08-28T00:00:00+00:00"},
            ],
        )
        upsert_parent_stock_mappings(connection, [
            {"cb_code": "11111", "mapping_date": "2026-08-28", "stock_code": "1101",
             "stock_name": "台泥", "market": "TWSE", "source": "official",
             "source_url": "https://example.test/twse", "verified_at": "2026-08-28T00:00:00+00:00"},
            {"cb_code": "22221", "mapping_date": "2026-08-28", "stock_code": "3131",
             "stock_name": "弘塑", "market": "TPEX", "source": "official",
             "source_url": "https://example.test/tpex", "verified_at": "2026-08-28T00:00:00+00:00"},
        ])


def test_parsers_store_exact_share_volume_without_lot_rounding():
    twse = parse_twse_market(
        twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"]),
        TRADE_DATE,
        {"1101"},
    )
    tpex = parse_tpex_market(
        tpex_payload(["3131", "弘塑", "120.5", "+1", "119", "121", "118", "567"]),
        TRADE_DATE,
        {"3131"},
    )
    assert twse["1101"]["p_volume_shares"] == 1234
    assert tpex["3131"]["p_volume_shares"] == 567


def test_twse_and_tpex_component_parsers_preserve_share_units():
    assert parse_twse_volume_component(
        twse_component_payload(["1101", "台泥", "114,544"]), TRADE_DATE, {"1101"},
    ) == {"1101": 114544}
    assert parse_twse_volume_component(
        twse_component_payload(["1101", "台泥", "29"], fixed_price=True),
        TRADE_DATE, {"1101"}, fixed_price=True,
    ) == {"1101": 29000}
    assert parse_tpex_volume_component(
        tpex_component_payload(["3131", "弘塑", "59,671"]), TRADE_DATE, {"3131"},
    ) == {"3131": 59671}
    assert parse_tpex_volume_component(
        tpex_component_payload(["3131", "弘塑", "32"], fixed_price=True),
        TRADE_DATE, {"3131"}, fixed_price=True,
    ) == {"3131": 32000}
    assert parse_tpex_block_trade(
        tpex_block_payload(
            ["配對交易-單一型", "T+2日交割", "3131", "弘塑", "1", "13,000"],
            ["配對交易-單一型", "T+2日交割", "3131", "弘塑", "1", "2,000"],
        ), TRADE_DATE, {"3131"},
    ) == {"3131": 15000}


def test_v2_components_sum_exact_shares_and_keep_regular_compatibility(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "33,714,369", "5", "100", "20", "21", "19", "20.5"]),
        tpex_payload(["3131", "弘塑", "120.5", "+1", "119", "121", "118", "848,000"]),
        twse_intraday=twse_component_payload(["1101", "台泥", "114,544"]),
        twse_post_odd=twse_component_payload(["1101", "台泥", "9,825"]),
        twse_fixed=twse_component_payload(["1101", "台泥", "29"], fixed_price=True),
        tpex_intraday=tpex_component_payload(["3131", "弘塑", "59,671"]),
        tpex_post_odd=tpex_component_payload(["3131", "弘塑", "232"]),
        tpex_fixed=tpex_component_payload(["3131", "弘塑", "0"], fixed_price=True),
        tpex_daily_quotes=tpex_payload(["3131", "弘塑", "120.5", "+1", "119", "121", "118", "907,903"]),
    )
    collect_stock_daily_market(TRADE_DATE, db_path, session)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT p_stock_code, p_volume_shares, p_regular_volume_shares,
                      p_intraday_odd_lot_shares, p_post_odd_lot_shares,
                      p_fixed_price_volume_shares, p_block_trade_volume_shares, p_total_volume_shares,
                      p_market_volume_shares, p_all_execution_volume_shares,
                      p_volume_definition, p_volume_component_status
               FROM stock_daily_market ORDER BY p_stock_code"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("1101", 33714369, 33590000, 114544, 9825, 29000, 0, 33743369,
         33714369, 33743369,
         "REGULAR_ODD_FIXED_V2", "COMPLETE"),
        ("3131", 848000, 848000, 59671, 232, 0, 0, 907903,
         907903, 907903,
         "REGULAR_ODD_FIXED_V2", "COMPLETE"),
    ]


@pytest.mark.parametrize(
    ("day", "daily_rows", "regular_rows", "intraday_rows", "post_odd_rows", "fixed_rows", "block_rows", "expected"),
    [
        (
            date(2026, 9, 4),
            [["6182", "合晶", "111", "+", "1", "1", "1", "36,609,244"],
             ["4979", "華星光", "1", "+", "1", "1", "1", "19,025,771"],
             ["3324", "雙鴻", "1", "+", "1", "1", "1", "3,081,121"]],
            [["6182", "合晶", "111", "+", "1", "1", "1", "36,225,000"],
             ["4979", "華星光", "1", "+", "1", "1", "1", "18,513,000"],
             ["3324", "雙鴻", "1", "+", "1", "1", "1", "2,720,000"]],
            [["6182", "合晶", "239,472"], ["4979", "華星光", "503,893"], ["3324", "雙鴻", "354,381"]],
            [["6182", "合晶", "10,772"], ["4979", "華星光", "2,878"], ["3324", "雙鴻", "3,740"]],
            [["6182", "合晶", "134"], ["4979", "華星光", "6"], ["3324", "雙鴻", "3"]],
            [],
            {"6182": 36_609_244, "4979": 19_025_771, "3324": 3_081_121},
        ),
        (
            date(2026, 9, 9),
            [["3324", "雙鴻", "1385", "+", "1", "1", "1", "1,436,893"]],
            [["3324", "雙鴻", "1385", "+", "1", "1", "1", "1,351,000"]],
            [["3324", "雙鴻", "79,472"]], [["3324", "雙鴻", "1,421"]],
            [["3324", "雙鴻", "5"]], [], {"3324": 1_436_893},
        ),
    ],
)
def test_tpex_dailyquotes_regression_and_component_reconciliation(
    tmp_path, day, daily_rows, regular_rows, intraday_rows, post_odd_rows, fixed_rows, block_rows, expected,
):
    db_path = tmp_path / "history.db"
    session = Session(
        with_trade_date(twse_payload(), day),
        with_trade_date(tpex_payload(*regular_rows), day),
        twse_intraday=with_trade_date(twse_component_payload(), day),
        twse_post_odd=with_trade_date(twse_component_payload(), day),
        twse_fixed=with_trade_date(twse_component_payload(fixed_price=True), day),
        tpex_daily_quotes=with_trade_date(tpex_payload(*daily_rows), day),
        tpex_regular=with_trade_date(tpex_payload(*regular_rows), day),
        tpex_intraday=with_trade_date(tpex_component_payload(*intraday_rows), day),
        tpex_post_odd=with_trade_date(tpex_component_payload(*post_odd_rows), day),
        tpex_fixed=with_trade_date(tpex_component_payload(*fixed_rows, fixed_price=True), day),
        tpex_block=with_trade_date(tpex_block_payload(*block_rows), day),
    )
    collect_stock_daily_market(
        day, db_path, session, verified_mappings=verified_tpex_mappings(*expected),
    )
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT p_stock_code, p_market_volume_shares, p_all_execution_volume_shares,
                      p_volume_component_status
                 FROM stock_daily_market ORDER BY p_stock_code"""
        ).fetchall()
    assert {row["p_stock_code"]: row["p_market_volume_shares"] for row in rows} == expected
    assert all(row["p_market_volume_shares"] == row["p_all_execution_volume_shares"] for row in rows)
    assert {row["p_volume_component_status"] for row in rows} == {"COMPLETE"}


def test_tpex_reconciliation_failure_preserves_official_dailyquotes(tmp_path):
    db_path = tmp_path / "history.db"
    session = Session(
        twse_payload(),
        tpex_payload(["3324", "雙鴻", "1", "+", "1", "1", "1", "1,000"]),
        tpex_daily_quotes=tpex_payload(["3324", "雙鴻", "1", "+", "1", "1", "1", "1,001"]),
        tpex_regular=tpex_payload(["3324", "雙鴻", "1", "+", "1", "1", "1", "1,000"]),
    )
    collect_stock_daily_market(
        TRADE_DATE, db_path, session, verified_mappings=verified_tpex_mappings("3324"),
    )
    with connect(db_path) as connection:
        row = connection.execute(
            """SELECT p_market_volume_shares, p_all_execution_volume_shares,
                      p_volume_component_status
                 FROM stock_daily_market WHERE p_stock_code = '3324'"""
        ).fetchone()
    assert tuple(row) == (1_001, 1_000, "RECONCILIATION_FAILURE")


def test_valid_component_report_missing_target_is_verified_zero(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    collect_stock_daily_market(
        TRADE_DATE, db_path,
        Session(
            twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
            tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "567"]),
        ),
    )
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT p_regular_volume_shares, p_intraday_odd_lot_shares,
                      p_post_odd_lot_shares, p_fixed_price_volume_shares,
                      p_block_trade_volume_shares,
                      p_total_volume_shares, p_market_volume_shares,
                      p_all_execution_volume_shares
               FROM stock_daily_market ORDER BY p_stock_code"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, 0, 0, 0, 0, 1, 1, 1),
        (567, 0, 0, 0, 0, 567, 567, 567),
    ]


def test_twse_rejects_negative_regular_after_removing_odd_lots(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    with pytest.raises(StockMarketFormatError, match="smaller than odd-lot components"):
        collect_stock_daily_market(
            TRADE_DATE, db_path,
            Session(
                twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
                tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "567"]),
                twse_intraday=twse_component_payload(["1101", "台泥", "2"]),
            ),
        )
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0


def test_component_source_error_never_becomes_zero_or_writes_v2_rows(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    bad_intraday = twse_component_payload()
    bad_intraday["date"] = "20260827"
    with pytest.raises(StockMarketFormatError, match="TWSE odd-lot response is not the requested"):
        collect_stock_daily_market(
            TRADE_DATE, db_path,
            Session(
                twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
                tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "567"]),
                twse_intraday=bad_intraday,
            ),
        )
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0


def test_internally_created_session_is_closed_after_success(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    owned = ClosableSession(twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]), tpex_payload(["3131", "弘塑", "1", "+", "1", "1", "1", "1"]))
    monkeypatch.setattr(stock_collector, "build_session", lambda: owned)
    collect_stock_daily_market(TRADE_DATE, db_path)
    assert owned.close_calls == 1


def test_internally_created_session_is_closed_after_exception(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    bad = twse_component_payload(); bad["date"] = "20260827"
    owned = ClosableSession(twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]), tpex_payload(["3131", "弘塑", "1", "+", "1", "1", "1", "1"]), twse_intraday=bad)
    monkeypatch.setattr(stock_collector, "build_session", lambda: owned)
    with pytest.raises(StockMarketFormatError):
        collect_stock_daily_market(TRADE_DATE, db_path)
    assert owned.close_calls == 1


def test_caller_owned_session_is_not_closed_and_deadline_stops_before_write(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    caller = ClosableSession(twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]), tpex_payload(["3131", "弘塑", "1", "+", "1", "1", "1", "1"]))
    with pytest.raises(StockMarketFormatError, match="deadline exceeded"):
        collect_stock_daily_market(TRADE_DATE, db_path, caller, deadline_monotonic=time.monotonic() - 1)
    assert caller.close_calls == 0
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0


def test_progress_reports_endpoint_parser_result(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    events = []
    collect_stock_daily_market(
        TRADE_DATE, db_path,
        Session(twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]), tpex_payload(["3131", "弘塑", "1", "+", "1", "1", "1", "1"])),
        progress=events.append,
    )
    assert [event["endpoint"] for event in events] == [
        "TWSE MI_INDEX", "TPEx dailyQuotes", "TWSE TWTC7U", "TWSE TWT53U", "TWSE BFT41U",
        "TPEx oddQuote", "TPEx odd", "TPEx fixPricing", "TPEx otc", "TPEx blockTrade/quote",
    ]
    assert all(event["outcome"].startswith("PASS") and event["attempt"] == 1 for event in events)


@pytest.mark.parametrize("transient_error", [
    requests.exceptions.ChunkedEncodingError(IncompleteRead(b"partial", 8)),
    requests.exceptions.TooManyRedirects("temporary official redirect loop"),
])
def test_daily_quotes_retries_transient_response_failure_then_uses_complete_response(
    tmp_path, monkeypatch, transient_error,
):
    """A partial first body is discarded; only the second response reaches parsing."""
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)

    class FlakyDailyQuotesSession(Session):
        def __init__(self):
            super().__init__(
                twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
                tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "567"]),
            )
            self.daily_quotes_attempts = 0

        def post(self, url, *args, **kwargs):
            if url.endswith("dailyQuotes"):
                self.daily_quotes_attempts += 1
                if self.daily_quotes_attempts == 1:
                    raise transient_error
            return super().post(url, *args, **kwargs)

    monkeypatch.setattr(stock_collector.time, "sleep", lambda _seconds: None)
    events = []
    session = FlakyDailyQuotesSession()
    result = collect_stock_daily_market(TRADE_DATE, db_path, session, progress=events.append)

    assert session.daily_quotes_attempts == 2
    assert result["complete_records"] == 2
    daily_events = [event for event in events if event["endpoint"] == "TPEx dailyQuotes"]
    assert [(event["attempt"], event["outcome"].split()[0]) for event in daily_events] == [
        (1, "RETRY"), (2, "PASS"),
    ]
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 2


def test_chunked_body_retry_exhaustion_writes_no_v2_rows_or_later_dates(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    with connect(db_path) as connection:
        upsert_stock_daily_market(connection, [
            {
                "trade_date": "2026-08-28", "p_stock_code": "1101",
                "p_open_price": 20.0, "p_high_price": 20.0, "p_low_price": 20.0,
                "p_close_price": 20.0, "p_volume_shares": 1,
            },
            {
                "trade_date": "2026-08-29", "p_stock_code": "1101",
                "p_open_price": 20.0, "p_high_price": 20.0, "p_low_price": 20.0,
                "p_close_price": 20.0, "p_volume_shares": 1,
            },
        ])

    class AlwaysBrokenDailyQuotesSession(Session):
        def __init__(self):
            super().__init__(
                twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
                tpex_payload(),
            )
            self.daily_quotes_attempts = 0

        def post(self, url, *args, **kwargs):
            if url.endswith("dailyQuotes"):
                self.daily_quotes_attempts += 1
                raise requests.exceptions.ChunkedEncodingError(
                    IncompleteRead(b"partial", 8)
                )
            return super().post(url, *args, **kwargs)

    monkeypatch.setattr(stock_collector.time, "sleep", lambda _seconds: None)
    session = AlwaysBrokenDailyQuotesSession()
    calls = []

    def collector(trade_date, database, **kwargs):
        calls.append(trade_date)
        return collect_stock_daily_market(trade_date, database, session=session, **kwargs)

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        enrich_existing_stock_daily_market_v2(
            db_path, date(2026, 8, 28), date(2026, 8, 29), collector=collector,
        )

    assert session.daily_quotes_attempts == 3
    assert calls == [date(2026, 8, 28)]
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT trade_date,p_volume_definition,p_market_volume_shares "
            "FROM stock_daily_market ORDER BY trade_date"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-08-28", "REGULAR_ONLY_V1", None),
        ("2026-08-29", "REGULAR_ONLY_V1", None),
    ]


def test_tpex_otc_component_requests_ew_type(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]), tpex_payload(["3131", "弘塑", "1", "+", "1", "1", "1", "1"]))
    collect_stock_daily_market(TRADE_DATE, db_path, session)
    otc_calls = [data for url, data in session.post_calls if url.endswith("/otc")]
    assert otc_calls == [{"date": "2026/08/28", "response": "json", "type": "EW"}]


def test_twse_missing_ohlc_is_saved_as_null_but_volume_stays_strict():
    record = parse_twse_market(
        twse_payload(["1538", "正峰", "1", "1", "8", "--", "---", "", "----"]),
        TRADE_DATE,
        {"1538"},
    )["1538"]
    assert record["p_volume_shares"] == 1
    assert [record[field] for field in (
        "p_open_price", "p_high_price", "p_low_price", "p_close_price"
    )] == [None, None, None, None]

    for missing_volume in ("", "--", "---"):
        with pytest.raises(StockMarketFormatError, match="numeric value is missing"):
            parse_twse_market(
                twse_payload(["1538", "正峰", missing_volume, "1", "8", "--", "--", "--", "--"]),
                TRADE_DATE,
                {"1538"},
            )


def test_collector_records_zero_and_missing_close_coverage_with_provenance(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "0", "0", "0", "20", "20", "20", "20"]),
        tpex_payload(["3131", "弘塑", "--", "+0", "--", "--", "--", "0"]),
    )

    collect_stock_daily_market(TRADE_DATE, db_path, session)

    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT stock_code, market, status, reason, source_url, response_date
            FROM stock_daily_coverage ORDER BY stock_code
            """
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("1101", "TWSE", "OFFICIAL_ZERO", "official_zero_volume",
         "https://www.twse.com.tw/exchangeReport/MI_INDEX", "2026-08-28"),
        ("3131", "TPEX", "MISSING_CLOSE", "official_close_price_missing",
         "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc", "2026-08-28"),
    ]


def test_collector_rejects_historical_date_without_exact_mapping(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    with connect(db_path) as connection:
        connection.execute("DELETE FROM cb_parent_stock_mapping")

    with pytest.raises(ParentStockMappingError, match="unverified_parent_stock_mapping"):
        collect_stock_daily_market(TRADE_DATE, db_path, Session(twse_payload(), tpex_payload()))


def test_tib_mapping_keeps_tib_coverage_when_twse_feed_has_the_official_row(tmp_path):
    db_path = tmp_path / "history.db"
    with connect(db_path) as connection:
        connection.execute(
            """INSERT INTO cb_master (
                cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
                issue_amount, source, source_url, collected_at
            ) VALUES ('33331', '創一', '6854', '錼創', '2024-01-01', '2027-01-01',
                      100000000, 'official', 'https://example.test', '2026-08-28T00:00:00+00:00')"""
        )
        upsert_daily(connection, [{
            "trade_date": "2026-08-28", "cb_code": "33331", "cb_name": "創一",
            "close_price": 100, "volume_lots": 0, "source": "test",
            "collected_at": "2026-08-28T00:00:00+00:00",
        }])
        upsert_parent_stock_mappings(connection, [{
            "cb_code": "33331", "mapping_date": "2026-08-28", "stock_code": "6854",
            "stock_name": "錼創", "market": "TIB", "source": "official",
            "source_url": "https://example.test/tib", "verified_at": "2026-08-28T00:00:00+00:00",
        }])
    collect_stock_daily_market(
        TRADE_DATE, db_path,
        Session(twse_payload(["6854", "錼創", "10", "1", "100", "10", "11", "9", "10"]), tpex_payload()),
    )
    with connect(db_path) as connection:
        assert tuple(connection.execute(
            "SELECT market, status FROM stock_daily_coverage WHERE stock_code='6854'"
        ).fetchone()) == ("TIB", "COMPLETE")


def test_monthly_mapping_requires_explicit_opt_in_and_labels_coverage(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    monthly = [
        {"cb_code": "11111", "year_month": "2026-08", "stock_code": "1101",
         "stock_name": "台泥", "market": "TWSE", "source": "MOPS:t120sg01",
         "source_url": "https://mopsov.twse.com.tw/mops/web/t120sg01?bond_id=11111&issuer_stock_code=1101&monyr_reg=202608",
         "verified_at": "2026-08-31T00:00:00+00:00"},
        {"cb_code": "22221", "year_month": "2026-08", "stock_code": "3131",
         "stock_name": "弘塑", "market": "TPEX", "source": "MOPS:t120sg01",
         "source_url": "https://mopsov.twse.com.tw/mops/web/t120sg01?bond_id=22221&issuer_stock_code=3131&monyr_reg=202608",
         "verified_at": "2026-08-31T00:00:00+00:00"},
    ]
    with connect(db_path) as connection:
        connection.execute("DELETE FROM cb_parent_stock_mapping")
        upsert_parent_stock_monthly_mappings(connection, monthly)
    session = Session(
        twse_payload(["1101", "台泥", "1", "1", "1", "20", "21", "19", "20"]),
        tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "1"]),
    )
    with pytest.raises(ParentStockMappingError):
        collect_stock_daily_market(TRADE_DATE, db_path, session)
    collect_stock_daily_market(TRADE_DATE, db_path, session, allow_monthly_verified=True)
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT mapping_level, mapping_source_url, mapping_year_month, mapping_verified_at
            FROM stock_daily_coverage ORDER BY stock_code
            """
        ).fetchall()
        assert len(rows) == 2
        assert all(row[0] == "MONTHLY_VERIFIED" for row in rows)
        assert all("mopsov.twse.com.tw" in row[1] for row in rows)
        assert all(row[2] == "2026-08" for row in rows)
        assert all(row[3] == "2026-08-31T00:00:00+00:00" for row in rows)


def test_daily_market_upsert_uses_both_official_markets_in_one_result(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"]),
        tpex_payload(["3131", "弘塑", "120.5", "+1", "119", "121", "118", "567"]),
    )

    result = collect_stock_daily_market(TRADE_DATE, db_path, session)

    assert result == {
        "trade_date": "2026-08-28", "target_stocks": 2, "twse_records": 1,
        "tpex_records": 1, "complete_records": 2, "reconciliation_failures": 0,
        "records_inserted": 2, "records_updated": 0,
    }
    with connect(db_path) as connection:
        assert connection.execute("PRAGMA table_info(stock_daily_market)").fetchall()
        saved = connection.execute(
            """
            SELECT p_stock_code, p_open_price, p_high_price, p_low_price,
                   p_close_price, p_volume_shares
            FROM stock_daily_market ORDER BY p_stock_code
            """
        ).fetchall()
        coverage = connection.execute(
            "SELECT stock_code, market, status, response_date FROM stock_daily_coverage ORDER BY stock_code"
        ).fetchall()
    assert [tuple(row) for row in saved] == [
        ("1101", 20.0, 21.0, 19.0, 20.5, 1234),
        ("3131", 119.0, 121.0, 118.0, 120.5, 567),
    ]
    assert [tuple(row) for row in coverage] == [
        ("1101", "TWSE", "COMPLETE", "2026-08-28"),
        ("3131", "TPEX", "COMPLETE", "2026-08-28"),
    ]


def test_missing_parent_stock_fails_before_any_market_row_is_written(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"]),
        tpex_payload(),
    )

    with pytest.raises(StockMarketFormatError, match="missing from official daily markets"):
        collect_stock_daily_market(TRADE_DATE, db_path, session, suspension_verifier=no_suspensions)
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0
        row = connection.execute(
            "SELECT status, reason, availability_status FROM stock_daily_coverage WHERE stock_code='3131'"
        ).fetchone()
        assert tuple(row) == ("MISSING_OFFICIAL_ROW", "missing_from_official_daily_market", "UNVERIFIED_MISSING")


def test_twse_recovery_evidence_strictly_verifies_only_the_suspension_interval():
    payload = {
        "fields": ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "減資原因", "詳細資料"],
        "data": [["115/09/21", "3591", "23.65", "退還股款", "3591  ,20260909"]],
    }

    class RecoverySession:
        def get(self, *_args, **_kwargs):
            return Response(payload)

    verified = verify_twse_suspensions(RecoverySession(), {"3591"}, date(2026, 9, 11))
    assert verified["3591"]["last_trade_date"] == "2026-09-09"
    assert verified["3591"]["recovery_date"] == "2026-09-21"
    assert verify_twse_suspensions(RecoverySession(), {"3591"}, date(2026, 9, 9)) == {}
    assert "3591" in verify_twse_suspensions(RecoverySession(), {"3591"}, date(2026, 9, 10))
    assert verify_twse_suspensions(RecoverySession(), {"3591"}, date(2026, 9, 21)) == {}


def test_twse_recovery_evidence_rejects_malformed_or_another_stock():
    class RecoverySession:
        def __init__(self, row):
            self.row = row

        def get(self, *_args, **_kwargs):
            return Response({
                "fields": ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "減資原因", "詳細資料"],
                "data": [self.row],
            })

    with pytest.raises(StockMarketFormatError, match="detail is malformed"):
        verify_twse_suspensions(
            RecoverySession(["115/09/21", "3591", "23.65", "退還股款", "bad-detail"]),
            {"3591"}, date(2026, 9, 11),
        )
    assert verify_twse_suspensions(
        RecoverySession(["115/09/21", "3356", "59.40", "退還股款", "3356,20260909"]),
        {"3591"}, date(2026, 9, 11),
    ) == {}


def test_verified_suspended_parent_passes_coverage_without_a_synthetic_market_row(tmp_path):
    db_path = tmp_path / "history.db"
    with connect(db_path) as connection:
        upsert_daily(connection, [{
            "trade_date": "2026-08-28", "cb_code": "35914", "cb_name": "艾笛森四",
            "close_price": 100, "volume_lots": 0, "source": "test", "collected_at": "x",
        }])
        upsert_parent_stock_mappings(connection, [{
            "cb_code": "35914", "mapping_date": "2026-08-28", "stock_code": "3591",
            "stock_name": "艾笛森", "market": "TWSE", "source": "official",
            "source_url": "https://example.test/issue", "verified_at": "2026-08-28T00:00:00+00:00",
        }])
    collect_stock_daily_market(
        TRADE_DATE, db_path, Session(twse_payload(), tpex_payload()),
        suspension_verifier=suspended_3591,
    )
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0
        row = connection.execute(
            """SELECT status, reason, availability_status, availability_evidence_json
               FROM stock_daily_coverage WHERE stock_code='3591'"""
        ).fetchone()
    assert tuple(row[:3]) == ("MISSING_OFFICIAL_ROW", "parent_stock_suspended", "VERIFIED_SUSPENDED")
    assert '"recovery_date":"2026-09-21"' in row[3]


def test_present_daily_row_does_not_call_suspension_fallback(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    calls = []

    def verifier(*_args, **_kwargs):
        calls.append(True)
        return {}

    collect_stock_daily_market(
        TRADE_DATE, db_path,
        Session(
            twse_payload(["1101", "台泥", "1", "1", "1", "20", "20", "20", "20"]),
            tpex_payload(["3131", "弘塑", "120", "+1", "119", "121", "118", "1"]),
        ),
        suspension_verifier=verifier,
    )
    assert calls == []


def test_suspension_verifier_error_remains_a_hard_failure(tmp_path):
    db_path = tmp_path / "history.db"
    with connect(db_path) as connection:
        upsert_daily(connection, [{
            "trade_date": "2026-08-28", "cb_code": "35914", "cb_name": "艾笛森四",
            "close_price": 100, "volume_lots": 0, "source": "test", "collected_at": "x",
        }])
        upsert_parent_stock_mappings(connection, [{
            "cb_code": "35914", "mapping_date": "2026-08-28", "stock_code": "3591",
            "stock_name": "艾笛森", "market": "TWSE", "source": "official",
            "source_url": "https://example.test/issue", "verified_at": "2026-08-28T00:00:00+00:00",
        }])

    def broken_verifier(*_args, **_kwargs):
        raise StockMarketFormatError("TWSE recovery evidence response structure changed")

    with pytest.raises(StockMarketFormatError, match="recovery evidence"):
        collect_stock_daily_market(
            TRADE_DATE, db_path, Session(twse_payload(), tpex_payload()),
            suspension_verifier=broken_verifier,
        )


def test_wrong_official_response_date_fails_before_writing(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    payload = twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"])
    payload["date"] = "20260827"

    with pytest.raises(StockMarketFormatError, match="requested published trade date"):
        collect_stock_daily_market(TRADE_DATE, db_path, Session(payload, tpex_payload()))
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM stock_daily_coverage WHERE status='SOURCE_ERROR'"
        ).fetchone()[0] == 2


def test_same_date_and_parent_stock_is_upserted(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "1000", "5", "100", "20.0", "21.0", "19.0", "20.5"]),
        tpex_payload(["3131", "弘塑", "120.5", "+1", "119", "121", "118", "567"]),
    )
    assert collect_stock_daily_market(TRADE_DATE, db_path, session)["records_inserted"] == 2
    assert collect_stock_daily_market(TRADE_DATE, db_path, session)["records_updated"] == 2


def test_database_rejects_fractional_share_volume(tmp_path):
    with connect(tmp_path / "history.db") as connection:
        with pytest.raises(ValueError, match="non-negative integer"):
            upsert_stock_daily_market(
                connection,
                [{
                    "trade_date": "2026-08-28", "p_stock_code": "1101",
                    "p_open_price": 20.0, "p_high_price": 21.0,
                    "p_low_price": 19.0, "p_close_price": 20.5,
                    "p_volume_shares": 1.5,
                }],
            )


def test_market_retry_does_not_honor_an_unbounded_server_retry_after(monkeypatch):
    monkeypatch.setattr("stock_collector.build_tpex_session", requests.Session)
    session = build_session()
    retry = session.get_adapter("https://").max_retries

    assert retry.respect_retry_after_header is False


def test_twse_component_urls_use_official_rwd_historical_routes():
    assert TWSE_INTRADAY_ODD_LOT_URL.endswith("/rwd/zh/afterTrading/TWTC7U")
    assert TWSE_POST_ODD_LOT_URL.endswith("/rwd/zh/afterTrading/TWT53U")
    assert TWSE_FIXED_PRICE_URL.endswith("/rwd/zh/afterTrading/BFT41U")
