"""Fail-closed completion handling shared by tool-calling analysts."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.agents.analysts.statement_excerpts import (
    statement_excerpts,
    verified_row_freshness,
)
from tradingagents.agents.utils.structured import (
    SubmissionConstraintError,
    bind_required_structured,
    invoke_required_structured,
    response_output_diagnostics,
    safe_output_diagnostics,
    submission_id_prefix,
)
from tradingagents.dataflows.market_data_validator import verified_snapshot_values
from tradingagents.dataflows.market_snapshot import (
    _minimum_history_for_indicator,
    get_active_authoritative_market_snapshot,
    get_active_market_snapshot_acquisition_record,
    refresh_active_evidence_physical_attempts,
)
from tradingagents.evidence import (
    AnalystEvidenceReport,
    EvidenceNote,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MarketSnapshotEvidence,
    MaterialClaim,
    SubmittedEvidenceNote,
    SubmittedMaterialClaim,
    ToolExecutionEvidenceEnvelope,
    _bound_snapshot_row,
    build_tool_evidence_state,
    merge_claim_validations,
    merge_evidence_notes,
    merge_evidence_sources,
    merge_material_claims,
    merge_source_acquisition_outcomes,
    merge_source_artifacts,
    merge_source_facts,
    project_history_store_access,
    stable_acquisition_source_ref,
    verified_financial_observations,
)
from tradingagents.financial_observations import (
    CURRENT_STATEMENT_COLUMNS,
    FINANCIAL_TOOLS,
    STATEMENT_TITLES,
    FinancialObservation,
)
from tradingagents.run_diagnostics import record_output

logger = logging.getLogger(__name__)

_REPORT_TOOL_NAME = AnalystEvidenceReport.__name__
_SOURCE_PREVIEW_LIMIT = 4_000
_PERSONAL_FULL_PROMPTS = ContextVar("personal_full_analyst_prompts", default=False)
_PERSONAL_EVIDENCE_GUIDANCE = {
    "market": (
        "Use one complete verified OHLCV or exact indicator row per claim, with its date and period. "
        "For Chinese moving averages use ‘〈周期〉日均线为〈读数〉。’. "
        "Keep thresholds and interpretations advisory. If qfq is established, disclose its "
        "total-return limitation. Auxiliary commentary is not a quotation source."
    ),
    "news": (
        "Separate company events from sector and macro context. An index entry alone cannot "
        "support article content or sentiment, and a coverage gap does not establish no news."
    ),
    "fundamentals": (
        "For CURRENT statements quote one complete CSV data row per observation. Keep period, "
        "currency, units and any reliability restrictions. CURRENT rows are advisory, and "
        "synthetic warnings, unavailable or inapplicable metrics belong in notes, never as zero."
    ),
}
ANALYST_OUTPUT_GUIDANCE = (
    "Keep the final JSON concise so the entire object, including exact source quotes, "
    "fits the configured output budget. Aim for a report of about 1,500 characters; "
    "this is a presentation target, not permission to omit required evidence checks. "
    "Prioritize decision-relevant findings, uncertainty, and every material data gap. "
    "Use short exact quotes and avoid repeating raw tables or the same premise in multiple claims. "
    "Every financial decision premise retained in the report needs its material_claim. "
    "Put source provenance, availability, coverage and applicability limitations "
    "in evidence_notes and retain them in the report; each note requires one exact "
    "source_ref and contiguous source_quote. "
    "A note quote must occur exactly once across all tool outputs sharing that source_ref. "
    "Copy a complete source line and preserve leading punctuation (such as '- ' or '# '); "
    "if that line repeats, include adjacent source text until the contiguous quote is unique. "
    "Never splice non-adjacent lines or separate tool outputs; do not invent a locator or discard a material limitation "
    "to avoid a binding failure. If no unique quote exists, report the unresolved binding explicitly. "
    "Notes are not financial evidence, do not qualify an investment conclusion, and must not hide unsupported "
    "financial premises. Missing metrics must never be inferred as zero; "
    "remove unsupported conclusions instead of weakening their evidence requirements. "
    "Do not equate a concise report with complete historical coverage. "
    "For a verified market snapshot, keep each material observation atomic: quote one complete "
    "OHLCV or indicator row per claim. Do not bundle prices and multiple indicators in one claim. "
    "A moving-average period is part of its identity and requires that exact indicator's unique row; "
    "do not use another indicator row or a whole table to justify the period. This does not exempt "
    "any price, date, threshold or other numeric assertion from validation. "
    "For versioned CURRENT statement tables, quote exactly one complete CSV data row "
    "per observation; do not combine rows, omit columns or quote a table header as a value. "
    "The application derives the row's field, base currency, period and quantity scale; "
    "your prose remains advisory. A CURRENT observation is not a canonical decision premise. "
)


MARKET_EVIDENCE_GUIDANCE = (
    "Use a market fact in material_claims only when its cited tool output supplies the exact "
    "observation, instrument and applicable date. Copy the supporting row verbatim. "
    "For a Chinese moving-average claim, use the form '〈周期〉日均线为〈读数〉。' "
    "with the period and reading from that indicator row; omit the claim if either is unbound. "
    "Price basis describes applicability. Only if the current sources or auxiliary context establish qfq, "
    "explain its total-return limitation in report_markdown. "
    "An evidence_note about applicability needs its own unique verbatim quotation from the cited "
    "tool output. The separate multi-year commentary, even when it displays a snapshot ID, "
    "is not such a tool output. Never turn its price-basis line into an availability note. "
    "Use availability only for an actual source-availability limitation documented by that tool. "
    "When only commentary supports a limitation, keep it in the report and leave it out of "
    "evidence_notes; an empty evidence_notes array is valid."
)


@contextmanager
def personal_full_prompt_scope(enabled: bool):
    """Select the analyst policy inside one personal Full graph run."""
    token = _PERSONAL_FULL_PROMPTS.set(enabled)
    try:
        yield
    finally:
        _PERSONAL_FULL_PROMPTS.reset(token)


def personal_full_analysts_active() -> bool:
    return _PERSONAL_FULL_PROMPTS.get()


def uses_personal_deepseek_analyst_policy(llm: Any) -> bool:
    if not personal_full_analysts_active():
        return False
    from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI

    return isinstance(llm, DeepSeekChatOpenAI) and llm.uses_personal_analyst_policy()


def _compact_submission_prompt(analyst: str) -> str:
    return (
        "Submit one AnalystEvidenceReport. report_markdown is a string; material_claims and evidence_notes "
        "are arrays of objects (use [] when empty). Use short bullets for the supported conclusion, "
        "counterevidence, uncertainty and material gaps. Aim for about 600 characters of prose when "
        "practical; keep all decision-relevant evidence and qualifications. Skip duplicated prose and tables. "
        "Preserve dates or periods and source reliability. Use material_claims for each supported material premise "
        "once (claim_id, statement, source_ref, source_quote). Use evidence_notes for material source limitations "
        "(note_id, kind, statement, source_ref, source_quote). Note kinds: provenance, availability, coverage, "
        "applicability. Keep notes separate from decision premises. "
        f"Use unique {analyst}. IDs. Copy allowed refs and exact contiguous, unique source quotations; "
        "keep required complete rows, without translation or splicing. Leave unsupported premises out and "
        "explain unresolved limitations in the report. "
        + _PERSONAL_EVIDENCE_GUIDANCE[analyst]
    )


def personal_full_tool_prompt(analyst: str, tool_guidance: str) -> str | None:
    if not _PERSONAL_FULL_PROMPTS.get():
        return None
    from tradingagents.agents.utils.agent_utils import get_language_instruction

    return f"{tool_guidance}\n{_compact_submission_prompt(analyst)}{get_language_instruction()}"


@dataclass(frozen=True)
class AnalystSubmissionResult:
    message: AIMessage
    report: str
    claims: tuple[MaterialClaim, ...] = ()
    submission_source: EvidenceSource | None = None
    notes: tuple[EvidenceNote, ...] = ()


_STATEMENT_REPORT_CHARS = 480
_STATEMENT_ENTRY_CHARS = 96
_STATEMENT_MAX_CLAIMS = 6
_STATEMENT_MAX_NOTES = 3


class _PersonalFundamentalsCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^R[0-9]{3}$")


class _PersonalFundamentalsNote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^N[0-9]{3}$")
    statement: str = Field(min_length=1, max_length=_STATEMENT_ENTRY_CHARS)
    kind: Literal["provenance", "availability", "coverage", "applicability"]


class PersonalFundamentalsReport(BaseModel):
    """Model-authored content for the personal statement finalizer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_markdown: str = Field(min_length=1, max_length=_STATEMENT_REPORT_CHARS)
    material_claims: tuple[_PersonalFundamentalsCitation, ...] = Field(max_length=_STATEMENT_MAX_CLAIMS)
    evidence_notes: tuple[_PersonalFundamentalsNote, ...] = Field(max_length=_STATEMENT_MAX_NOTES)


