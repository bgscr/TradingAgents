"""Deterministic terminal, coverage, and export contracts.

The graph may finish with either a validated Trading Decision or a
non-directional Analysis Outcome.  This module keeps that distinction separate
from operational lifecycle state and supplies the canonical identity used by
audits and exported reports.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.agents.managers.direction_selector import render_trading_decision
from tradingagents.decision_policy import (
    DECISION_POLICY_CONTRACT_VERSION,
    DecisionGateResultV2,
    EvidenceIntegrityStatus,
    TradingDecisionContract,
    ValidatedDecisionContext,
)
from tradingagents.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    AnalysisOutcome,
    EvidenceState,
    EvidenceStatus,
    SourceArtifact,
    SourceFact,
    render_analysis_outcome,
)

TERMINAL_CONTRACT_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_NON_SEMANTIC_CONFIG_KEYS = frozenset(
    {
        "callbacks",
        "data_cache_dir",
        "data_dir",
        "results_dir",
    }
)
_SENSITIVE_KEY_PARTS = ("api_key", "credential", "password", "secret", "token")


class RunLifecycleStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class TerminalOutcomeKind(str, Enum):
    TRADING_DECISION = "trading_decision"
    ANALYSIS_OUTCOME = "analysis_outcome"
    OPERATIONAL_FAILURE = "operational_failure"


class OperationalErrorCategory(str, Enum):
    CONFIGURATION = "configuration"
    ACQUISITION = "acquisition"
    GRAPH_EXECUTION = "graph_execution"
    REPORT_PUBLICATION = "report_publication"
    UNKNOWN = "unknown"


class CoverageMeasure(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    covered_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    ratio: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ratio_matches_counts(self) -> CoverageMeasure:
        if self.covered_count > self.total_count:
            raise ValueError("covered count cannot exceed total count")
        expected = self.covered_count / self.total_count if self.total_count else 0.0
        if abs(self.ratio - expected) > 1e-12:
            raise ValueError("coverage ratio does not match its counts")
        return self


class DecisionCoverage(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    source_availability: CoverageMeasure
    validated_facts: CoverageMeasure
    decision_assertions: CoverageMeasure


class TerminalContract(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = TERMINAL_CONTRACT_VERSION
    lifecycle_status: RunLifecycleStatus
    terminal_outcome_kind: TerminalOutcomeKind
    evidence_integrity_status: EvidenceIntegrityStatus
    active_phase: None = None
    coverage: DecisionCoverage
    trading_decision_id: str | None = None
    analysis_outcome_sha256: str | None = None
    operational_error_category: OperationalErrorCategory | None = None

    @model_validator(mode="after")
    def _validate_terminal_shape(self) -> TerminalContract:
        if self.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
            if self.lifecycle_status is not RunLifecycleStatus.COMPLETED:
                raise ValueError("a Trading Decision must be a completed run")
            if not self.trading_decision_id:
                raise ValueError("a Trading Decision requires its decision ID")
            if self.analysis_outcome_sha256 is not None:
                raise ValueError("a Trading Decision cannot carry an Analysis Outcome")
            if self.operational_error_category is not None:
                raise ValueError("a Trading Decision cannot carry an operational error")
            if self.evidence_integrity_status not in {
                EvidenceIntegrityStatus.DECISION_READY,
                EvidenceIntegrityStatus.DEGRADED,
            }:
                raise ValueError("a Trading Decision requires decision-ready evidence")
            if self.coverage.decision_assertions.ratio != 1.0:
                raise ValueError("a Trading Decision requires complete assertion coverage")
        elif self.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME:
            if self.lifecycle_status is not RunLifecycleStatus.COMPLETED:
                raise ValueError("an Analysis Outcome must be a completed run")
            if not self.analysis_outcome_sha256:
                raise ValueError("an Analysis Outcome requires its content digest")
            if self.trading_decision_id is not None:
                raise ValueError("an Analysis Outcome cannot carry a decision ID")
            if self.operational_error_category is not None:
                raise ValueError("an Analysis Outcome cannot carry an operational error")
        else:
            if self.lifecycle_status is not RunLifecycleStatus.FAILED:
                raise ValueError("an operational failure must have failed lifecycle status")
            if self.operational_error_category is None:
                raise ValueError("an operational failure requires a typed error category")
            if self.trading_decision_id or self.analysis_outcome_sha256:
                raise ValueError("an operational failure cannot carry a terminal result")
        return self


class CanonicalRunIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = TERMINAL_CONTRACT_VERSION
    run_id: str = Field(pattern=r"^run:[0-9a-f]{64}$")
    configuration_digest: str = Field(pattern=r"^config:[0-9a-f]{64}$")
    evidence_contract_version: str = Field(min_length=1)
    decision_contract_version: str = Field(min_length=1)
    rule_registry_digest: str | None = None
    calculation_registry_digest: str | None = None
    audit_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def configuration_digest(config: Mapping[str, Any] | None) -> str:
    """Hash semantic configuration without retaining paths or secret material."""

    semantic: dict[str, Any] = {}
    for raw_key, value in (config or {}).items():
        key = str(raw_key)
        normalized_key = key.casefold()
        if key in _NON_SEMANTIC_CONFIG_KEYS:
            continue
        if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
            semantic[key] = "<redacted>"
        else:
            semantic[key] = value
    return f"config:{_digest(semantic)}"


def _measure(covered: int, total: int) -> CoverageMeasure:
    return CoverageMeasure(
        covered_count=covered,
        total_count=total,
        ratio=covered / total if total else 0.0,
    )


def _source_availability(evidence: EvidenceState) -> CoverageMeasure:
    grouped: dict[str, set[EvidenceStatus]] = {
        "authoritative.instrument_identity": {
            EvidenceStatus.AVAILABLE
            if evidence.instrument_identity is not None
            and evidence.instrument_identity.is_authoritative
            else EvidenceStatus.UNAVAILABLE
        },
        "authoritative.market_snapshot": {
            EvidenceStatus.AVAILABLE
            if evidence.market_snapshot is not None
            else EvidenceStatus.UNAVAILABLE
        },
    }
    for source in evidence.sources:
        if source.status is EvidenceStatus.NOT_APPLICABLE:
            continue
        grouped.setdefault(source.source_id, set()).add(source.status)
    available = sum(statuses == {EvidenceStatus.AVAILABLE} for statuses in grouped.values())
    return _measure(available, len(grouped))


def _artifact_catalog(
    evidence: EvidenceState,
) -> dict[tuple[str, str], SourceArtifact | None]:
    artifacts: dict[tuple[str, str], SourceArtifact | None] = {}
    for artifact in evidence.source_artifacts:
        key = (artifact.artifact_sha256, artifact.source_ref)
        prior = artifacts.get(key)
        if prior is not None and prior.raw_text != artifact.raw_text:
            artifacts[key] = None
        elif key not in artifacts:
            artifacts[key] = artifact
    return artifacts


def _fact_semantics(fact: SourceFact) -> dict[str, Any]:
    return fact.model_dump(
        mode="json",
        exclude={"tool_call_id", "tool_name"},
    )


def _fact_has_valid_artifact(
    fact: SourceFact,
    artifacts: Mapping[tuple[str, str], SourceArtifact | None],
) -> bool:
    artifact = artifacts.get((fact.artifact_sha256, fact.source_ref))
    if artifact is None:
        return False
    if sha256(artifact.raw_text.encode("utf-8")).hexdigest() != fact.artifact_sha256:
        return False
    if fact.source_span_end > len(artifact.raw_text):
        return False
    return artifact.raw_text[fact.source_span_start : fact.source_span_end] == fact.raw_text


def _required_fact_ids(
    state: Mapping[str, Any],
    decision: TradingDecisionContract | None,
) -> set[str]:
    context = _validated_context(state)
    assertions = context.assertions if context is not None else (
        decision.assertions if decision is not None else ()
    )
    return {
        fact_id
        for assertion in assertions
        for fact_id in assertion.fact_ids
    }


def _validated_fact_coverage(
    evidence: EvidenceState,
    required_fact_ids: set[str],
) -> CoverageMeasure:
    grouped: dict[str, list[SourceFact]] = {}
    for fact in evidence.source_facts:
        if fact.fact_id in required_fact_ids:
            grouped.setdefault(fact.fact_id, []).append(fact)
    artifacts = _artifact_catalog(evidence)
    valid = 0
    for fact_id in required_fact_ids:
        facts = grouped.get(fact_id, [])
        if not facts:
            continue
        first = facts[0]
        if first.fact_kind != "canonical":
            continue
        if any(_fact_semantics(fact) != _fact_semantics(first) for fact in facts[1:]):
            continue
        if _fact_has_valid_artifact(first, artifacts):
            valid += 1
    return _measure(valid, len(required_fact_ids))


def _validated_context(state: Mapping[str, Any]) -> ValidatedDecisionContext | None:
    payload = state.get("validated_decision_context")
    if payload is None:
        return None
    try:
        return ValidatedDecisionContext.model_validate(payload)
    except (TypeError, ValueError):
        return None


def _assertion_coverage(
    state: Mapping[str, Any],
    decision: TradingDecisionContract | None,
) -> CoverageMeasure:
    context = _validated_context(state)
    expected = (
        {assertion.assertion_id for assertion in context.assertions}
        if context is not None
        else set()
    )
    selected = (
        {assertion.assertion_id for assertion in decision.assertions}
        if decision is not None
        else set()
    )
    return _measure(len(expected & selected), len(expected))


def decision_coverage(
    evidence: EvidenceState,
    state: Mapping[str, Any],
    decision: TradingDecisionContract | None,
) -> DecisionCoverage:
    required_fact_ids = _required_fact_ids(state, decision)
    return DecisionCoverage(
        source_availability=_source_availability(evidence),
        validated_facts=_validated_fact_coverage(evidence, required_fact_ids),
        decision_assertions=_assertion_coverage(state, decision),
    )


def _integrity_from_state(state: Mapping[str, Any]) -> EvidenceIntegrityStatus:
    for key in (
        "decision_gate_v2",
        "decision_gate",
        "admission_gate",
        "evidence_preflight",
    ):
        payload = state.get(key)
        if not isinstance(payload, Mapping):
            continue
        raw = payload.get("integrity_status", payload.get("readiness"))
        try:
            return EvidenceIntegrityStatus(str(raw))
        except ValueError:
            continue
    return EvidenceIntegrityStatus.INSUFFICIENT


def validated_trading_decision_from_state(
    state: Mapping[str, Any],
) -> TradingDecisionContract | None:
    """Return the gate-authorized decision; never infer one from rendered prose."""

    payload = state.get("trading_decision")
    if payload is None:
        return None
    if state.get("evidence_gate_mode") == "shadow":
        raise ValueError(
            "shadow evidence mode is diagnostic-only and cannot validate a "
            "Trading Decision"
        )
    decision = TradingDecisionContract.model_validate(payload)
    gate_payload = state.get("decision_gate_v2") or state.get("decision_gate")
    gate = DecisionGateResultV2.model_validate(gate_payload)
    if not gate.permitted or gate.decision != decision:
        raise ValueError("trading_decision is not the decision authorized by its gate")
    if gate.integrity_status is not decision.integrity_status:
        raise ValueError("trading_decision integrity differs from its decision gate")

    context = _validated_context(state)
    if context is None:
        raise ValueError("trading_decision is missing its validated decision context")
    if (
        decision.context_id != context.context_id
        or decision.registry_digest != context.registry_digest
        or decision.calculation_registry_digest != context.calculation_registry_digest
        or decision.instrument != context.instrument
        or decision.as_of_date != context.as_of_date
        or decision.horizon != context.horizon
        or decision.integrity_status is not context.integrity_status
    ):
        raise ValueError("trading_decision metadata differs from its validated context")

    context_assertions = {
        assertion.assertion_id: assertion for assertion in context.assertions
    }
    decision_assertions = {
        assertion.assertion_id: assertion for assertion in decision.assertions
    }
    if (
        len(context_assertions) != len(context.assertions)
        or len(decision_assertions) != len(decision.assertions)
        or decision_assertions != context_assertions
    ):
        raise ValueError("trading_decision assertions differ from its validated context")

    context_facts = {fact.fact_id: fact for fact in context.facts}
    decision_facts = {fact.fact_id: fact for fact in decision.facts}
    referenced_fact_ids = {
        fact_id
        for assertion in context.assertions
        for fact_id in assertion.fact_ids
    }
    if (
        len(context_facts) != len(context.facts)
        or len(decision_facts) != len(decision.facts)
        or not referenced_fact_ids.issubset(context_facts)
        or decision_facts
        != {fact_id: context_facts[fact_id] for fact_id in referenced_fact_ids}
    ):
        raise ValueError("trading_decision facts differ from its validated context")

    rendered = render_trading_decision(decision)
    if state.get("final_trade_decision") != rendered:
        raise ValueError("final_trade_decision is not rendered from trading_decision")
    return decision


def authorized_trading_decision_from_state(
    final_state: Mapping[str, Any],
) -> TradingDecisionContract:
    """Return a decision only after terminal and immutable-audit publication."""

    if not isinstance(final_state, Mapping):
        raise TypeError("authorized decision publication must be a final-state mapping")
    decision = validated_trading_decision_from_state(final_state)
    if decision is None:
        raise ValueError("authorized final state has no Trading Decision contract")

    terminal = TerminalContract.model_validate(final_state.get("terminal_contract"))
    if (
        terminal.terminal_outcome_kind is not TerminalOutcomeKind.TRADING_DECISION
        or terminal.trading_decision_id != decision.decision_id
    ):
        raise ValueError("terminal contract does not authorize this Trading Decision")

    identity = CanonicalRunIdentity.model_validate(final_state.get("run_identity"))
    audit_digest = final_state.get("decision_audit_sha256")
    if not isinstance(audit_digest, str) or identity.audit_digest != audit_digest:
        raise ValueError("canonical run identity is not bound to the decision audit")
    if (
        final_state.get("run_id") != identity.run_id
        or final_state.get("configuration_digest") != identity.configuration_digest
    ):
        raise ValueError("canonical run identity differs from final-state publication")
    return decision


def build_terminal_contract(final_state: Mapping[str, Any]) -> TerminalContract:
    """Validate and classify a final state without inferring direction from prose."""

    evidence = EvidenceState.model_validate(final_state.get("evidence_state", {}))
    analysis_outcome = final_state.get("analysis_outcome")
    analysis_text = analysis_outcome.strip() if isinstance(analysis_outcome, str) else ""
    analysis_payload = final_state.get("analysis_outcome_contract")
    typed_outcome = None
    if analysis_payload is None:
        if analysis_text:
            raise ValueError(
                "terminal Analysis Outcome requires its typed Analysis Outcome contract"
            )
    else:
        typed_outcome = AnalysisOutcome.model_validate(analysis_payload)
        expected_render = render_analysis_outcome(typed_outcome)
        if analysis_outcome != expected_render:
            raise ValueError(
                "analysis_outcome is not rendered from its typed Analysis Outcome contract"
            )
    decision = validated_trading_decision_from_state(final_state)
    if analysis_text and decision is not None:
        raise ValueError("a terminal state cannot contain both terminal outcome kinds")
    if typed_outcome is not None:
        if final_state.get("final_trade_decision"):
            raise ValueError("an Analysis Outcome cannot publish final_trade_decision")
        return TerminalContract(
            lifecycle_status=RunLifecycleStatus.COMPLETED,
            terminal_outcome_kind=TerminalOutcomeKind.ANALYSIS_OUTCOME,
            evidence_integrity_status=EvidenceIntegrityStatus(
                typed_outcome.readiness.value
            ),
            coverage=decision_coverage(evidence, final_state, None),
            analysis_outcome_sha256=sha256(
                analysis_outcome.encode("utf-8")
            ).hexdigest(),
        )
    if decision is not None:
        return TerminalContract(
            lifecycle_status=RunLifecycleStatus.COMPLETED,
            terminal_outcome_kind=TerminalOutcomeKind.TRADING_DECISION,
            evidence_integrity_status=decision.integrity_status,
            coverage=decision_coverage(evidence, final_state, decision),
            trading_decision_id=decision.decision_id,
        )

    failure = final_state.get("operational_error")
    if isinstance(failure, Mapping):
        raw_category = failure.get("category", OperationalErrorCategory.UNKNOWN.value)
        try:
            category = OperationalErrorCategory(str(raw_category))
        except ValueError:
            category = OperationalErrorCategory.UNKNOWN
        return TerminalContract(
            lifecycle_status=RunLifecycleStatus.FAILED,
            terminal_outcome_kind=TerminalOutcomeKind.OPERATIONAL_FAILURE,
            evidence_integrity_status=_integrity_from_state(final_state),
            coverage=decision_coverage(evidence, final_state, None),
            operational_error_category=category,
        )
    raise ValueError(
        "terminal state has neither a validated Trading Decision, "
        "an Analysis Outcome, nor a typed operational failure"
    )


def _terminal_identity_payload(
    final_state: Mapping[str, Any],
    terminal: TerminalContract,
    config_digest: str,
) -> dict[str, Any]:
    decision_payload = final_state.get("trading_decision")
    rule_digest = None
    calculation_digest = None
    if isinstance(decision_payload, Mapping):
        rule_digest = decision_payload.get("registry_digest")
        calculation_digest = decision_payload.get("calculation_registry_digest")
    context_payload = final_state.get("validated_decision_context")
    if isinstance(context_payload, Mapping):
        rule_digest = rule_digest or context_payload.get("registry_digest")
        calculation_digest = calculation_digest or context_payload.get(
            "calculation_registry_digest"
        )
    return {
        "ticker": final_state.get("company_of_interest"),
        "trade_date": final_state.get("trade_date"),
        "asset_type": final_state.get("asset_type"),
        "graph_signature": final_state.get("graph_signature"),
        "configuration_digest": config_digest,
        "terminal": terminal.model_dump(mode="json"),
        "rule_registry_digest": rule_digest,
        "calculation_registry_digest": calculation_digest,
    }


def build_run_identity(
    final_state: Mapping[str, Any],
    terminal: TerminalContract,
    *,
    config: Mapping[str, Any] | None = None,
    audit_digest: str | None = None,
) -> CanonicalRunIdentity:
    configured_digest = final_state.get("configuration_digest")
    if not isinstance(configured_digest, str) or not configured_digest.startswith(
        "config:"
    ):
        fallback_config = config or {
            "asset_type": final_state.get("asset_type"),
            "evidence_gate_mode": final_state.get("evidence_gate_mode", "enforce"),
            "graph_signature": final_state.get("graph_signature"),
        }
        configured_digest = configuration_digest(fallback_config)
    identity_payload = _terminal_identity_payload(
        final_state,
        terminal,
        configured_digest,
    )
    decision_payload = final_state.get("trading_decision")
    context_payload = final_state.get("validated_decision_context")
    rule_digest = None
    calculation_digest = None
    if isinstance(decision_payload, Mapping):
        rule_digest = decision_payload.get("registry_digest")
        calculation_digest = decision_payload.get("calculation_registry_digest")
    if isinstance(context_payload, Mapping):
        rule_digest = rule_digest or context_payload.get("registry_digest")
        calculation_digest = calculation_digest or context_payload.get(
            "calculation_registry_digest"
        )
    return CanonicalRunIdentity(
        run_id=f"run:{_digest(identity_payload)}",
        configuration_digest=configured_digest,
        evidence_contract_version=EVIDENCE_CONTRACT_VERSION,
        decision_contract_version=DECISION_POLICY_CONTRACT_VERSION,
        rule_registry_digest=str(rule_digest) if rule_digest else None,
        calculation_registry_digest=(
            str(calculation_digest) if calculation_digest else None
        ),
        audit_digest=audit_digest,
    )


def apply_terminal_contract(
    final_state: dict[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    audit_digest: str | None = None,
) -> tuple[TerminalContract, CanonicalRunIdentity]:
    """Attach the shared terminal and canonical run identity fields to state."""

    if config is not None:
        final_state["configuration_digest"] = configuration_digest(config)
    terminal = build_terminal_contract(final_state)
    identity = build_run_identity(
        final_state,
        terminal,
        config=config,
        audit_digest=audit_digest,
    )
    final_state.update(
        {
            "terminal_contract": terminal.model_dump(mode="json"),
            "lifecycle_status": terminal.lifecycle_status.value,
            "terminal_outcome_kind": terminal.terminal_outcome_kind.value,
            "evidence_integrity_status": terminal.evidence_integrity_status.value,
            "active_phase": None,
            "run_identity": identity.model_dump(mode="json"),
            "run_id": identity.run_id,
            "configuration_digest": identity.configuration_digest,
        }
    )
    if audit_digest is not None:
        final_state["decision_audit_sha256"] = audit_digest
    return terminal, identity
