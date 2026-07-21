"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches three complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks:

  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic). Providers
that cannot return the schema produce an explicit unavailable submission;
free text is never treated as decision evidence.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

import logging
from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.news_data_tools import acquire_news, get_news_legacy
from tradingagents.agents.utils.structured import (
    bind_required_structured,
    invoke_required_structured,
)
from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements_for_categories
from tradingagents.dataflows.china_sentiment import get_china_a_local_sentiment
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.reddit import (
    DEFAULT_SUBREDDITS,
    acquire_reddit_posts,
    fetch_reddit_posts,
)
from tradingagents.dataflows.stocktwits import (
    acquire_stocktwits_messages,
    fetch_stocktwits_messages,
)
from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol
from tradingagents.evidence import (
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MaterialClaim,
    build_inline_evidence_state,
    merge_claim_validations,
    merge_evidence_sources,
    merge_material_claims,
    merge_source_acquisition_outcomes,
    merge_source_artifacts,
    merge_source_facts,
    stable_acquisition_source_ref,
)

logger = logging.getLogger(__name__)


def _normalize_sentiment_submission(value):
    report = SentimentReport.model_validate(value)
    claim_ids = tuple(claim.claim_id for claim in report.material_claims)
    if len(set(claim_ids)) != len(claim_ids) or any(
        not claim_id.startswith("sentiment.") for claim_id in claim_ids
    ):
        raise ValueError("sentiment claims require unique sentiment.* IDs")
    claims = tuple(
        MaterialClaim(
            claim_id=claim.claim_id,
            analyst="sentiment",
            statement=claim.statement,
            source_refs=(claim.source_ref,),
            source_quote=claim.source_quote,
        )
        for claim in report.material_claims
    )
    return report, claims


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def _collect_sentiment_blocks(
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str | None = None,
    stocktwits_block: str | None = None,
    reddit_block: str | None = None,
) -> dict[str, str]:
    if news_block is None:
        news_block = get_news_legacy(ticker, start_date, end_date)
    if resolve_china_a_symbol(ticker) is not None:
        local_sentiment = get_china_a_local_sentiment(ticker, start_date, end_date)
        preset = get_config().get("china_a_enhancement_preset", "basic")
        try:
            flow_enhancement = get_china_a_enhancements_for_categories(
                ticker,
                end_date,
                preset,
                {"flow_sentiment"},
            )
        except Exception as exc:
            logger.warning(
                "China A-share sentiment enhancement unavailable for %s: %s",
                ticker,
                exc,
            )
            flow_enhancement = ""
        local_block = "\n\n".join(
            part for part in (local_sentiment, flow_enhancement) if part.strip()
        )
        return {
            "news_block": news_block,
            "stocktwits_block": (
                "<stocktwits skipped: not applicable for China A-shares; "
                "StockTwits does not reliably cover mainland China tickers. "
                "Do not treat this as a missing retail-sentiment failure.>"
            ),
            "reddit_block": (
                "<reddit skipped: not applicable for China A-shares; English finance "
                "subreddits are not a reliable mainland China ticker sentiment source. "
                "Do not infer absence of discussion from this skipped source.>"
            ),
            "local_sentiment_block": local_block,
        }

    return {
        "news_block": news_block,
        "stocktwits_block": stocktwits_block
        if stocktwits_block is not None
        else fetch_stocktwits_messages(ticker, limit=30),
        "reddit_block": reddit_block
        if reddit_block is not None
        else fetch_reddit_posts(ticker),
        "local_sentiment_block": "",
    }


