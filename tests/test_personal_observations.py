"""Personal Full uses only bounded evidence for the selected security and session."""

import json
import time
from datetime import date, datetime, timedelta

import pytest

from scripts import personal_bootstrap as cold, personal_observations as full
from tradingagents import margin_balance, powershell_fundflow as flow

DAY = "2024-02-29"
SYMBOL = "600895.SS"


def page_bytes(ticker, when="2024-02-29 13:00:00", *, empty=False, url=None, business=None):
    from tradingagents.powershell_news import make_request

    code = "202402290000000001"
    article = {
        "title": ticker[:6] + " 合成公告",
        "content": "普通测试正文",
        "code": code,
        "date": when,
        "url": url or "http://finance.eastmoney.com/a/" + code + ".html",
    }
    data = {"code": 0, "msg": "OK", "result": {"cmsArticleWebOld": [] if empty else [article]}}
    if business is not None:
        data.update(business)
    return (make_request(ticker, DAY).callback + "(" + json.dumps(data) + ")").encode()


@pytest.mark.parametrize(
    "case,status,reason,selection",
    [
        ("available", "verified", None, None),
        ("empty", "valid_empty", "no_candidates", {}),
        ("other_stock", "unverified", "candidates_rejected", {"returned": 1, "wrong_stock": 1}),
        ("wrong_day", "date_mismatch", "candidates_rejected", {"returned": 1, "outside_window": 1}),
        ("future_time", "date_mismatch", "candidates_rejected", None),
        ("wrong_source", "unverified", "candidates_rejected", {"returned": 1, "source_rejected": 1}),
        ("wrong_callback", "unverified", "unexpected_response", None),
        ("business_failure", "unverified", "source_business_rejected", None),
        ("unverified_status", "unverified", "unknown", None),
    ],
)
def test_news_provenance_and_time(case, status, reason, selection):
    stamp = datetime.fromisoformat("2024-02-29T16:30:00+08:00")
    private_marker = "fixture-raw-provider-marker"
    raw = page_bytes(
        "000001.SZ" if case == "other_stock" else SYMBOL,
        {"wrong_day": "2024-03-01 13:00:00", "future_time": "2024-02-29 18:00:00"}.get(
            case, "2024-02-29 13:00:00"
        ),
        empty=case == "empty",
        url="https://example.invalid/page" if case == "wrong_source" else None,
        business={
            "business_failure": {"success": False, "error": private_marker},
            "unverified_status": {"code": 98657, "msg": private_marker},
        }.get(case),
    )
    if case == "wrong_callback":
        raw = b"other" + raw[raw.index(b"(") :]
    result = full.news_page(raw, SYMBOL, DAY, stamp)
    assert result["status"] == status
    assert "普通测试正文" not in str(result) and "合成公告" not in str(result)
    assert all(marker not in str(result) for marker in (private_marker, "98657", "example.invalid"))
    if reason is None:
        assert "index_diagnostic" not in result
    else:
        expected = {"reason": reason}
        if selection is not None:
            expected["selection_counts"] = dict.fromkeys((
                "returned", "accepted", "source_rejected", "wrong_stock",
                "outside_window", "malformed", "undated", "duplicate",
            ), 0) | selection
        assert result["index_diagnostic"] == expected
    if status == "verified":
        assert result["coverage"] == "one_page"
        assert result["articles"][0]["published_at"] < result["observed_at"]
        assert result["articles"][0]["source"].startswith("https://finance.eastmoney.com/a/")


