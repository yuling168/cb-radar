import sqlite3
import json
from datetime import date

import pytest
import requests

from db import connect, conversion_price_on, upsert_master_data
from master_collector import (
    MasterFormatError,
    MOPS_ANNOUNCEMENT_SOURCE,
    MOPS_SOURCE,
    MOPS_RULES_SOURCE,
    TPEX_DELISTED_FIELDS,
    TPEX_ISSUE_SOURCE,
    TPEX_LIST_FIELDS,
    _history_for_cb,
    _merge_conversion_events,
    _validate_ambiguous_monthly_prices,
    balance_units_for_display,
    collect_master,
    latest_effective_event,
    issue_amount_yi_for_display,
    is_complete_reporting_month,
    is_active_on,
    month_end_date,
    parse_mops_conversion_announcements,
    parse_mops_rules_conversion_events,
    parse_mops_snapshot,
    parse_tdcc_book_entries,
    parse_tpex_issues,
    parse_tpex_delistings,
    parse_tpex_mops_links,
    secured_for_display,
    select_current_balance,
)


ISSUE_ROW = {
    "Date": "20260828",
    "IssuerCode": "3088",
    "IssuerName": "艾訊",
    "BondCode": "30882",
    "BondType": "5",
    "SeriesNumber": "2",
    "IssueDate": "20230828",
    "MaturityDate": "20260828",
    "IssueAmount": "800000000",
    "OutstandingAmount": "168300000",
    "ShortName": "艾訊二",
    "ListingStatus": "2",
    "PutOptionDate": "",
    "Currency": "1",
    "Guaranteed": "2",
    "GuaranteeDescription": "",
    "Conversion/ExchangePriceAtIssuance": "109.5000",
}
TDCC_CSV = """資料日期,證券代號,證券名稱,市場別,證券種類,登錄數額
20260827,30882,艾訊二,上櫃,可轉債(千股),1683
"""
TDCC_REGRESSION_CSV = """資料日期,證券代號,證券名稱,市場別,證券種類,登錄數額
20260827,31311,弘塑一,上櫃,可轉債(千股),1968
20260827,31312,弘塑二,上櫃,可轉債(千股),3013
20260827,37171,聯嘉投控一,上櫃,可轉債(千股),6340
20260827,0050,元大台灣50,上市,普通(千股),123
"""
MOPS_URL = (
    "https://mopsov.twse.com.tw/mops/web/t120sg01?bond_id=30882"
    "&issuer_stock_code=3088&monyr_reg=202607"
)
MOPS_HTML = """
<html><body>艾訊 之轉(交)換公司債發行資料
<div>債券中文名稱：艾訊股份有限公司國內第二次無擔保轉換公司債</div>
<div>發行人：國內</div>
<table><tr><td>發行日期：112/08/28</td><td>到期日期：115/08/28</td></tr>
<tr><td>申請發行總額：800,000,000元</td></tr>
<tr><td>實際發行總額：800,000,000元</td></tr>
<tr><td>發行面額：100,000元</td></tr>
<tr><td>發行張數：8,000張</td></tr>
<tr><td>本月底發行餘額：168,300,000元</td></tr>
<tr><td>最新轉(交)換價格：86.7000元</td>
<td>最近轉(交)換價格生效日期：115/07/31</td></tr></table>
</body></html>
"""
ANNOUNCEMENT_HTML = """
<html><body><center>轉換公司債轉換價格變更公告</center>
<div>3088 艾訊 115/08/01 1 公告艾訊股份有限公司國內第二次無擔保轉換公司債
(簡稱：艾訊二，代碼：30882)自115年08月15日起，
轉換價格自86.7元調整為85.2元。</div></body></html>
"""


def test_tpex_master_fields_are_normalized():
    row = parse_tpex_issues([ISSUE_ROW])["30882"]
    assert row["issue_date"] == "2023-08-28"
    assert row["maturity_date"] == "2026-08-28"
    assert row["put_date"] is None
    assert row["issue_amount"] == 800_000_000
    assert row["is_secured"] == 0
    assert row["issue_conversion_price"] == 109.5
    assert row["series_number"] == 2


