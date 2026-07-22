"""Pure, deterministic policy boundary for directional trading decisions.

This module intentionally defines no production strategy rules.  It projects the
canonical evidence contract into a small closed vocabulary, applies injected
deterministic predicates, and verifies the resulting assertions a second time at
the final decision gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from itertools import product
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.evidence import (
    CalculationDefinition,
    CalculationLineage,
    EvidenceState,
    EvidenceStatus,
    InstrumentKind,
    SourceArtifact,
    SourceFact,
    capability_profile_for,
    stable_market_snapshot_id,
    stable_source_fact_id,
    validate_calculation_lineage,
)

DECISION_POLICY_CONTRACT_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"


class HorizonUnit(str, Enum):
    TRADING_DAYS = "trading_days"
    CALENDAR_DAYS = "calendar_days"


class RulePolarity(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"


class MissingFactBehavior(str, Enum):
    BLOCK = "block"
    NOT_SATISFIED = "not_satisfied"


class EvidenceIntegrityStatus(str, Enum):
    DECISION_READY = "decision_ready"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"


class DecisionHorizon(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    count: int = Field(ge=1)
    unit: HorizonUnit


class StrategyRuleDefinition(BaseModel):
    """Versioned declaration of deterministic directional semantics."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    rule_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    version: str = Field(min_length=1)
    applicable_instrument_kinds: tuple[InstrumentKind, ...] = Field(min_length=1)
    required_canonical_fields: tuple[str, ...] = Field(min_length=1)
    target_rating: PortfolioRating
    polarity: RulePolarity
    horizon: DecisionHorizon
    predicate_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    comparator: str = Field(min_length=1)
    threshold: Decimal | None = None
    max_fact_age_days: int | None = Field(default=None, ge=0)
    minimum_history_rows: int = Field(default=1, ge=1)
    missing_fact_behavior: MissingFactBehavior = MissingFactBehavior.BLOCK
    block_on_conflict: bool = True
    implementation_version: str = Field(min_length=1)

    @field_validator(
        "applicable_instrument_kinds", "required_canonical_fields", mode="after"
    )
    @classmethod
    def _canonicalize_set_like_fields(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(sorted(value, key=lambda item: str(item.value if isinstance(item, Enum) else item)))

    @model_validator(mode="after")
    def _reject_duplicate_domain_values(self) -> StrategyRuleDefinition:
        if len(set(self.applicable_instrument_kinds)) != len(
            self.applicable_instrument_kinds
        ):
            raise ValueError("applicable instrument kinds must be unique")
        if len(set(self.required_canonical_fields)) != len(
            self.required_canonical_fields
        ):
            raise ValueError("required canonical fields must be unique")
        if any(not field.strip() for field in self.required_canonical_fields):
            raise ValueError("required canonical fields may not be blank")
        return self


class RuleApplicationRequest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    rule_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    rule_version: str = Field(min_length=1)
    fact_ids: tuple[str, ...] = Field(min_length=1)


class RulePredicateResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    satisfied: bool
    fact_ids: tuple[str, ...]
    evaluation_digest: str = Field(min_length=1)
    blockers: tuple[str, ...] = ()


class CanonicalFactAdapterResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    canonical_field: str = Field(min_length=1)
    normalized_value: Decimal | str | int | bool
    unit: str
    effective_range_start: str | None = None
    effective_range_end: str | None = None
    observations_used: int | None = Field(default=None, ge=1)


class DecisionFact(BaseModel):
    """Directional projection of a SourceFact; provider prose is excluded."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    fact_id: str = Field(min_length=1)
    canonical_field: str = Field(min_length=1)
    normalized_value: Decimal | str | int | bool
    unit: str
    instrument_symbol: str = Field(min_length=1)
    effective_date: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    artifact_sha256: str = Field(min_length=1)
    source_span_start: int = Field(ge=0)
    source_span_end: int = Field(ge=0)
    calculation_lineage: CalculationLineage | None = None

    @model_validator(mode="after")
    def _validate_span(self) -> DecisionFact:
        if self.source_span_end < self.source_span_start:
            raise ValueError("source span is reversed")
        return self


class DecisionInstrument(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    instrument_kind: InstrumentKind
    currency: str = Field(min_length=1)


class DecisionAssertion(BaseModel):
    """A satisfied rule application whose semantics can be recomputed."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    assertion_id: str = Field(min_length=1)
    rule_id: str
    rule_version: str
    fact_ids: tuple[str, ...] = Field(min_length=1)
    target_rating: PortfolioRating
    polarity: RulePolarity
    horizon: DecisionHorizon
    predicate_id: str
    comparator: str
    threshold: Decimal | None = None
    evaluation_digest: str = Field(min_length=1)


class ValidatedDecisionContext(BaseModel):
    """The complete and only input allowed at the direction-selection seam."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    context_id: str = Field(min_length=1)
    evidence_contract_version: str = Field(min_length=1)
    registry_digest: str = Field(min_length=1)
    calculation_registry_digest: str = Field(min_length=1)
    instrument: DecisionInstrument
    capability_profile_id: str = Field(min_length=1)
    as_of_date: date
    horizon: DecisionHorizon
    facts: tuple[DecisionFact, ...] = Field(min_length=1)
    assertions: tuple[DecisionAssertion, ...] = Field(min_length=1)
    integrity_status: EvidenceIntegrityStatus


class DecisionContextBuilt(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    kind: Literal["built"] = "built"
    context: ValidatedDecisionContext


class DecisionContextBlocked(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    kind: Literal["blocked"] = "blocked"
    integrity_status: EvidenceIntegrityStatus
    blocker_codes: tuple[str, ...] = Field(min_length=1)
    diagnostics: tuple[str, ...] = Field(min_length=1)


DecisionContextBuildResult = Annotated[
    DecisionContextBuilt | DecisionContextBlocked,
    Field(discriminator="kind"),
]


class DirectionSelection(BaseModel):
    """Closed deterministic proposal: identifiers only, with no factual prose."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    context_id: str = Field(min_length=1)
    rating: PortfolioRating
    assertion_ids: tuple[str, ...] = Field(min_length=1)


def deterministic_direction_selection(
    context: ValidatedDecisionContext,
) -> DirectionSelection:
    """Construct the sole valid selection encoded by a validated context."""

    assertion_ids = tuple(assertion.assertion_id for assertion in context.assertions)
    if len(assertion_ids) != len(set(assertion_ids)):
        raise ValueError("direction_assertion_ids_duplicate")
    target_ratings = {assertion.target_rating for assertion in context.assertions}
    if len(target_ratings) != 1:
        raise ValueError("direction_assertions_target_multiple_ratings")
    return DirectionSelection(
        context_id=context.context_id,
        rating=target_ratings.pop(),
        assertion_ids=tuple(sorted(assertion_ids)),
    )


class TradingDecisionContract(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    decision_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    registry_digest: str = Field(min_length=1)
    calculation_registry_digest: str = Field(min_length=1)
    instrument: DecisionInstrument
    as_of_date: date
    horizon: DecisionHorizon
    rating: PortfolioRating
    facts: tuple[DecisionFact, ...] = Field(min_length=1)
    assertions: tuple[DecisionAssertion, ...] = Field(min_length=1)
    integrity_status: EvidenceIntegrityStatus


class DecisionGateResultV2(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = DECISION_POLICY_CONTRACT_VERSION
    permitted: bool
    integrity_status: EvidenceIntegrityStatus
    diagnostics: tuple[str, ...]
    decision: TradingDecisionContract | None = None

    @model_validator(mode="after")
    def _decision_matches_permission(self) -> DecisionGateResultV2:
        if self.permitted != (self.decision is not None):
            raise ValueError("a decision must exist if and only if the gate is permitted")
        return self


RuleEvaluator = Callable[
    [StrategyRuleDefinition, tuple[DecisionFact, ...], date], RulePredicateResult
]
CanonicalFactAdapter = Callable[
    [SourceArtifact, int, int], CanonicalFactAdapterResult
]
ArtifactResolver = Callable[[str, str], SourceArtifact | None]


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")

    def json_default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item.normalize(), "f")
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"{type(item).__name__} is not canonically serializable")

    return json.dumps(
        value,
        default=json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(prefix: str, value: Any) -> str:
    return f"{prefix}:{sha256(_canonical_json(value).encode()).hexdigest()}"


def stable_decision_value_digest(
    *,
    canonical_field: str,
    normalized_value: Decimal | str | int | bool,
    unit: str,
) -> str:
    """Bind a deterministic adapter result to its decision-driving value."""

    return sha256(
        _canonical_json(
            {
                "canonical_field": canonical_field,
                "normalized_value": normalized_value,
                "unit": unit,
            }
        ).encode()
    ).hexdigest()


def _parse_effective_date(value: str) -> date | None:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            return None


def _project_fact(fact: SourceFact) -> DecisionFact | None:
    if fact.fact_kind != "canonical":
        return None
    if (
        not fact.canonical_field.strip()
        or fact.normalized_value is None
        or not fact.instrument_symbol.strip()
        or not fact.effective_date.strip()
    ):
        return None
    return DecisionFact(
        fact_id=fact.fact_id,
        canonical_field=fact.canonical_field,
        normalized_value=fact.normalized_value,
        unit=fact.unit,
        instrument_symbol=fact.instrument_symbol,
        effective_date=fact.effective_date,
        source_ref=fact.source_ref,
        artifact_sha256=fact.artifact_sha256,
        source_span_start=fact.source_span_start,
        source_span_end=fact.source_span_end,
        calculation_lineage=fact.calculation_lineage,
    )


def _rule_key(rule_id: str, version: str) -> tuple[str, str]:
    return rule_id, version


def _application_key(
    application: RuleApplicationRequest,
) -> tuple[str, str, tuple[str, ...]]:
    return (
        application.rule_id,
        application.rule_version,
        tuple(sorted(set(application.fact_ids))),
    )


def _canonical_rule_payload(rule: StrategyRuleDefinition) -> dict[str, Any]:
    payload = rule.model_dump(mode="json")
    payload["applicable_instrument_kinds"] = sorted(
        payload["applicable_instrument_kinds"]
    )
    payload["required_canonical_fields"] = sorted(
        payload["required_canonical_fields"]
    )
    return payload


def _assertion_payload(assertion: DecisionAssertion) -> dict[str, Any]:
    return assertion.model_dump(mode="json", exclude={"assertion_id"})


def _context_payload(context: ValidatedDecisionContext) -> dict[str, Any]:
    return context.model_dump(mode="json", exclude={"context_id"})


def _without_runtime_evidence_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_runtime_evidence_fields(item)
            for key, item in value.items()
            if key != "tool_call_id"
        }
    if isinstance(value, (list, tuple)):
        return [_without_runtime_evidence_fields(item) for item in value]
    return value


def _semantic_evidence_payload(evidence: EvidenceState) -> dict[str, Any]:
    """Return order-independent evidence semantics without runtime call IDs."""

    payload = _without_runtime_evidence_fields(evidence.model_dump(mode="json"))
    for field in (
        "material_claims",
        "source_facts",
        "source_artifacts",
        "claim_validations",
        "sources",
        "acquisition_outcomes",
    ):
        payload[field] = tuple(
            sorted({_canonical_json(item) for item in payload[field]})
        )
    return payload


def _blocked(*codes: str) -> DecisionContextBlocked:
    unique = tuple(sorted(set(codes))) or ("decision_context_invalid",)
    diagnostics = tuple(code.replace("_", " ") for code in unique)
    integrity = (
        EvidenceIntegrityStatus.CONFLICTED
        if any("conflict" in code for code in unique)
        else EvidenceIntegrityStatus.INSUFFICIENT
    )
    return DecisionContextBlocked(
        integrity_status=integrity,
        blocker_codes=unique,
        diagnostics=diagnostics,
    )


@dataclass(frozen=True, slots=True, init=False)
class DecisionPolicyEngine:
    """Immutable registry plus deterministic context builder and decision gate."""

    rules: tuple[StrategyRuleDefinition, ...]
    registry_digest: str
    calculation_registry_digest: str
    _rules_by_key: Mapping[tuple[str, str], StrategyRuleDefinition]
    _calculations_by_key: Mapping[tuple[str, str], CalculationDefinition]
    _evaluators: Mapping[str, RuleEvaluator]
    _fact_adapters: Mapping[tuple[str, str], CanonicalFactAdapter]
    _artifact_resolver: ArtifactResolver | None
    _trusted_run_evidence: EvidenceState | None
    _trusted_admitted_evidence_by_context_id: Mapping[str, EvidenceState]

    def __init__(
        self,
        rules: Iterable[StrategyRuleDefinition] = (),
        evaluators: Mapping[str, RuleEvaluator] | None = None,
        calculation_definitions: Iterable[CalculationDefinition] = (),
        fact_adapters: Mapping[tuple[str, str], CanonicalFactAdapter] | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> None:
        validated_rules = tuple(
            StrategyRuleDefinition.model_validate(rule.model_dump(mode="python"))
            for rule in rules
        )
        ordered_rules = tuple(
            sorted(
                validated_rules,
                key=lambda rule: (rule.rule_id, rule.version),
            )
        )
        rules_by_key: dict[tuple[str, str], StrategyRuleDefinition] = {}
        for rule in ordered_rules:
            key = _rule_key(rule.rule_id, rule.version)
            if key in rules_by_key:
                raise ValueError(
                    f"duplicate strategy rule {rule.rule_id!r} version {rule.version!r}"
                )
            rules_by_key[key] = rule
        evaluator_copy = dict(evaluators or {})
        fact_adapter_copy = dict(fact_adapters or {})
        calculations_by_key: dict[tuple[str, str], CalculationDefinition] = {}
        ordered_calculations = tuple(
            sorted(
                calculation_definitions,
                key=lambda definition: (definition.calculation_id, definition.version),
            )
        )
        for definition in ordered_calculations:
            key = (definition.calculation_id, definition.version)
            if key in calculations_by_key:
                raise ValueError(
                    "duplicate calculation definition "
                    f"{definition.calculation_id!r} version {definition.version!r}"
                )
            calculations_by_key[key] = definition
        object.__setattr__(self, "rules", ordered_rules)
        object.__setattr__(
            self,
            "registry_digest",
            _digest(
                "registry",
                [_canonical_rule_payload(rule) for rule in ordered_rules],
            ),
        )
        object.__setattr__(self, "_rules_by_key", MappingProxyType(rules_by_key))
        object.__setattr__(
            self,
            "calculation_registry_digest",
            _digest(
                "calculations",
                [definition.model_dump(mode="json") for definition in ordered_calculations],
            ),
        )
        object.__setattr__(
            self,
            "_calculations_by_key",
            MappingProxyType(calculations_by_key),
        )
        object.__setattr__(self, "_evaluators", MappingProxyType(evaluator_copy))
        object.__setattr__(
            self, "_fact_adapters", MappingProxyType(fact_adapter_copy)
        )
        object.__setattr__(self, "_artifact_resolver", artifact_resolver)
        object.__setattr__(self, "_trusted_run_evidence", None)
        object.__setattr__(
            self,
            "_trusted_admitted_evidence_by_context_id",
            MappingProxyType({}),
        )

    @property
    def configuration_blockers(self) -> tuple[str, ...]:
        """Return stable deterministic wiring failures for early preflight."""

        blockers: set[str] = set()
        if not self.rules:
            blockers.add("no_applicable_registered_strategy_rule")
        if not self._calculations_by_key:
            blockers.add("no_registered_calculation_definition")
        if any(rule.predicate_id not in self._evaluators for rule in self.rules):
            blockers.add("strategy_rule_evaluator_unavailable")
        if any(key not in self._fact_adapters for key in self._calculations_by_key):
            blockers.add("canonical_fact_adapter_required")
        return tuple(sorted(blockers))

    def preflight_minimum_history_rows(
        self,
        instrument_kind: InstrumentKind,
        horizon: DecisionHorizon,
    ) -> int | None:
        """Return the least history that can satisfy a registered rule path."""

        applicable = tuple(
            rule
            for rule in self.rules
            if instrument_kind in rule.applicable_instrument_kinds
            and rule.horizon == horizon
        )
        if not applicable:
            return None
        return min(rule.minimum_history_rows for rule in applicable)

    def register_trusted_evidence(self, evidence: EvidenceState) -> None:
        """Bind one pre-checkpoint run ledger and clear prior admission state."""

        trusted = EvidenceState.model_validate_json(evidence.model_dump_json())
        object.__setattr__(self, "_trusted_run_evidence", trusted)
        object.__setattr__(
            self,
            "_trusted_admitted_evidence_by_context_id",
            MappingProxyType({}),
        )

    def register_admitted_evidence(
        self,
        evidence: EvidenceState,
        context: ValidatedDecisionContext,
    ) -> None:
        """Promote evidence only after it preserves the trusted run ledger."""

        admitted = EvidenceState.model_validate_json(evidence.model_dump_json())
        binding_failure = self._trusted_run_binding_failure(admitted)
        if binding_failure is not None:
            raise ValueError(binding_failure)
        if context.context_id != _digest("context", _context_payload(context)):
            raise ValueError("validated_decision_context_digest_mismatch")
        if self._integrity_status(admitted) is not context.integrity_status:
            raise ValueError("validated_decision_context_integrity_mismatch")

        admitted_by_context_id = dict(
            self._trusted_admitted_evidence_by_context_id
        )
        existing = admitted_by_context_id.get(context.context_id)
        if existing is not None and _semantic_evidence_payload(
            existing
        ) != _semantic_evidence_payload(admitted):
            raise ValueError("trusted_admitted_evidence_redefined")
        admitted_by_context_id[context.context_id] = admitted
        object.__setattr__(
            self,
            "_trusted_admitted_evidence_by_context_id",
            MappingProxyType(admitted_by_context_id),
        )

    def candidate_applications(
        self,
        evidence: EvidenceState,
        *,
        horizon: DecisionHorizon,
        max_candidates: int = 256,
    ) -> tuple[RuleApplicationRequest, ...]:
        """Enumerate registered rule/fact bindings without inventing semantics.

        This is the deterministic production producer used when no explicit
        model-facing identifier selection is present. Predicate evaluation and
        every trust check still happen in :meth:`build_context` and again at the
        Decision Gate.
        """

        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        identity = evidence.instrument_identity
        if identity is None or not identity.is_authoritative:
            return ()
        facts_by_field: dict[str, dict[str, DecisionFact]] = {}
        for source_fact in evidence.source_facts:
            fact = _project_fact(source_fact)
            if fact is None or fact.instrument_symbol != identity.symbol:
                continue
            facts_by_field.setdefault(fact.canonical_field, {})[fact.fact_id] = fact

        applications: dict[
            tuple[str, str, tuple[str, ...]], RuleApplicationRequest
        ] = {}
        candidates_seen = 0
        for rule in self.rules:
            if identity.instrument_kind not in rule.applicable_instrument_kinds:
                continue
            if rule.horizon != horizon:
                continue
            fact_groups = tuple(
                tuple(
                    sorted(
                        facts_by_field.get(field, {}).values(),
                        key=lambda fact: fact.fact_id,
                    )
                )
                for field in rule.required_canonical_fields
            )
            if any(not group for group in fact_groups):
                continue
            for selected in product(*fact_groups):
                candidates_seen += 1
                if candidates_seen > max_candidates:
                    raise ValueError("strategy rule candidate limit exceeded")
                application = RuleApplicationRequest(
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    fact_ids=tuple(sorted({fact.fact_id for fact in selected})),
                )
                applications[_application_key(application)] = application
        return tuple(applications[key] for key in sorted(applications))

    def build_context(
        self,
        evidence: EvidenceState,
        applications: Iterable[RuleApplicationRequest],
        *,
        horizon: DecisionHorizon,
        as_of_date: date,
        tolerate_unsatisfied_applications: bool = False,
    ) -> DecisionContextBuilt | DecisionContextBlocked:
        if not self.rules:
            return _blocked("no_applicable_registered_strategy_rule")
        identity = evidence.instrument_identity
        if identity is None or not identity.is_authoritative:
            return _blocked("authoritative_instrument_identity_required")
        try:
            capability_profile = capability_profile_for(identity.instrument_kind)
        except ValueError:
            return _blocked("instrument_capability_profile_unavailable")

        facts_by_id: dict[str, DecisionFact] = {}
        source_facts_by_id: dict[str, SourceFact] = {}
        conflicting_fact_ids: set[str] = set()
        for source_fact in evidence.source_facts:
            projected = _project_fact(source_fact)
            if projected is None:
                continue
            prior = facts_by_id.get(projected.fact_id)
            prior_source = source_facts_by_id.get(projected.fact_id)
            if prior is not None and (
                prior != projected
                or (prior_source is not None and prior_source.raw_text != source_fact.raw_text)
            ):
                conflicting_fact_ids.add(projected.fact_id)
                continue
            facts_by_id[projected.fact_id] = projected
            source_facts_by_id[projected.fact_id] = source_fact
        if conflicting_fact_ids:
            return _blocked("conflicting_duplicate_fact_id")

        artifacts_by_key: dict[tuple[str, str], SourceArtifact] = {}
        conflicting_artifact_keys: set[tuple[str, str]] = set()
        for artifact in evidence.source_artifacts:
            key = (artifact.artifact_sha256, artifact.source_ref)
            prior = artifacts_by_key.get(key)
            if prior is not None and prior.raw_text != artifact.raw_text:
                conflicting_artifact_keys.add(key)
                continue
            artifacts_by_key[key] = artifact
        if conflicting_artifact_keys:
            return _blocked("conflicting_duplicate_source_artifact")

        normalized_applications = {
            _application_key(application): application for application in applications
        }
        if not normalized_applications:
            return _blocked("no_applicable_registered_strategy_rule")

        blockers: list[str] = []
        assertions: list[DecisionAssertion] = []
        selected_facts: dict[str, DecisionFact] = {}
        known_rule_ids = {rule.rule_id for rule in self.rules}
        for application_key in sorted(normalized_applications):
            application = normalized_applications[application_key]
            rule = self._rules_by_key.get(
                _rule_key(application.rule_id, application.rule_version)
            )
            if rule is None:
                blockers.append(
                    "strategy_rule_version_mismatch"
                    if application.rule_id in known_rule_ids
                    else "strategy_rule_not_registered"
                )
                continue
            if identity.instrument_kind not in rule.applicable_instrument_kinds:
                blockers.append("strategy_rule_instrument_kind_mismatch")
                continue
            if rule.horizon != horizon:
                blockers.append("strategy_rule_horizon_mismatch")
                continue

            fact_ids = tuple(sorted(set(application.fact_ids)))
            facts = tuple(
                facts_by_id[fact_id] for fact_id in fact_ids if fact_id in facts_by_id
            )
            if len(facts) != len(fact_ids):
                if rule.missing_fact_behavior is MissingFactBehavior.BLOCK:
                    blockers.append("selected_source_fact_missing")
                continue
            artifact_failure = self._artifact_binding_failure(
                facts, source_facts_by_id, artifacts_by_key
            )
            if artifact_failure is not None:
                blockers.append(artifact_failure)
                continue
            if any(fact.instrument_symbol != identity.symbol for fact in facts):
                blockers.append("source_fact_instrument_mismatch")
                continue
            actual_fields = {fact.canonical_field for fact in facts}
            required_fields = set(rule.required_canonical_fields)
            if actual_fields != required_fields:
                blockers.append("strategy_rule_canonical_field_mismatch")
                continue
            fact_dates = tuple(_parse_effective_date(fact.effective_date) for fact in facts)
            if any(fact_date is None for fact_date in fact_dates):
                blockers.append("source_fact_effective_date_invalid")
                continue
            if any(fact_date > as_of_date for fact_date in fact_dates if fact_date):
                blockers.append("source_fact_effective_date_in_future")
                continue
            if rule.max_fact_age_days is not None and any(
                (as_of_date - fact_date).days > rule.max_fact_age_days
                for fact_date in fact_dates
                if fact_date is not None
            ):
                blockers.append("source_fact_stale")
                continue
            if rule.minimum_history_rows > 1 and any(
                    fact.calculation_lineage is None
                    or fact.calculation_lineage.observations_used
                    < rule.minimum_history_rows
                    for fact in facts
            ):
                blockers.append("source_fact_insufficient_history")
                continue
            if rule.block_on_conflict and self._has_selected_conflict(facts):
                blockers.append("selected_source_facts_conflicted")
                continue
            evaluator = self._evaluators.get(rule.predicate_id)
            if evaluator is None:
                blockers.append("strategy_rule_evaluator_unavailable")
                continue
            try:
                result = evaluator(rule, facts, as_of_date)
            except Exception:
                blockers.append("strategy_rule_evaluation_failed")
                continue
            if tuple(sorted(set(result.fact_ids))) != fact_ids:
                blockers.append("strategy_rule_evaluation_fact_mismatch")
                continue
            if result.blockers:
                blockers.append("strategy_rule_evaluation_blocked")
                continue
            if not result.satisfied:
                if not tolerate_unsatisfied_applications:
                    blockers.append("strategy_rule_predicate_not_satisfied")
                continue
            assertion = DecisionAssertion(
                assertion_id="pending",
                rule_id=rule.rule_id,
                rule_version=rule.version,
                fact_ids=fact_ids,
                target_rating=rule.target_rating,
                polarity=rule.polarity,
                horizon=rule.horizon,
                predicate_id=rule.predicate_id,
                comparator=rule.comparator,
                threshold=rule.threshold,
                evaluation_digest=result.evaluation_digest,
            )
            assertion = assertion.model_copy(
                update={"assertion_id": _digest("assertion", _assertion_payload(assertion))}
            )
            assertions.append(assertion)
            selected_facts.update((fact.fact_id, fact) for fact in facts)

        if blockers:
            return _blocked(*blockers)
        if not assertions:
            return _blocked("no_applicable_registered_strategy_rule")

        integrity = self._integrity_status(evidence)
        if integrity in {
            EvidenceIntegrityStatus.INSUFFICIENT,
            EvidenceIntegrityStatus.CONFLICTED,
        }:
            return _blocked(
                "required_evidence_conflicted"
                if integrity is EvidenceIntegrityStatus.CONFLICTED
                else "required_evidence_unavailable"
            )
        instrument = DecisionInstrument(
            symbol=identity.symbol,
            venue=identity.venue,
            instrument_kind=identity.instrument_kind,
            currency=identity.currency,
        )
        context = ValidatedDecisionContext(
            context_id="pending",
            evidence_contract_version=evidence.contract_version,
            registry_digest=self.registry_digest,
            calculation_registry_digest=self.calculation_registry_digest,
            instrument=instrument,
            capability_profile_id=capability_profile.profile_id,
            as_of_date=as_of_date,
            horizon=horizon,
            facts=tuple(sorted(selected_facts.values(), key=lambda fact: fact.fact_id)),
            assertions=tuple(
                sorted(assertions, key=lambda assertion: assertion.assertion_id)
            ),
            integrity_status=integrity,
        )
        context = context.model_copy(
            update={"context_id": _digest("context", _context_payload(context))}
        )
        return DecisionContextBuilt(context=context)

    def gate(
        self,
        context: ValidatedDecisionContext,
        proposal: DirectionSelection,
        *,
        evidence: EvidenceState,
    ) -> DecisionGateResultV2:
        checkpoint_evidence = EvidenceState.model_validate_json(
            evidence.model_dump_json()
        )
        diagnostics: list[str] = []
        if context.registry_digest != self.registry_digest:
            diagnostics.append("strategy rule registry digest mismatch")
        if context.calculation_registry_digest != self.calculation_registry_digest:
            diagnostics.append("calculation registry digest mismatch")
        if context.context_id != _digest("context", _context_payload(context)):
            diagnostics.append("validated decision context digest mismatch")
        if context.integrity_status not in {
            EvidenceIntegrityStatus.DECISION_READY,
            EvidenceIntegrityStatus.DEGRADED,
        }:
            diagnostics.append("validated decision context is not decision ready")
        if proposal.context_id != context.context_id:
            diagnostics.append("direction proposal references a different context")
        if len(set(proposal.assertion_ids)) != len(proposal.assertion_ids):
            diagnostics.append("direction proposal contains duplicate assertion ids")

        assertions_by_id = {
            assertion.assertion_id: assertion for assertion in context.assertions
        }
        if len(assertions_by_id) != len(context.assertions):
            diagnostics.append("validated context contains duplicate assertion ids")
        if set(proposal.assertion_ids) != set(assertions_by_id):
            diagnostics.append(
                "direction proposal does not cover every validated assertion"
            )
        selected = tuple(
            assertions_by_id[assertion_id]
            for assertion_id in sorted(set(proposal.assertion_ids))
            if assertion_id in assertions_by_id
        )
        if len(selected) != len(set(proposal.assertion_ids)):
            diagnostics.append("direction proposal references an unknown assertion")
        if not selected:
            diagnostics.append("direction proposal has no rule-backed assertion")

        facts_by_id = {fact.fact_id: fact for fact in context.facts}
        if len(facts_by_id) != len(context.facts):
            diagnostics.append("validated context contains duplicate fact ids")
        ledger_failure = self._gate_ledger_failure(context, checkpoint_evidence)
        if ledger_failure is not None:
            diagnostics.append(ledger_failure.replace("_", " "))
        trusted_admitted_evidence = (
            self._trusted_admitted_evidence_by_context_id.get(context.context_id)
        )
        target_ratings: set[PortfolioRating] = set()
        for assertion in selected:
            target_ratings.add(assertion.target_rating)
            if len(set(assertion.fact_ids)) != len(assertion.fact_ids):
                diagnostics.append("decision assertion contains duplicate fact ids")
            if assertion.assertion_id != _digest(
                "assertion", _assertion_payload(assertion)
            ):
                diagnostics.append("decision assertion digest mismatch")
                continue
            rule = self._rules_by_key.get(
                _rule_key(assertion.rule_id, assertion.rule_version)
            )
            if rule is None:
                diagnostics.append("decision assertion rule is not registered")
                continue
            if not self._assertion_matches_rule(assertion, rule):
                diagnostics.append("decision assertion does not match its strategy rule")
                continue
            if context.instrument.instrument_kind not in rule.applicable_instrument_kinds:
                diagnostics.append("decision assertion rule does not apply to instrument")
            if context.horizon != rule.horizon:
                diagnostics.append("decision assertion horizon differs from context")
            facts = tuple(
                facts_by_id[fact_id]
                for fact_id in assertion.fact_ids
                if fact_id in facts_by_id
            )
            if len(facts) != len(assertion.fact_ids):
                diagnostics.append("decision assertion references a missing fact")
                continue
            if {fact.canonical_field for fact in facts} != set(
                rule.required_canonical_fields
            ):
                diagnostics.append("decision assertion has incompatible canonical fields")
                continue
            if any(
                fact.instrument_symbol != context.instrument.symbol for fact in facts
            ):
                diagnostics.append("decision assertion fact instrument mismatch")
                continue
            if any(
                fact.calculation_lineage is None
                or fact.calculation_lineage.input_artifact_sha256
                != fact.artifact_sha256
                or fact.calculation_lineage.result_digest
                != stable_decision_value_digest(
                    canonical_field=fact.canonical_field,
                    normalized_value=fact.normalized_value,
                    unit=fact.unit,
                )
                for fact in facts
            ):
                diagnostics.append("decision assertion value attestation mismatch")
                continue
            calculation_failure = self._calculation_binding_failure(facts)
            if calculation_failure is not None:
                diagnostics.append(calculation_failure.replace("_", " "))
                continue
            adapter_failure = self._gate_adapter_failure(
                facts,
                trusted_admitted_evidence,
            )
            if adapter_failure is not None:
                diagnostics.append(adapter_failure.replace("_", " "))
                continue
            fact_dates = tuple(_parse_effective_date(fact.effective_date) for fact in facts)
            if any(fact_date is None for fact_date in fact_dates):
                diagnostics.append("decision assertion fact date is invalid")
                continue
            if any(
                fact_date > context.as_of_date
                for fact_date in fact_dates
                if fact_date is not None
            ):
                diagnostics.append("decision assertion fact date is in the future")
                continue
            if rule.max_fact_age_days is not None and any(
                (context.as_of_date - fact_date).days > rule.max_fact_age_days
                for fact_date in fact_dates
                if fact_date is not None
            ):
                diagnostics.append("decision assertion fact is stale")
                continue
            if rule.minimum_history_rows > 1 and any(
                    fact.calculation_lineage is None
                    or fact.calculation_lineage.observations_used
                    < rule.minimum_history_rows
                    for fact in facts
            ):
                diagnostics.append("decision assertion fact history is insufficient")
                continue
            if rule.block_on_conflict and self._has_selected_conflict(facts):
                diagnostics.append("decision assertion facts are conflicted")
                continue
            evaluator = self._evaluators.get(rule.predicate_id)
            if evaluator is None:
                diagnostics.append("decision assertion evaluator is unavailable")
                continue
            try:
                result = evaluator(rule, facts, context.as_of_date)
            except Exception:
                diagnostics.append("decision assertion reevaluation failed")
                continue
            if (
                not result.satisfied
                or result.blockers
                or tuple(sorted(set(result.fact_ids))) != assertion.fact_ids
                or result.evaluation_digest != assertion.evaluation_digest
            ):
                diagnostics.append("decision assertion reevaluation mismatch")
            if assertion.polarity is not RulePolarity.SUPPORTS:
                diagnostics.append("opposing assertions cannot authorize a rating")
            if assertion.target_rating != proposal.rating:
                diagnostics.append("assertion does not support the proposed rating")

        if len(target_ratings) > 1:
            diagnostics.append("selected assertions target multiple ratings")
        if diagnostics:
            return DecisionGateResultV2(
                permitted=False,
                integrity_status=(
                    EvidenceIntegrityStatus.CONFLICTED
                    if context.integrity_status is EvidenceIntegrityStatus.CONFLICTED
                    else EvidenceIntegrityStatus.INSUFFICIENT
                ),
                diagnostics=tuple(sorted(set(diagnostics))),
            )

        selected_fact_ids = {
            fact_id for assertion in selected for fact_id in assertion.fact_ids
        }
        decision_facts = tuple(
            sorted(
                (facts_by_id[fact_id] for fact_id in selected_fact_ids),
                key=lambda fact: fact.fact_id,
            )
        )
        decision_payload = {
            "context_id": context.context_id,
            "rating": proposal.rating.value,
            "assertion_ids": sorted(proposal.assertion_ids),
        }
        decision = TradingDecisionContract(
            decision_id=_digest("decision", decision_payload),
            context_id=context.context_id,
            registry_digest=context.registry_digest,
            calculation_registry_digest=context.calculation_registry_digest,
            instrument=context.instrument,
            as_of_date=context.as_of_date,
            horizon=context.horizon,
            rating=proposal.rating,
            facts=decision_facts,
            assertions=tuple(
                sorted(selected, key=lambda assertion: assertion.assertion_id)
            ),
            integrity_status=context.integrity_status,
        )
        return DecisionGateResultV2(
            permitted=True,
            integrity_status=context.integrity_status,
            diagnostics=(),
            decision=decision,
        )

    def _artifact_binding_failure(
        self,
        facts: tuple[DecisionFact, ...],
        source_facts_by_id: Mapping[str, SourceFact],
        artifacts_by_key: Mapping[tuple[str, str], SourceArtifact],
    ) -> str | None:
        for fact in facts:
            source_fact = source_facts_by_id[fact.fact_id]
            artifact = artifacts_by_key.get((fact.artifact_sha256, fact.source_ref))
            if artifact is None:
                return "selected_source_fact_artifact_missing"
            actual_digest = sha256(artifact.raw_text.encode()).hexdigest()
            if actual_digest != artifact.artifact_sha256:
                return "selected_source_artifact_digest_mismatch"
            if fact.source_span_end > len(artifact.raw_text):
                return "selected_source_fact_span_invalid"
            excerpt = artifact.raw_text[fact.source_span_start : fact.source_span_end]
            if source_fact.raw_text != excerpt:
                return "selected_source_fact_excerpt_mismatch"
            expected_fact_id = stable_source_fact_id(
                source_ref=fact.source_ref,
                artifact_sha256=fact.artifact_sha256,
                source_span_start=fact.source_span_start,
                source_span_end=fact.source_span_end,
                canonical_field=fact.canonical_field,
                instrument_symbol=fact.instrument_symbol,
                effective_date=fact.effective_date,
            )
            if fact.fact_id != expected_fact_id:
                return "selected_source_fact_id_mismatch"
            lineage = fact.calculation_lineage
            if (
                lineage is None
                or lineage.calculation_id not in source_fact.calculation_ids
            ):
                return "canonical_fact_adapter_required"
            definition = self._calculations_by_key.get(
                (lineage.calculation_id, lineage.calculation_version)
            )
            if definition is None:
                return "canonical_calculation_definition_required"
            try:
                validate_calculation_lineage(definition, lineage)
            except ValueError:
                return "canonical_calculation_lineage_mismatch"
            if (
                definition.output_field != fact.canonical_field
                or definition.output_unit != fact.unit
            ):
                return "canonical_calculation_output_mismatch"
            if lineage.input_artifact_sha256 != fact.artifact_sha256:
                return "selected_source_fact_adapter_artifact_mismatch"
            adapter = self._fact_adapters.get(
                (lineage.calculation_id, lineage.calculation_version)
            )
            if adapter is None:
                return "canonical_fact_adapter_required"
            try:
                adapter_result = adapter(
                    artifact, fact.source_span_start, fact.source_span_end
                )
            except Exception:
                return "canonical_fact_adapter_failed"
            if (
                adapter_result.canonical_field != fact.canonical_field
                or adapter_result.normalized_value != fact.normalized_value
                or adapter_result.unit != fact.unit
            ):
                return "canonical_fact_adapter_output_mismatch"
            expected_result_digest = stable_decision_value_digest(
                canonical_field=fact.canonical_field,
                normalized_value=fact.normalized_value,
                unit=fact.unit,
            )
            if lineage.result_digest != expected_result_digest:
                return "selected_source_fact_normalized_value_digest_mismatch"
        return None

    @staticmethod
    def _calculation_binding_failure_for_definition(
        fact: DecisionFact,
        definition: CalculationDefinition,
    ) -> str | None:
        lineage = fact.calculation_lineage
        if lineage is None:
            return "canonical fact adapter required"
        try:
            validate_calculation_lineage(definition, lineage)
        except ValueError:
            return "canonical calculation lineage mismatch"
        if (
            definition.output_field != fact.canonical_field
            or definition.output_unit != fact.unit
        ):
            return "canonical calculation output mismatch"
        return None

    def _calculation_binding_failure(
        self, facts: tuple[DecisionFact, ...]
    ) -> str | None:
        for fact in facts:
            lineage = fact.calculation_lineage
            if lineage is None:
                return "canonical_fact_adapter_required"
            definition = self._calculations_by_key.get(
                (lineage.calculation_id, lineage.calculation_version)
            )
            if definition is None:
                return "canonical_calculation_definition_required"
            failure = self._calculation_binding_failure_for_definition(fact, definition)
            if failure is not None:
                return failure.replace(" ", "_")
        return None

    def _trusted_run_binding_failure(
        self,
        admitted_evidence: EvidenceState,
    ) -> str | None:
        trusted = self._trusted_run_evidence
        if trusted is None:
            return "trusted_run_evidence_unavailable"

        trusted_payload = _semantic_evidence_payload(trusted)
        admitted_payload = _semantic_evidence_payload(admitted_evidence)
        for field in (
            "contract_version",
            "instrument_identity",
            "market_snapshot",
        ):
            if admitted_payload[field] != trusted_payload[field]:
                return f"trusted_run_{field}_mismatch"
        for field in (
            "material_claims",
            "source_facts",
            "source_artifacts",
            "claim_validations",
            "acquisition_outcomes",
        ):
            if not set(trusted_payload[field]).issubset(admitted_payload[field]):
                return f"trusted_run_{field}_mismatch"

        severity = {
            EvidenceStatus.NOT_APPLICABLE: -1,
            EvidenceStatus.AVAILABLE: 0,
            EvidenceStatus.UNAVAILABLE: 1,
            EvidenceStatus.CONFLICTED: 2,
        }

        def source_bindings(
            evidence: EvidenceState,
        ) -> dict[str, tuple[int, bool]]:
            bindings: dict[str, tuple[int, bool]] = {}
            for source in evidence.sources:
                candidate = (severity[source.status], source.required)
                prior = bindings.get(source.source_id)
                if prior is None:
                    bindings[source.source_id] = candidate
                else:
                    bindings[source.source_id] = (
                        max(prior[0], candidate[0]),
                        prior[1] or candidate[1],
                    )
            return bindings

        admitted_sources = source_bindings(admitted_evidence)
        for source_id, trusted_binding in source_bindings(trusted).items():
            admitted_binding = admitted_sources.get(source_id)
            if (
                admitted_binding is None
                or admitted_binding[0] < trusted_binding[0]
                or (trusted_binding[1] and not admitted_binding[1])
            ):
                return "trusted_run_sources_mismatch"
        return None

    def _gate_adapter_failure(
        self,
        facts: tuple[DecisionFact, ...],
        trusted: EvidenceState | None,
    ) -> str | None:
        resolver = self._artifact_resolver
        for fact in facts:
            lineage = fact.calculation_lineage
            if lineage is None:
                return "canonical_fact_adapter_required"
            if trusted is None:
                return "decision_admitted_evidence_unavailable"
            trusted_artifact = next(
                (
                    artifact
                    for artifact in trusted.source_artifacts
                    if (
                        artifact.artifact_sha256,
                        artifact.source_ref,
                    )
                    == (fact.artifact_sha256, fact.source_ref)
                ),
                None,
            )
            artifact = (
                resolver(fact.artifact_sha256, fact.source_ref)
                if resolver is not None
                else trusted_artifact
            )
            if artifact is None:
                return "decision_source_artifact_unavailable"
            if trusted_artifact is None or (
                trusted_artifact.artifact_sha256 != artifact.artifact_sha256
                or trusted_artifact.source_ref != artifact.source_ref
                or trusted_artifact.raw_text != artifact.raw_text
            ):
                return "decision_source_artifact_mismatch"
            if (
                artifact.artifact_sha256 != fact.artifact_sha256
                or artifact.source_ref != fact.source_ref
                or sha256(artifact.raw_text.encode()).hexdigest()
                != fact.artifact_sha256
                or fact.source_span_end > len(artifact.raw_text)
            ):
                return "decision_source_artifact_mismatch"
            adapter = self._fact_adapters.get(
                (lineage.calculation_id, lineage.calculation_version)
            )
            if adapter is None:
                return "canonical_fact_adapter_required"
            try:
                result = adapter(
                    artifact, fact.source_span_start, fact.source_span_end
                )
            except Exception:
                return "canonical_fact_adapter_failed"
            if (
                result.canonical_field != fact.canonical_field
                or result.normalized_value != fact.normalized_value
                or result.unit != fact.unit
                or (
                    result.effective_range_start is not None
                    and result.effective_range_start
                    != lineage.effective_range_start
                )
                or (
                    result.effective_range_end is not None
                    and result.effective_range_end != lineage.effective_range_end
                )
                or (
                    result.observations_used is not None
                    and result.observations_used != lineage.observations_used
                )
            ):
                return "canonical_fact_adapter_output_mismatch"
        return None

    def _gate_ledger_failure(
        self,
        context: ValidatedDecisionContext,
        checkpoint_evidence: EvidenceState,
    ) -> str | None:
        trusted = self._trusted_admitted_evidence_by_context_id.get(
            context.context_id
        )
        if trusted is None:
            return "decision_admitted_evidence_unavailable"
        if _semantic_evidence_payload(
            checkpoint_evidence
        ) != _semantic_evidence_payload(trusted):
            return "decision_admitted_evidence_mismatch"
        if self._integrity_status(trusted) is not context.integrity_status:
            return "decision_evidence_integrity_mismatch"
        if (
            trusted.market_snapshot is not None
            and context.as_of_date.isoformat()
            != trusted.market_snapshot.requested_date
        ):
            return "decision_as_of_date_mismatch"

        source_facts_by_id: dict[str, SourceFact] = {}
        for source_fact in checkpoint_evidence.source_facts:
            prior = source_facts_by_id.get(source_fact.fact_id)
            if prior is not None and prior.model_dump(
                mode="python",
                exclude={"tool_call_id"},
            ) != source_fact.model_dump(
                mode="python",
                exclude={"tool_call_id"},
            ):
                return "decision_source_fact_ledger_conflict"
            source_facts_by_id[source_fact.fact_id] = source_fact

        artifacts_by_key: dict[tuple[str, str], SourceArtifact] = {}
        for artifact in checkpoint_evidence.source_artifacts:
            key = (artifact.artifact_sha256, artifact.source_ref)
            prior = artifacts_by_key.get(key)
            if prior is not None and prior.raw_text != artifact.raw_text:
                return "decision_source_artifact_ledger_conflict"
            artifacts_by_key[key] = artifact

        for fact in context.facts:
            source_fact = source_facts_by_id.get(fact.fact_id)
            if source_fact is None:
                return "decision_source_fact_unavailable"
            trusted_source_fact = next(
                (
                    candidate
                    for candidate in trusted.source_facts
                    if candidate.fact_id == fact.fact_id
                ),
                None,
            )
            if (
                trusted_source_fact is None
                or source_fact.model_dump(
                    mode="python",
                    exclude={"tool_call_id"},
                )
                != trusted_source_fact.model_dump(
                    mode="python",
                    exclude={"tool_call_id"},
                )
                or _project_fact(trusted_source_fact) != fact
            ):
                return "decision_source_fact_mismatch"

            identity = trusted.instrument_identity
            if identity is None or not identity.is_authoritative:
                return "decision_instrument_identity_unavailable"
            if checkpoint_evidence.instrument_identity != identity:
                return "decision_instrument_identity_mismatch"
            if (
                context.instrument.symbol != identity.symbol
                or context.instrument.venue != identity.venue
                or context.instrument.instrument_kind != identity.instrument_kind
                or context.instrument.currency != identity.currency
                or fact.instrument_symbol != identity.symbol
            ):
                return "decision_instrument_identity_mismatch"

            expected_fact_id = stable_source_fact_id(
                source_ref=trusted_source_fact.source_ref,
                artifact_sha256=trusted_source_fact.artifact_sha256,
                source_span_start=trusted_source_fact.source_span_start,
                source_span_end=trusted_source_fact.source_span_end,
                canonical_field=trusted_source_fact.canonical_field,
                instrument_symbol=trusted_source_fact.instrument_symbol,
                effective_date=trusted_source_fact.effective_date,
            )
            if trusted_source_fact.fact_id != expected_fact_id:
                return "decision_source_fact_id_mismatch"
            lineage = trusted_source_fact.calculation_lineage
            if (
                lineage is None
                or lineage.calculation_id not in trusted_source_fact.calculation_ids
            ):
                return "decision_source_fact_lineage_mismatch"

            artifact = artifacts_by_key.get(
                (
                    trusted_source_fact.artifact_sha256,
                    trusted_source_fact.source_ref,
                )
            )
            trusted_artifact = next(
                (
                    candidate
                    for candidate in trusted.source_artifacts
                    if (
                        candidate.artifact_sha256,
                        candidate.source_ref,
                    )
                    == (
                        trusted_source_fact.artifact_sha256,
                        trusted_source_fact.source_ref,
                    )
                ),
                None,
            )
            if artifact is None or trusted_artifact is None:
                return "decision_source_artifact_unavailable"
            if (
                artifact.artifact_sha256 != trusted_artifact.artifact_sha256
                or artifact.source_ref != trusted_artifact.source_ref
                or artifact.raw_text != trusted_artifact.raw_text
            ):
                return "decision_source_artifact_mismatch"
            if trusted_source_fact.source_span_end > len(trusted_artifact.raw_text):
                return "decision_source_fact_span_invalid"
            excerpt = trusted_artifact.raw_text[
                trusted_source_fact.source_span_start : trusted_source_fact.source_span_end
            ]
            if trusted_source_fact.raw_text != excerpt:
                return "decision_source_fact_excerpt_mismatch"

            definition = self._calculations_by_key.get(
                (lineage.calculation_id, lineage.calculation_version)
            )
            if definition is None:
                return "canonical_calculation_definition_required"
            if definition.input_frequency != "trading_day":
                continue
            snapshot = trusted.market_snapshot
            if snapshot is None:
                return "decision_market_snapshot_unavailable"
            if checkpoint_evidence.market_snapshot != snapshot:
                return "decision_market_snapshot_mismatch"
            expected_snapshot_id = stable_market_snapshot_id(
                symbol=snapshot.symbol,
                provider=snapshot.provider,
                adjustment_basis=snapshot.adjustment_basis,
                requested_date=snapshot.requested_date,
                effective_trading_date=snapshot.effective_trading_date,
                frame_sha256=snapshot.frame_sha256,
                history_rows=snapshot.history_rows,
            )
            if snapshot.snapshot_id != expected_snapshot_id:
                return "decision_market_snapshot_id_mismatch"
            if (
                snapshot.symbol != identity.symbol
                or snapshot.frame_sha256 != lineage.input_artifact_sha256
                or snapshot.snapshot_id != lineage.input_snapshot_id
                or snapshot.adjustment_basis != lineage.adjustment_basis
                or snapshot.effective_trading_date != fact.effective_date
                or snapshot.effective_trading_date != lineage.effective_range_end
                or snapshot.history_rows < lineage.observations_used
            ):
                return "decision_market_snapshot_binding_mismatch"
        return None

    @staticmethod
    def _has_selected_conflict(facts: tuple[DecisionFact, ...]) -> bool:
        values_by_field: dict[str, set[str]] = {}
        for fact in facts:
            values_by_field.setdefault(fact.canonical_field, set()).add(
                _canonical_json(
                    {
                        "value": fact.model_dump(mode="json")["normalized_value"],
                        "unit": fact.unit,
                        "effective_date": fact.effective_date,
                    }
                )
            )
        return any(len(values) > 1 for values in values_by_field.values())

    @staticmethod
    def _integrity_status(evidence: EvidenceState) -> EvidenceIntegrityStatus:
        if any(source.status is EvidenceStatus.CONFLICTED for source in evidence.sources):
            return EvidenceIntegrityStatus.CONFLICTED
        if any(
            source.required and source.status is not EvidenceStatus.AVAILABLE
            for source in evidence.sources
        ):
            return EvidenceIntegrityStatus.INSUFFICIENT
        if any(
            not source.required and source.status is not EvidenceStatus.AVAILABLE
            for source in evidence.sources
        ):
            return EvidenceIntegrityStatus.DEGRADED
        return EvidenceIntegrityStatus.DECISION_READY

    @staticmethod
    def _assertion_matches_rule(
        assertion: DecisionAssertion,
        rule: StrategyRuleDefinition,
    ) -> bool:
        return (
            assertion.rule_id == rule.rule_id
            and assertion.rule_version == rule.version
            and assertion.target_rating == rule.target_rating
            and assertion.polarity == rule.polarity
            and assertion.horizon == rule.horizon
            and assertion.predicate_id == rule.predicate_id
            and assertion.comparator == rule.comparator
            and assertion.threshold == rule.threshold
        )


__all__ = [
    "ArtifactResolver",
    "CanonicalFactAdapter",
    "CanonicalFactAdapterResult",
    "DECISION_POLICY_CONTRACT_VERSION",
    "DecisionAssertion",
    "DecisionContextBlocked",
    "DecisionContextBuildResult",
    "DecisionContextBuilt",
    "DecisionFact",
    "DecisionGateResultV2",
    "DecisionHorizon",
    "DecisionInstrument",
    "DecisionPolicyEngine",
    "DirectionSelection",
    "deterministic_direction_selection",
    "EvidenceIntegrityStatus",
    "HorizonUnit",
    "MissingFactBehavior",
    "RuleApplicationRequest",
    "RuleEvaluator",
    "RulePolarity",
    "RulePredicateResult",
    "StrategyRuleDefinition",
    "stable_decision_value_digest",
    "TradingDecisionContract",
    "ValidatedDecisionContext",
]
