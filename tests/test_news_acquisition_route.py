from unittest import mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from yfinance.exceptions import YFRateLimitError

from tradingagents.agents.analysts.submission import (
    AnalystSubmissionResult,
    build_analyst_update,
)
from tradingagents.agents.utils.news_data_tools import get_news
from tradingagents.dataflows import akshare_data, interface, yfinance_news
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.dataflows.market_snapshot import (
    authoritative_snapshot_run,
    get_active_run_telemetry,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceSource,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    build_tool_evidence_state,
    stable_acquisition_source_ref,
)


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
