from datetime import date

import pytest
import requests

import cb_backfill
from cb_backfill import BackfillIncompleteError, CountingSession, backfill_cb_daily
from collector import DataNotPublished, TpexFormatError


def test_backfill_skips_unpublished_dates_and_collects_exact_requested_days(monkeypatch, tmp_path):
    calls = []

    def fake_collect(requested_date, database, latest_available, session, write):
        calls.append((requested_date, database, latest_available, write))
        if requested_date == date(2026, 8, 30):
            raise DataNotPublished("weekend")
        return {
            "trade_date": requested_date.isoformat(),
            "records_inserted": 10,
            "records_updated": 2,
        }

    monkeypatch.setattr(cb_backfill, "collect", fake_collect)

    result = backfill_cb_daily(
        tmp_path / "history.db", days=3, end_date=date(2026, 8, 31), dry_run=True
    )

    assert [call[0] for call in calls] == [
        date(2026, 8, 31), date(2026, 8, 30), date(2026, 8, 29), date(2026, 8, 28)
    ]
    assert all(call[2] is False and call[3] is False for call in calls)
    assert result == {
        "start_date": "2026-08-28",
        "end_date": "2026-08-31",
        "trade_days": 3,
        "records_inserted": 30,
        "records_updated": 6,
        "unpublished_dates": 1,
        "request_count": 0,
        "dry_run": True,
    }


def test_backfill_propagates_source_errors_instead_of_skipping_them(monkeypatch, tmp_path):
    def fake_collect(*_args, **_kwargs):
        raise TpexFormatError("required fields changed")

    monkeypatch.setattr(cb_backfill, "collect", fake_collect)

    with pytest.raises(TpexFormatError, match="required fields changed"):
        backfill_cb_daily(tmp_path / "history.db", days=1, end_date=date(2026, 8, 31))


def test_backfill_fails_if_requested_trading_days_are_not_found(monkeypatch, tmp_path):
    def fake_collect(*_args, **_kwargs):
        raise DataNotPublished("not published")

    monkeypatch.setattr(cb_backfill, "collect", fake_collect)

    with pytest.raises(BackfillIncompleteError, match="Found 0 official trading days"):
        backfill_cb_daily(
            tmp_path / "history.db", days=1, end_date=date(2026, 8, 31), max_calendar_days=2
        )


def test_backfill_reuses_existing_collect_upsert_mode(monkeypatch, tmp_path):
    writes = []

    def fake_collect(
        _requested_date, _database, *, latest_available, session, write
    ):
        assert latest_available is False
        assert session is not None
        writes.append(write)
        return {"trade_date": "2026-08-31", "records_inserted": 3, "records_updated": 4}

    monkeypatch.setattr(cb_backfill, "collect", fake_collect)

    result = backfill_cb_daily(tmp_path / "history.db", days=1, end_date=date(2026, 8, 31))

    assert writes == [True]
    assert result["records_inserted"] == 3
    assert result["records_updated"] == 4


def test_counting_session_retries_520_and_counts_each_attempt(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    class Session:
        def __init__(self):
            self.headers = {}
            self.responses = [Response(520), Response(200)]

        def get(self, *_args, **_kwargs):
            return self.responses.pop(0)

    monkeypatch.setattr(cb_backfill.time, "sleep", lambda _seconds: None)
    session = CountingSession(Session())

    response = session.get("https://example.test")

    assert response.status_code == 200
    assert session.request_count == 2
