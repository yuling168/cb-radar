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
                stock_name TEXT,
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
                '12345', '1101', '測試母股', '2024-01-01', '2027-01-01', NULL, 2000, 200000000,
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
    connection.execute("""CREATE TABLE parent_flow_metrics (
        trade_date TEXT, stock_code TEXT, foreign_status TEXT, foreign_net_lots REAL,
        foreign_volume_pct REAL, foreign_streak_days INTEGER, foreign_streak_lots REAL,
        trust_status TEXT, trust_net_lots REAL, trust_volume_pct REAL, trust_streak_days INTEGER,
        trust_streak_lots REAL, active_etf_status TEXT, active_etf_change_lots REAL,
        active_etf_change_value_twd REAL, active_etf_streak_days INTEGER, active_etf_streak_lots REAL)""")
    connection.execute("""INSERT INTO parent_flow_metrics VALUES
        ('2026-08-29','1101','AVAILABLE',1.25,2.5,3,4.5,'AVAILABLE',-2,-3.5,2,-5,
         'AVAILABLE',0.75,15000,1,0.75)""")
    connection.execute("""CREATE TABLE institutional_coverage (
        trade_date TEXT, stock_code TEXT, status TEXT, reason TEXT)""")
    connection.execute("INSERT INTO institutional_coverage VALUES ('2026-08-29','1101','COMPLETE',NULL)")
    connection.execute("""CREATE TABLE active_etf_collection_status (
        trade_date TEXT, etf_code TEXT, status TEXT)""")
    connection.executemany("INSERT INTO active_etf_collection_status VALUES ('2026-08-29',?, 'succeeded')",
                           [(code,) for code in ('00980A','00985A','00999A','00982A','00992A')])
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
        "remaining_days": 125,
        "p_close_price": 24.3,
        "p_volume_lots": 16839,
        "conversion_value": 60.75,
        "premium_rate": 67.0781893,
        "issue_date": "2024-01-01",
        "maturity_date": "2027-01-01",
        "put_date": None,
        "issue_units": 2000,
        "balance_date": "2026-08-29",
        "balance_ratio": 99.15,
        "current_conversion_price": 35.5,
        "current_conversion_price_effective_date": "2026-07-31",
        "is_secured": "有",
        "delisting_date": None,
        "delisting_reason": None,
        "issue_amount_yi": 2.0,
        "balance_units": 1983,
    }
    institutional = payload["institutional_records"]
    assert institutional == [{
        "trade_date": "2026-08-29", "cb_code": "12345", "cb_name": "測試 CB",
        "parent_stock_code": "1101", "parent_stock_name": "測試母股",
        "foreign_status": "AVAILABLE", "foreign_net_lots": 1.25, "foreign_volume_pct": 2.5,
        "foreign_streak_days": 3, "foreign_streak_lots": 4.5,
        "trust_status": "AVAILABLE", "trust_net_lots": -2.0, "trust_volume_pct": -3.5,
        "trust_streak_days": 2, "trust_streak_lots": -5.0,
        "active_etf_status": "AVAILABLE", "active_etf_change_lots": 0.75,
        "active_etf_change_value_twd": 15000.0, "active_etf_streak_days": 1,
        "active_etf_streak_lots": 0.75, "institutional_reason": None,
        "active_etf_coverage": "complete",
    }]
    missing_master = payload["records"][1]
    assert missing_master["issue_amount_yi"] is None
    assert missing_master["balance_units"] is None
    assert missing_master["remaining_days"] is None
    assert missing_master["balance_ratio"] is None
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


