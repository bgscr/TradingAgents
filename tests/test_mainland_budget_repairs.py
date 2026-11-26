"""Offline integration of exact adapters, counters, routing and retained proof."""

import http.client
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_analysis_budget import config as budget_config, install_network_denial
from tests.test_budget_route_preflight import settings
from tradingagents import tushare_https
from tradingagents.analysis_budget import AnalysisBudgetStopped, analysis_budget_scope
from tradingagents.budget_preflight import apply_mainland_budget_sources, inspect_budget_routes
from tradingagents.financial_response_completeness import (
    FinancialResponseIncomplete,
    inspect_response_completeness,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_denial(monkeypatch)


def wire(monkeypatch, *, status=200, fail=False):
    """Keep real stdlib HTTPS constructor and real SDK query; replace only wire."""
    calls = []

    def request(connection, method, path, *, body, headers):
        payload = json.loads(body)
        calls.append((path, payload["params"], connection.timeout))
        if fail:
            raise TimeoutError("synthetic secret must not escape")
        offset = payload["params"].get("offset", 0)
        connection.synthetic_payload = {
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date"],
                "items": [["601328.SH", str(offset)]] if offset < 2 else [],
            },
        }

    def response(connection):
        return SimpleNamespace(
            status=status,
            getheader=lambda *a: "2",
            read=lambda maximum: json.dumps(connection.synthetic_payload).encode()[:maximum],
        )

    original_class = http.client.HTTPSConnection
    monkeypatch.setattr(original_class, "request", request)
    monkeypatch.setattr(original_class, "getresponse", response)
    return calls, original_class


@pytest.mark.parametrize("endpoint", ["daily", "adj_factor", "suspend_d", "trade_cal"])
def test_real_sdk_pagination_count_and_timeout(monkeypatch, endpoint):
    from tradingagents.picker.tushare_provider import TushareProvider

    calls, original_class = wire(monkeypatch)
    client = tushare_https.create_https_client("synthetic-token")
    provider = TushareProvider(client, page_size=1)
    kwargs = {"exchange": "SSE"} if endpoint == "trade_cal" else {"ts_code": "601328.SH"}
    with analysis_budget_scope(budget_config(analysis_timeout_seconds=5)) as budget:
        frame = provider.fetch_range(endpoint, start_date="20261001", end_date="20261016", **kwargs)
    assert len(frame) == 2
    assert [params["offset"] for _, params, _ in calls] == [0, 1, 2]
    assert all(path == "/dataapi/" + endpoint for path, _, _ in calls)
    assert all(0 < timeout <= 5 for _, _, timeout in calls)
    assert budget.receipt()["admitted_attempts"] == 3
    assert http.client.HTTPSConnection is original_class


def test_page_budget_stop_is_terminal_before_next_wire(monkeypatch):
    from tradingagents.picker.tushare_provider import TushareProvider

    calls, _ = wire(monkeypatch)
    provider = TushareProvider(tushare_https.create_https_client("synthetic"), page_size=1)
    with (
        pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"),
        analysis_budget_scope(budget_config(analysis_max_calls=1)) as budget,
    ):
        provider.fetch_range(
            "daily", start_date="20261001", end_date="20261016", ts_code="601328.SH"
        )
    assert len(calls) == budget.receipt()["admitted_attempts"] == 1


@pytest.mark.parametrize("status,fail", [(302, False), (429, False), (200, True)])
def test_failed_transport_counted_once_no_retry(monkeypatch, status, fail):
    calls, _ = wire(monkeypatch, status=status, fail=fail)
    payload = {
        "api_name": "daily",
        "token": "synthetic-token",
        "params": {"ts_type_name": tushare_https.HTTPS_BASE},
        "fields": "",
    }
    with (
        analysis_budget_scope(budget_config()) as budget,
        pytest.raises(tushare_https.TushareHTTPSFailure) as error,
    ):
        tushare_https.send_once(tushare_https.HTTPS_BASE + "/daily", payload, 30)
    assert len(calls) == budget.receipt()["admitted_attempts"] == 1
    assert "synthetic" not in str(error.value)
    assert error.value.__context__ is None


