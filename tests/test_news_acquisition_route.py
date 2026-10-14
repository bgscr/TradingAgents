import json
import warnings
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import TypeAdapter
from yfinance.exceptions import YFRateLimitError

from tradingagents.agents.analysts.submission import (
    AnalystSubmissionResult,
    build_analyst_update,
)
from tradingagents.agents.utils.news_data_tools import get_news
from tradingagents.dataflows import akshare_data, interface, yfinance_news
from tradingagents.dataflows.acquisition import AcquisitionController
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.dataflows.market_snapshot import (
    authoritative_snapshot_run,
    get_active_run_telemetry,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionOutcome,
    SourceAcquisitionUnavailable,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
    merge_source_acquisition_outcomes,
    stable_acquisition_source_ref,
)


@pytest.mark.unit
def test_akshare_news_wire_response_survives_arrow_string_backend(monkeypatch):
    """Exercise the registered news provider from a nonempty synthetic HTTP page."""
    from types import SimpleNamespace

    from tradingagents.dataflows import config as config_module
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setattr(config_module, "_config", {
        **DEFAULT_CONFIG, "tool_vendors": {"get_news": "akshare"},
    })
    calls = []
    rows = [{
        "date": "2026-10-10 12:00:00", "mediaName": "Synthetic fixture",
        "code": "202610100001", "title": "<em>项目</em>进展",
        "content": "园区\u3000运营\r\n消息", "image": "",
    }]

    def response(url, *, params, **kwargs):
        calls.append((url, json.loads(params["param"]), kwargs))
        body = params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": rows}}) + ")"
        return SimpleNamespace(status_code=200, text=body)

    # Keep the real SDK parser and replace only its wire response.
    import importlib

    sdk_module = importlib.import_module(akshare_data.ak.stock_news_em.__module__)
    sdk_function = akshare_data.ak.stock_news_em
    monkeypatch.setattr(sdk_module.requests, "get", response)
    string_storage = pd.options.mode.string_storage
    with authoritative_snapshot_run():
        result = interface.route_to_vendor_acquired(
            "get_news", "600895.SS", "2026-10-05", "2026-10-12",
            tool_call_id="news-wire", source_ref="news-wire:600895", capability="get_news",
        )
    assert len(calls) == 1
    assert result.artifact is not None
    assert "项目进展" in result.artifact.raw_text
    assert "园区运营 消息" in result.artifact.raw_text
    assert pd.options.mode.string_storage == string_storage
    assert sdk_module.pd is pd
    assert akshare_data.ak.stock_news_em is sdk_function


@pytest.mark.unit
@pytest.mark.parametrize("legacy_checkpoint", [False, True])
def test_repeated_news_calls_keep_distinct_unavailable_acquisitions(legacy_checkpoint):
    """A repeated query is a new call, including when its circuit is open."""
    set_config({"tool_vendors": {"get_news": "akshare"}})
    controller = AcquisitionController(providers=())
    provider = mock.Mock(side_effect=RuntimeError("offline provider failure"))
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([get_news]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    messages = []
    with mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": provider}), mock.patch.object(
        interface, "get_active_acquisition_controller", return_value=controller
    ):
        for call_id in ("news-turn-1", "news-turn-2"):
            messages.append(AIMessage(content="", tool_calls=[{
                "name": "get_news",
                "args": {"ticker": "600895.SS", "start_date": "2026-10-05", "end_date": "2026-10-12"},
                "id": call_id,
                "type": "tool_call",
            }]))
            messages = graph.invoke({"messages": messages})["messages"]
    if legacy_checkpoint:
        # Serialized pre-fix envelopes retained the call binding but no acquisition ID.
        for message in messages:
            if getattr(message, "artifact", None):
                for outcome in message.artifact["acquisition_outcomes"]:
                    outcome.pop("acquisition_id", None)
    source_ref = stable_acquisition_source_ref("news", "600895.SS", "2026-10-05", "2026-10-12")
    update = build_analyst_update(
        {"messages": messages, "company_of_interest": "600895.SS", "trade_date": "2026-10-12"},
        AnalystSubmissionResult(
            message=AIMessage(content="No news evidence"), report="No news evidence",
            claims=(MaterialClaim(claim_id="news.claim", analyst="news", statement="An unverified claim",
                source_refs=(source_ref,), source_quote="An unverified claim"),),
            submission_source=EvidenceSource(source_id="analyst.news.submission", status=EvidenceStatus.AVAILABLE, required=True),
        ),
        "news_report",
    )
    outcomes = update["evidence_state"]["acquisition_outcomes"]
    assert len(outcomes) == 2
    assert {item["reason"] for item in outcomes} == {"provider_error", "circuit_open"}
    assert update["evidence_state"]["source_facts"] == []
    assert provider.call_count == 1


