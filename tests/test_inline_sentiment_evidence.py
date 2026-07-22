from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.agents.schemas import SentimentBand, SentimentReport
from tradingagents.dataflows import interface, reddit, stocktwits
from tradingagents.dataflows.acquisition import AcquisitionResult
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.errors import VendorRateLimitError
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    ClaimValidationStatus,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SubmittedMaterialClaim,
    build_inline_evidence_state,
    stable_acquisition_source_ref,
)


@pytest.mark.unit
def test_inline_http_429_is_unavailable_and_cannot_create_artifact_or_fact():
    error_text = "HTTP 429 Too Many Requests: Retry-After=60"
    claim = MaterialClaim(
        claim_id="sentiment.rate_limit",
        analyst="sentiment",
        statement="The provider returned HTTP 429.",
        source_refs=("sentiment.news",),
        source_quote=error_text,
    )

    evidence = build_inline_evidence_state(
        {"sentiment.news": error_text},
        (claim,),
    )

    assert evidence.sources[0].status is EvidenceStatus.UNAVAILABLE
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert evidence.material_claims[0].fact_ids == ()
    assert evidence.claim_validations[0].status is ClaimValidationStatus.UNSUPPORTED
    outcome = evidence.acquisition_outcomes[0]
    assert isinstance(outcome, SourceAcquisitionUnavailable)
    assert outcome.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert outcome.retryable is True


@pytest.mark.unit
def test_inline_available_text_retains_typed_outcome_and_source_binding():
    source_text = "Company guidance increased revenue by 10%."
    claim = MaterialClaim(
        claim_id="sentiment.guidance",
        analyst="sentiment",
        statement="Revenue guidance increased by 10%.",
        source_refs=("sentiment.news",),
        source_quote="increased revenue by 10%",
    )

    evidence = build_inline_evidence_state(
        {"sentiment.news": source_text},
        (claim,),
    )

    assert evidence.sources[0].status is EvidenceStatus.AVAILABLE
    assert len(evidence.source_artifacts) == 1
    assert len(evidence.source_facts) == 1
    assert evidence.claim_validations[0].status is ClaimValidationStatus.SUPPORTED
    assert evidence.material_claims[0].fact_ids == (
        evidence.source_facts[0].fact_id,
    )
    assert isinstance(evidence.acquisition_outcomes[0], SourceAcquisitionAvailable)


@pytest.mark.unit
def test_inline_evidence_acquisition_outcome_uses_concrete_utc_timestamp():
    evidence = build_inline_evidence_state(
        {"sentiment.inline": "A deterministic inline source."},
        (),
    )

    outcome = evidence.acquisition_outcomes[0]
    assert outcome.retrieved_at != "unknown"
    assert datetime.fromisoformat(outcome.retrieved_at.replace("Z", "+00:00")).tzinfo is timezone.utc