def test_dashboard_exports_saved_strategy_a_signal_and_latest_unavailable_diagnostic(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript("""
            CREATE TABLE strategy_signals (cb_code TEXT, trade_date TEXT, strategy_code TEXT, strategy_version TEXT, strategy_name TEXT, condition_results_json TEXT, condition_values_json TEXT, data_status TEXT, created_at TEXT);
            CREATE TABLE strategy_evaluations (evaluation_id INTEGER PRIMARY KEY, cb_code TEXT, trade_date TEXT, strategy_code TEXT, strategy_version TEXT, strategy_name TEXT, condition_results_json TEXT, condition_values_json TEXT, data_status TEXT, unavailable_reasons_json TEXT, evaluated_at TEXT);
        """)
        values = json.dumps({"close_price": 101.5, "conversion_value": 60.75, "premium_rate_pct": 67.08, "today_volume_lots": 12})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "A", "v1", "CB 成交量創 10 日新高", '{"premium_rate_above_1_pct":true}', values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (1, "99999", "2026-08-29", "A", "v1", "CB 成交量創 10 日新高", "{}", "{}", "UNAVAILABLE", '["old"]', "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (2, "99999", "2026-08-29", "A", "v1", "CB 成交量創 10 日新高", "{}", "{}", "UNAVAILABLE", '["missing_cb_close_price"]', "y"))
        b_values = json.dumps({"close_price": 101.5, "average_43_close_price": 98.5, "today_volume_lots": 120, "average_10_volume_lots": 80, "average_5_volume_lots": 70, "prior_19_high_close_price": 100, "conversion_value": 96, "premium_rate_pct": 5.73, "converted_ratio_pct": 10, "balance_date": "2026-07-31", "window_43_trade_dates": ["2026-07-01"]})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "B", "v1", "CB 突破轉換價", '{"close_price_above_43_day_average":true}', b_values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (4, "77777", "2026-08-29", "B", "v1", "CB 突破轉換價", "{}", "{}", "UNAVAILABLE", '["missing_cb_daily_rows"]', "z"))
        c_values = json.dumps({"conversion_value": 108.5, "premium_rate_pct": 12.5, "converted_ratio_pct": 10.0, "conversion_value_bucket": "105-110", "bucket_rank": 1, "bucket_candidate_count": 3, "balance_date": "2026-07-31"})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "C", "v1", "CB 資優生", '{"within_bucket_top_two":true}', c_values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (3, "88888", "2026-08-29", "C", "v1", "CB 資優生", "{}", "{}", "UNAVAILABLE", '["missing_historical_balance"]', "z"))
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)
    build_dashboard.build_dashboard_data()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["strategy_a_signals"][0]["cb_name"] == "測試 CB"
    assert payload["strategy_a_signals"][0]["condition_values"]["conversion_value"] == 60.75
    assert payload["strategy_a_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "A", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_cb_close_price", "evaluation_count": 1,
    }]
    assert {row["strategy_code"] for row in payload["strategy_signals"]} == {"A", "B", "C"}
    assert payload["strategy_b_signals"][0]["condition_values"]["average_43_close_price"] == 98.5
    assert payload["strategy_b_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "B", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_cb_daily_rows", "evaluation_count": 1,
    }]
    assert payload["strategy_c_signals"][0]["condition_values"]["bucket_rank"] == 1
    assert payload["strategy_c_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "C", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_historical_balance", "evaluation_count": 1,
    }]
    assert "condition_values" not in payload["strategy_evaluations"][0]


def test_strategy_pages_show_signals_separately_from_unavailable_data():
    index = DASHBOARD_PATH.read_text(encoding="utf-8")
    strategy = (DASHBOARD_PATH.parent / "strategy-a.html").read_text(encoding="utf-8")
    strategy_b = (DASHBOARD_PATH.parent / "strategy-b.html").read_text(encoding="utf-8")
    strategy_c = (DASHBOARD_PATH.parent / "strategy-c.html").read_text(encoding="utf-8")
    assert 'id="strategySignals"' in index
    assert "非不符合策略" in index
    assert "unavailableByStrategy" in index
    assert "策略 ${strategyCode}：資料不足 ${unavailableCount} 檔" in index
    assert "reasonCounts" in index
    assert 'href="strategy-a.html"' in index
    assert 'href="strategy-b.html"' in index
    assert "策略 ${row.strategy_code || \"A\"}-v1" in index
    assert "average_43_close_price" in index
    assert 'id="dateSelect"' in strategy
    assert "資料不足、無法評估" in strategy
    assert "condition_results" in strategy
    assert 'id="dateSelect"' in strategy_b
    assert "strategy_b_signals" in strategy_b
    assert "window_43_trade_dates" in strategy_b
    assert "prior_19_high_close_price" in strategy_b
    assert "資料不足、無法評估" in strategy_b
    assert 'href="strategy-c.html"' in index
    assert "策略 C-v1" in index
    assert 'id="dateSelect"' in strategy_c
    assert "conversion_value_bucket" in strategy_c
    assert "資料不足、無法評估" in strategy_c
    assert "evaluation_count" in strategy
    assert "evaluation_count" in strategy_c


def test_dashboard_exports_parent_flow_for_each_current_cb_and_unavailable_reason(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO cb_daily VALUES ('2026-08-29','12346','測試 CB 二',101,99,1)")
        connection.execute("""INSERT INTO cb_master VALUES
            ('12346','1101','測試母股','2024-01-01','2027-01-01',NULL,1,100000,
             100000,'2026-08-29',40,'2026-08-01',0,NULL,NULL)""")
        connection.execute("INSERT INTO cb_daily VALUES ('2026-08-29','66451','創新 CB',101,99,1)")
        connection.execute("""INSERT INTO cb_master VALUES
            ('66451','6645','創新板','2024-01-01','2027-01-01',NULL,1,100000,
             100000,'2026-08-29',40,'2026-08-01',0,NULL,NULL)""")
        connection.execute("""INSERT INTO parent_flow_metrics VALUES
            ('2026-08-29','6645','UNAVAILABLE',NULL,NULL,NULL,NULL,'UNAVAILABLE',NULL,NULL,NULL,NULL,
             'UNAVAILABLE',NULL,NULL,NULL,NULL)""")
        connection.execute("INSERT INTO institutional_coverage VALUES ('2026-08-29','6645','UNAVAILABLE_MARKET','資料未提供（創新板）')")
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)
    build_dashboard.build_dashboard_data()
    rows = json.loads(output_path.read_text(encoding="utf-8"))["institutional_records"]
    assert [row["cb_code"] for row in rows] == ["12345", "12346", "66451"]
    assert rows[0]["parent_stock_code"] == rows[1]["parent_stock_code"] == "1101"
    assert rows[2]["institutional_reason"] == "資料未提供（創新板）"


