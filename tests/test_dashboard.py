import json
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

import pytest

from scripts import build_dashboard


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "docs" / "index.html"


class DashboardHeaderParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headers = []
        self.current_header = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "th":
            self.current_header = {"aria-sort": attributes.get("aria-sort")}
        elif tag == "button" and self.current_header is not None:
            self.current_header["sort"] = attributes.get("data-sort")

    def handle_endtag(self, tag):
        if tag == "th" and self.current_header is not None:
            self.headers.append(self.current_header)
            self.current_header = None


def create_dashboard_database(path, *, include_master=True):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE cb_daily (
            trade_date TEXT NOT NULL,
            cb_code TEXT NOT NULL,
            cb_name TEXT NOT NULL,
            close_price REAL,
            reference_price REAL,
            volume_lots INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO cb_daily VALUES ('2026-08-29', '12345', '測試 CB', 101.5, 99.5, 12)"
    )
    connection.execute(
        "INSERT INTO cb_daily VALUES ('2026-08-29', '99999', '尚未同步', NULL, 100.0, 0)"
    )
    if include_master:
        connection.execute(
            """
            CREATE TABLE cb_master (
                cb_code TEXT PRIMARY KEY,
                stock_code TEXT,
                issue_date TEXT,
                maturity_date TEXT,
                put_date TEXT,
                issue_units INTEGER,
                issue_amount INTEGER,
                balance_amount INTEGER,
                balance_date TEXT,
                current_conversion_price REAL,
                current_conversion_price_effective_date TEXT,
                is_secured INTEGER,
                delisting_date TEXT,
                delisting_reason TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO cb_master VALUES (
                '12345', '1101', '2024-01-01', '2027-01-01', NULL, 2000, 200000000,
                198300000, '2026-08-29', 35.5, '2026-07-31', 1, NULL, NULL
            )
            """
        )
    connection.execute(
        """
        CREATE TABLE stock_daily_market (
            trade_date TEXT NOT NULL,
            p_stock_code TEXT NOT NULL,
            p_open_price REAL,
            p_high_price REAL,
            p_low_price REAL,
            p_close_price REAL,
            p_volume_shares INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE conversion_price_events (
            cb_code TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            conversion_price REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO conversion_price_events VALUES
            ('12345', '2026-01-01', 30.0),
            ('12345', '2026-08-01', 40.0),
            ('12345', '2026-09-01', 50.0)
        """
    )
    connection.execute(
        """
        INSERT INTO stock_daily_market VALUES
            ('2026-08-29', '1101', 24.1, 24.4, 24.0, 24.3, 16839498)
        """
    )
    connection.commit()
    connection.close()


def test_dashboard_data_joins_phase_two_fields_and_formats_display_values(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    records, _ = build_dashboard.build_dashboard_data()

    assert records == 2
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    row = payload["records"][0]
    assert row == {
        "trade_date": "2026-08-29",
        "cb_code": "12345",
        "cb_name": "測試 CB",
        "close_price": 101.5,
        "reference_price": 99.5,
        "volume_lots": 12,
        "p_close_price": 24.3,
        "p_volume_lots": 16839,
        "conversion_value": 60.75,
        "premium_rate": 67.0781893,
        "issue_date": "2024-01-01",
        "maturity_date": "2027-01-01",
        "put_date": None,
        "issue_units": 2000,
        "balance_date": "2026-08-29",
        "current_conversion_price": 35.5,
        "current_conversion_price_effective_date": "2026-07-31",
        "is_secured": "有",
        "delisting_date": None,
        "delisting_reason": None,
        "issue_amount_yi": 2.0,
        "balance_units": 1983,
    }
    missing_master = payload["records"][1]
    assert missing_master["issue_amount_yi"] is None
    assert missing_master["balance_units"] is None
    assert missing_master["is_secured"] == "未知"
    assert missing_master["p_close_price"] is None
    assert missing_master["p_volume_lots"] is None
    assert missing_master["conversion_value"] is None
    assert missing_master["premium_rate"] is None


def test_dashboard_uses_reference_price_for_zero_volume_premium(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cb_daily SET close_price = NULL, reference_price = 90.0, volume_lots = 0 "
            "WHERE cb_code = '12345'"
        )
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    row = json.loads(output_path.read_text(encoding="utf-8"))["records"][0]
    assert row["close_price"] is None
    assert row["reference_price"] == 90.0
    assert row["premium_rate"] == pytest.approx(48.14814815)


def test_dashboard_keeps_official_zero_parent_volume_and_blank_parent_close(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE stock_daily_market
            SET p_close_price = NULL, p_volume_shares = 0
            WHERE trade_date = '2026-08-29' AND p_stock_code = '1101'
            """
        )
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    row = json.loads(output_path.read_text(encoding="utf-8"))["records"][0]
    assert row["p_close_price"] is None
    assert row["p_volume_lots"] == 0
    assert row["conversion_value"] is None
    assert row["premium_rate"] is None


def test_dashboard_uses_historical_effective_conversion_price(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    row = json.loads(output_path.read_text(encoding="utf-8"))["records"][0]
    assert row["current_conversion_price"] == 35.5
    assert row["conversion_value"] == 60.75
    assert row["premium_rate"] == pytest.approx(67.0781893)


def test_dashboard_leaves_valuation_blank_without_effective_conversion_price(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM conversion_price_events")
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    row = json.loads(output_path.read_text(encoding="utf-8"))["records"][0]
    assert row["conversion_value"] is None
    assert row["premium_rate"] is None


def test_dashboard_data_requires_cb_master(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    create_dashboard_database(database_path, include_master=False)
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)

    with pytest.raises(RuntimeError, match="cb_master"):
        build_dashboard.load_rows()


def test_balance_units_requires_exact_official_par_value():
    with pytest.raises(RuntimeError, match="whole CB unit"):
        build_dashboard.balance_units_for_display(200_000_000, 2_000, 198_350_000)


def test_dashboard_every_column_has_type_aware_sorting_and_sticky_headers():
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    parser = DashboardHeaderParser()
    parser.feed(source)

    assert [header["sort"] for header in parser.headers] == [
        "cb_name",
        "cb_code",
        "issue_date",
        "maturity_date",
        "put_date",
        "issue_units",
        "issue_amount_yi",
        "balance_units",
        "balance_date",
        "current_conversion_price",
        "current_conversion_price_effective_date",
        "is_secured",
        "delisting_date",
        "delisting_reason",
        "close_price",
        "reference_price",
        "volume_lots",
        "p_close_price",
        "p_volume_lots",
        "conversion_value",
        "premium_rate",
    ]
    assert all(header["aria-sort"] == "none" for header in parser.headers)
    assert "thead th {\n      position: sticky;\n      top: 0;" in source
    assert ".sticky-name {\n      position: sticky;\n      left: 0;" in source
    assert 'issue_units: "number"' in source
    assert 'balance_date: "date"' in source
    assert 'p_close_price: "number"' in source
    assert 'reference_price: "number"' in source
    assert 'p_volume_lots: "number"' in source
    assert 'conversion_value: "number"' in source
    assert 'premium_rate: "number"' in source
    assert "if (!hasValue(aValue)) return 1;" in source
    assert 'sortType === "number"' in source
    assert 'sortType === "date"' in source
    assert 'state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";' in source