@pytest.mark.unit
def test_open_news_circuit_retains_each_call_and_replay_is_idempotent():
    set_config({"tool_vendors": {"get_news": "akshare,yfinance"}})
    ticks = iter(datetime(2026, 10, 14, tzinfo=timezone.utc) + timedelta(seconds=i) for i in range(6))
    controller = AcquisitionController(providers=(), clock=lambda: next(ticks))
    failed = mock.Mock(side_effect=RuntimeError("offline failure"))
    with mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": failed, "yfinance": failed}), mock.patch.object(
        interface, "get_active_acquisition_controller", return_value=controller
    ):
        outcomes = []
        # The sentiment node opens the operational circuits, then news re-enters.
        for call_id, capability in (("sentiment-1", "sentiment_news"), ("news-1", "get_news"), ("news-2", "get_news")):
            result = interface.route_to_vendor_acquired(
                "get_news", "600895.SS", "2026-10-05", "2026-10-12",
                tool_call_id=call_id, source_ref="news:synthetic", capability=capability,
            )
            outcomes.extend(result.outcomes)
    assert failed.call_count == 2
    assert [item.reason.value for item in outcomes] == ["provider_error"] * 2 + ["circuit_open"] * 4
    assert len({item.acquisition_id for item in outcomes}) == 3
    evidence = merge_source_acquisition_outcomes(None, outcomes)
    saved = EvidenceState.model_validate_json(evidence.model_dump_json())
    assert merge_source_acquisition_outcomes(saved, reversed(outcomes)) == saved
    assert len(saved.acquisition_outcomes) == 6
    changed = outcomes[-1].model_copy(update={"retrieved_at": "2026-10-15T00:00:00Z"})
    with pytest.raises(ValueError, match="source acquisition attempt was redefined"):
        merge_source_acquisition_outcomes(saved, (changed,))


@pytest.mark.unit
def test_news_new_calls_allow_changed_results_but_same_call_cannot_be_redefined():
    set_config({"tool_vendors": {"get_news": "akshare"}})
    results = []
    for call_id, payload in (("news-1", RuntimeError("offline failure")), ("news-2", "First headline"), ("news-3", "Changed headline")):
        provider = mock.Mock(side_effect=payload) if isinstance(payload, Exception) else mock.Mock(return_value=payload)
        # A fresh controller represents a process restart, not a continuing circuit.
        with mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": provider}), mock.patch.object(
            interface, "get_active_acquisition_controller", return_value=AcquisitionController(providers=())
        ):
            results.append(interface.route_to_vendor_acquired(
                "get_news", "600895.SS", "2026-10-05", "2026-10-12",
                tool_call_id=call_id, source_ref="news:synthetic", capability="get_news",
            ))
    evidence = merge_source_acquisition_outcomes(None, (item for result in results for item in result.outcomes))
    assert len(evidence.acquisition_outcomes) == 3
    assert {item.outcome for item in evidence.acquisition_outcomes} == {"available", "unavailable"}
    assert {item.artifact.raw_text for item in evidence.acquisition_outcomes if isinstance(item, SourceAcquisitionAvailable)} == {"First headline", "Changed headline"}
    assert merge_source_acquisition_outcomes(evidence, evidence.acquisition_outcomes) == evidence
    # A success cannot overwrite an unavailable result of the same logical attempt.
    redefined = results[1].outcomes[0].model_copy(update={"acquisition_id": results[0].outcomes[0].acquisition_id})
    with pytest.raises(ValueError, match="source acquisition attempt was redefined"):
        merge_source_acquisition_outcomes(evidence, (redefined,))
    # A changed artifact cannot overwrite the available result either.
    redefined = results[2].outcomes[0].model_copy(update={"acquisition_id": results[1].outcomes[0].acquisition_id})
    with pytest.raises(ValueError, match="source acquisition attempt was redefined"):
        merge_source_acquisition_outcomes(evidence, (redefined,))
    with pytest.raises(ValueError, match="news acquisition identity does not match"):
        ToolExecutionEvidenceEnvelope(
            tool_call_id="different-call", tool_name="get_news", capability="get_news",
            source_ref="news:synthetic", acquisition_outcomes=results[0].outcomes,
        )