def test_mops_latest_price_and_monthly_balance_are_parsed():
    row = parse_mops_snapshot(MOPS_HTML, MOPS_URL)
    assert row["year_month"] == "2026-07"
    assert row["balance_amount"] == 168_300_000
    assert row["issue_units"] == 8_000
    assert row["conversion_price"] == 86.7
    assert row["effective_date"] == "2026-07-31"
    assert row["instrument_kind"] == "convertible"


def test_tdcc_book_entry_balance_uses_csv_amount_and_date():
    tdcc_balance = parse_tdcc_book_entries(TDCC_CSV)["30882"]
    assert select_current_balance(tdcc_balance, date(2026, 8, 30)) == (
        168_300_000, "2026-08-27"
    )
    assert month_end_date("2024-02") == "2024-02-29"


def test_tdcc_book_entry_regression_balances_are_converted_to_dollars():
    balances = parse_tdcc_book_entries(TDCC_REGRESSION_CSV)
    assert {
        code: (row["balance_amount"], row["balance_date"])
        for code, row in balances.items()
    } == {
        "31311": (196_800_000, "2026-08-27"),
        "31312": (301_300_000, "2026-08-27"),
        "37171": (634_000_000, "2026-08-27"),
    }


def test_incomplete_mops_month_is_not_a_verified_month_end():
    august = parse_mops_snapshot(MOPS_HTML, MOPS_URL.replace("202607", "202608"))
    assert not is_complete_reporting_month("2026-08", date(2026, 8, 30))
    assert august["year_month"] == "2026-08"


def test_incomplete_mops_month_is_not_returned_as_monthly_history():
    master = {
        "cb_code": "30882", "issue_date": "2026-08-28",
        "maturity_date": "2029-08-28", "issue_amount": 800_000_000,
        "issue_conversion_price": 109.5,
    }
    august = {
        "year_month": "2026-08", "effective_date": "2026-08-28",
        "conversion_price": 109.5, "balance_amount": 800_000_000,
        "issue_units": 8_000, "source_url": "mops-current-month",
    }
    _events, balances, _ambiguities = _history_for_cb(
        object(), master, "mops-current-month?monyr_reg=202608", august,
        "2026-08-30T00:00:00+00:00", date(2026, 8, 30),
    )
    assert balances == []


def test_future_tdcc_balance_date_is_rejected():
    tdcc_balance = parse_tdcc_book_entries(TDCC_CSV.replace("20260827", "20260831"))["30882"]
    with pytest.raises(MasterFormatError, match="after run date"):
        select_current_balance(tdcc_balance, date(2026, 8, 30))


def test_issue_units_uses_official_mops_count():
    row = parse_mops_snapshot(MOPS_HTML, MOPS_URL)
    assert row["issue_units"] == 8_000
    assert row["issue_amount"] == 800_000_000


def test_issue_units_uses_official_par_when_mops_count_reflects_balance_units():
    html = MOPS_HTML.replace("發行張數：8,000張", "發行張數：1,683張")
    row = parse_mops_snapshot(html, MOPS_URL)
    assert row["par_value"] == 100_000
    assert row["issue_units"] == 8_000
    assert row["balance_amount"] == 168_300_000


def test_issue_amount_uses_face_principal_not_premium_proceeds():
    html = MOPS_HTML.replace(
        "實際發行總額：800,000,000元",
        "實際發行總額：887,978,900元",
    )
    row = parse_mops_snapshot(html, MOPS_URL)
    assert row["issue_amount"] == 800_000_000
    assert row["actual_issue_amount"] == 887_978_900
    assert row["issue_units"] == 8_000


def test_partial_issue_uses_verified_actual_face_principal():
    html = (
        MOPS_HTML
        .replace("申請發行總額：800,000,000元", "申請發行總額：1,000,000,000元")
        .replace("實際發行總額：800,000,000元", "實際發行總額：600,000,000元")
        .replace("發行張數：8,000張", "發行張數：6,000張")
        .replace("本月底發行餘額：168,300,000元", "本月底發行餘額：502,400,000元")
    )
    row = parse_mops_snapshot(html, MOPS_URL)
    assert row["application_issue_amount"] == 1_000_000_000
    assert row["actual_issue_amount"] == 600_000_000
    assert row["issue_amount"] == 600_000_000
    assert row["issue_units"] == 6_000