@pytest.mark.unit
def test_sentiment_node_merges_controller_and_inline_acquisition_outcomes(monkeypatch):
    previous_config = get_config()
    set_config({"tool_vendors": {"get_news": "inline_fixture"}})

    class StockTwitsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"messages":[{"created_at":"2026-07-18T12:00:00Z",'
                b'"user":{"username":"fixture"},"entities":{"sentiment":'
                b'{"basic":"Bullish"}},"body":"One ordinary StockTwits message."}]}'
            )

    class RedditResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                b'<title>One ordinary Reddit post.</title>'
                b'<published>2026-07-18T12:00:00Z</published>'
                b'<content type="html">ordinary fixture body</content>'
                b'</entry></feed>'
            )

    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_news"],
        "inline_fixture",
        lambda *_args, **_kwargs: "One ordinary news item.",
    )
    monkeypatch.setattr(
        stocktwits,
        "urlopen",
        lambda *_args, **_kwargs: StockTwitsResponse(),
    )
    monkeypatch.setattr(
        reddit,
        "urlopen",
        lambda *_args, **_kwargs: RedditResponse(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "bind_required_structured",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "invoke_required_structured",
        lambda *args, **kwargs: SimpleNamespace(
            value=None,
            reason="validation_error",
        ),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "resolve_china_a_symbol",
        lambda ticker: None,
    )

    news_source_ref = stable_acquisition_source_ref(
        "sentiment.news", "AAPL", "2026-07-11", "2026-07-18"
    )
    stocktwits_source_ref = stable_acquisition_source_ref(
        "sentiment.stocktwits", "AAPL", 30
    )
    reddit_source_ref = stable_acquisition_source_ref(
        "sentiment.reddit", "AAPL", *reddit.DEFAULT_SUBREDDITS, 5
    )
    node = sentiment_analyst.create_sentiment_analyst(object())
    try:
        update = node(
            {
                "company_of_interest": "AAPL",
                "trade_date": "2026-07-18",
                "instrument_context": "Instrument: AAPL",
                "messages": [],
                "evidence_state": EvidenceState().model_dump(mode="json"),
            }
        )
    finally:
        set_config(previous_config)
    evidence = EvidenceState.model_validate(update["evidence_state"])

    outcomes = {outcome.source_ref: outcome for outcome in evidence.acquisition_outcomes}
    assert set(outcomes) == {
        news_source_ref,
        stocktwits_source_ref,
        reddit_source_ref,
    }
    assert isinstance(outcomes[news_source_ref], SourceAcquisitionAvailable)
    assert outcomes[news_source_ref].capability == "get_news"
    assert isinstance(outcomes[stocktwits_source_ref], SourceAcquisitionAvailable)
    assert outcomes[stocktwits_source_ref].capability == "sentiment_stocktwits"
    assert isinstance(outcomes[reddit_source_ref], SourceAcquisitionAvailable)
    assert outcomes[reddit_source_ref].capability == "sentiment_reddit"
    assert {
        artifact.source_ref for artifact in evidence.source_artifacts
    } >= {news_source_ref, stocktwits_source_ref, reddit_source_ref}


@pytest.mark.unit
def test_sentiment_node_binds_alias_claim_to_exact_acquired_news_artifact(monkeypatch):
    previous_config = get_config()
    set_config({"tool_vendors": {"get_news": "claim_fixture"}})
    news_text = "Management raised full-year revenue guidance."
    source_quote = "raised full-year revenue guidance"

    class StockTwitsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"messages": []}'

    class RedditResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_news"],
        "claim_fixture",
        lambda *_args, **_kwargs: news_text,
    )
    monkeypatch.setattr(
        stocktwits,
        "urlopen",
        lambda *_args, **_kwargs: StockTwitsResponse(),
    )
    monkeypatch.setattr(
        reddit,
        "urlopen",
        lambda *_args, **_kwargs: RedditResponse(),
    )
    structured = MagicMock()
    structured.invoke.return_value = SentimentReport(
        overall_band=SentimentBand.MILDLY_BULLISH,
        overall_score=6.5,
        confidence="medium",
        narrative="Management guidance improved.",
        material_claims=(
            SubmittedMaterialClaim(
                claim_id="sentiment.guidance_update",
                statement="Management raised its full-year revenue guidance.",
                source_ref="sentiment.news",
                source_quote=source_quote,
            ),
        ),
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    source_ref = stable_acquisition_source_ref(
        "sentiment.news", "AAPL", "2026-07-11", "2026-07-18"
    )
    try:
        update = sentiment_analyst.create_sentiment_analyst(llm)(
            {
                "company_of_interest": "AAPL",
                "trade_date": "2026-07-18",
                "instrument_context": "Instrument: AAPL",
                "messages": [],
                "evidence_state": EvidenceState().model_dump(mode="json"),
            }
        )
    finally:
        set_config(previous_config)

    evidence = EvidenceState.model_validate(update["evidence_state"])
    validation = next(
        item
        for item in evidence.claim_validations
        if item.claim_id == "sentiment.guidance_update"
    )
    fact = next(
        item
        for item in evidence.source_facts
        if item.fact_id in validation.fact_ids
    )
    news_outcomes = tuple(
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.source_ref in {"sentiment.news", source_ref}
    )
    news_artifacts = tuple(
        artifact
        for artifact in evidence.source_artifacts
        if artifact.source_ref in {"sentiment.news", source_ref}
    )

    assert validation.status is ClaimValidationStatus.SUPPORTED
    assert evidence.material_claims[-1].fact_ids == (fact.fact_id,)
    assert fact.source_ref == source_ref
    assert fact.tool_call_id == f"sentiment-news:{source_ref}"
    assert fact.tool_name == "get_news"
    assert fact.artifact_sha256 == (
        "50f4ab2c1e8b79221018878ad13f009b1e413eb6f3425cd8a35d9a9d436112ed"
    )
    assert fact.raw_text == source_quote
    assert len(news_outcomes) == 1
    assert isinstance(news_outcomes[0], SourceAcquisitionAvailable)
    assert news_outcomes[0].artifact == news_artifacts[0]
    assert news_artifacts == (news_outcomes[0].artifact,)
    assert news_outcomes[0].retrieved_at != "unknown"
    assert (
        datetime.fromisoformat(
            news_outcomes[0].retrieved_at.replace("Z", "+00:00")
        ).tzinfo
        is timezone.utc
    )


@pytest.mark.unit
def test_china_local_prompt_context_cannot_mint_synthetic_evidence(monkeypatch):
    news_text = "One acquired mainland-market news item."
    local_text = "Northbound holdings increased by 12%."
    local_quote = "increased by 12%"
    captured = {}
    identity = InstrumentIdentityEvidence(
        symbol="600895.SS",
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        display_name="上海张江高科技园区开发股份有限公司",
    )

    def fake_acquire_news(
        acquired_ticker,
        _start_date,
        _end_date,
        *,
        tool_call_id,
        source_ref,
        capability,
        instrument_identity,
    ):
        artifact = SourceArtifact(
            artifact_sha256=sha256(news_text.encode()).hexdigest(),
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name="get_news",
            raw_text=news_text,
        )
        outcome = SourceAcquisitionAvailable(
            provider="fixture_news",
            capability="get_news",
            source_ref=source_ref,
            attempt=1,
            retrieved_at="2026-07-20T12:00:00Z",
            artifact=artifact,
        )
        assert capability == "sentiment_news"
        assert acquired_ticker == identity.symbol
        assert instrument_identity == identity
        return AcquisitionResult(
            value=news_text,
            artifact=artifact,
            outcomes=(outcome,),
            provider="fixture_news",
        )

    monkeypatch.setattr(sentiment_analyst, "acquire_news", fake_acquire_news)
    monkeypatch.setattr(
        sentiment_analyst,
        "resolve_china_a_symbol",
        lambda _ticker: object(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "get_china_a_local_sentiment",
        lambda *_args, **_kwargs: local_text,
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "get_china_a_enhancements_for_categories",
        lambda *_args, **_kwargs: "",
    )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt)
        or SentimentReport(
            overall_band=SentimentBand.MILDLY_BULLISH,
            overall_score=6.0,
            confidence="low",
            narrative="Local context was constructive.",
            material_claims=(
                SubmittedMaterialClaim(
                    claim_id="sentiment.local_holdings",
                    statement="Northbound holdings increased.",
                    source_ref="sentiment.china_local",
                    source_quote=local_quote,
                ),
            ),
        )
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    update = sentiment_analyst.create_sentiment_analyst(llm)(
        {
                "company_of_interest": "600895.SH",
            "trade_date": "2026-07-20",
            "instrument_context": "Instrument: 600895.SS",
            "messages": [],
                "evidence_state": EvidenceState(
                    instrument_identity=identity
                ).model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(update["evidence_state"])
    validation = next(
        item
        for item in evidence.claim_validations
        if item.claim_id == "sentiment.local_holdings"
    )
    prompt_text = "\n".join(
        str(message.content) for message in captured["prompt"]
    )

    assert local_text in prompt_text
    assert "advisory" in prompt_text.casefold()
    assert validation.status is ClaimValidationStatus.UNSUPPORTED
    assert validation.fact_ids == ()
    assert evidence.material_claims[-1].fact_ids == ()
    assert all(
        fact.source_ref != "sentiment.china_local"
        for fact in evidence.source_facts
    )
    assert all(
        artifact.source_ref != "sentiment.china_local"
        for artifact in evidence.source_artifacts
    )
    assert all(
        outcome.source_ref != "sentiment.china_local"
        for outcome in evidence.acquisition_outcomes
    )


@pytest.mark.unit
def test_sentiment_news_prefetch_uses_run_owned_acquisition_controller(monkeypatch):
    """A throttled sentiment-news provider is not re-invoked within one run."""
    previous_config = get_config()
    set_config({"tool_vendors": {"get_news": "rate_limited"}})
    provider_calls = 0

    class StockTwitsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"messages": []}'

    class RedditResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def rate_limited_news(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise VendorRateLimitError(status_code=429, retry_after_seconds=12)

    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_news"], "rate_limited", rate_limited_news
    )
    monkeypatch.setattr(
        stocktwits,
        "urlopen",
        lambda *_args, **_kwargs: StockTwitsResponse(),
    )
    monkeypatch.setattr(
        reddit,
        "urlopen",
        lambda *_args, **_kwargs: RedditResponse(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "bind_required_structured",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "invoke_required_structured",
        lambda *_args, **_kwargs: SimpleNamespace(
            value=None, reason="validation_error"
        ),
    )
    monkeypatch.setattr(sentiment_analyst, "resolve_china_a_symbol", lambda _: None)

    state = {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "instrument_context": "Instrument: AAPL",
        "messages": [],
        "evidence_state": EvidenceState().model_dump(mode="json"),
    }
    source_ref = stable_acquisition_source_ref(
        "sentiment.news", "AAPL", "2026-07-11", "2026-07-18"
    )
    node = sentiment_analyst.create_sentiment_analyst(object())
    try:
        with authoritative_snapshot_run():
            first = EvidenceState.model_validate(node(state)["evidence_state"])
            second = EvidenceState.model_validate(node(state)["evidence_state"])
    finally:
        set_config(previous_config)

    first_news = next(
        outcome
        for outcome in first.acquisition_outcomes
        if outcome.source_ref == source_ref
    )
    second_news = next(
        outcome
        for outcome in second.acquisition_outcomes
        if outcome.source_ref == source_ref
    )

    assert isinstance(first_news, SourceAcquisitionUnavailable)
    assert first_news.capability == "get_news"
    assert first_news.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert isinstance(second_news, SourceAcquisitionUnavailable)
    assert second_news.capability == "get_news"
    assert second_news.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert provider_calls == 1
    for outcome in (first_news, second_news):
        assert outcome.retrieved_at != "unknown"
        assert datetime.fromisoformat(outcome.retrieved_at.replace("Z", "+00:00")).tzinfo is timezone.utc
    for evidence in (first, second):
        assert all(artifact.source_ref != source_ref for artifact in evidence.source_artifacts)
        assert all(fact.source_ref != source_ref for fact in evidence.source_facts)


@pytest.mark.unit
def test_sentiment_stocktwits_prefetch_uses_run_owned_acquisition_controller(
    monkeypatch,
):
    """A throttled StockTwits endpoint is not re-opened within one run."""
    previous_config = get_config()
    set_config({"tool_vendors": {"get_news": "fixture_news"}})
    stocktwits_calls = 0

    class RedditResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def rate_limited_urlopen(request, timeout):
        nonlocal stocktwits_calls
        stocktwits_calls += 1
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "12"},
            None,
        )

    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_news"],
        "fixture_news",
        lambda *_args, **_kwargs: "Fixture news content.",
    )
    monkeypatch.setattr(stocktwits, "urlopen", rate_limited_urlopen)
    monkeypatch.setattr(
        reddit,
        "urlopen",
        lambda *_args, **_kwargs: RedditResponse(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "bind_required_structured",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "invoke_required_structured",
        lambda *_args, **_kwargs: SimpleNamespace(
            value=None, reason="validation_error"
        ),
    )
    monkeypatch.setattr(sentiment_analyst, "resolve_china_a_symbol", lambda _: None)

    state = {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "instrument_context": "Instrument: AAPL",
        "messages": [],
        "evidence_state": EvidenceState().model_dump(mode="json"),
    }
    source_ref = stable_acquisition_source_ref("sentiment.stocktwits", "AAPL", 30)
    node = sentiment_analyst.create_sentiment_analyst(object())
    try:
        with authoritative_snapshot_run():
            first = EvidenceState.model_validate(node(state)["evidence_state"])
            second = EvidenceState.model_validate(node(state)["evidence_state"])
    finally:
        set_config(previous_config)

    first_stocktwits = next(
        outcome
        for outcome in first.acquisition_outcomes
        if outcome.source_ref == source_ref
    )
    second_stocktwits = next(
        outcome
        for outcome in second.acquisition_outcomes
        if outcome.source_ref == source_ref
    )

    assert isinstance(first_stocktwits, SourceAcquisitionUnavailable)
    assert first_stocktwits.capability == "sentiment_stocktwits"
    assert first_stocktwits.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert isinstance(second_stocktwits, SourceAcquisitionUnavailable)
    assert second_stocktwits.capability == "sentiment_stocktwits"
    assert second_stocktwits.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert stocktwits_calls == 1
    for outcome in (first_stocktwits, second_stocktwits):
        assert outcome.retrieved_at != "unknown"
        assert datetime.fromisoformat(outcome.retrieved_at.replace("Z", "+00:00")).tzinfo is timezone.utc
    for evidence in (first, second):
        assert all(artifact.source_ref != source_ref for artifact in evidence.source_artifacts)
        assert all(fact.source_ref != source_ref for fact in evidence.source_facts)


@pytest.mark.unit
def test_sentiment_reddit_prefetch_uses_run_owned_acquisition_controller(
    monkeypatch,
):
    """A throttled Reddit RSS endpoint is not retried or re-opened within one run."""
    previous_config = get_config()
    set_config({"tool_vendors": {"get_news": "fixture_news"}})
    reddit_calls = 0

    class StockTwitsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"messages": []}'

    def rate_limited_reddit_urlopen(request, timeout):
        nonlocal reddit_calls
        reddit_calls += 1
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "12"},
            None,
        )

    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_news"],
        "fixture_news",
        lambda *_args, **_kwargs: "Fixture news content.",
    )
    monkeypatch.setattr(
        stocktwits,
        "urlopen",
        lambda *_args, **_kwargs: StockTwitsResponse(),
    )
    monkeypatch.setattr(reddit, "urlopen", rate_limited_reddit_urlopen)
    monkeypatch.setattr(
        reddit.time,
        "sleep",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("Reddit must not sleep or retry locally")
        ),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "bind_required_structured",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        sentiment_analyst,
        "invoke_required_structured",
        lambda *_args, **_kwargs: SimpleNamespace(
            value=None, reason="validation_error"
        ),
    )
    monkeypatch.setattr(sentiment_analyst, "resolve_china_a_symbol", lambda _: None)

    state = {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "instrument_context": "Instrument: AAPL",
        "messages": [],
        "evidence_state": EvidenceState().model_dump(mode="json"),
    }
    source_ref = stable_acquisition_source_ref(
        "sentiment.reddit", "AAPL", *reddit.DEFAULT_SUBREDDITS, 5
    )
    node = sentiment_analyst.create_sentiment_analyst(object())
    try:
        with authoritative_snapshot_run():
            first = EvidenceState.model_validate(node(state)["evidence_state"])
            second = EvidenceState.model_validate(node(state)["evidence_state"])
    finally:
        set_config(previous_config)

    first_reddit = next(
        outcome
        for outcome in first.acquisition_outcomes
        if outcome.source_ref == source_ref
    )
    second_reddit = next(
        outcome
        for outcome in second.acquisition_outcomes
        if outcome.source_ref == source_ref
    )

    assert isinstance(first_reddit, SourceAcquisitionUnavailable)
    assert first_reddit.provider == "reddit_rss"
    assert first_reddit.capability == "sentiment_reddit"
    assert first_reddit.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert first_reddit.http_status == 429
    assert first_reddit.retry_after_seconds == 12
    assert isinstance(second_reddit, SourceAcquisitionUnavailable)
    assert second_reddit.provider == "reddit_rss"
    assert second_reddit.capability == "sentiment_reddit"
    assert second_reddit.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert reddit_calls == 1
    for outcome in (first_reddit, second_reddit):
        assert outcome.retrieved_at != "unknown"
        assert datetime.fromisoformat(outcome.retrieved_at.replace("Z", "+00:00")).tzinfo is timezone.utc
    for evidence in (first, second):
        assert all(artifact.source_ref != source_ref for artifact in evidence.source_artifacts)
        assert all(fact.source_ref != source_ref for fact in evidence.source_facts)