@pytest.mark.unit
def test_unbound_legacy_attempts_keep_strict_identity_and_serialization():
    original = SourceAcquisitionUnavailable(
        provider="akshare", capability="get_news", source_ref="legacy:news", attempt=1,
        retrieved_at="2026-10-14T00:00:00Z", retryable=False,
        reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
    )
    assert "acquisition_id" not in original.model_dump(mode="json")
    evidence = merge_source_acquisition_outcomes(None, (original, original))
    assert evidence.acquisition_outcomes == (original,)
    changed = original.model_copy(update={"reason": AcquisitionUnavailableReason.CIRCUIT_OPEN})
    with pytest.raises(ValueError, match="source acquisition attempt was redefined"):
        merge_source_acquisition_outcomes(evidence, (changed,))


@pytest.mark.unit
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("acquisition_id", [None, "news:serialization-call"])
def test_acquisition_identity_serializes_through_unions_without_warnings(available, acquisition_id):
    set_config({"tool_vendors": {"get_news": "akshare"}})
    provider = mock.Mock(return_value="A headline") if available else mock.Mock(side_effect=RuntimeError("offline"))
    with mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": provider}), mock.patch.object(
        interface, "get_active_acquisition_controller", return_value=AcquisitionController(providers=())
    ):
        result = interface.route_to_vendor_acquired(
            "get_news", "600895.SS", "2026-10-05", "2026-10-12",
            tool_call_id="serialization-call", source_ref="news:synthetic", capability="get_news",
        )
    outcome = result.outcomes[0].model_copy(update={"acquisition_id": acquisition_id})
    adapter = TypeAdapter(SourceAcquisitionOutcome)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        encoded = adapter.dump_json(outcome)
        assert adapter.validate_json(encoded) == outcome
        state = merge_source_acquisition_outcomes(None, (outcome,))
        assert EvidenceState.model_validate_json(state.model_dump_json()) == state
        assert adapter.dump_python(outcome, mode="json") == json.loads(encoded)
    payload = json.loads(encoded)
    assert ("acquisition_id" in payload) == (acquisition_id is not None)
    if acquisition_id is not None:
        assert payload["acquisition_id"] == acquisition_id
    if not available:
        assert "error_code" in payload and payload["error_code"] is None


def _mainland_identity() -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol="601658.SS",
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        display_name="中国邮政储蓄银行股份有限公司",
        provenance=IdentityProvenance(
            provider="identity-registry",
            source_ref="registry:601658.SS",
            retrieved_at="2026-07-20T00:00:00+00:00",
            artifact_sha256="a" * 64,
        ),
    )


def _yahoo_article(
    *,
    title: str,
    summary: str = "",
    symbols: tuple[str, ...] | None = None,
) -> dict[str, object]:
    finance = (
        {}
        if symbols is None
        else {"stockTickers": [{"symbol": symbol} for symbol in symbols]}
    )
    return {
        "content": {
            "title": title,
            "summary": summary,
            "provider": {"displayName": "Test Wire"},
            "pubDate": "2026-07-18T08:00:00Z",
            "canonicalUrl": {"url": "https://example.test/article"},
            "finance": finance,
        }
    }


@pytest.mark.unit
def test_acquired_yfinance_news_requires_instrument_relevance_metadata():
    stock = mock.Mock()
    stock.get_news.return_value = [
        _yahoo_article(
            title="CXMT advances memory production",
            symbols=("688256.SS",),
        )
    ]

    with mock.patch.object(
        yfinance_news.yf, "Ticker", return_value=stock
    ), pytest.raises(NoMarketDataError, match="instrument-relevant"):
        yfinance_news.get_news_yfinance(
            "601658.SS",
            "2026-07-01",
            "2026-07-20",
            _acquired=True,
            instrument_identity=_mainland_identity(),
        )


