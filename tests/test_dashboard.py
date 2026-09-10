import json
import shutil
import sqlite3
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from scripts import build_dashboard
from db import connect
from strategy_registry import get_strategy
from strategy_runs import publish_a_baseline, publish_a_date, run_a_v2_recalculation


DOCS_PATH = Path(__file__).resolve().parents[1] / "docs"
DASHBOARD_PATH = DOCS_PATH / "daily-market.html"
HOME_PATH = DOCS_PATH / "index.html"
SOURCE_DATABASE = Path(__file__).resolve().parents[1] / "data" / "cb_history.db"


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
        "volume_ma5": None,
        "volume_ma10": None,
        "price_ma20": None,
        "price_ma43": None,
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


def test_dashboard_calculates_cb_rolling_averages_from_observed_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        start = date(2026, 1, 1)
        connection.executemany(
            "INSERT INTO cb_daily VALUES (?,?,?,?,?,?)",
            [
                ((start + timedelta(days=index)).isoformat(), "12345", "測試 CB", 100.0,
                 99.0, 0 if index == 41 else index + 1)
                for index in range(42)
            ],
        )
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    rows = json.loads(output_path.read_text(encoding="utf-8"))["records"]
    row = next(item for item in rows if item["trade_date"] == "2026-08-29" and item["cb_code"] == "12345")
    assert row["volume_ma5"] == pytest.approx((39 + 40 + 41 + 0 + 12) / 5)
    assert row["volume_ma10"] == pytest.approx((sum(range(34, 42)) + 0 + 12) / 10)
    assert row["price_ma20"] == pytest.approx((19 * 100 + 101.5) / 20)
    assert row["price_ma43"] == pytest.approx((42 * 100 + 101.5) / 43)
    short_history = next(item for item in rows if item["cb_code"] == "99999")
    assert all(short_history[key] is None for key in ("volume_ma5", "volume_ma10", "price_ma20", "price_ma43"))


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
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "A", "v2", "CB 成交量創 10 日新高", '{"premium_rate_above_1_pct":true}', values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (1, "99999", "2026-08-29", "A", "v2", "CB 成交量創 10 日新高", "{}", "{}", "UNAVAILABLE", '["old"]', "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (2, "99999", "2026-08-29", "A", "v2", "CB 成交量創 10 日新高", "{}", "{}", "UNAVAILABLE", '["missing_cb_close_price"]', "y"))
        b_values = json.dumps({"close_price": 101.5, "average_43_close_price": 98.5, "today_volume_lots": 120, "average_10_volume_lots": 80, "average_5_volume_lots": 70, "prior_19_high_close_price": 100, "conversion_value": 96, "premium_rate_pct": 5.73, "converted_ratio_pct": 10, "balance_date": "2026-07-31", "window_43_trade_dates": ["2026-07-01"]})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "B", "v1", "CB 突破轉換價", '{"close_price_above_43_day_average":true}', b_values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (4, "77777", "2026-08-29", "B", "v1", "CB 突破轉換價", "{}", "{}", "UNAVAILABLE", '["missing_cb_daily_rows"]', "z"))
        c_values = json.dumps({"conversion_value": 108.5, "premium_rate_pct": 12.5, "converted_ratio_pct": 10.0, "conversion_value_bucket": "105-110", "bucket_rank": 1, "bucket_candidate_count": 3, "balance_date": "2026-07-31"})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "C", "v1", "CB 資優生", '{"within_bucket_top_two":true}', c_values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (3, "88888", "2026-08-29", "C", "v1", "CB 資優生", "{}", "{}", "UNAVAILABLE", '["missing_historical_balance"]', "z"))
        g_values = json.dumps({"trigger_types": ["G1", "G3"], "close_price": 110, "conversion_value": 100, "converted_ratio_pct": 5, "issue_anniversary_date": "2026-08-29", "maturity_final_year_start_date": "2026-08-29", "prior_19_high_close_price": 100, "prior_5_average_volume_lots": 10})
        connection.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?)", ("12345", "2026-08-29", "G", "v1", "時間發動策略", '{"g1_first_effective_trade_date_after_issue_anniversary":true}', g_values, "AVAILABLE", "x"))
        connection.execute("INSERT INTO strategy_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (5, "66666", "2026-08-29", "G", "v1", "時間發動策略", "{}", "{}", "UNAVAILABLE", '["baseline_unknown"]', "z"))
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)
    build_dashboard.build_dashboard_data()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert get_strategy("A").active_version == "v2"
    assert payload["strategy_a_signals"][0]["cb_name"] == "測試 CB"
    assert payload["strategy_a_signals"][0]["condition_values"]["conversion_value"] == 60.75
    assert payload["strategy_a_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "A", "strategy_version": "v2",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_cb_close_price", "evaluation_count": 1,
    }]
    assert payload["metadata"]["strategy_sources"]["A"] == {
        "source": "LEGACY", "definition_id": None, "baseline_run_id": None,
    }
    assert {row["strategy_code"] for row in payload["strategy_signals"]} == {"A", "B", "C", "G"}
    assert payload["strategy_b_signals"][0]["condition_values"]["average_43_close_price"] == 98.5
    assert payload["strategy_b_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "B", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_cb_daily_rows", "evaluation_count": 1,
    }]
    c_signal = payload["strategy_c_signals"][0]
    assert c_signal["condition_values"]["bucket_rank"] == 1
    assert c_signal["close_price"] == 101.5
    assert c_signal["put_date"] is None
    assert c_signal["maturity_date"] == "2027-01-01"
    assert payload["strategy_c_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "C", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "missing_historical_balance", "evaluation_count": 1,
    }]
    g_signal = payload["strategy_g_signals"][0]
    assert g_signal["condition_values"]["trigger_types"] == ["G1", "G3"]
    assert g_signal["cb_name"] == "測試 CB"
    assert g_signal["cb_code"] == "12345"
    assert next(row for row in payload["records"] if row["cb_code"] == "12345")["premium_rate"] == pytest.approx(67.0781893)
    assert payload["strategy_g_evaluations"] == [{
        "trade_date": "2026-08-29", "strategy_code": "G", "strategy_version": "v1",
        "data_status": "UNAVAILABLE", "unavailable_reason": "baseline_unknown", "evaluation_count": 1,
    }]
    assert "condition_values" not in payload["strategy_evaluations"][0]


