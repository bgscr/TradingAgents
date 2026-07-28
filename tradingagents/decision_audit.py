"""Immutable, deterministic audit records for terminal analysis outcomes."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tradingagents.asset_configuration import RunAssetConfigurationProjection
from tradingagents.decision_policy import (
    DirectionSelection,
    TradingDecisionContract,
    ValidatedDecisionContext,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    EvidenceReadiness,
    EvidenceState,
)
from tradingagents.evidence_artifacts import (
    AuditEvidenceProjection,
    persist_source_artifacts,
    project_evidence_for_audit,
)
from tradingagents.run_telemetry import RunTelemetryProjection
from tradingagents.terminal_contract import (
    TerminalOutcomeKind,
    apply_terminal_contract,
    canonical_json_bytes,
)


class _AssetConfigurationFailureProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    reason: str
    diagnostic_code: str


class _AuditValidatedDecisionFact(BaseModel):
    """Safe, artifact-addressable projection of a decision fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    artifact_ref: str


class _AuditValidatedDecisionContext(BaseModel):
    """Safe projection of the direction-selection input contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    context_id: str
    evidence_contract_version: str
    registry_digest: str
    calculation_registry_digest: str
    instrument: dict[str, Any]
    capability_profile_id: str
    as_of_date: str
    horizon: dict[str, Any]
    facts: tuple[_AuditValidatedDecisionFact, ...]
    assertion_ids: tuple[str, ...]
    integrity_status: str


class _AuditDecisionAssertion(BaseModel):
    """Safe, rule-backed projection of a trading-decision assertion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str
    rule_id: str
    rule_version: str
    fact_ids: tuple[str, ...]
    target_rating: str
    polarity: str
    horizon: dict[str, Any]
    predicate_id: str
    comparator: str
    threshold: str | None
    evaluation_digest: str


class _AuditTradingDecision(BaseModel):
    """Closed audit projection that excludes decision-driving source details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    decision_id: str
    context_id: str
    registry_digest: str
    calculation_registry_digest: str
    instrument: dict[str, Any]
    as_of_date: str
    horizon: dict[str, Any]
    rating: str
    facts: tuple[_AuditValidatedDecisionFact, ...]
    assertions: tuple[_AuditDecisionAssertion, ...]
    integrity_status: str


class _AuditDirectionSelectionDiagnostics(BaseModel):
    """Closed projection of model-neutral selection failure diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    reason: str