@pytest.mark.unit
def test_acquired_yfinance_news_accepts_provider_symbol_or_authoritative_name():
    stock = mock.Mock()
    stock.get_news.return_value = [
        _yahoo_article(
            title="Bank publishes an operating update",
            symbols=("601658.SS",),
        ),
        _yahoo_article(
            title="中国邮政储蓄银行股份有限公司发布公告",
            symbols=None,
        ),
    ]

    with mock.patch.object(yfinance_news.yf, "Ticker", return_value=stock):
        result = yfinance_news.get_news_yfinance(
            "601658.SS",
            "2026-07-01",
            "2026-07-20",
            _acquired=True,
            instrument_identity=_mainland_identity(),
        )

    assert "Bank publishes an operating update" in result
    assert "中国邮政储蓄银行股份有限公司发布公告" in result


@pytest.mark.unit
def test_acquired_yfinance_news_does_not_accept_symbol_substrings():
    stock = mock.Mock()
    stock.get_news.return_value = [
        _yahoo_article(title="Bond 1601658 completed an unrelated offering")
    ]

    with mock.patch.object(
        yfinance_news.yf, "Ticker", return_value=stock
    ), pytest.raises(NoMarketDataError):
        yfinance_news.get_news_yfinance(
            "601658.SS",
            "2026-07-01",
            "2026-07-20",
            _acquired=True,
            instrument_identity=_mainland_identity(),
        )


@pytest.mark.unit
def test_irrelevant_yfinance_news_becomes_typed_no_data():
    set_config({"tool_vendors": {"get_news": "yfinance"}})
    stock = mock.Mock()
    stock.get_news.return_value = [
        _yahoo_article(title="CXMT expansion", symbols=("688256.SS",))
    ]
    with mock.patch.object(yfinance_news.yf, "Ticker", return_value=stock):
        result = interface.route_to_vendor_acquired(
            "get_news",
            "601658.SS",
            "2026-07-01",
            "2026-07-20",
            tool_call_id="call-irrelevant-news",
            source_ref="get_news:601658.SS:2026-07-20",
            capability="get_news",
            instrument_identity=_mainland_identity(),
        )

    assert result.value is None
    assert result.artifact is None
    assert result.outcomes[-1].reason is AcquisitionUnavailableReason.NO_DATA


@pytest.mark.unit
def test_acquired_news_records_primary_rate_limit_before_secondary_success():
    set_config({"tool_vendors": {"get_news": "primary,secondary"}})
    calls: list[str] = []

    def primary(*_args, **_kwargs):
        calls.append("primary")
        raise VendorRateLimitError(status_code=429, retry_after_seconds=12)

    def secondary(*_args, **_kwargs):
        calls.append("secondary")
        return "SECONDARY NEWS"

    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"primary": primary, "secondary": secondary},
        clear=False,
    ):
        result = interface.route_to_vendor_acquired(
            "get_news",
            "AAPL",
            "2026-07-01",
            "2026-07-20",
            tool_call_id="call-news-1",
            source_ref="get_news:AAPL:2026-07-20",
            capability="get_news",
        )

    assert calls == ["primary", "secondary"]
    assert result.value == "SECONDARY NEWS"
    assert result.artifact is not None
    assert result.artifact.raw_text == "SECONDARY NEWS"
    assert len(result.outcomes) == 2
    first, second = result.outcomes
    assert isinstance(first, SourceAcquisitionUnavailable)
    assert first.provider == "primary"
    assert first.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert first.http_status == 429
    assert not hasattr(first, "diagnostics")
    assert first.retry_after_seconds == 12
    assert isinstance(second, SourceAcquisitionAvailable)
    assert second.provider == "secondary"
    assert second.artifact == result.artifact


@pytest.mark.unit
def test_acquired_news_retains_unknown_configured_vendor_before_success():
    set_config({"tool_vendors": {"get_news": "unknown_vendor,secondary"}})

    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"secondary": lambda *_args, **_kwargs: "SECONDARY NEWS"},
        clear=False,
    ):
        result = interface.route_to_vendor_acquired(
            "get_news",
            "AAPL",
            "2026-07-01",
            "2026-07-20",
            tool_call_id="call-news-unknown",
            source_ref="get_news:AAPL:2026-07-20",
            capability="get_news",
        )

    assert [outcome.provider for outcome in result.outcomes] == [
        "unknown_vendor",
        "secondary",
    ]
    assert isinstance(result.outcomes[0], SourceAcquisitionUnavailable)
    assert (
        result.outcomes[0].reason
        is AcquisitionUnavailableReason.NOT_CONFIGURED
    )