def test_dashboard_does_not_use_unpublished_a_run_cache(
    tmp_path, monkeypatch,
):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    shutil.copy2(SOURCE_DATABASE, database_path)
    with connect(database_path) as connection:
        first_run = run_a_v2_recalculation(
            connection, "2026-09-09", git_commit="dashboard-cache-test",
        )
        definition_id = connection.execute(
            "SELECT definition_id FROM strategy_run WHERE run_id=?", (first_run,)
        ).fetchone()[0]
        # A newer failed active-version run must never replace the completed cache.
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at, error_message)
               VALUES (?, '2026-09-09', '2026-09-09', 'HISTORICAL_RECALCULATION', 'FAILED',
                       '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', 'intentional')""",
            (definition_id,),
        )
        failed_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        # A newer completed A-v1 run must also be excluded by the registry-active version.
        connection.execute(
            """INSERT INTO strategy_definition
               (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
                git_commit, is_active, created_at)
               VALUES ('A', 'v1', 'obsolete', '{}', 'obsolete-rule', 'obsolete-commit', 0,
                       '2099-01-01T00:00:00+00:00')"""
        )
        v1_definition_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-09-09', '2026-09-09', 'HISTORICAL_RECALCULATION', 'COMPLETED',
                       '2099-01-02T00:00:00+00:00', '2099-01-02T00:00:00+00:00')""",
            (v1_definition_id,),
        )
        latest_run = run_a_v2_recalculation(
            connection, "2026-09-09", git_commit="dashboard-cache-test",
        )
        selected = build_dashboard.select_active_strategy_a_run(connection)
        assert latest_run != failed_run
        assert selected is None

    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)
    build_dashboard.build_dashboard_data()
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["metadata"]["strategy_sources"]["A"] == {
        "source": "LEGACY", "definition_id": None, "baseline_run_id": None,
    }
    assert {row["strategy_code"] for row in payload["strategy_signals"]} >= {"B", "C", "G"}