def test_balance_units_display_uses_official_per_bond_par_value():
    assert balance_units_for_display(200_000_000, 2_000, 198_300_000) == 1_983


def test_balance_units_display_rejects_non_integral_official_amounts():
    with pytest.raises(MasterFormatError, match="whole bond unit"):
        balance_units_for_display(200_000_000, 2_000, 198_350_000)


def test_issue_amount_yi_display_preserves_fraction():
    assert issue_amount_yi_for_display(200_000_000) == "2"
    assert issue_amount_yi_for_display(250_000_000) == "2.5"


def test_secured_display_labels():
    assert secured_for_display(1) == "有"
    assert secured_for_display(0) == "無"
    assert secured_for_display(None) == "未知"


def test_exchangeable_bond_is_identified_from_official_name():
    html = MOPS_HTML.replace("無擔保轉換公司債", "無擔保交換公司債")
    assert parse_mops_snapshot(html, MOPS_URL)["instrument_kind"] == "exchangeable"


def test_tpex_secured_and_unsecured_are_parsed():
    secured = dict(ISSUE_ROW, BondCode="30881", Guaranteed="1",
                   GuaranteeDescription="第一銀行")
    unsecured = dict(ISSUE_ROW, BondCode="30882", Guaranteed="2",
                     GuaranteeDescription="")
    rows = parse_tpex_issues([secured, unsecured])
    assert rows["30881"]["is_secured"] == 1
    assert rows["30882"]["is_secured"] == 0


def test_latest_effective_announcement_replaces_stale_monthly_price():
    events = parse_mops_conversion_announcements(
        ANNOUNCEMENT_HTML, "30882", "2026-08-29T00:00:00+00:00", "official-url"
    )
    assert events == [{
        "cb_code": "30882",
        "effective_date": "2026-08-15",
        "conversion_price": 85.2,
        "source": "MOPS:t108sb08_1",
        "source_url": "official-url",
        "collected_at": "2026-08-29T00:00:00+00:00",
    }]


def test_announcement_parser_does_not_cross_between_bond_rows():
    content = """
    <div>1436 華友聯 115/06/25 1 公告華友聯四(簡稱：華友聯四，代碼：14364)停止受理轉換等事項。</div>
    <div>1436 華友聯 115/07/08 1 公告華友聯三(簡稱：華友聯三，代碼：14363)自115年07月31日起，
    轉換價格自128.7元調整為114.3元。</div>
    <div>1436 華友聯 115/07/08 3 公告華友聯四(簡稱：華友聯四，代碼：14364)自115年07月31日起，
    轉換價格自99.0元調整為87.9元。</div>
    """
    events = parse_mops_conversion_announcements(
        "<h1>轉換公司債轉換價格變更公告</h1>" + content,
        "14364",
        "2026-08-29T00:00:00+00:00",
        "official-url",
    )
    assert [(row["effective_date"], row["conversion_price"]) for row in events] == [
        ("2026-07-31", 87.9)
    ]


def test_later_official_filing_resolves_same_effective_date_announcement():
    content = """
    <h1>轉換公司債轉換價格變更公告</h1>
    <div>3717 聯嘉投控 115/06/03 4 公告聯嘉投控一
    (簡稱：聯嘉投控一，代碼：37171)自115年06月15日起，
    轉換價格自17.5元調整為17.2元。</div>
    <div>3717 聯嘉投控 115/06/12 1 公告聯嘉投控一
    (簡稱：聯嘉投控一，代碼：37171)自115年06月15日起，
    轉換價格自17.2元調整為17.5元。</div>
    """
    events = parse_mops_conversion_announcements(
        content, "37171", "2026-08-29T00:00:00+00:00", "official-url"
    )
    assert [(row["effective_date"], row["conversion_price"]) for row in events] == [
        ("2026-06-15", 17.5)
    ]


def test_current_price_and_effective_date_ignore_future_event():
    events = [
        {"effective_date": "2026-07-15", "conversion_price": 17.3},
        {"effective_date": "2026-09-01", "conversion_price": 16.8},
    ]
    latest = latest_effective_event(events, date(2026, 8, 29))
    assert latest["effective_date"] == "2026-07-15"
    assert latest["conversion_price"] == 17.3


