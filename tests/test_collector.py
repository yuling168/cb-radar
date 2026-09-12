from datetime import date

import pytest
import requests

from collector import (
    DataNotPublished,
    TpexFormatError,
    collect,
    get_with_transient_retry,
    parse_tpex_csv,
    volume_to_lots,
)
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
    content = b'{"stat":"ok","tables":[{"fields":["\xe8\xb3\x87\xe6\x96\x99\xe6\x97\xa5\xe6\x9c\x9f","\xe6\xaa\x94\xe6\xa1\x88\xe4\xb8\x8b\xe8\xbc\x89"],"data":[]}]}'

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


class SuccessfulResponse:
    content = b""

    def raise_for_status(self):
        return None


class TransientSequenceSession:
    def __init__(self, outcomes):
        self.headers = {}
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_transient_ssl_error_retries_once_then_succeeds(monkeypatch, capsys):
    response = SuccessfulResponse()
    session = TransientSequenceSession([requests.exceptions.SSLError("tls"), response])
    sleeps = []
    monkeypatch.setattr("collector.time.sleep", sleeps.append)

    assert get_with_transient_retry(session, "https://www.tpex.org.tw/test") is response
    assert len(session.calls) == 2
    assert sleeps == [1]
    assert "SSLError attempt=1/3" in capsys.readouterr().err


def test_transient_ssl_errors_retry_until_third_attempt_success(monkeypatch, capsys):
    response = SuccessfulResponse()
    session = TransientSequenceSession([
        requests.exceptions.SSLError("first"),
        requests.exceptions.SSLError("second"),
        response,
    ])
    sleeps = []
    monkeypatch.setattr("collector.time.sleep", sleeps.append)

    assert get_with_transient_retry(session, "https://www.tpex.org.tw/test") is response
    assert len(session.calls) == 3
    assert sleeps == [1, 2]
    assert "recovered after retry: host=www.tpex.org.tw attempt=3/3" in capsys.readouterr().err


def test_transient_ssl_error_hard_fails_after_three_attempts(monkeypatch):
    session = TransientSequenceSession([requests.exceptions.SSLError("tls")] * 3)
    sleeps = []
    monkeypatch.setattr("collector.time.sleep", sleeps.append)

    with pytest.raises(requests.exceptions.SSLError, match="tls"):
        get_with_transient_retry(session, "https://www.tpex.org.tw/test")
    assert len(session.calls) == 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize("error", [
    requests.exceptions.ConnectionError("connection"),
    requests.exceptions.Timeout("timeout"),
])
def test_transient_connection_and_timeout_errors_retry(monkeypatch, error):
    response = SuccessfulResponse()
    session = TransientSequenceSession([error, response])
    sleeps = []
    monkeypatch.setattr("collector.time.sleep", sleeps.append)

    assert get_with_transient_retry(session, "https://www.tpex.org.tw/test") is response
    assert len(session.calls) == 2
    assert sleeps == [1]


def test_normal_request_is_not_retried_or_given_an_ssl_override(monkeypatch):
    response = SuccessfulResponse()
    session = TransientSequenceSession([response])
    monkeypatch.setattr("collector.time.sleep", lambda _seconds: pytest.fail("unexpected sleep"))

    assert get_with_transient_retry(session, "https://www.tpex.org.tw/test", timeout=12) is response
    assert len(session.calls) == 1
    assert session.calls[0][1] == {"timeout": 12, "stream": True}