def _canonical_json(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _project_gate_diagnostics(
    value: Any,
    *,
    status_field: str,
    diagnostic_field: str,
    readiness_field: str = "readiness",
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("decision audit gate payload must be a mapping")
    if not value:
        return None
    status = value.get(status_field)
    if not isinstance(status, bool):
        raise ValueError(f"decision audit gate {status_field} must be boolean")
    readiness = EvidenceReadiness(str(value.get(readiness_field))).value
    raw_diagnostics = value.get(diagnostic_field, ())
    if not isinstance(raw_diagnostics, (list, tuple)):
        raise ValueError("decision audit gate diagnostics must be a sequence")
    raw_codes = value.get("diagnostic_codes")
    if raw_codes is None:
        if raw_diagnostics:
            raise ValueError(
                "decision audit gate diagnostics require typed diagnostic_codes"
            )
        diagnostic_codes = ()
    else:
        if not isinstance(raw_codes, (list, tuple)):
            raise ValueError("decision audit gate diagnostic codes must be a sequence")
        diagnostic_codes = tuple(
            sorted(
                {AnalysisDiagnosticCode(str(code)) for code in raw_codes},
                key=lambda code: code.value,
            )
        )
    if raw_diagnostics and not diagnostic_codes:
        raise ValueError(
            "decision audit gate diagnostics require typed diagnostic_codes"
        )
    if status:
        permitted_codes = (
            {AnalysisDiagnosticCode.OPTIONAL_EVIDENCE_UNAVAILABLE}
            if status_field == "admitted"
            else set()
        )
        if any(code not in permitted_codes for code in diagnostic_codes):
            raise ValueError(
                f"decision audit gate {status_field}=true cannot carry blocker codes"
            )
    return {
        status_field: status,
        "readiness": readiness,
        "diagnostic_codes": [
            code.value for code in diagnostic_codes
        ],
    }


def _project_validated_decision_context(
    value: Any,
    evidence_projection: AuditEvidenceProjection,
) -> dict[str, Any] | None:
    if value is None:
        return None
    context = ValidatedDecisionContext.model_validate(value)
    artifact_by_fact_id = {
        fact.fact_id: fact.artifact_ref
        for fact in evidence_projection.source_facts
    }
    facts: list[_AuditValidatedDecisionFact] = []
    for fact in context.facts:
        expected_artifact_ref = f"artifact=sha256:{fact.artifact_sha256}"
        actual_artifact_ref = artifact_by_fact_id.get(fact.fact_id)
        if actual_artifact_ref is None:
            raise ValueError(
                "validated decision context fact is absent from evidence projection: "
                f"{fact.fact_id}"
            )
        if actual_artifact_ref != expected_artifact_ref:
            raise ValueError(
                "validated decision context fact artifact does not match evidence "
                f"projection: {fact.fact_id}"
            )
        facts.append(
            _AuditValidatedDecisionFact(
                fact_id=fact.fact_id,
                artifact_ref=actual_artifact_ref,
            )
        )
    return _AuditValidatedDecisionContext(
        contract_version=context.contract_version,
        context_id=context.context_id,
        evidence_contract_version=context.evidence_contract_version,
        registry_digest=context.registry_digest,
        calculation_registry_digest=context.calculation_registry_digest,
        instrument=context.instrument.model_dump(mode="json"),
        capability_profile_id=context.capability_profile_id,
        as_of_date=context.as_of_date.isoformat(),
        horizon={
            "count": context.horizon.count,
            "unit": context.horizon.unit.value,
        },
        facts=tuple(sorted(facts, key=lambda fact: fact.fact_id)),
        assertion_ids=tuple(sorted(assertion.assertion_id for assertion in context.assertions)),
        integrity_status=context.integrity_status.value,
    ).model_dump(mode="json")


def _project_direction_selection(
    value: Any,
    validated_context: Any,
) -> dict[str, Any] | None:
    """Publish the closed direction-selection contract in canonical order."""

    if value is None:
        return None
    selection = DirectionSelection.model_validate(value)
    if validated_context is None:
        raise ValueError("direction selection requires validated decision context")
    context = ValidatedDecisionContext.model_validate(validated_context)
    if selection.context_id != context.context_id:
        raise ValueError("direction selection context mismatch")
    if tuple(sorted(selection.assertion_ids)) != tuple(
        sorted(assertion.assertion_id for assertion in context.assertions)
    ):
        raise ValueError("direction selection assertion coverage mismatch")
    return {
        "contract_version": selection.contract_version,
        "context_id": selection.context_id,
        "rating": selection.rating.value,
        "assertion_ids": sorted(selection.assertion_ids),
    }


def _project_direction_selection_diagnostics(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("direction selection diagnostics must be a mapping")
    status = value.get("status")
    reason = value.get("reason")
    safe_reasons = {
        "direction_context_invalid",
        "validated_decision_context_invalid",
        "direction_assertion_ids_duplicate",
        "direction_assertions_target_multiple_ratings",
        "direction_selection_invalid",
        "direction_selection_unavailable",
        "none_parsed",
        "validation_error",
        "transport_error",
    }
    if status != "blocked" or reason not in safe_reasons:
        raise ValueError("direction selection diagnostics are not publication-safe")
    return _AuditDirectionSelectionDiagnostics(
        status="blocked",
        reason=str(reason),
    ).model_dump(mode="json")


def _project_trading_decision(
    value: Any,
    evidence_projection: AuditEvidenceProjection,
) -> dict[str, Any] | None:
    """Publish only artifact-addressable facts and rule-backed assertions."""

    if value is None:
        return None
    decision = TradingDecisionContract.model_validate(value)
    artifact_by_fact_id = {
        fact.fact_id: fact.artifact_ref
        for fact in evidence_projection.source_facts
    }
    facts: list[_AuditValidatedDecisionFact] = []
    for fact in decision.facts:
        expected_artifact_ref = f"artifact=sha256:{fact.artifact_sha256}"
        actual_artifact_ref = artifact_by_fact_id.get(fact.fact_id)
        if actual_artifact_ref is None:
            raise ValueError(
                "trading decision fact is absent from evidence projection: "
                f"{fact.fact_id}"
            )
        if actual_artifact_ref != expected_artifact_ref:
            raise ValueError(
                "trading decision fact artifact does not match evidence projection: "
                f"{fact.fact_id}"
            )
        facts.append(
            _AuditValidatedDecisionFact(
                fact_id=fact.fact_id,
                artifact_ref=actual_artifact_ref,
            )
        )
    fact_ids = {fact.fact_id for fact in facts}
    assertions: list[_AuditDecisionAssertion] = []
    for assertion in decision.assertions:
        if not set(assertion.fact_ids).issubset(fact_ids):
            raise ValueError(
                "trading decision assertion references a fact absent from the "
                f"decision: {assertion.assertion_id}"
            )
        assertions.append(
            _AuditDecisionAssertion(
                assertion_id=assertion.assertion_id,
                rule_id=assertion.rule_id,
                rule_version=assertion.rule_version,
                fact_ids=tuple(sorted(assertion.fact_ids)),
                target_rating=assertion.target_rating.value,
                polarity=assertion.polarity.value,
                horizon={
                    "count": assertion.horizon.count,
                    "unit": assertion.horizon.unit.value,
                },
                predicate_id=assertion.predicate_id,
                comparator=assertion.comparator,
                threshold=(
                    None
                    if assertion.threshold is None
                    else format(assertion.threshold, "f")
                ),
                evaluation_digest=assertion.evaluation_digest,
            )
        )
    return _AuditTradingDecision(
        contract_version=decision.contract_version,
        decision_id=decision.decision_id,
        context_id=decision.context_id,
        registry_digest=decision.registry_digest,
        calculation_registry_digest=decision.calculation_registry_digest,
        instrument=decision.instrument.model_dump(mode="json"),
        as_of_date=decision.as_of_date.isoformat(),
        horizon={
            "count": decision.horizon.count,
            "unit": decision.horizon.unit.value,
        },
        rating=decision.rating.value,
        facts=tuple(sorted(facts, key=lambda fact: fact.fact_id)),
        assertions=tuple(sorted(assertions, key=lambda assertion: assertion.assertion_id)),
        integrity_status=decision.integrity_status.value,
    ).model_dump(mode="json")


def prepare_decision_audit(
    final_state: dict[str, Any],
    directory: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist source artifacts before authorizing a safe decision audit."""

    evidence = EvidenceState.model_validate(final_state.get("evidence_state", {}))
    from tradingagents.dataflows.market_snapshot import (
        refresh_active_evidence_physical_attempts,
    )

    evidence = refresh_active_evidence_physical_attempts(evidence)
    final_state["evidence_state"] = evidence.model_dump(mode="python")
    manifest = persist_source_artifacts(evidence, Path(directory))
    projection = project_evidence_for_audit(evidence, manifest)
    payload = build_decision_audit(
        final_state,
        config=config,
        _evidence_projection=projection,
    )
    final_state["evidence_audit_projection"] = projection.model_dump(mode="json")
    return payload


def build_decision_audit(
    final_state: dict[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    _evidence_projection: AuditEvidenceProjection | None = None,
) -> dict[str, Any]:
    """Build an audit only for a validated terminal contract."""

    if _evidence_projection is None:
        raise ValueError(
            "build_decision_audit requires persisted artifacts; "
            "use prepare_decision_audit"
        )
    terminal, run_identity = apply_terminal_contract(final_state, config=config)
    created_at = final_state.get("decision_audit_created_at")
    if not created_at:
        created_at = datetime.now(timezone.utc).isoformat()
        final_state["decision_audit_created_at"] = created_at
    EvidenceState.model_validate(final_state.get("evidence_state", {}))
    analysis_outcome_contract = final_state.get("analysis_outcome_contract")
    if analysis_outcome_contract is not None:
        analysis_outcome_contract = AnalysisOutcome.model_validate(
            analysis_outcome_contract
        ).model_dump(mode="json", exclude_none=True)
    terminal_output: Any
    if terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
        terminal_output = final_state["trading_decision"]
    elif terminal.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME:
        terminal_output = analysis_outcome_contract
    else:
        terminal_output = final_state["operational_error"]
    graph_signature = final_state.get("graph_signature")
    if not isinstance(graph_signature, str):
        graph_signature = None
    raw_asset_configuration = final_state.get("asset_configuration")
    asset_configuration = (
        None
        if raw_asset_configuration is None
        else RunAssetConfigurationProjection.model_validate(
            raw_asset_configuration
        )
    )
    if (
        asset_configuration is not None
        and graph_signature is not None
        and asset_configuration.asset_configuration_signature not in graph_signature
    ):
        raise ValueError(
            "graph signature does not commit to the run asset configuration"
        )
    raw_asset_failure = final_state.get("asset_configuration_failure")
    asset_configuration_failure = (
        None
        if raw_asset_failure is None
        else _AssetConfigurationFailureProjection.model_validate(raw_asset_failure)
    )
    if asset_configuration is not None and asset_configuration_failure is not None:
        raise ValueError("run cannot contain both asset configuration and failure")
    raw_telemetry = final_state.get("run_telemetry")
    telemetry = (
        RunTelemetryProjection.empty(
            terminal_route=terminal.terminal_outcome_kind.value,
        )
        if raw_telemetry is None
        else RunTelemetryProjection.model_validate(raw_telemetry)
    )
    if telemetry.terminal_route != terminal.terminal_outcome_kind.value:
        raise ValueError("run telemetry terminal route does not match terminal contract")
    payload: dict[str, Any] = {
        "schema_version": "4.0",
        "created_at": created_at,
        "run": {
            **run_identity.model_dump(mode="json", exclude={"audit_digest"}),
            "ticker": final_state.get("company_of_interest"),
            "trade_date": final_state.get("trade_date"),
            "asset_type": final_state.get("asset_type"),
            "graph_signature": graph_signature,
            "evidence_gate_mode": final_state.get("evidence_gate_mode", "enforce"),
        },
        "terminal": {
            **terminal.model_dump(mode="json"),
            "output_sha256": sha256(_canonical_json(terminal_output)).hexdigest(),
        },
        "telemetry": telemetry.model_dump(mode="json"),
        "asset_configuration": (
            asset_configuration.model_dump(mode="json")
            if asset_configuration is not None
            else None
        ),
        "asset_configuration_failure": (
            asset_configuration_failure.model_dump(mode="json")
            if asset_configuration_failure is not None
            else None
        ),
        "evidence_state": _evidence_projection.model_dump(mode="json"),
        "evidence_preflight": _project_gate_diagnostics(
            final_state.get("evidence_preflight"),
            status_field="passed",
            diagnostic_field="blockers",
        ),
        "admission_gate": _project_gate_diagnostics(
            final_state.get("admission_gate"),
            status_field="admitted",
            diagnostic_field="diagnostics",
        ),
        "validated_decision_context": _project_validated_decision_context(
            final_state.get("validated_decision_context"),
            _evidence_projection,
        ),
        "direction_selection": _project_direction_selection(
            final_state.get("direction_selection"),
            final_state.get("validated_decision_context"),
        ),
        "direction_selection_diagnostics": _project_direction_selection_diagnostics(
            final_state.get("direction_selection_diagnostics")
            or final_state.get("direction_selector_diagnostics")
        ),
        "decision_gate": _project_gate_diagnostics(
            final_state.get("decision_gate_v2") or final_state.get("decision_gate"),
            status_field="permitted",
            diagnostic_field="diagnostics",
            readiness_field="integrity_status",
        ),
        "trading_decision": _project_trading_decision(
            final_state.get("trading_decision"),
            _evidence_projection,
        ),
        "analysis_outcome_contract": analysis_outcome_contract,
    }
    payload["audit_sha256"] = sha256(_canonical_json(payload)).hexdigest()
    apply_terminal_contract(
        final_state,
        config=config,
        audit_digest=payload["audit_sha256"],
    )
    return payload


def write_immutable_decision_audit(
    final_state: dict[str, Any],
    directory: str | Path,
    *,
    filename: str | None = "decision-audit.json",
    config: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Path:
    """Publish a complete audit atomically and never overwrite different data."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        raise ValueError(
            "prebuilt decision audit payloads are not accepted; "
            "artifact persistence must occur at the write boundary"
        )
    audit_payload = prepare_decision_audit(
        final_state,
        directory,
        config=config,
    )
    if filename is None:
        filename = (
            f"decision-audit-{final_state.get('trade_date')}-"
            f"{audit_payload['audit_sha256'][:16]}.json"
        )
    encoded = (
        json.dumps(audit_payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    target = directory / filename
    if target.exists():
        if target.read_bytes() != encoded:
            raise FileExistsError(f"immutable decision audit already exists: {target}")
        return target

    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{filename}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != encoded:
                raise FileExistsError(
                    f"immutable decision audit already exists: {target}"
                ) from None
        except OSError as exc:
            # Some Windows filesystems disallow hard links. Exclusive creation
            # preserves immutability; the source bytes are already fully flushed.
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                target_descriptor = os.open(target, flags, 0o600)
            except FileExistsError:
                if target.read_bytes() != encoded:
                    raise FileExistsError(
                        f"immutable decision audit already exists: {target}"
                    ) from None
            else:
                with os.fdopen(target_descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)
    return target