def test_33621_ambiguous_monthly_price_is_resolved_only_by_official_announcement():
    master = {
        "cb_code": "33621",
        "issue_date": "2024-07-16",
        "maturity_date": "2029-07-16",
        "issue_amount": 1_200_000_000,
        "issue_conversion_price": 254.0,
    }
    ambiguous_monthly_snapshot = {
        "year_month": "2024-10",
        "effective_date": "2024-07-16",
        "conversion_price": 253.0,
        "balance_amount": 1_200_000_000,
        "issue_units": 12_000,
        "source_url": "mops-monthly-url",
    }
    events, _balances, ambiguities = _history_for_cb(
        object(),
        master,
        "mops-monthly-url?monyr_reg=202410",
        ambiguous_monthly_snapshot,
        "2026-08-29T00:00:00+00:00",
        date(2026, 8, 29),
    )

    assert [(row["effective_date"], row["conversion_price"], row["source"])
            for row in events] == [("2024-07-16", 254.0, TPEX_ISSUE_SOURCE)]
    assert ambiguities == [{
        "reported_effective_date": "2024-07-16",
        "prices": {254.0, 253.0},
    }]

    official_adjustment = {
        "cb_code": "33621",
        "effective_date": "2024-07-26",
        "conversion_price": 253.0,
        "source": MOPS_ANNOUNCEMENT_SOURCE,
        "source_url": "mops-announcement-url",
        "collected_at": "2026-08-29T00:00:00+00:00",
    }
    _validate_ambiguous_monthly_prices(
        "33621", ambiguities, [*events, official_adjustment]
    )
    final_events = [*events, official_adjustment]
    assert {(row["effective_date"], row["conversion_price"])
            for row in final_events} == {
        ("2024-07-16", 254.0),
        ("2024-07-26", 253.0),
    }
    assert len({row["effective_date"] for row in final_events}) == len(final_events)


def test_ambiguous_monthly_price_without_official_resolution_fails():
    ambiguities = [{
        "reported_effective_date": "2024-07-16",
        "prices": {254.0, 253.0},
    }]
    initial_event = {
        "effective_date": "2024-07-16",
        "conversion_price": 254.0,
        "source": TPEX_ISSUE_SOURCE,
    }
    with pytest.raises(MasterFormatError, match="not resolved"):
        _validate_ambiguous_monthly_prices(
            "33621", ambiguities, [initial_event]
        )


def test_official_announcement_replaces_monthly_event_with_wrong_same_date_price():
    monthly = [{
        "effective_date": "2024-08-30",
        "conversion_price": 75.0,
        "source": "MOPS:t120sg01",
    }]
    announcements = [
        {
            "effective_date": "2024-08-30",
            "conversion_price": 78.1,
            "source": MOPS_ANNOUNCEMENT_SOURCE,
        },
        {
            "effective_date": "2025-08-29",
            "conversion_price": 75.0,
            "source": MOPS_ANNOUNCEMENT_SOURCE,
        },
    ]
    merged = _merge_conversion_events("22013", monthly, announcements)
    assert [(day, row["conversion_price"]) for day, row in sorted(merged.items())] == [
        ("2024-08-30", 78.1),
        ("2025-08-29", 75.0),
    ]


def test_official_terms_resolve_pre_issue_price_adjustment(monkeypatch):
    terms_text = """
    113 年 10 月 17 日為轉換價格訂定基準日，依上述方式，
    轉換價格為每股新台幣 166 元。
    轉換價格由每股新台幣 166 元調整為每股新台幣 162.7 元，
    並於除權基準日 113 年 11 月 1 日進行轉換價格調整。
    """

    class FakePage:
        def extract_text(self):
            return terms_text

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage()]

    monkeypatch.setattr("master_collector.PdfReader", FakeReader)
    events = parse_mops_rules_conversion_events(
        b"%PDF-test", "45491", "2026-08-29T00:00:00+00:00", "official-pdf"
    )
    assert [(row["effective_date"], row["conversion_price"], row["source"])
            for row in events] == [
        ("2024-10-17", 166.0, MOPS_RULES_SOURCE),
        ("2024-11-01", 162.7, MOPS_RULES_SOURCE),
    ]