def test_publication_index_cannot_satisfy_article_evidence(monkeypatch):
    from types import SimpleNamespace

    from scripts.personal_graph import personal_sources
    from tests.test_decision_graph_v2 import _permitted_policy_case
    from tradingagents.analysis_budget import analysis_budget_scope
    from tradingagents.dataflows import config as active_config, interface
    from tradingagents.default_config import DEFAULT_CONFIG

    _, _, _, evidence = _permitted_policy_case(
        symbol=SYMBOL, venue="XSHG", currency="CNY", trade_date=DAY
    )
    owner = full.PersonalNews(SYMBOL, DAY, SimpleNamespace(remaining=lambda: 30), True, True)
    sends = []

    def get(ticker, date_text, remaining, admit):
        admit()
        sends.append((ticker, date_text))
        return page_bytes(ticker)

    monkeypatch.setattr(full, "get_page", get)
    monkeypatch.setattr(cold, "now", lambda: datetime.fromisoformat("2024-02-29T16:30:00+08:00"))
    config = dict(
        DEFAULT_CONFIG,
        market_history_mode="current",
        llm_provider="deepseek",
        analysis_budget_enabled=True,
        analysis_allowed_data_vendors=["akshare"],
        tool_vendors={"get_news": "akshare"},
    )
    monkeypatch.setattr(active_config, "_config", config)

    def ask(ticker, number):
        return interface.route_to_vendor_acquired(
            "get_news",
            ticker,
            "2024-02-23",
            DAY,
            tool_call_id=f"fixture-{number}",
            source_ref="news:fixture",
            capability="get_news",
            instrument_identity=evidence.instrument_identity,
        )

    with personal_sources(SYMBOL, DAY, owner), analysis_budget_scope(config) as budget:
        assert ask("000001.SZ", 0).artifact is None and not sends
        for n in (1, 2):
            result = ask(SYMBOL, n)
            assert result.artifact is None
            assert any(
                getattr(o, "error_code", None) == "personal_news_index_only"
                for o in result.outcomes
            )
            article = owner.result["articles"][0]
            assert article["published_at"] == "2024-02-29T13:00:00+08:00"
            assert article["source"] == "https://finance.eastmoney.com/a/202402290000000001.html"
            assert "合成公告" not in str(owner.result) and "普通测试正文" not in str(owner.result)
        assert sends == [(SYMBOL, DAY)] and budget.receipt()["data_attempts"] == 1
    assert not owner.returned_to_news_tool


def test_denied_optional_vendor_stays_denied(tmp_path, monkeypatch):
    from scripts.personal_graph import configured

    c = configured(
        dict(
            profile(tmp_path),
            analysis_allowed_data_vendors=["tushare"],
            personal_public_news_allowed=True,
        ),
        SYMBOL,
    )
    assert c["personal_public_news_allowed"] is False
    c["personal_sources"] = ("news", "eastmoney_fundflow_ratio")
    s = cold.Session(c, (time.time(), time.monotonic()))

    def forbidden(*args, **kw):
        pytest.fail("disabled source must not reach cache or network")

    monkeypatch.setattr(full, "get_page", forbidden)
    monkeypatch.setattr(full, "ratio_observation", forbidden)
    rec = {"enhancements": {}, "admitted_attempts": 0}
    full.supplement(c, {}, SYMBOL, DAY, rec, s, full.PersonalNews(SYMBOL, DAY, s, True, False), "")
    assert rec["enhancements"] == {
        "news": "permission_not_configured",
        "eastmoney_fundflow_ratio": "permission_not_configured",
    }
    assert not s.counts


@pytest.mark.parametrize(
    "status,expected", [(200, "verified"), (302, "unavailable"), (403, "permission_denied")]
)
def test_public_page_fixed_transport_no_redirect_or_credentials(monkeypatch, status, expected):
    from types import SimpleNamespace

    calls = []

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert (
                host == "search-api-web.eastmoney.com" and context.check_hostname and timeout == 15
            )
            self.sock = SimpleNamespace(settimeout=lambda value: None)
            self.blocks = iter([page_bytes(SYMBOL), b""])

        def request(self, method, path, *, headers):
            assert (
                method == "GET"
                and path.startswith("/search/jsonp?")
                and headers == {"Accept-Encoding": "identity"}
            )
            calls.append(method)

        def getresponse(self):
            return SimpleNamespace(
                status=status,
                getheader=lambda key, default: default,
                read1=lambda maximum: next(self.blocks),
            )

        def close(self):
            pass

    monkeypatch.setattr(full.http.client, "HTTPSConnection", Connection)
    owner = full.PersonalNews(SYMBOL, DAY, SimpleNamespace(remaining=lambda: 30), True, True)
    for _ in range(2):
        assert owner.collect(lambda: None)["status"] == expected
    assert calls == ["GET"]


def test_supplement_without_budget_does_not_send(tmp_path, monkeypatch):
    c = profile(tmp_path)
    c["personal_sources"] = ("news",)
    s = cold.Session(c, (time.time(), time.monotonic()))
    s.counts = {"stock_basic": 1}
    s.maximum = 2

    def bounded(ticker, day, remaining, admit):
        admit()
        pytest.fail("budget spent in preparation and graph")

    monkeypatch.setattr(full, "get_page", bounded)
    rec = {"enhancements": {}, "admitted_attempts": 1}
    full.supplement(c, {}, SYMBOL, DAY, rec, s, full.PersonalNews(SYMBOL, DAY, s, True, True), "")
    assert rec["enhancements"]["news"] == "budget_unavailable" and s.counts == {"stock_basic": 1}


