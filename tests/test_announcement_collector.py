import json

import pytest
import requests

from announcement_collector import AnnouncementSourceError, collect_market
from db import connect


def twse_row(**changes):
    row = {
        "出表日期": "1150903", "發言日期": "1150902", "發言時間": "70003",
        "公司代號": "1560", "公司名稱": "中砂", "主旨 ": "公告行使債券贖回權",
        "符合條款": "第51款", "事實發生日": "1150902",
        "說明": "轉換公司債收回基準日：115年09月02日",
    }
    row.update(changes)
    return row


class Response:
    def __init__(self, payload=None, *, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else "<html>error</html>"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def test_collects_raw_snapshot_and_is_idempotent_on_rerun(tmp_path):
    db_path = tmp_path / "announcements.db"
    session = Session([Response([twse_row()])])

    first = collect_market("TWSE", db_path, session, max_attempts=1)
    second = collect_market("TWSE", db_path, session, max_attempts=1)

    assert first["batch_date"] == "2026-09-03"
    assert first["inserted"] == 1 and first["updated"] == 0
    assert second["inserted"] == 0 and second["updated"] == 1
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM announcement_fetch WHERE status = 'succeeded'").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM announcement_snapshot").fetchone()[0] == 1
        saved = connection.execute(
            "SELECT source_market, company_code, spoken_date, spoken_time, logical_key, event_key, body FROM company_announcements"
        ).fetchone()
    assert tuple(saved[:4]) == ("TWSE", "1560", "2026-09-02", "07:00:03")
    assert len(saved[4]) == len(saved[5]) == 64
    assert saved[6] == "轉換公司債收回基準日:115年09月02日"


def test_content_change_creates_a_new_event_version_with_same_logical_key(tmp_path):
    db_path = tmp_path / "announcements.db"
    session = Session([
        Response([twse_row()]),
        Response([twse_row(**{"說明": "轉換公司債收回基準日：115年09月03日"})]),
    ])

    collect_market("TWSE", db_path, session, max_attempts=1)
    collect_market("TWSE", db_path, session, max_attempts=1)

    with connect(db_path) as connection:
        rows = connection.execute("SELECT logical_key, event_key FROM company_announcements ORDER BY event_key").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM announcement_snapshot").fetchone()[0] == 2
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]
    assert rows[0][1] != rows[1][1]


def test_failed_api_response_is_recorded_not_treated_as_zero_rows(tmp_path):
    db_path = tmp_path / "announcements.db"
    session = Session([Response(status_code=503), Response(status_code=503)])

    with pytest.raises(AnnouncementSourceError, match="failed after 2 attempts"):
        collect_market("TPEX", db_path, session, max_attempts=2)

    with connect(db_path) as connection:
        fetches = connection.execute("SELECT status, http_status, row_count FROM announcement_fetch ORDER BY fetch_id").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM announcement_snapshot").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM company_announcements").fetchone()[0] == 0
    assert [tuple(row) for row in fetches] == [("failed", 503, None), ("failed", 503, None)]


def test_invalid_json_is_failed_then_a_later_retry_can_succeed(tmp_path):
    db_path = tmp_path / "announcements.db"
    session = Session([
        Response(json_error=json.JSONDecodeError("bad", "x", 0)),
        Response([twse_row()]),
    ])

    result = collect_market("TWSE", db_path, session, max_attempts=2)

    assert result["inserted"] == 1
    with connect(db_path) as connection:
        statuses = connection.execute("SELECT status FROM announcement_fetch ORDER BY fetch_id").fetchall()
    assert [row[0] for row in statuses] == ["failed", "succeeded"]


def test_empty_array_without_an_official_batch_date_is_not_recorded_as_zero_announcements(tmp_path):
    db_path = tmp_path / "announcements.db"

    with pytest.raises(AnnouncementSourceError, match="zero or inconsistent batch dates"):
        collect_market("TWSE", db_path, Session([Response([])]), max_attempts=1)

    with connect(db_path) as connection:
        fetch = connection.execute("SELECT status, row_count FROM announcement_fetch").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM company_announcements").fetchone()[0] == 0
    assert tuple(fetch) == ("failed", None)