def test_official_terms_support_gregorian_initial_price_without_adjustment(monkeypatch):
    class FakePage:
        def extract_text(self):
            return (
                "以西元2025年08月19日為轉換價格訂定基準日，"
                "依上述方式轉換價格為每股新臺幣79.79元。"
            )

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage()]

    monkeypatch.setattr("master_collector.PdfReader", FakeReader)
    events = parse_mops_rules_conversion_events(
        b"%PDF-test", "65914", "2026-08-29T00:00:00+00:00", "official-pdf"
    )
    assert [(row["effective_date"], row["conversion_price"])
            for row in events] == [("2025-08-19", 79.79)]


def test_required_official_field_missing_fails_loudly():
    malformed = dict(ISSUE_ROW)
    malformed.pop("IssueAmount")
    with pytest.raises(MasterFormatError, match="required fields changed"):
        parse_tpex_issues([malformed])
    with pytest.raises(MasterFormatError, match="MOPS required field missing"):
        parse_mops_snapshot(MOPS_HTML.replace("本月底發行餘額", "餘額"), MOPS_URL)


def test_tpex_list_link_validation():
    payload = {
        "stat": "ok",
        "tables": [{
            "fields": ["發行機構代碼", "發行機構名稱", "債券名稱", "掛牌日期", "發行資料"],
            "data": [["3088", "艾訊", "艾訊二", "112/08/28", MOPS_URL]],
        }],
    }
    assert parse_tpex_mops_links(payload) == {"30882": MOPS_URL}


def test_master_event_and_month_upserts_are_idempotent(tmp_path):
    collected = "2026-08-29T00:00:00+00:00"
    master = {
        "cb_code": "30882", "cb_name": "艾訊二", "stock_code": "3088",
        "stock_name": "艾訊", "issue_date": "2023-08-28",
        "maturity_date": "2026-08-28", "put_date": None,
        "issue_units": 8_000, "issue_amount": 800_000_000,
        "balance_amount": 168_300_000, "balance_date": "2026-08-29",
        "current_conversion_price": 86.7,
        "current_conversion_price_effective_date": "2026-07-31",
        "is_secured": 0, "source": "official",
        "source_url": MOPS_URL, "collected_at": collected,
    }
    events = [
        {"cb_code": "30882", "effective_date": "2023-08-28",
         "conversion_price": 109.5, "source": "official", "source_url": MOPS_URL,
         "collected_at": collected},
        {"cb_code": "30882", "effective_date": "2026-07-31",
         "conversion_price": 86.7, "source": "official", "source_url": MOPS_URL,
         "collected_at": collected},
    ]
    balances = [{"cb_code": "30882", "year_month": "2026-07",
                 "balance_amount": 168_300_000, "source": "official",
                 "source_url": MOPS_URL, "collected_at": collected}]
    with connect(tmp_path / "test.db") as connection:
        upsert_master_data(connection, [master], events, balances)
        upsert_master_data(connection, [master], events, balances)
        assert connection.execute("SELECT COUNT(*) FROM cb_master").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM conversion_price_events").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM cb_monthly_balance").fetchone()[0] == 1
        assert conversion_price_on(connection, "30882", "2024-01-01") == 109.5
        assert conversion_price_on(connection, "30882", "2026-08-01") == 86.7
        current = connection.execute(
            "SELECT balance_amount, balance_date, current_conversion_price, "
            "current_conversion_price_effective_date FROM cb_master WHERE cb_code = ?",
            ("30882",),
        ).fetchone()
        assert tuple(current) == (
            168_300_000, "2026-08-29", 86.7, "2026-07-31"
        )


def test_schema_preserves_existing_daily_table(tmp_path):
    with connect(tmp_path / "test.db") as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )}
        master_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(cb_master)")
        }
    assert {"cb_daily", "cb_master", "conversion_price_events", "cb_monthly_balance"} <= tables
    assert "current_conversion_price_effective_date" in master_columns
    assert {"balance_date", "delisting_date", "delisting_reason"} <= master_columns


