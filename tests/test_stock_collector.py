from datetime import date

import pytest

from db import connect, upsert_daily, upsert_stock_daily_market
from stock_collector import (
    StockMarketFormatError,
    collect_stock_daily_market,
    parse_tpex_market,
    parse_twse_market,
)


TRADE_DATE = date(2026, 8, 28)
TWSE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]
TPEX_FIELDS = ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  "]


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
    def __init__(self, twse, tpex):
        self.twse = twse
        self.tpex = tpex

    def get(self, *args, **kwargs):
        return Response(self.twse)

    def post(self, *args, **kwargs):
        return Response(self.tpex)


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
        "tpex_records": 1, "records_inserted": 2, "records_updated": 0,
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
    assert [tuple(row) for row in saved] == [
        ("1101", 20.0, 21.0, 19.0, 20.5, 1234),
        ("3131", 119.0, 121.0, 118.0, 120.5, 567),
    ]


def test_missing_parent_stock_fails_before_any_market_row_is_written(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    session = Session(
        twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"]),
        tpex_payload(),
    )

    with pytest.raises(StockMarketFormatError, match="missing from official daily markets"):
        collect_stock_daily_market(TRADE_DATE, db_path, session)
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0


def test_wrong_official_response_date_fails_before_writing(tmp_path):
    db_path = tmp_path / "history.db"
    seed_phase1_and_master(db_path)
    payload = twse_payload(["1101", "台泥", "1,234", "5", "100", "20.0", "21.0", "19.0", "20.5"])
    payload["date"] = "20260827"

    with pytest.raises(StockMarketFormatError, match="requested published trade date"):
        collect_stock_daily_market(TRADE_DATE, db_path, Session(payload, tpex_payload()))
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_daily_market").fetchone()[0] == 0


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
