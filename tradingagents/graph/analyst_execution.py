import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from langchain_core.messages import AIMessage

from tradingagents.evidence import (
    EvidenceCapability,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    capability_profile_for,
)


@dataclass(frozen=True)
class AnalystNodeSpec:
    key: str
    agent_node: str
    clear_node: str
    tool_node: str
    report_key: str
    required_capabilities: tuple[EvidenceCapability, ...] = ()


@dataclass(frozen=True)
class AnalystExecutionPlan:
    specs: list[AnalystNodeSpec]


ANALYST_NODE_SPECS: dict[str, AnalystNodeSpec] = {
    "market": AnalystNodeSpec(
        key="market",
        agent_node="Market Analyst",
        clear_node="Msg Clear Market",
        tool_node="tools_market",
        report_key="market_report",
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
    ),
    "social": AnalystNodeSpec(
        # Wire key stays "social" for saved-config back-compat; the
        # user-facing label is "Sentiment Analyst" to match the rename
        # that landed in v0.2.5 (sentiment_analyst now ingests news +
        # StockTwits + Reddit, not just social media).
        key="social",
        agent_node="Sentiment Analyst",
        clear_node="Msg Clear Sentiment",
        tool_node="tools_social",
        report_key="sentiment_report",
    ),
    "news": AnalystNodeSpec(
        key="news",
        agent_node="News Analyst",
        clear_node="Msg Clear News",
        tool_node="tools_news",
        report_key="news_report",
    ),
    "fundamentals": AnalystNodeSpec(
        key="fundamentals",
        agent_node="Fundamentals Analyst",
        clear_node="Msg Clear Fundamentals",
        tool_node="tools_fundamentals",
        report_key="fundamentals_report",
        required_capabilities=(EvidenceCapability.COMPANY_FINANCIALS,),
    ),
}


def create_capability_guarded_analyst_node(
    spec: AnalystNodeSpec,
    analyst_factory: Callable[[], Callable[[dict[str, Any]], dict[str, Any]]],
):
    """Create a lazy analyst node governed only by authoritative run evidence."""

    analyst_node = None

    def guarded_node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal analyst_node
        evidence = EvidenceState()
        try:
            raw_evidence = state.get("evidence_state", {})
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, EvidenceState)
                else EvidenceState.model_validate_json(json.dumps(raw_evidence or {}))
            )
            identity = evidence.instrument_identity
            if identity is None or not identity.is_authoritative:
                raise ValueError("authoritative instrument identity is unavailable")
            profile = capability_profile_for(identity.instrument_kind)
        except (TypeError, ValueError):
            status = EvidenceStatus.UNAVAILABLE
            required = True
            report = (
                "ANALYSIS_UNAVAILABLE: authoritative instrument identity and a "
                "registered capability profile are required for analyst routing."
            )
            detail = "authoritative identity or capability profile unavailable"
        else:
            applicable = (
                spec.key in profile.applicable_analysts
                and set(spec.required_capabilities).issubset(profile.all_capabilities)
            )
            if applicable:
                if analyst_node is None:
                    analyst_node = analyst_factory()
                return analyst_node(state)
            status = EvidenceStatus.NOT_APPLICABLE
            required = False
            report = (
                f"NOT_APPLICABLE: {spec.agent_node} is not applicable under "
                f"capability profile {profile.profile_id}."
            )
            detail = f"not applicable under capability profile {profile.profile_id}"

        source_id = f"analyst.{spec.key}.submission"
        source = EvidenceSource(
            source_id=source_id,
            status=status,
            required=required,
            detail=detail,
        )
        updated = evidence.model_copy(
            update={
                "sources": tuple(
                    existing
                    for existing in evidence.sources
                    if existing.source_id != source_id
                )
                + (source,)
            }
        )
        return {
            "messages": [AIMessage(content=report)],
            spec.report_key: report,
            "evidence_state": updated.model_dump(mode="json"),
        }

    return guarded_node


def build_analyst_execution_plan(
    selected_analysts: Iterable[str],
) -> AnalystExecutionPlan:
    specs: list[AnalystNodeSpec] = []
    seen: set[str] = set()
    for analyst_key in selected_analysts:
        if analyst_key in seen:
            raise ValueError(f"duplicate analyst key: {analyst_key}")
        seen.add(analyst_key)
        spec = ANALYST_NODE_SPECS.get(analyst_key)
        if spec is None:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        specs.append(spec)

    if not specs:
        raise ValueError("at least one analyst must be selected")

    return AnalystExecutionPlan(specs=specs)


def get_initial_analyst_node(plan: AnalystExecutionPlan) -> str:
    return plan.specs[0].agent_node


class AnalystWallTimeTracker:
    def __init__(self, plan: AnalystExecutionPlan):
        self.plan = plan
        self._started_at: dict[str, float] = {}
        self._wall_times: dict[str, float] = {}

    def mark_started(self, analyst_key: str, started_at: float | None = None) -> None:
        if analyst_key not in ANALYST_NODE_SPECS:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        self._started_at.setdefault(analyst_key, monotonic() if started_at is None else started_at)

    def mark_completed(
        self,
        analyst_key: str,
        completed_at: float | None = None,
    ) -> None:
        if analyst_key not in ANALYST_NODE_SPECS:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        if analyst_key in self._wall_times:
            return
        started_at = self._started_at.get(analyst_key)
        if started_at is None:
            return
        finished_at = monotonic() if completed_at is None else completed_at
        self._wall_times[analyst_key] = max(0.0, finished_at - started_at)

    def get_wall_times(self) -> dict[str, float]:
        return dict(self._wall_times)

    def format_summary(self) -> str:
        parts = []
        for spec in self.plan.specs:
            duration = self._wall_times.get(spec.key)
            if duration is not None:
                label = spec.agent_node.removesuffix(" Analyst")
                parts.append(f"{label} {duration:.2f}s")
        if not parts:
            return "Analyst wall time: pending"
        return "Analyst wall time: " + " | ".join(parts)


def sync_analyst_tracker_from_chunk(
    tracker: AnalystWallTimeTracker,
    chunk: dict[str, str],
    now: float | None = None,
) -> None:
    current_time = monotonic() if now is None else now
    active_found = False

    for spec in tracker.plan.specs:
        has_report = bool(chunk.get(spec.report_key))

        if has_report:
            tracker.mark_started(spec.key, started_at=current_time)
            tracker.mark_completed(spec.key, completed_at=current_time)
            continue

        if not active_found:
            tracker.mark_started(spec.key, started_at=current_time)
            active_found = True
