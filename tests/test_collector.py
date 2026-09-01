from datetime import date

import pytest
import requests

from collector import DataNotPublished, TpexFormatError, collect, parse_tpex_csv, volume_to_lots
from db import connect, upsert_daily


HEADER = "HEADER,代號,名稱,交易,收市,漲跌,開市,最高,最低,筆數,單位,金額,均價,明日參價,明日漲停,明日跌停"


def csv_bytes(*body_rows: str, trade_date: str = "日期:115年08月28日") -> bytes:
    return "\n".join([f"DATADATE,{trade_date}", HEADER, *body_rows]).encode("cp950")


def equal_row(
    code: str, name: str, close: str, volume: str, reference: str = ""
) -> str:
    return (
        f'BODY,"{code}","{name}","等價","{close}","","","","","1",'
        f'"{volume}","","","{reference}","",""'
    )


def test_normal_trade_volume_is_saved_as_lots():
    records = parse_tpex_csv(
        csv_bytes(equal_row("17172", "長興二", "133.50", "65")), date(2026, 8, 28)
    )
    assert records[0]["volume_lots"] == 65
    assert volume_to_lots("300,000", "面額(元)") == 3


def test_official_blank_volume_becomes_zero_and_is_inserted(tmp_path):
    records = parse_tpex_csv(
        csv_bytes(equal_row("16095", "大亞五", "", "")), date(2026, 8, 28)
    )
    assert len(records) == 1
    assert records[0]["volume_lots"] == 0
    with connect(tmp_path / "test.db") as connection:
        assert upsert_daily(connection, records) == (1, 0)
        assert connection.execute("SELECT volume_lots FROM cb_daily").fetchone()[0] == 0


def test_blank_close_price_remains_null():
    records = parse_tpex_csv(
        csv_bytes(equal_row("16095", "大亞五", "", "")), date(2026, 8, 28)
    )
    assert records[0]["close_price"] is None


def test_official_reference_price_is_saved_separately_from_blank_close():
    records = parse_tpex_csv(
        csv_bytes(
            equal_row("37171", "聯嘉投控一", "", "", "135.95"),
            trade_date="日期:115年08月31日",
        ),
        date(2026, 8, 31),
    )
    assert records[0]["volume_lots"] == 0
    assert records[0]["close_price"] is None
    assert records[0]["reference_price"] == 135.95


class FailedSession:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        raise requests.ConnectionError("TPEx unavailable")


def test_tpex_source_failure_does_not_create_zero_rows(tmp_path):
    db_path = tmp_path / "test.db"
    with pytest.raises(requests.ConnectionError, match="TPEx unavailable"):
        collect(date(2026, 8, 28), db_path=db_path, session=FailedSession())
    assert not db_path.exists()


def test_required_field_disappearance_fails_loudly():
    malformed = csv_bytes().replace("單位".encode("cp950"), "成交量".encode("cp950"))
    with pytest.raises(TpexFormatError, match="required fields changed"):
        parse_tpex_csv(malformed, date(2026, 8, 28))


def test_same_date_and_code_is_idempotent(tmp_path):
    row = {
        "trade_date": "2026-08-28", "cb_code": "17172", "cb_name": "長興二",
        "close_price": 133.5, "reference_price": 133.5, "volume_lots": 65, "source": "TPEx:RSta0113",
        "collected_at": "2026-08-28T08:00:00+00:00",
    }
    with connect(tmp_path / "test.db") as connection:
        assert upsert_daily(connection, [row]) == (1, 0)
        assert upsert_daily(connection, [row]) == (0, 1)
        assert connection.execute("SELECT COUNT(*) FROM cb_daily").fetchone()[0] == 1


class EmptyIndexResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"stat": "ok", "tables": [{"fields": ["資料日期", "檔案下載"], "data": []}]}


class EmptyIndexSession:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        return EmptyIndexResponse()


def test_non_trading_day_does_not_write_fake_data(tmp_path):
    db_path = tmp_path / "test.db"
    with pytest.raises(DataNotPublished):
        collect(date(2026, 8, 29), db_path=db_path, session=EmptyIndexSession())
    assert not db_path.exists()