def test_institutional_page_has_cb_filter_mobile_cards_and_fixed_etf_name():
    source = (DASHBOARD_PATH.parent / "institutional.html").read_text(encoding="utf-8")
    assert 'id="dateSelect"' in source
    assert 'id="cbSearch"' in source
    assert 'id="stockSearch"' not in source
    assert "state.stock" not in source
    assert '已追蹤主動式 ETF' in source
    assert '資料未提供（創新板）' in source
    assert '.cards{display:none}' in source and '@media(max-width:768px)' in source


def test_institutional_page_sorts_raw_values_with_missing_values_last_and_taiwan_colors():
    source = (DASHBOARD_PATH.parent / "institutional.html").read_text(encoding="utf-8")
    assert source.count('button data-sort=') == 10
    assert 'sortValue(r,key)' in source
    assert 'typeof av==="number"?av-bv' in source
    assert 'return bv===null||bv===undefined?0:1' in source
    assert 'state.sortDirection==="asc"?"▲":"▼"' in source
    assert '.positive{color:var(--red)}' in source
    assert '.negative{color:var(--green)}' in source
    assert '.neutral,.unavailable{color:var(--muted)}' in source
    assert 'const cls=n=>n>0?"positive":n<0?"negative":"neutral";' in source


def test_institutional_page_sticks_cb_column_and_formats_lots_to_whole_numbers():
    source = (DASHBOARD_PATH.parent / "institutional.html").read_text(encoding="utf-8")

    assert '<button data-sort="cb_name">CB <span' in source
    assert 'th:first-child{left:0;z-index:3;background:#f6f9f6}' in source
    assert 'td:first-child{position:sticky;left:0;z-index:2;background:var(--card)}' in source
    assert 'max-height:70vh' not in source
    assert '<footer>' not in source
    assert 'const lotsFormat=new Intl.NumberFormat("zh-TW",{maximumFractionDigits:0})' in source
    assert 'const signedLots=n=>' in source
    assert 'Math.sign(n)*Math.round(Math.abs(n)+Number.EPSILON)' in source
    assert 'function desktop(r){return `<tr><td class="identity">${r.cb_name}<span class="sub">${r.cb_code}</span>' in source
    assert '${r.parent_stock_name}（${r.parent_stock_code}）</span></td>' not in source


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


def test_remaining_days_uses_the_nearest_unexpired_put_or_maturity_date():
    assert build_dashboard.remaining_days("2026-08-29", "2026-09-10", "2027-01-01") == 12
    assert build_dashboard.remaining_days("2026-08-29", "2026-08-28", "2027-01-01") == 125
    assert build_dashboard.remaining_days("2026-08-29", "2026-08-28", None) is None
    assert build_dashboard.remaining_days("2026-08-29", None, None) is None


def test_remaining_days_uses_redemption_date_as_the_lifecycle_countdown():
    assert build_dashboard.remaining_days(
        "2026-09-01", "2027-06-24", "2029-06-24", "2026-09-02", "已贖回"
    ) == 1
    assert build_dashboard.remaining_days(
        "2026-09-02", "2027-06-24", "2029-06-24", "2026-09-02", "已贖回"
    ) == 0
    assert build_dashboard.remaining_days(
        "2026-09-01", "2026-09-10", "2027-01-01", "2026-09-03", "已下市"
    ) == 9


def test_balance_ratio_requires_a_positive_issue_unit_count():
    assert build_dashboard.balance_ratio(198_300_000, 2_000) == 99.15
    assert build_dashboard.balance_ratio(None, 2_000) is None
    assert build_dashboard.balance_ratio(198_300_000, 0) is None


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
        "balance_ratio",
        "remaining_days",
    ]
    assert all(header["aria-sort"] == "none" for header in parser.headers)
    assert "thead th {\n      position: sticky;\n      top: 0;" in source
    assert ".sticky-name {\n      position: sticky;\n      left: 0;" in source
    assert 'issue_units: "number"' in source
    assert 'balance_date: "date"' in source
    assert 'remaining_days: "number"' in source
    assert 'balance_ratio: "number"' in source
    assert 'p_close_price: "number"' in source
    assert 'reference_price: "number"' in source
    assert 'p_volume_lots: "number"' in source
    assert 'conversion_value: "number"' in source
    assert 'premium_rate: "number"' in source
    assert 'minimumFractionDigits: 2' in source
    assert 'maximumFractionDigits: 2' in source
    assert 'valuationFormat.format(record.conversion_value)' in source
    assert 'valuationFormat.format(record.premium_rate)' in source
    assert "if (!hasValue(aValue)) return 1;" in source
    assert 'sortType === "number"' in source
    assert 'sortType === "date"' in source
    assert 'state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";' in source
