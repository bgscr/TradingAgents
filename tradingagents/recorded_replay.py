"""Typed loading for immutable, offline recorded-run replay fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    CalculationDefinition,
    ClaimValidationStatus,
    EvidenceState,
    EvidenceStatus,
    MissingValuePolicy,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    calculation_readiness_outcome,
)
from tradingagents.graph.evidence_gate import (
    create_preflight_gate_node,
    route_after_preflight,
)
from tradingagents.reporting import write_report_tree
from tradingagents.run_telemetry import RunTelemetryProjection
from tradingagents.strategy_registry import (
    DEFAULT_DECISION_HORIZON,
    create_production_decision_policy,
)
from tradingagents.terminal_contract import (
    CanonicalRunIdentity,
    TerminalContract,
    apply_terminal_contract,
)

RECORDED_FIXTURE_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)

DEFAULT_RECORDED_FIXTURE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "recorded_runs"
    / "manifest.json"
)


class RecordedRunCoordinates(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    ticker: str = Field(min_length=1)
    requested_date: date
    asset_type: str = Field(min_length=1)


class RecordedLegacyAssertionReference(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    claim_id: str = Field(min_length=1)
    fact_ids: tuple[str, ...] = ()


class RecordedLegacySelection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    rating: str = Field(min_length=1)
    material_claim_ids: tuple[str, ...] = ()
    decision_assertions: tuple[RecordedLegacyAssertionReference, ...] = ()


class RecordedRunInput(BaseModel):
    """Sanitized recorded input, without normalized replay expectations."""

    model_config = _CLOSED_MODEL_CONFIG

    fixture_version: Literal["1.0"] = RECORDED_FIXTURE_VERSION
    source_report: str = Field(min_length=1)
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run: RecordedRunCoordinates
    evidence_state: EvidenceState
    run_telemetry: RunTelemetryProjection
    legacy_selection: RecordedLegacySelection | None = None


class RecordedRunExpectation(BaseModel):
    """Normalized oracle kept outside the immutable recorded input bytes."""

    model_config = _CLOSED_MODEL_CONFIG

    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticker: str = Field(min_length=1)
    requested_date: date
    expected_terminal_kind: Literal["analysis_outcome", "trading_decision"]
    expected_blocked_stage: Literal[
        "preflight",
        "admission",
        "direction_selection",
        "decision_gate",
    ]


class RecordedFixtureManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    manifest_version: Literal["1.0"] = RECORDED_FIXTURE_VERSION
    fixtures: tuple[RecordedRunExpectation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _fixture_addresses_are_unique(self) -> RecordedFixtureManifest:
        addresses = tuple(item.fixture_sha256 for item in self.fixtures)
        if len(set(addresses)) != len(addresses):
            raise ValueError("recorded fixture manifest repeats a content address")
        return self


@dataclass(frozen=True)
class LoadedRecordedRunFixture:
    path: Path
    raw_bytes: bytes
    content_sha256: str
    recorded: RecordedRunInput
    expectation: RecordedRunExpectation


class RecordedReplayCounters(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    model_calls: int = Field(default=0, ge=0)
    preflight_gate_calls: int = Field(default=0, ge=0)
    admission_gate_calls: int = Field(default=0, ge=0)
    direction_selector_calls: int = Field(default=0, ge=0)
    decision_gate_calls: int = Field(default=0, ge=0)
    decision_writes: int = Field(default=0, ge=0)
    signal_writes: int = Field(default=0, ge=0)
    position_writes: int = Field(default=0, ge=0)
    memory_writes: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class RecordedReplayResult:
    final_state: dict
    terminal: TerminalContract
    run_identity: CanonicalRunIdentity
    counters: RecordedReplayCounters
    blocked_stage: str | None
    report_path: Path
    audit_path: Path


def load_recorded_run_fixtures(
    manifest_path: str | Path = DEFAULT_RECORDED_FIXTURE_MANIFEST,
) -> tuple[LoadedRecordedRunFixture, ...]:
    """Load and verify every content-addressed fixture in one manifest."""

    manifest_path = Path(manifest_path)
    manifest = RecordedFixtureManifest.model_validate_json(manifest_path.read_bytes())
    loaded: list[LoadedRecordedRunFixture] = []

    for expectation in manifest.fixtures:
        fixture_path = manifest_path.parent / f"{expectation.fixture_sha256}.json"
        raw_bytes = fixture_path.read_bytes()
        actual_sha256 = sha256(raw_bytes).hexdigest()
        if actual_sha256 != expectation.fixture_sha256:
            raise ValueError(
                f"recorded fixture content address mismatch: {fixture_path.name}"
            )
        recorded = RecordedRunInput.model_validate_json(raw_bytes)
        if recorded.run.ticker != expectation.ticker:
            raise ValueError("recorded fixture ticker does not match its expectation")
        if recorded.run.requested_date != expectation.requested_date:
            raise ValueError(
                "recorded fixture requested date does not match its expectation"
            )
        loaded.append(
            LoadedRecordedRunFixture(
                path=fixture_path,
                raw_bytes=raw_bytes,
                content_sha256=actual_sha256,
                recorded=recorded,
                expectation=expectation,
            )
        )

    return tuple(loaded)


def adapt_recorded_evidence(evidence: EvidenceState) -> EvidenceState:
    """Normalize legacy sentinel payloads through current typed evidence contracts."""

    rate_limited_artifacts = {
        artifact.artifact_sha256
        for artifact in evidence.source_artifacts
        if artifact.raw_text.startswith("Error fetching news")
        and (
            "too many requests" in artifact.raw_text.casefold()
            or "rate limit" in artifact.raw_text.casefold()
        )
    }
    rejected_fact_ids = {
        fact.fact_id
        for fact in evidence.source_facts
        if fact.artifact_sha256 in rate_limited_artifacts
    }
    artifacts = tuple(
        artifact
        for artifact in evidence.source_artifacts
        if artifact.artifact_sha256 not in rate_limited_artifacts
    )
    facts = tuple(
        fact for fact in evidence.source_facts if fact.fact_id not in rejected_fact_ids
    )
    claims = tuple(
        claim.model_copy(
            update={
                "fact_ids": tuple(
                    fact_id
                    for fact_id in claim.fact_ids
                    if fact_id not in rejected_fact_ids
                )
            }
        )
        for claim in evidence.material_claims
    )
    validations = tuple(
        validation.model_copy(
            update={
                "status": ClaimValidationStatus.UNSUPPORTED,
                "detail": "rate-limited provider response is unavailable evidence",
                "fact_ids": (),
            }
        )
        if rejected_fact_ids.intersection(validation.fact_ids)
        else validation
        for validation in evidence.claim_validations
    )
    rate_limited_source_refs = {
        artifact.source_ref
        for artifact in evidence.source_artifacts
        if artifact.artifact_sha256 in rate_limited_artifacts
    }
    sources = tuple(
        source.model_copy(
            update={
                "status": EvidenceStatus.UNAVAILABLE,
                "detail": "typed rate-limited acquisition outcome",
            }
        )
        if source.source_id in rate_limited_source_refs
        else source
        for source in evidence.sources
    )

    outcomes = []
    for outcome in evidence.acquisition_outcomes:
        if (
            isinstance(outcome, SourceAcquisitionAvailable)
            and outcome.artifact.artifact_sha256 in rate_limited_artifacts
        ):
            outcomes.append(
                SourceAcquisitionUnavailable(
                    provider=outcome.provider,
                    provider_order=outcome.provider_order,
                    capability=outcome.capability,
                    source_ref=outcome.source_ref,
                    attempt=outcome.attempt,
                    retrieved_at=outcome.retrieved_at,
                    retryable=True,
                    reason=AcquisitionUnavailableReason.RATE_LIMITED,
                    http_status=429,
                )
            )
        else:
            outcomes.append(outcome)

    snapshot = evidence.market_snapshot
    if snapshot is not None and not any(
        fact.canonical_field == "close_200_sma"
        or "close_200_sma" in fact.raw_text
        for fact in facts
    ):
        snapshot_artifact = next(
            (
                artifact
                for artifact in artifacts
                if artifact.source_ref == snapshot.snapshot_id
            ),
            None,
        )
        if snapshot_artifact is not None:
            definition = CalculationDefinition(
                calculation_id="indicator.close_200_sma",
                version="1.0",
                input_fields=("Close",),
                input_frequency="trading_day",
                minimum_history_rows=200,
                warmup_rows=199,
                adjustment_basis=snapshot.adjustment_basis,
                missing_value_policy=MissingValuePolicy.FAIL,
                formula="mean(close[t-199:t])",
                implementation_version="stockstats-sma-1",
                output_field="close_200_sma",
                output_unit="price",
                precision=8,
            )
            readiness = calculation_readiness_outcome(
                definition,
                observations_available=snapshot.history_rows,
                adjustment_basis=snapshot.adjustment_basis,
                input_artifact_sha256=snapshot_artifact.artifact_sha256,
                provider=snapshot.provider,
                attempt=1,
                retrieved_at=snapshot.retrieved_at,
            )
            if readiness is not None:
                outcomes.append(readiness)

    return EvidenceState.model_validate(
        {
            "instrument_identity": evidence.instrument_identity,
            "market_snapshot": evidence.market_snapshot,
            "material_claims": claims,
            "source_facts": facts,
            "source_artifacts": artifacts,
            "claim_validations": validations,
            "sources": sources,
            "acquisition_outcomes": tuple(outcomes),
        }
    )


def replay_recorded_run(
    fixture: LoadedRecordedRunFixture,
    output_directory: str | Path,
) -> RecordedReplayResult:
    """Replay one immutable fixture without live providers or model calls."""

    recorded = fixture.recorded
    output_directory = Path(output_directory)
    evidence = adapt_recorded_evidence(recorded.evidence_state)
    state = {
        "company_of_interest": recorded.run.ticker,
        "ticker": recorded.run.ticker,
        "trade_date": recorded.run.requested_date.isoformat(),
        "asset_type": recorded.run.asset_type,
        "graph_signature": "recorded-replay|evidence_schema=4|decision_schema=1",
        "evidence_gate_mode": "enforce",
        "run_id": f"run:{fixture.content_sha256}",
        "evidence_state": evidence.model_dump(mode="json"),
        "run_telemetry": recorded.run_telemetry.model_dump(mode="json"),
        "decision_audit_created_at": (
            evidence.acquisition_outcomes[0].retrieved_at
        ),
    }
    policy = create_production_decision_policy()
    preflight_update = create_preflight_gate_node(
        policy,
        DEFAULT_DECISION_HORIZON,
    )(state)
    state.update(preflight_update)
    if route_after_preflight(state) != "blocked":
        raise ValueError("recorded legacy fixture unexpectedly passed preflight")

    config = {
        "mode": "recorded_replay",
        "fixture_sha256": fixture.content_sha256,
    }
    apply_terminal_contract(state, config=config)
    report_path = write_report_tree(state, recorded.run.ticker, output_directory)
    terminal = TerminalContract.model_validate(state["terminal_contract"])
    identity = CanonicalRunIdentity.model_validate(state["run_identity"])
    counters = RecordedReplayCounters(
        preflight_gate_calls=1,
        decision_writes=int("trading_decision" in state),
        signal_writes=int("signal" in state),
        position_writes=int("position" in state),
    )
    return RecordedReplayResult(
        final_state=state,
        terminal=terminal,
        run_identity=identity,
        counters=counters,
        blocked_stage="preflight",
        report_path=report_path,
        audit_path=output_directory / "decision-audit.json",
    )