def test_extra_tushare_endpoint_still_denied_before_wire(monkeypatch):
    calls, _ = wire(monkeypatch)
    payload = {
        "api_name": "stock_basic",
        "token": "synthetic",
        "params": {"ts_type_name": tushare_https.HTTPS_BASE},
        "fields": "",
    }
    with (
        pytest.raises(AnalysisBudgetStopped, match="uncovered_tushare_endpoint"),
        analysis_budget_scope(budget_config()) as budget,
    ):
        tushare_https.send_once(tushare_https.HTTPS_BASE + "/stock_basic", payload, 30)
    assert not calls and budget.receipt()["admitted_attempts"] == 0


def test_exact_snapshot_binding_and_all_four_tool_chains():
    from tradingagents.budget_data import require_budget_snapshot, supports_budget_snapshot
    from tradingagents.dataflows.market_snapshot import SNAPSHOT_PROVIDERS
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = apply_mainland_budget_sources(settings(), ticker="601328.SH")
    report = inspect_budget_routes(config, symbol="601328.SH")
    assert not report.issues
    chains = dict(report.chains)
    assert chains["market_snapshot"] == ("tushare",)
    assert chains["news.get_news"] == ("akshare",)
    assert "news.get_insider_transactions" not in chains
    assert {k.split(".")[0] for k in chains if "." in k} == {"social", "news", "fundamentals"}
    actual = SNAPSHOT_PROVIDERS["tushare"]
    impostor = replace(actual, history_load=lambda *args: None)
    assert supports_budget_snapshot("tushare", actual)
    assert not supports_budget_snapshot("tushare", impostor)
    with analysis_budget_scope(budget_config()):
        require_budget_snapshot("tushare", actual)
    with (
        pytest.raises(AnalysisBudgetStopped, match="uncovered_data_vendor"),
        analysis_budget_scope(budget_config()),
    ):
        require_budget_snapshot("tushare", impostor)
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = config
    graph.asset_configuration = SimpleNamespace(instrument_identity=SimpleNamespace(venue="XSHG"))
    assert "get_insider_transactions" not in graph._create_tool_nodes()["news"].tools_by_name


def test_explicit_sources_do_not_persist_or_permit_non_mainland():
    config = settings()
    original = json.dumps(config, sort_keys=True)
    selected = apply_mainland_budget_sources(config, ticker="601328.SH")
    assert json.dumps(config, sort_keys=True) == original
    assert selected["budget_require_macro_sources"]
    for ticker in ("AAPL", "BTC-USD"):
        with pytest.raises(ValueError):
            apply_mainland_budget_sources(config, ticker=ticker)
    config["financial_cache_only"] = False
    with pytest.raises(ValueError):
        apply_mainland_budget_sources(config, ticker="601328.SH")


def test_core_sources_keep_optional_gaps_and_retain_checkpoint():
    config = settings()
    config["checkpoint_enabled"] = True
    selected = apply_mainland_budget_sources(config, ticker="601328.SH", include_macro_sources=False)
    assert selected["checkpoint_enabled"] is True
    assert selected["analysis_allowed_data_vendors"] == ["tushare", "akshare"]
    assert selected["analysis_disable_alpha_vantage"] is True
    assert selected["budget_require_macro_sources"] is False
    assert "analysis_allowed_data_vendors" not in config
    assert not inspect_budget_routes(selected, symbol="601328.SH").issues


def test_required_macro_failure_cannot_degrade_to_success(monkeypatch):
    from tradingagents import budget_data
    from tradingagents.dataflows import alpha_vantage_news as news, config as runtime
    from tradingagents.dataflows.alpha_vantage_common import AlphaVantageEntitlementError

    runtime.set_config(apply_mainland_budget_sources(settings(), ticker="601328.SH"))

    def denied(*args, **kwargs):
        raise AlphaVantageEntitlementError("synthetic")

    monkeypatch.setattr(news, "get_global_news", denied)
    with (
        pytest.raises(AnalysisBudgetStopped, match="required_macro_source_unavailable"),
        analysis_budget_scope(budget_config()) as budget,
    ):
        budget_data.invoke_budget_data(
            "get_global_news", "alpha_vantage", denied, "2026-10-16", 7, 5
        )
    assert budget.receipt()["admitted_attempts"] == 0


def response():
    return {
        "code": 0,
        "data": {"fields": ["ts_code"], "items": [["601328.SH"]], "has_more": False, "total": 1},
    }


