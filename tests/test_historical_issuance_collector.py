from datetime import date

import pytest

from historical_issuance_collector import HistoricalIssuanceSourceError, fetch_index, notice_storage_url, parse_notice


class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload


class Session:
    def post(self, *_args, **_kwargs):
        return Response({"stat":"ok", "tables":[{"totalCount":1,"data":[[1,"112/08/14","證櫃債字第11200080781號","公告 CB","./detail"]]}]})


def test_index_reconstructs_stable_official_notice_url():
    rows = fetch_index(Session(), date(2023, 8, 1), date(2023, 8, 31))
    assert rows[0]["source_url"] == "https://www.tpex.org.tw/storage/eb_data/11208/11200080781.html"
    assert notice_storage_url("112/08/14", "證櫃債字第11200080781號").endswith("11208/11200080781.html")
    assert notice_storage_url("94/01/31", "證櫃債字第940000001號").endswith("9401/940000001.html")


def test_known_notice_terms_are_extracted_only_with_exact_code():
    raw = "<p>代碼：64145。</p><p>發行總面額：新臺幣30億元整。</p><p>發行日：112年8月16日。</p><p>到期日：115年8月16日。</p>"
    assert parse_notice("64145", raw) == {"cb_code":"64145", "issue_date":"2023-08-16", "maturity_date":"2026-08-16", "issue_amount":3_000_000_000}


def test_index_refuses_partial_page_instead_of_silently_losing_notices():
    class PartialSession:
        def post(self, *_args, **_kwargs):
            return Response({"stat":"ok", "tables":[{"totalCount":2,"data":[]}]})
    with pytest.raises(HistoricalIssuanceSourceError, match="truncated"):
        fetch_index(PartialSession(), date(2023, 8, 1), date(2023, 8, 31))