class _PersonalMarketCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^M[0-9]{2}$")


class PersonalMarketReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_markdown: str = Field(min_length=1, max_length=900)
    material_claims: tuple[_PersonalMarketCitation, ...] = Field(min_length=1, max_length=6)
    evidence_notes: tuple[SubmittedEvidenceNote, ...] = Field(max_length=3)


_MARKET_ROWS = {
    "Close": ("Close", "CNY"),
    "Open": ("Open", "CNY"),
    "High": ("High", "CNY"),
    "Low": ("Low", "CNY"),
    "Volume": ("Volume", "shares"),
    "close_10_ema": ("10-day EMA", "CNY"),
    "close_50_sma": ("50-day SMA", "CNY"),
    "close_200_sma": ("200-day SMA", "CNY"),
    "rsi": ("RSI", ""),
}


@dataclass(frozen=True)
class _MarketObservation:
    quote: str
    statement: str
    display: str


@dataclass(frozen=True)
class _StatementEvidence:
    source_ref: str
    quote: str
    observation: FinancialObservation | None = None


def bind_analyst_finalizer(
    llm: Any, analyst: str, *, personal_statements: bool = False, personal_market: bool = False,
) -> Any | None:
    """Bind the required structured finalizer when the provider supports it."""
    schema = AnalystEvidenceReport
    if personal_statements:
        schema = PersonalFundamentalsReport
    if personal_market:
        schema = PersonalMarketReport
    return bind_required_structured(
        llm,
        schema,
        f"{analyst.title()} Analyst",
        strict_json=personal_statements or personal_market,
    )


def _submission_source(
    analyst: str,
    status: EvidenceStatus,
    detail: str,
) -> EvidenceSource:
    return EvidenceSource(
        source_id=f"analyst.{analyst}.submission",
        status=status,
        required=True,
        detail=detail,
    )


def _tool_calls_by_id(messages: list[Any]) -> dict[str, Mapping[str, Any]]:
    calls = {}
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None and isinstance(message, Mapping):
            tool_calls = message.get("tool_calls")
        for call in tool_calls or ():
            call_id = call.get("id")
            if call_id:
                calls[str(call_id)] = call
    return calls


def _paired_tool_turns(messages):
    calls = _tool_calls_by_id(messages)
    ai_messages = [message for message in messages if isinstance(message, AIMessage)]
    results = [message for message in messages if isinstance(message, ToolMessage)]
    if (
        any(message.invalid_tool_calls for message in ai_messages)
        or sum(len(message.tool_calls) for message in ai_messages) != len(calls)
        or len(results) != len(calls)
        or {message.tool_call_id for message in results} != calls.keys()
    ):
        return None
    return calls, results


def _source_ref(
    message: Any,
    ticker: str,
    trade_date: str,
    tool_calls_by_id: Mapping[str, Mapping[str, Any]],
    *,
    include_unavailable_financial: bool = False,
) -> str | None:
    tool_name = getattr(message, "name", None)
    tool_call_id = getattr(message, "tool_call_id", None)
    if not tool_name or not tool_call_id:
        return None
    tool_call = tool_calls_by_id.get(str(tool_call_id))
    if tool_call is None or tool_call.get("name") != tool_name:
        return None
    args = tool_call.get("args") or {}
    if not isinstance(args, Mapping):
        return None
    called_ticker = args.get("ticker", args.get("symbol"))
    if called_ticker is not None and str(called_ticker).strip().upper() != str(
        ticker
    ).strip().upper():
        return None
    called_date = args.get("curr_date", args.get("end_date"))
    if called_date is not None and str(called_date) != str(trade_date):
        return None
    raw_envelope = getattr(message, "artifact", None)
    if raw_envelope is None and isinstance(message, Mapping):
        raw_envelope = message.get("artifact")
    if raw_envelope is not None:
        try:
            envelope = ToolExecutionEvidenceEnvelope.model_validate(raw_envelope)
        except (TypeError, ValidationError, ValueError):
            from tradingagents.dataflows.financial_dispatch import (
                FinancialToolMessageAuditEnvelope,
            )

            try:
                financial = FinancialToolMessageAuditEnvelope.model_validate(
                    raw_envelope
                )
            except (TypeError, ValidationError, ValueError):
                return None
            if (
                financial.tool_call_id != str(tool_call_id)
                or financial.tool_name != tool_name
                or (financial.artifact_sha256 is None and not include_unavailable_financial)
            ):
                return None
            return financial.request_ref
        if envelope.tool_call_id != str(tool_call_id) or envelope.tool_name != tool_name:
            return None
        return envelope.source_ref
    if tool_name in {"get_verified_market_snapshot", "get_indicators"}:
        snapshot_match = re.search(
            r"(?mi)^#?\s*Snapshot ID:\s*(snapshot:[0-9a-f]{64})\s*$",
            str(getattr(message, "content", "")),
        )
        if snapshot_match:
            return snapshot_match.group(1)
    if tool_name == "get_verified_market_snapshot":
        return f"snapshot:{ticker}:{trade_date}"
    return f"{tool_name}:{ticker}:{trade_date}"