@pytest.mark.parametrize(
    "case",
    [
        "true",
        "invalid",
        "total_bool",
        "total_mismatch",
        "total_conflict",
        "truncated",
        "offset",
        "limit",
    ],
)
def test_explicit_inconsistent_financial_response_fails_closed(case):
    payload = response()
    data = payload["data"]
    if case == "true":
        data["has_more"] = True
    elif case == "invalid":
        data["has_more"] = 0
    elif case == "total_bool":
        data["total"] = True
    elif case == "total_mismatch":
        data["total"] = 2
    elif case == "total_conflict":
        data["total_count"] = 3
    elif case == "truncated":
        data["truncated"] = True
    elif case == "offset":
        data["offset"] = 1
    elif case == "limit":
        data["limit"] = False
    with pytest.raises(FinancialResponseIncomplete) as error:
        inspect_response_completeness(payload)
    assert "secret" not in repr(error.value.summary) + str(error.value)
    assert error.value.summary["status"] == "unresolved"


@pytest.mark.parametrize("case", ["missing", "total_missing", "top_extra", "data_extra"])
def test_optional_or_extension_metadata_is_not_malformed(case):
    payload = response()
    if case == "missing":
        del payload["data"]["has_more"]
    elif case == "total_missing":
        del payload["data"]["total"]
    elif case == "top_extra":
        payload["secret_unknown_key"] = "secret-value"
    else:
        payload["data"]["secret_unknown_key"] = "secret-value"
    summary = inspect_response_completeness(payload)
    assert summary["status"] == "usable_response"
    assert summary["coverage"] == "query_response_only"
    assert "secret" not in repr(summary)


def test_financial_transport_and_artifact_preserve_proof_and_time(monkeypatch):
    from tests.test_tushare_financial_https import fake_connection
    from tradingagents import tushare_financial_https as transport
    from tradingagents.dataflows._tushare_statement_artifacts import encode_provider_artifact
    from tradingagents.dataflows.current_tushare_statements import decode_current_statement_artifact

    fake_connection(monkeypatch)
    frame = transport.create_client("https://api.waditu.com/dataapi").query(
        "income", ts_code="601328.SH", start_date="20200101", end_date="20261016", report_type="1"
    )
    observed = datetime(2026, 10, 16, tzinfo=timezone.utc)
    payload = encode_provider_artifact(
        endpoint_id="income",
        frame=frame,
        retrieved_at=observed,
        observed_at=observed,
        adapter_metadata={"response_completeness": frame.attrs["response_completeness"]},
    )
    decoded = decode_current_statement_artifact(payload, endpoint_id="income")
    assert decoded["observed_at"] == "2026-10-16T00:00:00Z"
    legacy = encode_provider_artifact(
        endpoint_id="income", frame=frame, retrieved_at=observed, observed_at=observed
    )
    with pytest.raises(ValueError, match="completeness"):
        decode_current_statement_artifact(legacy, endpoint_id="income")


def test_cli_flag_forwarded_without_running_analysis(monkeypatch):
    from typer.testing import CliRunner

    import cli.main as cli

    calls = []
    monkeypatch.setattr(cli, "run_analysis", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        cli.app, ["analyze", "--no-checkpoint", "--mainland-budget-sources"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"checkpoint": False, "mainland_budget_sources": True}]


@pytest.mark.parametrize("strict", [False, True])
def test_polymarket_permission_failure_preserves_default_but_stops_explicit_mode(
    monkeypatch, strict
):
    import requests

    from tradingagents import budget_data
    from tradingagents.dataflows import config as runtime, polymarket

    config = settings()
    if strict:
        config = apply_mainland_budget_sources(config, ticker="601328.SH")
    runtime.set_config(config)

    def denied(*args, **kwargs):
        raise requests.RequestException("synthetic")

    monkeypatch.setattr(polymarket, "_request", denied)

    def invoke():
        with analysis_budget_scope(budget_config()):
            return budget_data.invoke_budget_data(
                "get_prediction_markets", "polymarket", polymarket.get_prediction_markets, "macro"
            )

    if strict:
        with pytest.raises(AnalysisBudgetStopped, match="required_macro_source_unavailable"):
            invoke()
    else:
        assert "currently unavailable" in invoke()