def profile(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG

    return cold.profile(
        dict(DEFAULT_CONFIG, analysis_max_calls=60, analysis_timeout_seconds=1200),
        tmp_path,
        {"TUSHARE_TOKEN": "synthetic"},
    )


def binding(day=DAY):
    return {
        "symbol": "600895.SH",
        "trade_date": day,
        "as_of": day,
        "identity_sha256": "a" * 64,
        "snapshot_id": "snapshot:v2:" + "b" * 64,
    }


@pytest.mark.parametrize(
    "case,status,calls",
    [
        ("available", "verified", 1),
        ("empty", "valid_empty", 1),
        ("forbidden", "permission_denied", 1),
        ("wrong_day", "date_mismatch", 1),
        ("same_day", "publication_pending", 0),
        ("statements_used_three", "financial_budget_unavailable", 0),
        ("basic", "financial_budget_unavailable", 0),
        ("skip_margin", "financial_budget_unavailable", 0),
    ],
)
def test_own_margin_scope_and_physical_cache(tmp_path, monkeypatch, case, status, calls):
    from tradingagents.market_history.config import provider_request_authority_config
    from tradingagents.market_history.store import MarketHistoryStore

    c = profile(tmp_path)
    if case == "basic":
        c.update(personal_mode="basic", personal_sources=())
    elif case == "skip_margin":
        c.update(personal_mode="full", personal_sources=("news", "eastmoney_fundflow_ratio"))
    s = cold.Session(c, (time.time(), time.monotonic()))
    s.symbol, s.day = SYMBOL, date.fromisoformat(DAY)
    moment = datetime.fromisoformat(
        ("2024-02-29" if case == "same_day" else "2024-03-01") + "T04:00:00+00:00"
    )
    monkeypatch.setattr(cold, "now", lambda: moment)
    if case in {"statements_used_three", "basic", "skip_margin"}:
        s.counts = dict.fromkeys(cold.FINANCE, 1)
    with MarketHistoryStore.open_provider_request_authority(s.history):
        pass
    authority = provider_request_authority_config(s.history)
    sent = []

    def wire(token, endpoint, params, fields, remaining, admit):
        assert (
            token == "synthetic"
            and endpoint == "margin_detail"
            and fields == ",".join(margin_balance.FIELDS)
        )
        assert params == {"ts_code": "600895.SH", "trade_date": "20240229"} and remaining() > 0
        admit()
        sent.append(endpoint)
        rows = (
            []
            if case == "empty"
            else [["20240228" if case == "wrong_day" else "20240229", "600895.SH", 321]]
        )
        return json.dumps(
            {
                "code": 2002 if case == "forbidden" else 0,
                "data": {"fields": fields.split(","), "items": rows},
            }
        ).encode()

    monkeypatch.setattr(cold, "post", wire)
    result = full.margin_observation(s, binding(), "synthetic", s.account_scope, authority)
    assert result["status"] == status and len(sent) == calls
    assert ("rzye_yuan" in result) == (status == "verified")
    if status in ("verified", "valid_empty"):
        again = full.margin_observation(s, binding(), "synthetic", s.account_scope, authority)
        assert (
            again["status"] == status
            and again["acquisition"] == "coordinator_cache"
            and len(sent) == 1
        )


@pytest.mark.parametrize("margin_position", range(4))
def test_reserved_margin_and_statements_in_any_order(tmp_path, monkeypatch, margin_position):
    from scripts import personal_dc_ratio as ratio
    from tests.test_personal_bootstrap import feed_for
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.market_history.config import provider_request_authority_config
    from tradingagents.market_history.store import MarketHistoryStore

    token = "synthetic-example"
    feed = feed_for(monkeypatch, SYMBOL)
    feed.instant += timedelta(hours=17)
    c = cold.profile(DEFAULT_CONFIG, tmp_path, {"TUSHARE_TOKEN": token})
    c.update(personal_mode="full", personal_sources=full.SOURCES, personal_fetch_dc_ratio=True)
    s = cold.Session(c, (time.time(), time.monotonic()))
    s.symbol, s.day, s.today = SYMBOL, date.fromisoformat(DAY), feed.instant.date()
    with MarketHistoryStore.open_provider_request_authority(s.history):
        pass
    authority = provider_request_authority_config(s.history)
    order = list(cold.FINANCE)
    order.insert(margin_position, "margin_detail")
    params = {
        "ts_code": binding()["symbol"], "report_type": "1",
        "start_date": "20180101", "end_date": "20240229",
    }

    def query_ratio():
        return s.query(
            token, ratio.ENDPOINT, {"ts_code": binding()["symbol"], "trade_date": "20240229"},
            ratio.FIELDS, observation_binding=binding(),
        )

    for endpoint in order:
        if len(feed.calls) == 3:
            rejected = ratio.observe(s, binding(), token, authority, fetch=True)
            assert rejected["status"] == "financial_budget_unavailable"
            with pytest.raises(cold.PreparationError, match="personal_attempt_limit"):
                query_ratio()
        if endpoint == "margin_detail":
            result = full.margin_observation(s, binding(), token, s.account_scope, authority)
            assert result["status"] == "verified"
        else:
            s.query(token, endpoint, params, "")
    assert s.financial_attempts() == 4
    with pytest.raises(cold.PreparationError, match="personal_attempt_limit"):
        query_ratio()
    with pytest.raises(cold.PreparationError, match="personal_attempt_limit"):
        s.query(token, "income", params, "")
    assert feed.calls == order


@pytest.mark.parametrize("boundary", ["total", "deadline", "at_send"])
def test_margin_reserve_respects_physical_boundaries(tmp_path, monkeypatch, boundary):
    from tradingagents.market_history.config import provider_request_authority_config
    from tradingagents.market_history.store import MarketHistoryStore

    c = dict(profile(tmp_path), personal_mode="full", personal_sources=full.SOURCES)
    s = cold.Session(c, (time.time(), time.monotonic()))
    s.symbol, s.day = SYMBOL, date.fromisoformat(DAY)
    s.counts = dict.fromkeys(cold.FINANCE, 1)
    if boundary == "total":
        # The graph has finalized 57 attempts, leaving three for preparation/supplements.
        s.maximum -= 57
    if boundary == "deadline":
        s.deadline = time.monotonic() - 1
    entered = []

    def wire(token, endpoint, params, fields, remaining, admit):
        entered.append(endpoint)
        s.maximum = sum(s.counts.values())
        try:
            admit()
        except cold.PreparationError as exc:
            assert str(exc) == "personal_attempt_limit"
            entered.append("admission_denied")
            raise
        pytest.fail("physical send after budget exhaustion")

    monkeypatch.setattr(cold, "post", wire)
    with MarketHistoryStore.open_provider_request_authority(s.history):
        pass
    authority = provider_request_authority_config(s.history)
    error = "personal_deadline_reached" if boundary == "deadline" else "personal_attempt_limit"

    def observe():
        return full.margin_observation(s, binding(), "synthetic", s.account_scope, authority)

    if boundary == "at_send":
        # The coordinator projects a send-stage failure to the observation's safe status.
        assert observe()["status"] == "unverified"
    else:
        with pytest.raises(cold.PreparationError, match=error):
            observe()
    assert entered == (["margin_detail", "admission_denied"] if boundary == "at_send" else [])
    assert s.financial_attempts() == 3


@pytest.mark.parametrize(
    "case,expected",
    [
        ("verified", "verified"),
        ("missing", "cache_missing_or_unverified"),
        ("other_stock", "cache_missing_or_unverified"),
        ("bad_digest", "cache_missing_or_unverified"),
    ],
)
def test_only_bound_personal_ratio_cache(tmp_path, case, expected):
    from tests.test_basic_fundflow_cache import seed
    from tradingagents.market_history.config import (
        MarketHistoryConfig,
        provider_request_authority_config,
    )

    day = "2026-07-02"
    b = binding(day)
    c = profile(tmp_path)
    authority = provider_request_authority_config(MarketHistoryConfig.from_mapping(c))
    stamp = "2026-07-03T05:00:02+00:00"
    projection = {
        "counts": {"returned": 1, "duplicate": 0, "older": 0, "future": 0, "accepted": 1},
        "rows": [[day, "1", "2", "3", "4", "5", "6"]],
    }
    old = flow.PowerShellFundflow(
        tmp_path / "pwsh.exe", tmp_path / "Get-PublicFundflow.ps1", SYMBOL, day, after_report=True
    )
    old.identity_sha = b["identity_sha256"]
    digest = old._observation("available_partial", projection, stamp)["normalized_sha256"]
    seed(
        c,
        flow._digest(b),
        {
            "received_at": stamp,
            "projection": projection,
            "normalized_sha256": "0" * 64 if case == "bad_digest" else digest,
        },
        "missing" if case == "missing" else "full_key",
    )
    if case == "other_stock":
        b = dict(b, symbol="000001.SZ")
    result = full.ratio_observation(b, authority)
    assert result["status"] == expected
    if case == "verified":
        assert result["main_net_percent"] == "1" and set(result) == {
            "status",
            "trade_date",
            "main_net_percent",
            "observed_at",
            "acquisition",
        }