def _dispatched_statement(message, ticker, trade_date, calls):
    from tradingagents.dataflows.financial_dispatch import FinancialToolMessageAuditEnvelope

    if not isinstance(message, ToolMessage) or message.name not in STATEMENT_TITLES:
        return None
    try:
        envelope = FinancialToolMessageAuditEnvelope.model_validate(message.artifact)
    except ValidationError:
        return None
    if _source_ref(
        message, ticker, trade_date, calls, include_unavailable_financial=True,
    ) == envelope.request_ref:
        return envelope
    return None


def _personal_market_catalog(
    messages, ticker, trade_date,
) -> tuple[str, dict[str, _MarketObservation]] | None:
    """Select complete current rows only from the run-bound, model-visible snapshot."""
    paired = _paired_tool_turns(messages)
    snapshot = get_active_authoritative_market_snapshot(ticker, trade_date)
    if paired is None or snapshot is None or snapshot.effective_trading_date != trade_date:
        return None
    calls, results = paired
    candidates = [item for item in results if item.name == "get_verified_market_snapshot"]
    if len(candidates) != 1:
        return None
    message = candidates[0]
    call = calls[message.tool_call_id]
    if not isinstance(message.content, str) or _source_ref(
        message, ticker, trade_date, calls,
    ) != snapshot.snapshot_id:
        return None
    try:
        envelope = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
    except (TypeError, ValueError):
        return None
    artifact = envelope.selected_artifact
    text = message.content
    if artifact is None or artifact.raw_text != text or envelope.capability != "market_snapshot":
        return None
    labels = {
        "- Provider: ": snapshot.provider,
        "- Adjustment basis: ": snapshot.adjustment_basis,
        "- History rows: ": str(len(snapshot.frame)),
        "Frame SHA-256: ": snapshot.frame_sha256,
    }
    lines = text.splitlines()
    if any([line for line in lines if line.startswith(label)] != [label + value]
           for label, value in labels.items()):
        return None
    prices, indicators = verified_snapshot_values(
        snapshot.frame, (field for field in _MARKET_ROWS if field[0].islower()),
    )
    values = prices | indicators
    catalog = {}
    for number, (field, (label, unit)) in enumerate(_MARKET_ROWS.items(), 1):
        quotes = [line for line in lines if line.startswith(f"| {field} | ")]
        if len(quotes) != 1 or text.count(quotes[0]) != 1:
            continue
        quote = quotes[0]
        if quote != f"| {field} | {values[field]} |":
            return None
        row = _bound_snapshot_row(message, text, quote, text.index(quote), call, snapshot.snapshot_id)
        if row is None or row.effective_date != trade_date:
            continue
        statement = f"{label} on {row.effective_date}: {row.value}"
        display = f"{statement} {unit}".rstrip() + f" ({snapshot.adjustment_basis})"
        catalog[f"M{number:02d}"] = _MarketObservation(quote, statement, display)
    return snapshot.snapshot_id, catalog


def _statement_frequency(call):
    args = call["args"]
    raw = args.get("freq", args.get("frequency", "quarterly"))
    value = raw.strip().casefold() if isinstance(raw, str) else None
    return value if value in ("annual", "quarterly") else "unverified"


def collected_personal_statements(messages: list[Any], ticker: str, trade_date: str) -> bool:
    """Check for dispatched results from all three statement tools, including typed gaps."""
    paired = _paired_tool_turns(messages)
    if paired is None:
        return False
    calls, results = paired
    statements = [
        message for message in results if calls[message.tool_call_id]["name"] in STATEMENT_TITLES
    ]
    if {message.name for message in statements} != set(STATEMENT_TITLES):
        return False
    dispatched = [_dispatched_statement(message, ticker, trade_date, calls) for message in statements]
    if any(result is None for result in dispatched):
        return False
    results_by_request = [
        ((result.tool_name, result.request_ref, _statement_frequency(calls[result.tool_call_id])), result)
        for result in dispatched
    ]
    executed = {
        key: (result.artifact_sha256, result.acquisition_outcomes)
        for key, result in results_by_request if result.disposition == "executed"
    }
    return all(
        key[2] != "unverified"
        and executed.get(key) == (result.artifact_sha256, result.acquisition_outcomes)
        for key, result in results_by_request
    )


def allowed_source_refs(
    messages: list[Any],
    ticker: str,
    trade_date: str,
) -> tuple[str, ...]:
    """Return the exact source-ref vocabulary represented by tool messages."""
    return tuple(source_ref_tool_call_ids(messages, ticker, trade_date))