def test_dashboard_uses_only_explicitly_published_a_run_and_keeps_that_pointer(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE strategy_definition (
                definition_id INTEGER PRIMARY KEY, strategy_code TEXT, strategy_version TEXT,
                strategy_name TEXT, parameters_json TEXT, rule_hash TEXT, git_commit TEXT,
                is_active INTEGER, created_at TEXT
            );
            CREATE TABLE strategy_run (
                run_id INTEGER PRIMARY KEY, definition_id INTEGER, start_date TEXT, end_date TEXT,
                run_type TEXT, status TEXT, started_at TEXT, completed_at TEXT, error_message TEXT
            );
            CREATE TABLE strategy_run_evaluations (
                run_id INTEGER, cb_code TEXT, trade_date TEXT, condition_results_json TEXT,
                condition_values_json TEXT, data_status TEXT, unavailable_reasons_json TEXT, evaluated_at TEXT
            );
            CREATE TABLE strategy_run_signals (
                run_id INTEGER, cb_code TEXT, trade_date TEXT, condition_results_json TEXT,
                condition_values_json TEXT, created_at TEXT
            );
            CREATE TABLE strategy_published_series (
                strategy_code TEXT PRIMARY KEY, definition_id INTEGER, baseline_run_id INTEGER UNIQUE,
                published_at TEXT
            );
            CREATE TABLE strategy_published_date (
                definition_id INTEGER, trade_date TEXT, run_id INTEGER, published_at TEXT,
                PRIMARY KEY (definition_id, trade_date)
            );
        """)
        connection.execute(
            """INSERT INTO strategy_definition
               (strategy_code, strategy_version, strategy_name, parameters_json, rule_hash,
                git_commit, is_active, created_at)
               VALUES ('A', 'v2', 'CB 成交量創 10 日新高', '{}', 'test-rule', 'test-commit', 1, 'x')"""
        )
        definition_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-08-29', '2026-08-29', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'y')""",
            (definition_id,),
        )
        published_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run_evaluations
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
                data_status, unavailable_reasons_json, evaluated_at)
               VALUES (?, '12345', '2026-08-29', '{"all":true}', '{"today_volume_lots":12}',
                       'AVAILABLE', '[]', 'x')""",
            (published_run,),
        )
        connection.execute(
            """INSERT INTO strategy_run_evaluations
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
                data_status, unavailable_reasons_json, evaluated_at)
               VALUES (?, '99999', '2026-08-29', '{}', '{}', 'UNAVAILABLE', '["missing_cb_close_price"]', 'x')""",
            (published_run,),
        )
        connection.execute(
            """INSERT INTO strategy_run_signals
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json, created_at)
               VALUES (?, '12345', '2026-08-29', '{"all":true}', '{"today_volume_lots":12}', 'x')""",
            (published_run,),
        )
        publish_a_baseline(connection, published_run)
        # A subsequent normal completed run is intentionally not published.
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-08-29', '2026-08-29', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'z')""",
            (definition_id,),
        )
        later_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        # A new market date and its completed but unpublished run must not change the site.
        connection.execute(
            "INSERT INTO cb_daily VALUES ('2026-08-30', '12345', '測試 CB', 102.0, 100.0, 20)"
        )
        connection.execute(
            """INSERT INTO strategy_run
               (definition_id, start_date, end_date, run_type, status, started_at, completed_at)
               VALUES (?, '2026-08-30', '2026-08-30', 'HISTORICAL_RECALCULATION', 'COMPLETED', 'x', 'z')""",
            (definition_id,),
        )
        incremental_run = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO strategy_run_evaluations
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json,
                data_status, unavailable_reasons_json, evaluated_at)
               VALUES (?, '12345', '2026-08-30', '{"incremental":true}', '{"marker":"incremental"}',
                       'AVAILABLE', '[]', 'x')""",
            (incremental_run,),
        )
        connection.execute(
            """INSERT INTO strategy_run_signals
               (run_id, cb_code, trade_date, condition_results_json, condition_values_json, created_at)
               VALUES (?, '12345', '2026-08-30', '{"incremental":true}', '{"marker":"incremental"}', 'x')""",
            (incremental_run,),
        )

    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)
    build_dashboard.build_dashboard_data()
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["metadata"]["strategy_sources"]["A"] == {
        "source": "RUN_CACHE", "definition_id": definition_id, "baseline_run_id": published_run,
        "coverage": {
            "baseline_start_date": "2026-08-29", "baseline_end_date": "2026-08-29",
            "published_through_date": "2026-08-29", "override_date_count": 0,
        },
    }
    assert payload["strategy_a_signals"][0]["cb_code"] == "12345"
    assert payload["strategy_a_evaluations"][0] == {
        "trade_date": "2026-08-29", "strategy_code": "A", "strategy_version": "v2",
        "data_status": "AVAILABLE", "unavailable_reason": None, "evaluation_count": 1,
    }
    assert later_run != published_run
    assert all(row["trade_date"] != "2026-08-30" for row in payload["strategy_a_signals"])

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        publish_a_date(connection, incremental_run, "2026-08-30")
    build_dashboard.build_dashboard_data()
    published_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert any(row["trade_date"] == "2026-08-30" for row in published_payload["strategy_a_signals"])
    assert published_payload["metadata"]["strategy_sources"]["A"]["coverage"] == {
        "baseline_start_date": "2026-08-29", "baseline_end_date": "2026-08-29",
        "published_through_date": "2026-08-30", "override_date_count": 1,
    }


def test_dashboard_exports_existing_announcements_without_collecting_them(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    output_path = tmp_path / "data.json"
    create_dashboard_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("""
            CREATE TABLE company_announcements (
                company_code TEXT, company_name TEXT, fact_date TEXT,
                spoken_time TEXT, subject TEXT
            )
        """)
        connection.execute(
            "INSERT INTO company_announcements VALUES (?,?,?,?,?)",
            ("1101", "台泥", "2026-08-29", "13:30", "公告本公司可轉換公司債轉換價格調整"),
        )
    monkeypatch.setattr(build_dashboard, "DB_PATH", database_path)
    monkeypatch.setattr(build_dashboard, "OUTPUT_PATH", output_path)

    build_dashboard.build_dashboard_data()

    assert json.loads(output_path.read_text(encoding="utf-8"))["announcements"] == [{
        "company_code": "1101", "company_name": "台泥",
        "announcement_date": "2026-08-29", "announcement_time": "13:30",
        "subject": "公告本公司可轉換公司債轉換價格調整",
    }]


def test_strategy_pages_show_signals_separately_from_unavailable_data():
    index = HOME_PATH.read_text(encoding="utf-8")
    strategy = (DASHBOARD_PATH.parent / "strategy-a.html").read_text(encoding="utf-8")
    strategy_b = (DASHBOARD_PATH.parent / "strategy-b.html").read_text(encoding="utf-8")
    strategy_c = (DASHBOARD_PATH.parent / "strategy-c.html").read_text(encoding="utf-8")
    strategy_g = (DASHBOARD_PATH.parent / "strategy-g.html").read_text(encoding="utf-8")
    assert "CB策略雷達" in index
    assert "TAIWAN CONVERTIBLE BONDS" not in index
    assert 'id="signals"' in index
    assert 'id="announcements"' in index
    assert "G 發行滿一年" in index
    assert "現CB價格" in index
    assert "到期／賣回" not in index
    assert "new-issue" in index
    assert "新發行" in index
    assert "已轉換比率" in index
    assert "當日CB量" in index
    assert "nearerEventDate" in index
    assert 'if(r.put_date&&(!r.maturity_date||r.put_date<=r.maturity_date))return `賣回日 ${formatDate(r.put_date)}`' in index
    assert 'if(r.maturity_date)return `到期日 ${formatDate(r.maturity_date)}`' in index
    assert 'return ""' in index
    assert '${eventDate?`<span class="fact">${eventDate}</span>`:""}' in index
    assert "100-r.balance_ratio" in index
    assert "策略總覽" not in index
    assert 'href="strategy-a.html"' in index
    assert 'href="strategy-b.html"' in index
    assert 'href="strategy-g.html"' in index
    assert "成交量10日新高" in index
    assert "CB突破轉換價" in index
    assert "CB資優生" in index
    assert "時間發動" in index
    assert 'id="dateSelect"' in strategy
    assert "資料不足、無法評估" in strategy
    assert "完整策略條件" in strategy
    assert "signal-card" in index
    assert 'id="sortKey"' in index
    assert 'id="dateSelect"' in strategy_b
    assert "strategy_b_signals" in strategy_b
    assert "43日均價" in strategy_b
    assert "10日均量" in strategy_b
    assert "5日均量" in strategy_b
    assert "20日均價" in strategy_b
    assert "window_43_trade_dates" not in strategy_b
    assert "prior_19_trade_dates" not in strategy_b
    assert "前19日" not in strategy_b
    assert "前19日最高收盤" not in strategy_b
    assert "餘額日期" not in strategy_b
    assert "窗口日期" not in strategy_b
    assert "prior_19_high_close_price" not in strategy_b
    assert "20日均價" in strategy_b
    assert "資料不足、無法評估" in strategy_b
    assert "完整策略條件" in strategy_b
    assert 'href="strategy-c.html"' in index
    assert 'id="dateSelect"' in strategy_c
    assert "conversion_value_bucket" in strategy_c
    assert "收盤價" in strategy_c
    assert "賣回日" in strategy_c
    assert "到期日" in strategy_c
    assert "餘額日期" not in strategy_c
    assert "priceFormat" in strategy_c
    assert "formatDate(r.put_date)" in strategy_c
    assert "formatDate(r.maturity_date)" in strategy_c
    assert "資料不足、無法評估" in strategy_c
    assert "完整策略條件" in strategy_c
    assert "evaluation_count" in strategy
    assert "evaluation_count" in strategy_c
    assert 'id="dateSelect"' in strategy_g
    assert "strategy_g_signals" in strategy_g
    assert "trigger_types" in strategy_g
    assert "完整策略條件" in strategy_g
    assert "已轉換比例 &lt; 10%" in strategy_g
    assert "轉換價值 ≥ 90" in strategy_g
    assert "當日收盤價 ≤ 130" in strategy_g
    assert "價格突破（收盤價 &gt; 20日均價）" in strategy_g
    assert "成交量放大（成交量 &gt; 5日均量 × 3倍）" in strategy_g
    assert "發動基本條件" not in strategy_g
    assert "時間發動分類條件" not in strategy_g
    assert "basic-grid" not in strategy_g
    assert "conditions-panel ol" in strategy_g
    assert 'G1：首次進入或基本條件首次轉為成立。' not in strategy_g
    assert 'G2：保留既有量價突破條件。' not in strategy_g
    assert 'G3：首次進入或基本條件首次轉為成立。' not in strategy_g
    assert 'padding:10px 14px' in strategy_g
    assert '<th>市價</th><th>轉換價值</th><th>溢價率</th><th>成交量</th>' in strategy_g
    assert '<th>發行日</th><th>賣回日</th><th>到期日</th>' in strategy_g
    assert 'lots(r.volume_lots)' in strategy_g
    assert 'cbCell.textContent=r.cb_name??""' in strategy_g
    assert 'cbCell.append(`（${r.cb_code}）`)' in strategy_g
    assert 'decimal(record.premium_rate,"%")' in strategy_g
    assert 'decimal(record.balance_ratio,"%")' in strategy_g
    assert 'n===null||n===undefined?""' in strategy_g
    assert 'recordsByKey=new Map((p.records||[]).map' in strategy_g
    assert 'formatDate(v.issue_date),formatDate(v.put_date),formatDate(v.maturity_date)' in strategy_g
    assert '市價／轉換價值／溢價率' not in strategy_g
    assert '對應關鍵日期' not in strategy_g
    assert "發行滿一年" in strategy_g
    assert "賣回日後發動" in strategy_g
    assert "到期前一年" in strategy_g
    assert "資料不足、無法評估" in strategy_g
    assert 'href="strategy-g.html"' in strategy
    assert 'href="strategy-g.html"' in strategy_b
    assert 'href="strategy-g.html"' in strategy_c


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
        "volume_ma5",
        "volume_ma10",
        "price_ma20",
        "price_ma43",
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
    assert 'volume_ma5: "number"' in source
    assert 'price_ma43: "number"' in source
    assert 'id="exportExcel"' in source
    assert 'xlsx@0.18.5' in source
    assert 'filteredRows().map' in source
    assert 'CB每日行情_${state.selectedDate}.xlsx' in source
    assert 'minimumFractionDigits: 2' in source
    assert 'maximumFractionDigits: 2' in source
    assert 'valuationFormat.format(record.conversion_value)' in source
    assert 'valuationFormat.format(record.premium_rate)' in source
    assert "if (!hasValue(aValue)) return 1;" in source
    assert 'sortType === "number"' in source
    assert 'sortType === "date"' in source
    assert 'state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";' in source