def _evidence_for_blocks(
    ticker: str,
    blocks: dict[str, str],
    claims: tuple[MaterialClaim, ...] = (),
    news_acquisition=None,
    stocktwits_acquisition=None,
    reddit_acquisition=None,
) -> EvidenceState:
    is_china = resolve_china_a_symbol(ticker) is not None
    source_blocks = []
    if news_acquisition is None:
        source_blocks.append(("sentiment.news", blocks["news_block"]))
    if not is_china and stocktwits_acquisition is None:
        source_blocks.append(("sentiment.stocktwits", blocks["stocktwits_block"]))
    if not is_china and reddit_acquisition is None:
        source_blocks.append(("sentiment.reddit", blocks["reddit_block"]))

    source_text_by_ref: dict[str, str | None] = {}
    for source_id, block in source_blocks:
        source_text_by_ref[source_id] = block if block.strip() else None
    direct_acquisitions = (
        ("sentiment.news", "acquired_news", news_acquisition),
        ("sentiment.stocktwits", "acquired_stocktwits", stocktwits_acquisition),
        ("sentiment.reddit", "acquired_reddit", reddit_acquisition),
    )
    direct_acquisitions = tuple(
        item for item in direct_acquisitions if item[2] is not None
    )
    inline_evidence = build_inline_evidence_state(
        source_text_by_ref,
        claims,
        source_artifact_by_ref={
            source_id: acquisition.artifact
            for source_id, _, acquisition in direct_acquisitions
            if acquisition.artifact is not None
        },
    )
    if not direct_acquisitions:
        return inline_evidence
    return inline_evidence.model_copy(
        update={
            "sources": (
                *inline_evidence.sources,
                *(
                    EvidenceSource(
                        source_id=source_id,
                        status=(
                            EvidenceStatus.AVAILABLE
                            if acquisition.artifact is not None
                            else EvidenceStatus.UNAVAILABLE
                        ),
                        required=True,
                        detail=detail,
                    )
                    for source_id, detail, acquisition in direct_acquisitions
                ),
            ),
            "source_artifacts": (
                *inline_evidence.source_artifacts,
                *(
                    acquisition.artifact
                    for _, _, acquisition in direct_acquisitions
                    if acquisition.artifact is not None
                ),
            ),
            "acquisition_outcomes": (
                *inline_evidence.acquisition_outcomes,
                *(
                    outcome
                    for _, _, acquisition in direct_acquisitions
                    for outcome in acquisition.outcomes
                ),
            ),
        }
    )


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via required structured output.
    """
    structured_llm = bind_required_structured(
        llm,
        SentimentReport,
        "Sentiment Analyst",
    )

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        news_source_ref = stable_acquisition_source_ref(
            "sentiment.news", ticker, start_date, end_date
        )
        news_acquisition = acquire_news(
            ticker,
            start_date,
            end_date,
            tool_call_id=f"sentiment-news:{news_source_ref}",
            source_ref=news_source_ref,
            capability="sentiment_news",
        )
        news_block = news_acquisition.value
        if news_block is None:
            reason = (
                news_acquisition.outcomes[-1].reason.value
                if news_acquisition.outcomes
                else "provider_error"
            )
            news_block = f"DATA_UNAVAILABLE: news acquisition unavailable ({reason})."

        stocktwits_acquisition = None
        stocktwits_block = None
        if resolve_china_a_symbol(ticker) is None:
            stocktwits_source_ref = stable_acquisition_source_ref(
                "sentiment.stocktwits", ticker, 30
            )
            stocktwits_acquisition = acquire_stocktwits_messages(
                ticker,
                30,
                tool_call_id=f"sentiment-stocktwits:{stocktwits_source_ref}",
                source_ref=stocktwits_source_ref,
                capability="sentiment_stocktwits",
            )
            stocktwits_block = stocktwits_acquisition.value
            if stocktwits_block is None:
                reason = (
                    stocktwits_acquisition.outcomes[-1].reason.value
                    if stocktwits_acquisition.outcomes
                    else "provider_error"
                )
                stocktwits_block = (
                    "DATA_UNAVAILABLE: StockTwits acquisition unavailable "
                    f"({reason})."
                )

        reddit_acquisition = None
        reddit_block = None
        if resolve_china_a_symbol(ticker) is None:
            reddit_source_ref = stable_acquisition_source_ref(
                "sentiment.reddit", ticker, *DEFAULT_SUBREDDITS, 5
            )
            reddit_acquisition = acquire_reddit_posts(
                ticker,
                DEFAULT_SUBREDDITS,
                5,
                tool_call_id=f"sentiment-reddit:{reddit_source_ref}",
                source_ref=reddit_source_ref,
                capability="sentiment_reddit",
            )
            reddit_block = reddit_acquisition.value
            if reddit_block is None:
                reason = (
                    reddit_acquisition.outcomes[-1].reason.value
                    if reddit_acquisition.outcomes
                    else "provider_error"
                )
                reddit_block = (
                    "DATA_UNAVAILABLE: Reddit acquisition unavailable "
                    f"({reason})."
                )

        # Render typed acquisition results into prompt blocks. Unavailable
        # outcomes become prompt-only placeholders and remain diagnostics,
        # never evidence artifacts or Source Facts.
        blocks = _collect_sentiment_blocks(
            ticker,
            start_date,
            end_date,
            news_block,
            stocktwits_block,
            reddit_block,
        )

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=blocks["news_block"],
            stocktwits_block=blocks["stocktwits_block"],
            reddit_block=blocks["reddit_block"],
            local_sentiment_block=blocks["local_sentiment_block"],
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}"
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        structured_result = invoke_required_structured(
            structured_llm,
            formatted_messages,
            "Sentiment Analyst",
            validator=_normalize_sentiment_submission,
        )
        material_claims = ()
        failure_reason = structured_result.reason
        if structured_result.value is not None:
            report, material_claims = structured_result.value
            report_text = render_sentiment_report(report)
        if failure_reason is not None:
            report_text = (
                "ANALYSIS_UNAVAILABLE: The Sentiment Analyst did not produce a "
                "validated structured evidence report. No directional conclusion "
                "was issued."
            )

        update = {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }
        submission_source = EvidenceSource(
            source_id="analyst.sentiment.submission",
            status=(
                EvidenceStatus.UNAVAILABLE
                if failure_reason is not None
                else EvidenceStatus.AVAILABLE
            ),
            required=True,
            detail=failure_reason or "direct_structured",
        )
        inline_evidence = _evidence_for_blocks(
            ticker,
            blocks,
            material_claims,
            news_acquisition=news_acquisition,
            stocktwits_acquisition=stocktwits_acquisition,
            reddit_acquisition=reddit_acquisition,
        )
        if material_claims or inline_evidence.sources:
            try:
                merged_evidence = merge_material_claims(
                    state.get("evidence_state"),
                    inline_evidence.material_claims,
                )
                merged_evidence = merge_source_facts(
                    merged_evidence,
                    inline_evidence.source_facts,
                )
                merged_evidence = merge_source_artifacts(
                    merged_evidence,
                    inline_evidence.source_artifacts,
                )
                merged_evidence = merge_claim_validations(
                    merged_evidence,
                    inline_evidence.claim_validations,
                )
                merged_evidence = merge_source_acquisition_outcomes(
                    merged_evidence,
                    inline_evidence.acquisition_outcomes,
                )
            except ValueError:
                merged_evidence = EvidenceState.model_validate(
                    state.get("evidence_state", {})
                )
                submission_source = submission_source.model_copy(
                    update={
                        "status": EvidenceStatus.CONFLICTED,
                        "detail": "immutable claim or fact ID was redefined",
                    }
                )
            update["evidence_state"] = merge_evidence_sources(
                merged_evidence,
                (submission_source, *inline_evidence.sources),
            ).model_dump(mode="json")
        return update

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
    local_sentiment_block: str = "",
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    local_section = ""
    if local_sentiment_block.strip():
        local_section = f"""