@pytest.mark.unit
def test_get_news_toolnode_emits_content_and_bound_evidence_artifact():
    set_config({"tool_vendors": {"get_news": "secondary"}})
    content = "## AAPL News\n\n### Deterministic headline"
    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"secondary": lambda *_args, **_kwargs: content},
        clear=False,
    ):
        builder = StateGraph(MessagesState)
        builder.add_node("tools", ToolNode([get_news]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        update = builder.compile().invoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "get_news",
                                "args": {
                                    "ticker": "AAPL",
                                    "start_date": "2026-07-01",
                                    "end_date": "2026-07-20",
                                },
                                "id": "call-news-toolnode",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        )

    message = update["messages"][1]
    assert message.content == content
    assert message.tool_call_id == "call-news-toolnode"
    assert message.artifact["tool_call_id"] == "call-news-toolnode"
    source_ref = stable_acquisition_source_ref(
        "news",
        "AAPL",
        "2026-07-01",
        "2026-07-20",
    )
    assert message.artifact["source_ref"] == source_ref
    assert message.artifact["capability"] == "get_news"
    assert message.artifact["selected_artifact"]["raw_text"] == content
    claim = MaterialClaim(
        claim_id="news.headline",
        analyst="news",
        statement="Deterministic headline",
        source_refs=(source_ref,),
        source_quote="Deterministic headline",
    )
    analyst_update = build_analyst_update(
        {
            "messages": update["messages"],
            "company_of_interest": "AAPL",
            "trade_date": "2026-07-20",
        },
        AnalystSubmissionResult(
            message=AIMessage(content="report"),
            report="report",
            claims=(claim,),
            submission_source=EvidenceSource(
                source_id="analyst.news.submission",
                status=EvidenceStatus.AVAILABLE,
                required=True,
            ),
        ),
        "news_report",
    )
    evidence = analyst_update["evidence_state"]
    assert [item["provider"] for item in evidence["acquisition_outcomes"]] == [
        "secondary"
    ]
    assert evidence["source_artifacts"][0]["raw_text"] == content
    assert len(evidence["source_facts"]) == 1


@pytest.mark.unit
def test_acquired_news_caller_labels_share_one_operational_circuit():
    set_config({"tool_vendors": {"get_news": "primary,secondary"}})
    primary_calls = 0

    def primary(*_args, **_kwargs):
        nonlocal primary_calls
        primary_calls += 1
        raise VendorRateLimitError(status_code=429)

    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"primary": primary, "secondary": lambda *_a, **_k: "NEWS"},
        clear=False,
    ), authoritative_snapshot_run():
        telemetry = get_active_run_telemetry()
        assert telemetry is not None
        first = interface.route_to_vendor_acquired(
            "get_news", "AAPL", "2026-07-01", "2026-07-20",
            tool_call_id="call-1", source_ref="get_news:AAPL:2026-07-20",
            capability="get_news",
        )
        sentiment_news = interface.route_to_vendor_acquired(
            "get_news", "MSFT", "2026-07-02", "2026-07-20",
            tool_call_id="call-2", source_ref="sentiment.news:MSFT:2026-07-20",
            capability="sentiment_news",
        )
        projection = telemetry.finalize()

    assert first.outcomes[0].reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert sentiment_news.outcomes[0].reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert [outcome.capability for outcome in first.outcomes] == [
        "get_news",
        "get_news",
    ]
    assert [outcome.capability for outcome in sentiment_news.outcomes] == [
        "sentiment_news",
        "sentiment_news",
    ]
    assert [
        event.outcome.capability for event in projection.acquisition.events
    ] == [
        "get_news",
        "get_news",
        "sentiment_news",
        "sentiment_news",
    ]
    assert primary_calls == 1