def test_balance_date_migration_does_not_backfill_from_completed_mops_month(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE cb_master (
            cb_code TEXT PRIMARY KEY,
            cb_name TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            issue_date TEXT NOT NULL,
            maturity_date TEXT NOT NULL,
            issue_amount INTEGER NOT NULL,
            balance_amount INTEGER,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL
        );
        CREATE TABLE cb_monthly_balance (
            cb_code TEXT NOT NULL,
            year_month TEXT NOT NULL,
            balance_amount INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (cb_code, year_month)
        );
        INSERT INTO cb_master VALUES (
            '27551', '揚秦一', '2755', '揚秦', '2023-01-01', '2028-01-01',
            200000000, 73000000, 'official', 'official-url', '2026-08-30T00:00:00+00:00'
        );
        INSERT INTO cb_monthly_balance VALUES (
            '27551', '2026-07', 73000000, 'official', 'official-url', '2026-08-01T00:00:00+00:00'
        );
        """
    )
    legacy.commit()
    legacy.close()
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT balance_amount, balance_date FROM cb_master WHERE cb_code = '27551'"
        ).fetchone()
    assert tuple(row) == (73_000_000, None)


def test_balance_date_migration_does_not_use_unfinished_month(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE cb_master (
            cb_code TEXT PRIMARY KEY, cb_name TEXT NOT NULL,
            stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
            issue_date TEXT NOT NULL, maturity_date TEXT NOT NULL,
            issue_amount INTEGER NOT NULL, balance_amount INTEGER,
            source TEXT NOT NULL, source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL
        );
        CREATE TABLE cb_monthly_balance (
            cb_code TEXT NOT NULL, year_month TEXT NOT NULL,
            balance_amount INTEGER NOT NULL, source TEXT NOT NULL,
            source_url TEXT NOT NULL, collected_at TEXT NOT NULL,
            PRIMARY KEY (cb_code, year_month)
        );
        INSERT INTO cb_master VALUES (
            '33571', '臺慶科一', '3357', '臺慶科', '2026-08-01', '2031-08-01',
            200000000, 145300000, 'official', 'official-url', '2026-08-30T00:00:00+00:00'
        );
        INSERT INTO cb_monthly_balance VALUES (
            '33571', strftime('%Y-%m', 'now', 'localtime'), 145300000,
            'official', 'official-url', '2026-08-30T00:00:00+00:00'
        );
        """
    )
    legacy.commit()
    legacy.close()
    with connect(db_path) as connection:
        balance_date = connection.execute(
            "SELECT balance_date FROM cb_master WHERE cb_code = '33571'"
        ).fetchone()[0]
    assert balance_date is None


def test_upsert_rejects_balance_date_after_run_date(tmp_path):
    master = {
        "cb_code": "30882", "cb_name": "艾訊二", "stock_code": "3088",
        "stock_name": "艾訊", "issue_date": "2023-08-28",
        "maturity_date": "2026-08-28", "put_date": None,
        "issue_units": 8_000, "issue_amount": 800_000_000,
        "balance_amount": 168_300_000, "balance_date": "2026-08-31",
        "current_conversion_price": 86.7,
        "current_conversion_price_effective_date": "2026-07-31",
        "is_secured": 0, "source": "official", "source_url": MOPS_URL,
        "collected_at": "2026-08-30T00:00:00+00:00",
    }
    with connect(tmp_path / "test.db") as connection:
        with pytest.raises(ValueError, match="after run date"):
            upsert_master_data(
                connection, [master], [], [], as_of_date=date(2026, 8, 30)
            )


def test_upsert_rejects_unfinished_monthly_balance(tmp_path):
    balance = {
        "cb_code": "30882", "year_month": "2026-08",
        "balance_amount": 168_300_000, "source": "official",
        "source_url": MOPS_URL, "collected_at": "2026-08-30T00:00:00+00:00",
    }
    with connect(tmp_path / "test.db") as connection:
        with pytest.raises(ValueError, match="not from a completed month"):
            upsert_master_data(
                connection, [], [], [balance], as_of_date=date(2026, 8, 30)
            )


def test_68061_is_inactive_after_official_delisting_date():
    payload = {
        "stat": "ok",
        "tables": [{
            "fields": ["代碼", "簡稱", "下櫃日期"],
            "data": [["68061", "森崴能源一", "115/06/23"]],
        }],
    }
    delistings = parse_tpex_delistings(payload)
    assert delistings["68061"]["delisting_date"] == "2026-06-23"
    assert not is_active_on("2023-11-22", "2026-06-23", date(2026, 8, 30))


