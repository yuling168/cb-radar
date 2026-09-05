import pytest

from db import (
    connect,
    parent_stock_mappings_for_trade_date,
    upsert_daily,
    upsert_parent_stock_mappings,
)
from monthly_mapping_collector import (
    MonthlyMappingError,
    collect_monthly_verified_mappings,
    parse_mops_monthly_mapping,
)


MOPS_URL = (
    "https://mopsov.twse.com.tw/mops/web/t120sg01?bond_id=11111&"
    "issuer_stock_code=1101&monyr_reg=202608"
)
MOPS_CONTENT = """
<html><body>11111 台泥 1101 之轉(交)換公司債發行資料</body></html>
"""


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, text=MOPS_CONTENT):
        self.text = text
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return Response(self.text)


def seed_master(db_path):
    with connect(db_path) as connection:
        connection.execute(
            """INSERT INTO cb_master (
                cb_code, cb_name, stock_code, stock_name, issue_date, maturity_date,
                issue_amount, source, source_url, collected_at
            ) VALUES ('11111', '台泥一', '1101', '台泥', '2024-01-01', '2027-01-01',
                      100000000, 'official', ?, '2026-08-31T00:00:00+00:00')""",
            (MOPS_URL,),
        )


def test_mops_monthly_mapping_is_separate_and_verifies_provenance(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    session = Session()

    result = collect_monthly_verified_mappings(
        "2026-08", db_path, session=session, cb_codes={"11111"}
    )

    assert result["verified"] == 1
    assert "monyr_reg=202608" in session.urls[0]
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT year_month, source, source_url FROM cb_parent_stock_monthly_mapping"
        ).fetchone()
        assert tuple(row[:2]) == ("2026-08", "MOPS:t120sg01")
        assert connection.execute("SELECT count(*) FROM cb_parent_stock_mapping").fetchone()[0] == 0


def test_mops_mismatch_or_missing_identity_is_rejected_without_monthly_mapping(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    with pytest.raises(MonthlyMappingError, match="parent stock name"):
        collect_monthly_verified_mappings(
            "2026-08", db_path, session=Session(MOPS_CONTENT.replace("台泥", "錯名"))
        )
    with connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM cb_parent_stock_monthly_mapping").fetchone()[0] == 0


def test_exact_mapping_wins_and_monthly_requires_matching_month(tmp_path):
    db_path = tmp_path / "history.db"
    seed_master(db_path)
    with connect(db_path) as connection:
        upsert_daily(connection, [{
            "trade_date": "2026-08-28", "cb_code": "11111", "cb_name": "台泥一",
            "close_price": 100, "volume_lots": 0, "source": "test",
            "collected_at": "2026-08-28T00:00:00+00:00",
        }])
        collect_monthly_verified_mappings("2026-08", db_path, session=Session())
        monthly = parent_stock_mappings_for_trade_date(
            connection, "2026-08-28", allow_monthly_verified=True
        )
        assert monthly["11111"]["mapping_level"] == "MONTHLY_VERIFIED"
        upsert_parent_stock_mappings(connection, [{
            "cb_code": "11111", "mapping_date": "2026-08-28", "stock_code": "9999",
            "stock_name": "日級母股", "market": "TWSE", "source": "official",
            "source_url": "https://example.test/exact", "verified_at": "2026-08-28T00:00:00+00:00",
        }])
        exact = parent_stock_mappings_for_trade_date(
            connection, "2026-08-28", allow_monthly_verified=True
        )
        assert exact["11111"]["stock_code"] == "9999"
        assert exact["11111"]["mapping_level"] == "EXACT"
        connection.execute("UPDATE cb_daily SET trade_date='2026-09-01'")
        with pytest.raises(ValueError, match="unverified_parent_stock_mapping"):
            parent_stock_mappings_for_trade_date(
                connection, "2026-09-01", allow_monthly_verified=True
            )


def test_parser_rejects_non_mops_month_or_non_cb_document():
    with pytest.raises(MonthlyMappingError, match="not a CB issue detail"):
        parse_mops_monthly_mapping(
            "not official", MOPS_URL, cb_code="11111", stock_code="1101", stock_name="台泥"
        )