@pytest.mark.unit
def test_acquired_yfinance_news_rate_limit_is_one_typed_attempt():
    set_config({"tool_vendors": {"get_news": "yfinance"}})
    stock = mock.Mock()
    stock.get_news.side_effect = YFRateLimitError()

    with mock.patch.object(
        yfinance_news.yf,
        "Ticker",
        return_value=stock,
    ), mock.patch("tradingagents.dataflows.stockstats_utils.time.sleep"):
        result = interface.route_to_vendor_acquired(
            "get_news",
            "AAPL",
            "2026-07-01",
            "2026-07-20",
            tool_call_id="call-yfinance-rate-limit",
            source_ref="get_news:AAPL:2026-07-20",
            capability="get_news",
        )

    assert stock.get_news.call_count == 1
    assert result.value is None
    assert result.artifact is None
    assert len(result.outcomes) == 1
    assert isinstance(result.outcomes[0], SourceAcquisitionUnavailable)
    assert result.outcomes[0].reason is AcquisitionUnavailableReason.RATE_LIMITED


@pytest.mark.unit
def test_rate_limited_only_news_tool_exposes_no_artifact_fact_or_available_source():
    set_config({"tool_vendors": {"get_news": "primary"}})
    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"primary": lambda *_a, **_k: (_ for _ in ()).throw(
            VendorRateLimitError(status_code=429)
        )},
        clear=False,
    ):
        builder = StateGraph(MessagesState)
        builder.add_node("tools", ToolNode([get_news]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        result = builder.compile().invoke({"messages": [AIMessage(content="", tool_calls=[{
            "name": "get_news", "args": {"ticker": "AAPL", "start_date": "2026-07-01", "end_date": "2026-07-20"},
            "id": "call-rate-only", "type": "tool_call",
        }])]})

    message = result["messages"][1]
    claim = MaterialClaim(
        claim_id="news.rate_limited",
        analyst="news",
        statement="A headline exists.",
        source_refs=("get_news:AAPL:2026-07-20",),
        source_quote="headline",
    )
    evidence = build_tool_evidence_state(
        (message,), (claim,),
        tool_call_ids_by_source={"get_news:AAPL:2026-07-20": ("call-rate-only",)},
    )
    assert message.artifact["selected_artifact"] is None
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert all(source.status.value != "available" for source in evidence.sources)


@pytest.mark.unit
def test_news_providers_raise_typed_no_data_on_acquired_path():
    with mock.patch.object(
        akshare_data.ak, "stock_news_em", return_value=pd.DataFrame()
    ), pytest.raises(NoMarketDataError):
        akshare_data.get_news("600895.SS", "2026-07-01", "2026-07-20")

    with mock.patch.object(yfinance_news.yf, "Ticker") as ticker, mock.patch.object(
        yfinance_news, "yf_retry", return_value=[]
    ):
        ticker.return_value.get_news.return_value = []
        assert yfinance_news.get_news_yfinance(
            "AAPL", "2026-07-01", "2026-07-20"
        ) == "No news found for AAPL"
        with pytest.raises(NoMarketDataError):
            yfinance_news.get_news_yfinance(
                "AAPL", "2026-07-01", "2026-07-20", _acquired=True
            )
    with mock.patch.object(yfinance_news.yf, "Ticker") as ticker, mock.patch.object(
        yfinance_news, "yf_retry", side_effect=RuntimeError("secret upstream text")
    ):
        ticker.return_value.get_news.side_effect = RuntimeError("secret upstream text")
        assert yfinance_news.get_news_yfinance(
            "AAPL", "2026-07-01", "2026-07-20"
        ).startswith("Error fetching news")
        with pytest.raises(RuntimeError):
            yfinance_news.get_news_yfinance(
                "AAPL", "2026-07-01", "2026-07-20", _acquired=True
            )


@pytest.mark.unit
def test_acquired_news_does_not_mislabel_china_enhancement_as_base_artifact():
    set_config({
        "tool_vendors": {"get_news": "akshare"},
        "china_a_enhancement_preset": "all",
    })
    with mock.patch.dict(
        interface.VENDOR_METHODS["get_news"],
        {"akshare": lambda *_a, **_k: "BASE NEWS"},
        clear=False,
    ), mock.patch.object(interface, "append_china_a_enhancement") as enhancement:
        result = interface.route_to_vendor_acquired(
            "get_news", "600895.SS", "2026-07-01", "2026-07-20",
            tool_call_id="call-cn", source_ref="get_news:600895.SS:2026-07-20",
            capability="get_news",
        )

    enhancement.assert_not_called()
    assert result.value == "BASE NEWS"
    assert result.artifact is not None
    assert result.artifact.raw_text == "BASE NEWS"