def test_15864_remains_active_until_future_official_delisting_date():
    assert is_active_on("2023-08-30", "2026-08-31", date(2026, 8, 30))
    assert not is_active_on("2023-08-30", "2026-08-31", date(2026, 8, 31))


def test_lifecycle_sync_is_append_only(tmp_path):
    collected = "2026-08-30T00:00:00+00:00"
    master = {
        "cb_code": "68061", "cb_name": "森崴能源一", "stock_code": "6806",
        "stock_name": "森崴能源", "issue_date": "2023-11-22",
        "maturity_date": "2026-11-22", "put_date": None,
        "issue_units": 30_000, "issue_amount": 3_000_000_000,
        "balance_amount": 2_031_800_000, "current_conversion_price": 105.0,
        "current_conversion_price_effective_date": "2025-08-29",
        "is_secured": 1, "source": "official", "source_url": MOPS_URL,
        "collected_at": collected,
    }
    lifecycle = {
        "cb_code": "68061", "delisting_date": "2026-06-23",
        "delisting_reason": "已下市",
    }
    with connect(tmp_path / "test.db") as connection:
        upsert_master_data(connection, [master], [], [], lifecycle_updates=[lifecycle])
        upsert_master_data(connection, [], [], [])
        row = connection.execute(
            "SELECT delisting_date, delisting_reason FROM cb_master WHERE cb_code = ?",
            ("68061",),
        ).fetchone()
    assert tuple(row) == ("2026-06-23", "已下市")


def test_exchangeable_cleanup_removes_master_and_children(tmp_path):
    collected = "2026-08-29T00:00:00+00:00"
    master = {
        "cb_code": "140201", "cb_name": "遠東新E1永", "stock_code": "1402",
        "stock_name": "遠東新", "issue_date": "2024-08-08",
        "maturity_date": "2029-08-08", "put_date": "2027-08-08",
        "issue_units": 10_000, "issue_amount": 1_000_000_000,
        "balance_amount": 999_900_000, "current_conversion_price": 45.4,
        "current_conversion_price_effective_date": "2024-08-08",
        "is_secured": 0, "source": "official", "source_url": MOPS_URL,
        "collected_at": collected,
    }
    with connect(tmp_path / "test.db") as connection:
        upsert_master_data(connection, [master], [], [])
        upsert_master_data(connection, [], [], [], ["140201"])
        assert connection.execute(
            "SELECT COUNT(*) FROM cb_master WHERE cb_code='140201'"
        ).fetchone()[0] == 0


class FailedSession:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        raise requests.ConnectionError("official sources unavailable")


def test_master_source_failure_does_not_create_database(tmp_path):
    db_path = tmp_path / "master.db"
    with pytest.raises(requests.ConnectionError, match="official sources unavailable"):
        collect_master(db_path=db_path, session=FailedSession())
    assert not db_path.exists()


class FakeResponse:
    def __init__(self, *, json_payload=None, text=""):
        self.content = (
            json.dumps(json_payload).encode("utf-8")
            if json_payload is not None
            else text.encode("utf-8")
        )
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        return None


class IncrementalSession:
    def __init__(self):
        self.headers = {}
        self.get_urls = []
        self.post_urls = []

    def get(self, url, **_kwargs):
        self.get_urls.append(url)
        if url.endswith("bond_ISSBD5_data"):
            return FakeResponse(json_payload=[ISSUE_ROW])
        if url.endswith("bond/convSearch"):
            return FakeResponse(json_payload={
                "stat": "ok",
                "tables": [{
                    "fields": TPEX_LIST_FIELDS,
                    "data": [["3088", "艾訊", "艾訊二", "112/08/28", MOPS_URL]],
                }],
            })
        if url.endswith("bond/convDelist"):
            return FakeResponse(json_payload={
                "stat": "ok",
                "tables": [{"fields": TPEX_DELISTED_FIELDS, "data": []}],
            })
        if "opendata.tdcc.com.tw/getOD.ashx?id=1-16" in url:
            return FakeResponse(text=TDCC_CSV)
        return FakeResponse(text=MOPS_HTML)

    def post(self, url, **_kwargs):
        self.post_urls.append(url)
        return FakeResponse(text=ANNOUNCEMENT_HTML)