### China A-share local sentiment — AKShare/Eastmoney
Mainland-market retail attention, stock comment, and northbound-holding context. This section replaces US-centric retail/social sources when the ticker is a China A-share.
This block is advisory context only because its provider calls do not yet emit run-owned acquisition artifacts. Do not cite `sentiment.china_local` in material_claims; use it only in the advisory narrative.

<start_of_china_local_sentiment>
{local_sentiment_block}
<end_of_china_local_sentiment>
"""

    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — configured news vendor, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>
{local_section}

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, Reddit was rate-limited, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If a source is marked skipped or not applicable for China A-shares, do not count it as a data failure; the China A-share local sentiment block may inform advisory narrative but is not material evidence.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).
- **material_claims**: Decision-relevant factual premises only. Each claim contains only claim_id, statement, one source_ref, and source_quote. Every claim_id must be unique within this report and must begin with `sentiment.` (for example, `sentiment.news_guidance`). Copy source_ref exactly from sentiment.news, sentiment.stocktwits, or sentiment.reddit as applicable above; never cite a skipped, unavailable, or advisory-only source. In particular, never cite sentiment.china_local until it has run-owned acquisition provenance. Each source_quote must copy one exact contiguous source-language phrase from the cited source block. The statement may be a localized paraphrase, but source_quote must not be translated or rewritten.

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