def source_ref_tool_call_ids(
    messages: list[Any],
    ticker: str,
    trade_date: str,
    *,
    include_unavailable_financial: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Map paired results; unavailable financial refs are operational-only opt-in.

    Claim/finalizer catalogs keep the default. The evidence collector opts in so
    typed failures survive even when the model makes no claim or note about them.
    No failure text is parsed to construct an outcome or source fact.
    """
    refs: dict[str, list[str]] = {}
    tool_calls_by_id = _tool_calls_by_id(messages)
    for message in messages:
        source_ref = _source_ref(
            message,
            ticker,
            trade_date,
            tool_calls_by_id,
            include_unavailable_financial=include_unavailable_financial,
        )
        tool_call_id = getattr(message, "tool_call_id", None)
        if source_ref is None or not tool_call_id:
            continue
        call_ids = refs.setdefault(source_ref, [])
        normalized_call_id = str(tool_call_id)
        if normalized_call_id not in call_ids:
            call_ids.append(normalized_call_id)
    return {source_ref: tuple(call_ids) for source_ref, call_ids in refs.items()}


def render_allowed_source_ref_catalog(
    messages: list[Any],
    ticker: str,
    trade_date: str,
) -> str:
    refs = allowed_source_refs(messages, ticker, trade_date)
    if not refs:
        return (
            "Allowed source_refs for this turn: none. Call a data tool before "
            "submitting any material claim."
        )
    return (
        "Allowed source_refs for this turn (copy exactly):\n"
        + "\n".join(f"- {source_ref}" for source_ref in refs)
        + "\nEach MaterialClaim.source_quote must copy one exact contiguous "
        "source-language phrase from its cited tool output. The localized "
        "statement may paraphrase that quote."
    )


def _personal_statement_prompt(messages, ticker, trade_date) -> tuple[str, dict[str, _StatementEvidence]]:
    from tradingagents.agents.utils.agent_utils import get_language_instruction

    calls = _tool_calls_by_id(messages)
    sources = []
    for message in messages:
        result = _dispatched_statement(message, ticker, trade_date, calls)
        if result is not None and result.disposition == "executed":
            frequency = _statement_frequency(calls[message.tool_call_id])
            sources.append((message, result, frequency))
    groups = Counter((message.name, frequency) for message, _, frequency in sources)
    catalog = {}
    next_id = {"R": 1, "N": 1}
    sections = []
    gaps = []
    for message, result, frequency in sources:
        if groups[message.name, frequency] != 1:
            continue
        label = f"{message.name} ({frequency}) through {trade_date}"
        if result.artifact_sha256 is None:
            reasons = ", ".join(sorted({outcome.reason.value for outcome in result.acquisition_outcomes}))
            gaps.append(f"- {label}: {reasons}.")
            continue
        rows, notes, missing = statement_excerpts(
            message.content if isinstance(message.content, str) else "",
            tool_name=message.name, frequency=frequency, ticker=ticker, trade_date=trade_date,
            source_ref=result.request_ref, tool_call_id=message.tool_call_id,
            artifact_sha256=result.artifact_sha256,
        )
        if not rows:
            gaps.append(f"- {label}: no bindable core statement rows.")
            continue
        gaps.extend(f"- {label}: {entry}." for entry in missing)
        sections.append(f"# {STATEMENT_TITLES[message.name]} data for {ticker} ({frequency})")
        for quote, observation in rows:
            evidence_id = f"R{next_id['R']:03d}"
            next_id["R"] += 1
            catalog[evidence_id] = _StatementEvidence(result.request_ref, quote, observation)
            sections.append(f"{evidence_id} | {quote}")
        for quote in notes:
            evidence_id = f"N{next_id['N']:03d}"
            next_id["N"] += 1
            catalog[evidence_id] = _StatementEvidence(result.request_ref, quote)
            sections.append(f"{evidence_id} | {quote}")
    if any(count > 1 for count in groups.values()):
        gaps.append("- Duplicate statement scopes were excluded.")
    prompt = (
        f"Finalize the fundamentals analyst report for {ticker} on {trade_date}. "
        f"Return one PersonalFundamentalsReport JSON object. report_markdown: at most {_STATEMENT_REPORT_CHARS} characters. "
        f"Select up to {_STATEMENT_MAX_CLAIMS} material_claims using R IDs and up to {_STATEMENT_MAX_NOTES} evidence_notes using N IDs. "
        f"Each evidence note statement is at most {_STATEMENT_ENTRY_CHARS} characters. Use [] if no supported entry. "
        "Each material_claims entry contains only evidence_id. The application renders the exact amount, unit and period. "
        "Cover material findings, counterevidence and relevant gaps in short bullets. Leave amounts to the selected rows. "
        "CURRENT rows are advisory. Report missing values as unavailable. "
        + get_language_instruction()
        + "\n\nExact core statement excerpts. Row columns: " + ",".join(CURRENT_STATEMENT_COLUMNS) + "\n"
        + ("\n".join(sections) if sections else "(none)")
    )
    if gaps:
        prompt += "\n\nStatement acquisition gaps (report as limitations):\n" + "\n".join(gaps)
    return prompt, catalog


def _collected_evidence_preview(messages, ticker, trade_date):
    catalog = []
    seen_artifacts = set()
    preview_omitted_evidence = False
    tool_calls_by_id = _tool_calls_by_id(messages)
    for message in messages:
        source_ref = _source_ref(
            message,
            ticker,
            trade_date,
            tool_calls_by_id,
        )
        artifact_key = (source_ref, str(getattr(message, "tool_call_id", "")))
        if source_ref is None or artifact_key in seen_artifacts:
            continue
        seen_artifacts.add(artifact_key)
        content = str(getattr(message, "content", "")).strip()
        if len(content) > _SOURCE_PREVIEW_LIMIT:
            preview_omitted_evidence = True
            head_length = _SOURCE_PREVIEW_LIMIT // 2
            tail_length = _SOURCE_PREVIEW_LIMIT - head_length
            content = (
                content[:head_length]
                + "\n... [middle omitted] ...\n"
                + content[-tail_length:]
            )
        catalog.append(f"- {source_ref}\n  Evidence: {content or '[empty tool result]'}")
    return "\n".join(catalog) if catalog else "- (no source refs available)", preview_omitted_evidence


def _finalization_prompt(
    *,
    analyst: str,
    ticker: str,
    trade_date: str,
    draft: str,
    messages: list[Any],
    auxiliary_context: str = "",
    personal_news_policy: bool = False,
) -> str:
    from tradingagents.agents.utils.agent_utils import get_language_instruction

    source_catalog, preview_omitted_evidence = _collected_evidence_preview(messages, ticker, trade_date)
    source_context = f"Allowed source refs and collected evidence:\n{source_catalog}\n\n"
    if auxiliary_context:
        source_context += (
            "Deterministic auxiliary observations (report commentary only; do not add "
            "material_claims, evidence_notes, source refs or rating premises from this section). "
            "Include a concise multi-year commentary section in report_markdown. When available, "
            "retain the cumulative adjusted-price return, annual-return pattern with its baseline "
            "and partial-period labels, and maximum drawdown with peak/trough dates. Preserve "
            "coverage and retrospective limitations. Do not discard these supplied auxiliary "
            "observations merely because they are outside the source catalog; they remain "
            "advisory, never decision premises. If unavailable, preserve the limitation and do "
            "not invent numbers. Distinct tool date windows can legitimately have distinct row "
            "counts; a subwindow is not by itself a conflict with the full snapshot.\n"
            + auxiliary_context + "\n\n"
        )
    language = (
        get_language_instruction()
        + " Preserve JSON schema keys, identifiers, source_ref and source_quote unchanged; "
        "the language instruction applies only to report_markdown and human-readable "
        "statements, never to exact source quotations.\n\n"
    )
    draft_context = (
        "Unvalidated prose draft (do not return it unless converted into the schema):\n"
        f"{draft.strip() or '[empty draft]'}"
    )
    if _PERSONAL_FULL_PROMPTS.get() and analyst in _PERSONAL_EVIDENCE_GUIDANCE:
        # A draft may point to dated evidence outside a shortened preview.
        news_gap_guidance = (
            "If no substantive article is verified, state that coverage limit in report_markdown. "
            "Use empty material_claims and evidence_notes unless a real source supports an entry. "
            if personal_news_policy else ""
        )
        return (
            f"Finalize the {analyst} analyst report for {ticker} on {trade_date}. "
            "Return one JSON object without surrounding prose. "
            + _compact_submission_prompt(analyst) + news_gap_guidance + language + source_context
            + (draft_context if preview_omitted_evidence and draft.strip() else "")
        )
    note_example = ('"evidence_notes":[]}.' if analyst == "market" else
                    '"evidence_notes":[{"note_id":"' + analyst + '.unique_note",'
                    '"kind":"availability","statement":"source limitation",'
                    '"source_ref":"one allowed ref","source_quote":"exact quote"}]}.')
    return (
        f"Finalize the {analyst} analyst report for {ticker} on {trade_date}. "
        "Return exactly one AnalystEvidenceReport. Preserve only claims supported "
        "by the source catalog. Every material claim must use one source_ref copied "
        "verbatim from the allowed list; inventing a source ref is forbidden. "
        "Each MaterialClaim.source_quote must copy one exact contiguous "
        "source-language phrase from the cited tool evidence. The statement may "
        "be a localized paraphrase, but source_quote must not be translated or "
        "rewritten. Return one JSON object shaped exactly as "
        '{"report_markdown":"...","material_claims":['
        '{"claim_id":"' + analyst + '.unique_id","statement":"...",'
        '"source_ref":"one allowed ref","source_quote":"exact quote"}],'
        + note_example + " "
        + "Do not add analyst, source_refs, fact_ids, or minimum_history_rows; "
        "the application derives those fields. "
        "If no listed source supports a decision-relevant premise, omit that claim.\n\n"
        + ANALYST_OUTPUT_GUIDANCE + (" " + MARKET_EVIDENCE_GUIDANCE if analyst == "market" else "") + "\n\n"
        + language + source_context + draft_context
    )


def _normalize_submission(
    submitted: AnalystEvidenceReport | Mapping[str, Any],
    analyst: str,
) -> tuple[str, tuple[MaterialClaim, ...], tuple[EvidenceNote, ...]]:
    validated = AnalystEvidenceReport.model_validate(submitted)
    report = validated.report_markdown.strip()
    if not report:
        raise SubmissionConstraintError("blank_report")
    claim_ids = tuple(claim.claim_id for claim in validated.material_claims)
    if len(set(claim_ids)) != len(claim_ids):
        raise SubmissionConstraintError("duplicate_claim_id")
    invalid_ids = tuple(
        claim_id
        for claim_id in claim_ids
        if not claim_id.startswith(submission_id_prefix(analyst))
    )
    if invalid_ids:
        raise SubmissionConstraintError("claim_namespace")
    claims = tuple(
        MaterialClaim(
            claim_id=claim.claim_id,
            analyst=analyst,
            statement=claim.statement,
            source_refs=(claim.source_ref,),
            source_quote=claim.source_quote,
        )
        for claim in validated.material_claims
    )
    note_ids = tuple(note.note_id for note in validated.evidence_notes)
    if len(set(note_ids)) != len(note_ids):
        raise SubmissionConstraintError("duplicate_note_id")
    if set(note_ids) & set(claim_ids):
        raise SubmissionConstraintError("claim_note_collision")
    if any(not note_id.startswith(submission_id_prefix(analyst)) for note_id in note_ids):
        raise SubmissionConstraintError("note_namespace")
    notes = tuple(
        EvidenceNote(analyst=analyst, **note.model_dump())
        for note in validated.evidence_notes
    )
    return report, claims, notes


def _normalize_personal_fundamentals(
    submitted: PersonalFundamentalsReport,
    catalog: Mapping[str, _StatementEvidence],
) -> tuple[str, tuple[MaterialClaim, ...], tuple[EvidenceNote, ...]]:
    content = PersonalFundamentalsReport.model_validate(submitted)

    def resolve(entry):
        match = catalog.get(entry.evidence_id)
        if match is None:
            raise SubmissionConstraintError("unknown_evidence_id")
        return match

    claims = []
    for index, claim in enumerate(content.material_claims, 1):
        match = resolve(claim)
        observation = match.observation
        if observation is None:
            raise SubmissionConstraintError("unknown_evidence_id")
        period = (
            f"balance at period end {observation.period_end}"
            if observation.period_basis == "period_end_balance"
            else f"cumulative from fiscal year start through {observation.period_end}"
        )
        claims.append(SubmittedMaterialClaim(
            claim_id=f"fundamentals.claim-{index}",
            statement=f"{observation.field}: {observation.value} {observation.unit} "
                      f"({period}; {observation.frequency} source).",
            source_ref=match.source_ref,
            source_quote=match.quote,
        ))

    notes = []
    for index, note in enumerate(content.evidence_notes, 1):
        match = resolve(note)
        notes.append(SubmittedEvidenceNote(
            note_id=f"fundamentals.note-{index}", kind=note.kind, statement=note.statement,
            source_ref=match.source_ref, source_quote=match.quote,
        ))

    canonical = AnalystEvidenceReport(
        report_markdown=content.report_markdown,
        material_claims=tuple(claims),
        evidence_notes=tuple(notes),
    )
    return _normalize_submission(canonical, "fundamentals")


def _apply_deterministic_history_requirements(
    evidence: EvidenceState,
) -> EvidenceState:
    """Derive indicator warmups from exact fact-to-calculation bindings."""
    facts_by_id = {fact.fact_id: fact for fact in evidence.source_facts}
    claims = []
    for claim in evidence.material_claims:
        calculation_ids = tuple(
            calculation_id
            for fact_id in claim.fact_ids
            for fact in (facts_by_id.get(fact_id),)
            if fact is not None
            for calculation_id in fact.calculation_ids
        )
        derived_requirements = tuple(
            _minimum_history_for_indicator(calculation_id)
            for calculation_id in calculation_ids
        )
        minimum_history_rows = max(derived_requirements, default=1)
        claims.append(
            claim.model_copy(
                update={"minimum_history_rows": minimum_history_rows}
            )
        )
    return evidence.model_copy(update={"material_claims": tuple(claims)})


def _unavailable_result(
    analyst: str, reason: str, *, stage: str = "finalizer", attempts: int = 0,
    diagnostics: Mapping[str, Any] | None = None,
) -> AnalystSubmissionResult:
    label = analyst.title()
    report = (
        f"ANALYSIS_UNAVAILABLE: The {label} Analyst did not produce a validated "
        "structured evidence report. No directional conclusion was issued."
    )
    return AnalystSubmissionResult(
        message=AIMessage(content=report, response_metadata={
            "analyst_submission_diagnostic": {
                "version": 1, "analyst": analyst, "reason_class": reason,
                "stage": stage, "attempts": attempts,
                **safe_output_diagnostics(diagnostics),
            },
        }),
        report=report,
        submission_source=_submission_source(
            analyst,
            EvidenceStatus.UNAVAILABLE,
            reason,
        ),
    )


def finalize_observed_unavailable_news(
    messages: list[Any], ticker: str, trade_date: str,
) -> AnalystSubmissionResult | None:
    """Submit a coverage limitation only after the scoped issuer query actually returned."""
    paired = _paired_tool_turns(messages)
    if paired is None:
        return None
    calls, results = paired
    first = (date.fromisoformat(trade_date) - timedelta(days=7)).isoformat()
    issuer_unavailable = False
    for message in results:
        try:
            envelope = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
        except ValidationError:
            return None
        if (
            envelope.selected_artifact is not None
            or envelope.capability != message.name
            or _source_ref(message, ticker, trade_date, calls) != envelope.source_ref
        ):
            return None
        if message.name == "get_news":
            args = calls[message.tool_call_id]["args"]
            window = (args.get("ticker"), args.get("start_date"), args.get("end_date"))
            if envelope.source_ref != stable_acquisition_source_ref("news", *window):
                return None
            if window == (ticker, first, trade_date):
                issuer_unavailable = True
    if not issuer_unavailable:
        return None

    report, claims, notes = _normalize_submission(
        AnalystEvidenceReport(
            report_markdown="本次查询未取得可核验的发行人新闻正文。覆盖不足，无法判断期间是否发生发行人事件。",
        ),
        "news",
    )
    record_output("news", "finalizer", 0, {}, outcome="deterministic_unavailable_news")
    return AnalystSubmissionResult(
        message=AIMessage(content=report), report=report, claims=claims, notes=notes,
        submission_source=_submission_source("news", EvidenceStatus.AVAILABLE, "observed_unavailable"),
    )


def finalize_analyst_submission(
    *,
    finalizer: Any | None,
    analyst: str,
    ticker: str,
    trade_date: str,
    draft: str = "",
    messages: list[Any],
    auxiliary_context: str = "",
    personal_statements: bool = False,
    personal_news_policy: bool = False,
    personal_market: bool = False,
) -> AnalystSubmissionResult:
    if finalizer is None:
        return _unavailable_result(analyst, "unsupported")
    if personal_market:
        from tradingagents.agents.utils.agent_utils import get_language_instruction

        observed = _personal_market_catalog(messages, ticker, trade_date)
        if observed is None or not observed[1]:
            return _unavailable_result(analyst, "incomplete_acquisition")
        source_ref, catalog = observed
        preview, _ = _collected_evidence_preview(messages, ticker, trade_date)
        prompt = (
            f"Finalize the market analyst report for {ticker} on {trade_date}. "
            "Return PersonalMarketReport. Choose one to six evidence IDs, each for one complete row. "
            "Material claims contain only evidence_id. The application attaches the exact observation. "
            "Do not repeat or calculate numbers in commentary. Summarize trends, risks and gaps. "
            "Use [] for evidence_notes unless an exact listed source quote establishes a limitation. "
            "Front-adjusted prices do not measure total return. "
            + get_language_instruction() + "\n\nVerified snapshot rows:\n"
            + "\n".join(f"{key} | {row.display}\n  Exact quote: {row.quote}" for key, row in catalog.items())
            + f"\nSource ref: {source_ref}"
            + "\n\nCollected market context for commentary and evidence notes only. "
            "Describe material gaps and window differences. A subwindow can have fewer rows "
            "than the full snapshot. Supplemental enhancements are advisory. "
            "Keep material claims limited to the selectable IDs above.\n" + preview
        )

        def validator(submitted):
            if not submitted.report_markdown.strip():
                raise SubmissionConstraintError("blank_report")
            selected = [entry.evidence_id for entry in submitted.material_claims]
            if any(key not in catalog for key in selected):
                raise SubmissionConstraintError("unknown_evidence_id")
            observations = [catalog[key] for key in selected]
            canonical = AnalystEvidenceReport(
                report_markdown=(
                    "## Advisory market commentary / 市场分析参考\n"
                    + submitted.report_markdown
                    + "\n\n## Verified snapshot observations / 已核验行情观测\n"
                    + "\n".join(f"- {row.display}" for row in observations)
                ),
                material_claims=tuple(
                    SubmittedMaterialClaim(
                        claim_id=f"market.{key}", statement=row.statement,
                        source_ref=source_ref, source_quote=row.quote,
                    )
                    for key, row in zip(selected, observations, strict=True)
                ),
                evidence_notes=submitted.evidence_notes,
            )
            return _normalize_submission(canonical, analyst)

    elif personal_statements:
        prompt, catalog = _personal_statement_prompt(messages, ticker, trade_date)
        def validator(submitted):
            return _normalize_personal_fundamentals(submitted, catalog)
    else:
        prompt = _finalization_prompt(
            analyst=analyst, ticker=ticker, trade_date=trade_date, draft=draft,
            messages=messages, auxiliary_context=auxiliary_context, personal_news_policy=personal_news_policy,
        )
        def validator(submitted):
            return _normalize_submission(submitted, analyst)
    structured_result = invoke_required_structured(
        finalizer,
        prompt,
        f"{analyst.title()} Analyst",
        validator=validator,
        personal_validation_details=personal_news_policy or personal_statements or personal_market,
    )
    if structured_result.value is None:
        return _unavailable_result(
            analyst,
            structured_result.reason or "none_parsed",
            attempts=structured_result.attempts,
            diagnostics=structured_result.diagnostics,
        )
    report, claims, notes = structured_result.value
    return AnalystSubmissionResult(
        message=AIMessage(content=report),
        report=report,
        claims=claims,
        notes=notes,
        submission_source=_submission_source(
            analyst,
            EvidenceStatus.AVAILABLE,
            "finalized_structured",
        ),
    )


def process_analyst_response(
    response: AIMessage,
    *,
    finalizer: Any | None,
    analyst: str,
    ticker: str,
    trade_date: str,
    messages: list[Any],
    auxiliary_context: str = "",
    acquisition_only: bool = False,
    personal_news_policy: bool = False,
) -> AnalystSubmissionResult:
    """Accept a typed report, continue data calls, or finalize prose exactly once."""
    truncated = response.response_metadata.get("finish_reason") == "length"
    diagnostics = response_output_diagnostics(response)
    record_output(
        analyst, "initial_response", 1, diagnostics,
        outcome="output_limit" if truncated else "response",
        personal_observation=("raw_finish_reason_length", diagnostics) if truncated else None,
    )
    if truncated:
        # The initial tool-calling turn has the same completeness requirement as
        # the finalizer. A parseable prefix is not a completed report/tool plan,
        # and an output-limit failure must not spend an automatic repair call.
        return _unavailable_result(
            analyst, "output_limit", stage="initial_response", attempts=1,
            diagnostics=diagnostics,
        )
    tool_calls = tuple(response.tool_calls or ())
    report_calls = tuple(
        call for call in tool_calls if call["name"] == _REPORT_TOOL_NAME
    )
    data_calls = tuple(
        call for call in tool_calls if call["name"] != _REPORT_TOOL_NAME
    )

    if data_calls:
        # A report emitted alongside data calls is premature. Remove only the
        # report pseudo-tool so the graph's ToolNode can execute every data call.
        return AnalystSubmissionResult(
            message=response.model_copy(update={"tool_calls": list(data_calls)}),
            report="",
        )

    if acquisition_only:
        return _unavailable_result(analyst, "incomplete_acquisition", stage="initial_response", attempts=1)

    if report_calls:
        try:
            report, claims, notes = _normalize_submission(report_calls[0]["args"], analyst)
        except (ValidationError, ValueError, TypeError):
            return finalize_analyst_submission(
                finalizer=finalizer,
                analyst=analyst,
                ticker=ticker,
                trade_date=trade_date,
                draft=str(response.content),
                messages=messages,
                auxiliary_context=auxiliary_context,
                personal_news_policy=personal_news_policy,
            )
        return AnalystSubmissionResult(
            message=AIMessage(content=report),
            report=report,
            claims=claims,
            notes=notes,
            submission_source=_submission_source(
                analyst,
                EvidenceStatus.AVAILABLE,
                "direct_tool",
            ),
        )

    return finalize_analyst_submission(
        finalizer=finalizer,
        analyst=analyst,
        ticker=ticker,
        trade_date=trade_date,
        draft=str(response.content),
        messages=messages,
        auxiliary_context=auxiliary_context,
        personal_news_policy=personal_news_policy,
    )


def _same_market_snapshot_observation(
    checkpointed: MarketSnapshotEvidence | None,
    active: MarketSnapshotEvidence,
) -> bool:
    if checkpointed is None:
        return False
    runtime_fields = {
        "retrieved_at",
        "physical_attempt_events",
        "physical_attempt_count",
    }
    return checkpointed.model_dump(
        mode="python", exclude=runtime_fields
    ) == active.model_dump(mode="python", exclude=runtime_fields)


def _route_multiyear_advisory_notes(state, result, evidence, active_snapshot, report_key):
    """Demote only exact application-generated limitations, never qualify them.

    Ordinary source notes and every material claim keep the original gate.
    Model paraphrases are discarded here, not certified or promoted to facts.
    """
    if (report_key != "market_report" or not result.notes
            or result.submission_source.source_id != "analyst.market.submission"
            or result.submission_source.status is not EvidenceStatus.AVAILABLE):
        return result
    from tradingagents.multiyear_trend import (
        TrendUnavailable,
        bound_multiyear_limitations,
        render_multiyear_trend,
    )

    try:
        summary, quotes = bound_multiyear_limitations(
            evidence, symbol=state["company_of_interest"], as_of=state["trade_date"],
            active_snapshot=active_snapshot,
        )
    except TrendUnavailable:
        return result
    if active_snapshot is not None and (
        active_snapshot.snapshot_id != summary.lineage.input_snapshot_id
        or active_snapshot.frame_sha256 != summary.lineage.input_artifact_sha256
        or active_snapshot.requested_date != summary.as_of
    ):
        return result
    protected_ids = {c.claim_id for c in (*evidence.material_claims, *result.claims)}
    protected_ids.update(n.note_id for n in evidence.evidence_notes)
    routed = tuple(n for n in result.notes if (
        n.analyst == "market" and n.note_id not in protected_ids
        and n.source_ref == summary.lineage.input_snapshot_id
        and (quotes.get(n.source_quote) == n.kind or (
            # Historical availability is also a valid label for this exact
            # regenerated temporal limitation. The model's label/paraphrase
            # is discarded, not certified; arbitrary notes retain the gate.
            n.kind == "availability" and quotes.get(n.source_quote) == "applicability"
            and n.source_quote.startswith(("- 时间证据:", "- Temporal evidence:"))
        ))
    ))
    if not routed:
        return result
    from tradingagents.dataflows.config import get_config
    from tradingagents.report_presentation import is_chinese

    language = get_config().get("output_language", "English")
    zh = is_chinese(language)
    lines = [line for line in render_multiyear_trend(summary, language).splitlines() if line in quotes]
    title = "### 确定性辅助说明（不属于决定证据）" if zh else "### Deterministic advisory limitations (not decision evidence)"
    notice = ("以下说明由当前绑定快照重新生成；模型附带的转述未获事实认证，不进入评分。" if zh else
              "Regenerated from the current bound snapshot; model paraphrases are not certified facts and do not enter scoring.")
    appendix = "\n".join([title, notice, f"{summary.symbol} / {summary.as_of}",
        f"Snapshot: `{summary.lineage.input_snapshot_id}`",
        f"SHA-256: `{summary.lineage.input_artifact_sha256}`", *lines])
    report = result.report + "\n\n" + appendix
    return replace(result, report=report, message=result.message.model_copy(update={"content": report}),
                   notes=tuple(n for n in result.notes if n not in routed))


def build_analyst_update(
    state: Mapping[str, Any],
    result: AnalystSubmissionResult,
    report_key: str,
    *,
    personal_statements: bool = False,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "messages": [result.message],
        report_key: result.report,
    }
    if result.submission_source is None:
        return update
    evidence = EvidenceState.model_validate(state.get("evidence_state", {}))
    active_snapshot = get_active_authoritative_market_snapshot(
        str(state["company_of_interest"]), str(state["trade_date"]),
    )
    result = _route_multiyear_advisory_notes(state, result, evidence, active_snapshot, report_key)
    update.update({"messages": [result.message], report_key: result.report})
    call_ids_by_source = source_ref_tool_call_ids(
        state["messages"],
        str(state["company_of_interest"]),
        str(state["trade_date"]),
        include_unavailable_financial=True,
    )
    tool_calls_by_id = _tool_calls_by_id(state["messages"])
    tool_evidence = build_tool_evidence_state(
        state["messages"],
        result.claims,
        tool_call_ids_by_source=call_ids_by_source,
        tool_calls_by_id=tool_calls_by_id,
        instrument_identity=evidence.instrument_identity,
        notes=result.notes,
        include_catalog_sources=True,
    )
    tool_evidence = _apply_deterministic_history_requirements(tool_evidence)
    if active_snapshot is not None:
        history_access, history_diagnostic = project_history_store_access(
            active_snapshot.history_store_status,
            active_snapshot.history_store_diagnostic,
        )
        active_snapshot_evidence = MarketSnapshotEvidence(
                    symbol=active_snapshot.symbol,
                    provider=active_snapshot.provider,
                    retrieved_at=active_snapshot.retrieved_at,
                    adjustment_basis=active_snapshot.adjustment_basis,
                    requested_date=active_snapshot.requested_date,
                    effective_trading_date=active_snapshot.effective_trading_date,
                    history_rows=len(active_snapshot.frame),
                    frame_sha256=active_snapshot.frame_sha256,
                    snapshot_id=active_snapshot.snapshot_id,
                    snapshot_id_version=active_snapshot.snapshot_id_version,
                    pin_membership_digest=active_snapshot.pin_membership_digest,
                    snapshot_manifest_json=active_snapshot.snapshot_manifest_json,
                    current_tradeability=active_snapshot.current_tradeability,
                    current_status_provenance=(
                        {
                            "provider": active_snapshot.current_status_provenance.provider,
                            "provider_dataset_id": (
                                active_snapshot.current_status_provenance.provider_dataset_id
                            ),
                            "session_date": (
                                active_snapshot.current_status_provenance.session_date.isoformat()
                            ),
                            "status": active_snapshot.current_status_provenance.status.value,
                            "observed_at": (
                                active_snapshot.current_status_provenance.observed_at.isoformat()
                            ),
                            "revision_id": (
                                active_snapshot.current_status_provenance.revision_id
                            ),
                        }
                        if active_snapshot.current_status_provenance is not None
                        else None
                    ),
                    latest_traded_close=active_snapshot.latest_traded_close,
                    latest_traded_close_diagnostic=(
                        active_snapshot.latest_traded_close_diagnostic
                    ),
                    carried_suspension_close=(
                        active_snapshot.carried_suspension_close
                    ),
                    history_gap_dates=active_snapshot.history_gap_dates,
                    history_store_status=history_access,
                    history_store_diagnostic=history_diagnostic,
                    physical_attempt_events=tuple(
                        {
                            "attempt_event_id": event.attempt_event_id,
                            "sequence_id": event.sequence_id,
                            "request_key": event.request_key,
                            "upstream_service_id": event.upstream_service_id,
                            "upstream_service_name": event.upstream_service_name,
                            "attempt_index": event.attempt_index,
                            "attempted_at": event.attempted_at.isoformat(),
                            "pacing_event": event.pacing_event,
                            "pacing_wait_seconds": event.pacing_wait_seconds,
                            "outcome": event.outcome.value,
                            "retryable": event.retryable,
                            "status_code": event.status_code,
                            "error_code": event.error_code,
                            "retry_after_seconds": event.retry_after_seconds,
                            "cooldown_changed": event.cooldown_changed,
                            "cooldown_until": (
                                event.cooldown_until.isoformat()
                                if event.cooldown_until is not None
                                else None
                            ),
                            "final_physical_attempt_count": (
                                event.final_physical_attempt_count
                            ),
                        }
                        for event in active_snapshot.physical_attempt_events
                    ),
                    physical_attempt_count=len(
                        active_snapshot.physical_attempt_events
                    ),
                )
        if not _same_market_snapshot_observation(
            evidence.market_snapshot,
            active_snapshot_evidence,
        ):
            evidence = evidence.model_copy(
                update={"market_snapshot": active_snapshot_evidence}
            )
            snapshot_record = get_active_market_snapshot_acquisition_record(
                str(state["company_of_interest"]),
                str(state["trade_date"]),
            )
            if snapshot_record is not None:
                snapshot_artifacts = tuple(
                    artifact
                    for artifact in (
                        snapshot_record.source_artifact,
                        snapshot_record.status_artifact,
                    )
                    if artifact is not None
                )
                if snapshot_artifacts:
                    evidence = merge_source_artifacts(
                        evidence, snapshot_artifacts
                    )
                evidence = merge_source_acquisition_outcomes(
                    evidence, snapshot_record.outcomes
                )
        evidence = refresh_active_evidence_physical_attempts(evidence)
    try:
        # Capture source necessity from policy/state, never from a model note.
        # This projection is independent of whether the model cited the source.
        evidence = merge_evidence_sources(evidence, tool_evidence.sources)
        sources = {source.source_id: source for source in evidence.sources}
        notes = tuple(note.model_copy(update={
            "source_status": sources[note.source_ref].status,
            "source_required": sources[note.source_ref].required,
        }) for note in tool_evidence.evidence_notes)
        evidence = merge_material_claims(
            evidence,
            tool_evidence.material_claims,
        )
        evidence = merge_source_facts(evidence, tool_evidence.source_facts)
        evidence = merge_source_artifacts(
            evidence,
            tool_evidence.source_artifacts,
        )
        evidence = merge_source_acquisition_outcomes(
            evidence,
            tool_evidence.acquisition_outcomes,
        )
        evidence = merge_claim_validations(evidence, tool_evidence.claim_validations)
        evidence = merge_evidence_notes(evidence, notes)
    except ValueError:
        conflicted_submission = result.submission_source.model_copy(
            update={
                "status": EvidenceStatus.CONFLICTED,
                "detail": "immutable claim or fact ID was redefined",
            }
        )
        update["evidence_state"] = merge_evidence_sources(
            evidence,
            (conflicted_submission,),
        ).model_dump(mode="json")
        return update
    submission_source = result.submission_source
    if any(note.binding_status != "source_bound_quote" for note in notes):
        submission_source = submission_source.model_copy(update={
            "status": EvidenceStatus.UNAVAILABLE,
            "detail": "evidence note quote is not uniquely source-bound",
        })
    update["evidence_state"] = merge_evidence_sources(
        evidence,
        (submission_source,),
    ).model_dump(mode="json")
    financial_refs = {source_ref for source_ref, call_ids in call_ids_by_source.items()
                      if any(tool_calls_by_id.get(call_id, {}).get("name") in FINANCIAL_TOOLS
                             for call_id in call_ids)}
    if (any(fact.tool_name in FINANCIAL_TOOLS for fact in tool_evidence.source_facts)
            or any(ref in financial_refs for claim in result.claims for ref in claim.source_refs)):
        observations = verified_financial_observations(
            update["evidence_state"], claim_ids=(claim.claim_id for claim in result.claims))
        freshness = {}
        if personal_statements:
            accepted = {observation.observation_id for observation in observations}
            freshness = {
                fact.financial_observation.observation_id: verified_row_freshness(fact.raw_text)
                for fact in tool_evidence.source_facts
                if fact.financial_observation and fact.financial_observation.observation_id in accepted
            }
        rendered = "\n".join(
            "- " + observation.render() + freshness.get(observation.observation_id, "")
            for observation in observations
        )
        update[report_key] = (
            "## Advisory commentary / 咨询性文字\n"
            "Source quotation support does not verify this prose's financial meaning. "
            "引用一致不等于字段、币种或期间语义已核验。\n\n"
            + result.report
            + "\n\n## Source-bound CURRENT observations / 来源绑定的财务观测\n"
            "Only the following source-row values are deterministically bound. "
            "They are not PIT evidence, canonical facts or investment-qualified premises; "
            "quarterly income/cashflow retain cumulative report_type=1 semantics.\n"
            + (rendered or "- No uniquely bound complete financial row / 无唯一完整财务行绑定。")
        )
    return update