def _seed_incremental_master(db_path, *, monthly=True):
    collected = "2026-08-29T00:00:00+00:00"
    master = {
        "cb_code": "30882", "cb_name": "艾訊二", "stock_code": "3088",
        "stock_name": "艾訊", "issue_date": "2023-08-28",
        "maturity_date": "2026-08-28", "put_date": None,
        "issue_units": 8_000, "issue_amount": 800_000_000,
        "balance_amount": 168_300_000, "balance_date": "2026-07-31",
        "current_conversion_price": 86.7,
        "current_conversion_price_effective_date": "2026-07-31",
        "is_secured": 0, "source": "official", "source_url": MOPS_URL,
        "collected_at": collected,
    }
    events = [
        {"cb_code": "30882", "effective_date": "2023-08-28",
         "conversion_price": 109.5, "source": TPEX_ISSUE_SOURCE,
         "source_url": "official-url", "collected_at": collected},
        {"cb_code": "30882", "effective_date": "2026-07-31",
         "conversion_price": 86.7, "source": MOPS_SOURCE,
         "source_url": MOPS_URL, "collected_at": collected},
    ]
    balances = []
    if monthly:
        balances.append({
            "cb_code": "30882", "year_month": "2026-07",
            "balance_amount": 168_300_000, "source": MOPS_SOURCE,
            "source_url": MOPS_URL, "collected_at": collected,
        })
    with connect(db_path) as connection:
        upsert_master_data(connection, [master], events, balances)


def test_incremental_run_skips_existing_mops_history_and_reports_request_counts(tmp_path):
    db_path = tmp_path / "master.db"
    _seed_incremental_master(db_path)
    session = IncrementalSession()

    result = collect_master(
        db_path=db_path, session=session, as_of_date=date(2026, 8, 30)
    )

    assert result["tpex_requests"] == 3
    assert result["tdcc_requests"] == 1
    assert result["mops_detail_requests"] == 0
    assert result["mops_announcement_requests"] == 1
    assert result["bootstrap_cbs"] == 0
    assert result["monthly_incremental_cbs"] == 0
    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conversion_price_events WHERE cb_code = '30882'"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT balance_date FROM cb_master WHERE cb_code = '30882'"
        ).fetchone()[0] == "2026-08-27"


def test_incremental_run_fetches_only_missing_latest_completed_month(tmp_path):
    db_path = tmp_path / "master.db"
    _seed_incremental_master(db_path, monthly=False)
    session = IncrementalSession()

    result = collect_master(
        db_path=db_path, session=session, as_of_date=date(2026, 8, 30)
    )

    assert result["mops_detail_requests"] == 1
    assert result["monthly_incremental_cbs"] == 1
    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT balance_amount FROM cb_monthly_balance "
            "WHERE cb_code = '30882' AND year_month = '2026-07'"
        ).fetchone()[0] == 168_300_000


def test_new_cb_uses_mops_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr("master_collector.time.sleep", lambda _seconds: None)
    session = IncrementalSession()

    result = collect_master(
        db_path=tmp_path / "master.db",
        session=session,
        as_of_date=date(2026, 8, 30),
    )

    assert result["bootstrap_cbs"] == 1
    assert result["mops_detail_requests"] > 1
    assert result["mops_announcement_requests"] == 4


def test_incremental_mops_announcement_failure_aborts_without_database_update(tmp_path):
    class FailingAnnouncementSession(IncrementalSession):
        def post(self, url, **_kwargs):
            self.post_urls.append(url)
            raise requests.ConnectionError("MOPS announcement connection refused")

    db_path = tmp_path / "master.db"
    _seed_incremental_master(db_path)
    with pytest.raises(requests.ConnectionError, match="connection refused"):
        collect_master(
            db_path=db_path,
            session=FailingAnnouncementSession(),
            as_of_date=date(2026, 8, 30),
        )
    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT balance_date FROM cb_master WHERE cb_code = '30882'"
        ).fetchone()[0] == "2026-07-31"
        assert connection.execute(
            "SELECT COUNT(*) FROM conversion_price_events WHERE cb_code = '30882'"
        ).fetchone()[0] == 2