@pytest.mark.parametrize(
    "payload", ['{"Information":"unrecognized notice"}', '{"feed":[],"Note":"unknown"}', "{}"]
)
def test_unknown_macro_permission_response_is_terminal(monkeypatch, payload):
    from tradingagents import budget_data
    from tradingagents.dataflows import alpha_vantage_news as news, config as runtime

    runtime.set_config(apply_mainland_budget_sources(settings(), ticker="601328.SH"))
    monkeypatch.setattr(news, "_make_api_request", lambda *a, **k: payload)
    with (
        pytest.raises(AnalysisBudgetStopped, match="required_macro_response_unverified"),
        analysis_budget_scope(budget_config()),
    ):
        budget_data.invoke_budget_data(
            "get_global_news", "alpha_vantage", news.get_global_news, "2026-10-16", 7, 5
        )


def test_cli_explicit_source_override_after_profile_merge_keeps_readiness(monkeypatch):
    import cli.main as cli
    import tradingagents.budget_preflight as preflight
    from tradingagents.dataflows import config as runtime

    config = settings()
    config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock,yfinance"
    config["tool_vendors"]["get_news"] = "yfinance"
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", config)
    runtime.set_config(config)
    observed = []

    class MissingCache(Exception):
        pass

    def validate(final, **context):
        observed.append((final, context))
        assert not inspect_budget_routes(final, symbol=context["ticker"]).issues
        raise MissingCache("synthetic cache-only gap remains")

    monkeypatch.setattr(preflight, "validate_budget_preflight", validate)
    with pytest.raises(MissingCache):
        cli._build_run_config(
            {
                "ticker": "601328.SH",
                "analysis_date": "2026-10-16",
                "research_depth": 1,
                "shallow_thinker": "deepseek-flash",
                "deep_thinker": "deepseek-v4-pro",
                "backend_url": "https://api.deepseek.com",
                "llm_provider": "DeepSeek",
                "analysts": ["market", "social", "news", "fundamentals"],
            },
            False,
            mainland_budget_sources=True,
        )
    final, context = observed[0]
    assert final["tool_vendors"]["get_news"] == "akshare"
    assert final["analysis_max_calls"] == 30 and final["analysis_timeout_seconds"] == 600
    assert final["financial_cache_only"] is True and final["checkpoint_enabled"] is False
    assert context["selected_analysts"] == ("market", "social", "news", "fundamentals") or context[
        "selected_analysts"
    ] == ["market", "social", "news", "fundamentals"]


def test_later_unflagged_cli_does_not_inherit_strict_macro_policy(monkeypatch):
    import cli.main as cli
    import tradingagents.budget_preflight as preflight
    from tradingagents.dataflows import config as runtime

    runtime.set_config(apply_mainland_budget_sources(settings(), ticker="601328.SH"))
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", settings())
    seen = []
    monkeypatch.setattr(
        preflight, "validate_budget_preflight", lambda config, **kw: seen.append(config)
    )
    final = cli._build_run_config(
        {
            "ticker": "601328.SH",
            "analysis_date": "2026-10-16",
            "research_depth": 1,
            "shallow_thinker": "deepseek-flash",
            "deep_thinker": "deepseek-v4-pro",
            "backend_url": "https://api.deepseek.com",
            "llm_provider": "DeepSeek",
            "analysts": ["market", "social", "news", "fundamentals"],
        },
        False,
    )
    assert final["budget_require_macro_sources"] is False
    assert seen[0]["budget_require_macro_sources"] is False


@pytest.mark.parametrize("case", ["true", "conflict", "invalid"])
def test_financial_http_rejection_preserves_bounded_metadata(monkeypatch, case):
    from tests.test_tushare_financial_https import fake_connection
    from tradingagents import tushare_financial_https as transport
    from tradingagents.market_history.coordinator import PhysicalAttemptFailure

    payload = response()
    if case == "true":
        payload["data"]["has_more"] = True
    elif case == "conflict":
        payload["data"]["total_count"] = 2
    else:
        payload["data"]["has_more"] = "false"
    calls = fake_connection(monkeypatch, payload=payload)
    metadata = {}
    client = transport._Client(
        "https://api.waditu.com/dataapi", "synthetic", failure_metadata=metadata
    )
    with pytest.raises(PhysicalAttemptFailure) as captured:
        client.query(
            "income",
            ts_code="601328.SH",
            start_date="20200101",
            end_date="20261016",
            report_type="1",
        )
    assert captured.value.error_code.startswith("financial_completeness_")
    assert metadata["response_completeness"]["status"] == "unresolved"
    assert "private" not in json.dumps(metadata) + str(captured.value)
    assert sum(call[0] == "request" for call in calls) == 1
