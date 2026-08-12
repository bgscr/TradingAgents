"""Closed provider-neutral contracts for acquired financial periods."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.dataflows.financial_eligibility_policy import (
    MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
    MainlandFinancialEligibilityProjectionBinding,
    MainlandFinancialPeriodTargetConstruction,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    FinancialTemporalEligibilityCheckpointProjection,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestAttemptEvent,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    InstrumentIdentityEvidence,
)

FINANCIAL_CONTRACT_VERSION = "1.0"
FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION = "financial-provider-dataset:v1"
FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION = "financial-provider-artifact:v1"
FINANCIAL_PERIOD_IDENTITY_VERSION = "financial-period:v1"
FINANCIAL_STATEMENT_DECLARATION_V1 = "financial-field-declarations-v1"
FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2 = (
    "financial-field-declarations-tushare-v2"
)
FINANCIAL_RATIO_DECLARATION_V1 = "financial-ratio-declarations-v1"
FINANCIAL_RATIO_DECLARATION_V2 = "financial-ratio-declarations-v2"
FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1 = (
    "financial-ratio-declarations-baostock-v1"
)
FINANCIAL_PERIOD_SELECTION_V1 = "financial-period-selection-v1"
FINANCIAL_PERIOD_SELECTION_V2 = "financial-period-selection-v2"
FINANCIAL_PERIOD_SELECTION_TUSHARE_STATEMENT_V2 = (
    "financial-period-selection-tushare-statement-v2"
)
FINANCIAL_PERIOD_SELECTION_BAOSTOCK_V1 = (
    "financial-period-selection-baostock-v1"
)
FINANCIAL_PERIOD_SELECTION_V3 = "financial-period-selection-v3"
FINANCIAL_PROVIDER_CANDIDATE_V2 = "financial-provider-candidate-v2"
FINANCIAL_MANIFEST_V1 = "financial-manifest-v1"
FINANCIAL_MANIFEST_V2 = "financial-manifest-v2"
FINANCIAL_MANIFEST_V3 = "financial-manifest-v3"
FINANCIAL_MANIFEST_V4 = "financial-manifest-v4"
FINANCIAL_FAMILY_REQUIREMENTS_V1 = "financial-family-requirements-v1"
FINANCIAL_FAMILY_ASSESSMENT_V2 = "financial-family-assessment-v2"
FINANCIAL_FAMILY_AGGREGATE_V2 = "financial-family-aggregate-v2"

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UNORDERED_REQUEST_KEYS = frozenset({"fields", "field_set", "requested_fields"})
_UNORDERED_PAYLOAD_KEYS = frozenset({"data", "records", "rows"})
_UNSAFE_CONTRACT_TEXT = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "client_secret",
    "credential",
    "password",
    "raw_error",
    "secret",
    "sk-",
    "token=",
    "traceback",
)
_UNSAFE_TOKEN_LIKE_TEXT = re.compile(
    r"(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
    r"|\b(?:[A-Za-z][A-Za-z0-9_]*Error|"
    r"[A-Za-z][A-Za-z0-9_]*Exception|Exception):"
    r")",
    re.IGNORECASE,
)
_UNSAFE_URL_QUERY = re.compile(r"https?://\S*\?", re.IGNORECASE)
_SECRET_IDENTITY_KEY_MARKERS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "secret",
    "token",
)
_NONMATERIAL_IDENTITY_KEYS = frozenset(
    {
        "analysis",
        "attempt_order",
        "call_index",
        "call_order",
        "commentary",
        "generated_text",
        "model_prose",
        "narrative",
        "pid",
        "process_id",
        "prompt",
        "provider_order",
        "reasoning",
        "response_text",
        "sequence_number",
        "thread_id",
        "worker_id",
    }
)
_CLASSIFIER_METADATA_KEYS = frozenset(
    {
        "company_type",
        "comp_type",
        "industry",
        "industry_name",
        "provider_company_type",
        "provider_industry",
        "sector",
        "sector_name",
    }
)


def _assert_safe_contract_payload(value: object) -> None:
    if isinstance(value, BaseModel):
        _assert_safe_contract_payload(value.model_dump(mode="json"))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_safe_contract_payload(str(key))
            _assert_safe_contract_payload(item)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _assert_safe_contract_payload(item)
        return
    if isinstance(value, str) and (
        any(marker in value.casefold() for marker in _UNSAFE_CONTRACT_TEXT)
        or _UNSAFE_TOKEN_LIKE_TEXT.search(value) is not None
    ):
        raise ValueError("financial contract text contains unsafe operational data")


def assert_safe_financial_contract_payload(value: object) -> None:
    """Reject credential-, token-, and raw-error-like contract text."""

    _assert_safe_contract_payload(value)


def assert_safe_financial_projection(value: object) -> None:
    """Reject unsafe contract text plus URL queries at external projections."""

    _assert_safe_contract_payload(value)
    projected = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value
    )
    serialized = json.dumps(
        projected,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if _UNSAFE_URL_QUERY.search(serialized) is not None:
        raise ValueError("financial projection contains a URL query")


def _project_identity_material(value: object) -> object:
    """Remove operational/secret fields before canonical identity hashing."""

    if isinstance(value, BaseModel):
        return _project_identity_material(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in _NONMATERIAL_IDENTITY_KEYS or any(
                marker in normalized_key for marker in _SECRET_IDENTITY_KEY_MARKERS
            ):
                continue
            projected[str(key)] = _project_identity_material(item)
        return projected
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_project_identity_material(item) for item in value]
    if isinstance(value, set | frozenset):
        projected_items = [_project_identity_material(item) for item in value]
        return sorted(projected_items, key=_canonical_json)
    return value


def _canonicalize_identity_data(
    value: object,
    *,
    sort_sequence: bool = False,
    unordered_mapping_keys: frozenset[str] = frozenset(),
) -> object:
    if isinstance(value, BaseModel):
        return _canonicalize_identity_data(
            value.model_dump(mode="json"),
            sort_sequence=sort_sequence,
            unordered_mapping_keys=unordered_mapping_keys,
        )
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize_identity_data(
                item,
                sort_sequence=str(key).casefold() in unordered_mapping_keys,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        normalized = [
            _canonicalize_identity_data(
                item,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for item in value
        ]
        return sorted(normalized, key=_canonical_json) if sort_sequence else normalized
    if isinstance(value, set | frozenset):
        normalized = [
            _canonicalize_identity_data(
                item,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for item in value
        ]
        return sorted(normalized, key=_canonical_json)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_digest(
    value: object,
    *,
    unordered_top_level_sequence: bool = False,
    unordered_mapping_keys: frozenset[str] = frozenset(),
) -> str:
    canonical = _canonicalize_identity_data(
        value,
        sort_sequence=unordered_top_level_sequence,
        unordered_mapping_keys=unordered_mapping_keys,
    )
    return sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("financial observation time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FinancialCapability(str, Enum):
    STATEMENT = "financial_statement"
    RATIO_FAMILY = "financial_ratio_family"


class FinancialStatementType(str, Enum):
    COMPREHENSIVE_FUNDAMENTALS = "comprehensive_fundamentals"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    INCOME_STATEMENT = "income_statement"


class FinancialReportingFrequency(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class FinancialRatioFamily(str, Enum):
    PROFIT = "profit"
    OPERATION = "operation"
    GROWTH = "growth"
    BALANCE = "balance"
    CASH_FLOW = "cash_flow"
    DUPONT = "dupont"


class FinancialCompanyType(str, Enum):
    BANK = "bank"
    INDUSTRIAL_NON_BANK = "industrial/non-bank"
    INSURANCE = "insurance"
    SECURITIES = "securities"
    OTHER_FINANCIAL = "other-financial"
    UNKNOWN = "unknown"


class FinancialConsolidationScope(str, Enum):
    CONSOLIDATED = "consolidated"
    PARENT = "parent"
    UNKNOWN = "unknown"


class FinancialCompanyTypeResolutionMethod(str, Enum):
    QUALIFIED_PROVIDER = "qualified_provider"
    CLASSIFIER = "classifier"
    UNRESOLVED = "unresolved"


class FinancialPeriodDisposition(str, Enum):
    ACCEPTED = "accepted"
    CURRENT_ONLY = "current_only"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    UNSELECTED = "unselected"


class FinancialAvailabilityScope(str, Enum):
    CURRENT_ONLY = "current_only"
    STRICT_PIT_ELIGIBLE = "strict_pit_eligible"


class FinancialAvailabilityBasis(str, Enum):
    ANN_DATE_WITHOUT_FIRST_PUBLICATION = (
        "ann_date_without_first_publication"
    )
    ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE = (
        "ann_date_with_strict_pit_cutoff_failure"
    )
    ANN_DATE_FIRST_PUBLICATION_AND_OBSERVED_REVISION = (
        "ann_date_first_publication_and_observed_revision"
    )


class FinancialPeriodRejectionReason(str, Enum):
    PERMISSION_DENIED = "permission_denied"
    INSUFFICIENT_COMPLETENESS = "insufficient_completeness"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    CAPABILITY_MISMATCH = "capability_mismatch"
    MISSING_FIRST_PUBLICATION_DATE = "missing_first_publication_date"
    PIT_CUTOFF_NOT_SATISFIED = "pit_cutoff_not_satisfied"
    UNSTABLE_REVISION_IDENTITY = "unstable_revision_identity"
    CONFLICTING_CRITICAL_VALUES = "conflicting_critical_values"
    UNSUPPORTED_SINCE_LISTING_COVERAGE = "unsupported_since_listing_coverage"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    INCOMPATIBLE_CURRENCY = "incompatible_currency"
    INCOMPATIBLE_CONSOLIDATION_SCOPE = "incompatible_consolidation_scope"
    INCOMPATIBLE_COMPANY_TYPE = "incompatible_company_type"
    AMBIGUOUS_COMPANY_TYPE = "ambiguous_company_type"
    ANNOUNCEMENT_AFTER_CUTOFF = "announcement_after_cutoff"
    MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT = (
        "missing_first_publication_for_strict_pit"
    )


class FinancialEvidenceDisposition(str, Enum):
    CURRENT_ONLY = "current_only"
    STRICT_PIT_ELIGIBLE = "strict_pit_eligible"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"


class FinancialFamilyRequirementStatus(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class FinancialFamilyRequirementAuthority(str, Enum):
    CANONICAL_EVIDENCE_CONTRACT = "canonical_evidence_contract"
    CAPABILITY_PROFILE = "capability_profile"
    STRATEGY_RULES = "strategy_rules"


class FinancialFamilyAssessmentReason(str, Enum):
    CURRENT_ONLY_LINEAGE = "current_only_lineage"
    OPTIONAL_FAMILY_INCOMPLETE = "optional_family_incomplete"
    REQUIRED_FAMILY_INCOMPLETE = "required_family_incomplete"
    MATERIAL_PREMISE_FAMILY_INCOMPLETE = "material_premise_family_incomplete"
    MATERIAL_PREMISE_FAMILY_CONFLICTED = "material_premise_family_conflicted"
    NO_COMPLETE_FAMILY = "no_complete_family"
    STRICT_PIT_CURRENT_ONLY_CANDIDATES = "strict_pit_current_only_candidates"
    PUBLICATION_METADATA_UNPROVEN = "publication_metadata_unproven"
    CRITICAL_FAMILY_PERIOD_CONFLICT = "critical_family_period_conflict"


class FinancialFamilyAuthorityBinding(BaseModel):
    """One applicable authority's fixed requirement for one indicator family."""

    model_config = _CLOSED_MODEL_CONFIG

    authority: FinancialFamilyRequirementAuthority
    authority_id: str = Field(pattern=_SAFE_ID_PATTERN)
    required: bool


class FinancialMaterialPremiseRequirement(BaseModel):
    """A proposed material premise may only add required family bindings."""

    model_config = _CLOSED_MODEL_CONFIG

    premise_id: str = Field(pattern=_SAFE_ID_PATTERN)
    required_families: tuple[FinancialRatioFamily, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_premise(self) -> FinancialMaterialPremiseRequirement:
        canonical = tuple(
            family
            for family in FinancialRatioFamily
            if family in self.required_families
        )
        if self.required_families != canonical:
            raise ValueError("material-premise families must be unique and canonical")
        return self


class FinancialFamilyRequirement(BaseModel):
    """Required/optional status derived from all fixed authorities and premises."""

    model_config = _CLOSED_MODEL_CONFIG

    family: FinancialRatioFamily
    status: FinancialFamilyRequirementStatus
    authority_bindings: tuple[FinancialFamilyAuthorityBinding, ...] = Field(
        min_length=3,
        max_length=3,
    )
    material_premise_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_requirement(self) -> FinancialFamilyRequirement:
        canonical_bindings = tuple(
            binding
            for authority in FinancialFamilyRequirementAuthority
            for binding in self.authority_bindings
            if binding.authority is authority
        )
        if (
            self.authority_bindings != canonical_bindings
            or tuple(binding.authority for binding in self.authority_bindings)
            != tuple(FinancialFamilyRequirementAuthority)
        ):
            raise ValueError("family requirement authorities must be exact and canonical")
        canonical_premises = tuple(sorted(set(self.material_premise_ids)))
        if self.material_premise_ids != canonical_premises:
            raise ValueError("material-premise identities must be unique and canonical")
        expected = (
            FinancialFamilyRequirementStatus.REQUIRED
            if any(binding.required for binding in self.authority_bindings)
            or self.material_premise_ids
            else FinancialFamilyRequirementStatus.OPTIONAL
        )
        if self.status is not expected:
            raise ValueError(
                "family requirement status must be derived from every authority and premise"
            )
        assert_safe_financial_contract_payload(self.model_dump(mode="json"))
        return self


class FinancialIndicatorEvidenceRequirements(BaseModel):
    """Closed authority snapshot for one v2 indicator-family assessment."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-family-requirements-v1"] = (
        FINANCIAL_FAMILY_REQUIREMENTS_V1
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] = MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
    requested_families: tuple[FinancialRatioFamily, ...] = Field(min_length=1)
    families: tuple[FinancialFamilyRequirement, ...] = Field(min_length=1)
    material_premises: tuple[FinancialMaterialPremiseRequirement, ...] = ()
    requirements_identity: str = Field(
        pattern=r"^financial-family-requirements:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        requested_families: Sequence[FinancialRatioFamily],
        canonical_evidence_contract_id: str,
        canonical_evidence_contract_required_families: Sequence[
            FinancialRatioFamily
        ] = (),
        capability_profile_id: str,
        capability_profile_required_families: Sequence[FinancialRatioFamily] = (),
        strategy_rules_id: str,
        strategy_rules_required_families: Sequence[FinancialRatioFamily] = (),
        material_premises: Sequence[FinancialMaterialPremiseRequirement] = (),
    ) -> FinancialIndicatorEvidenceRequirements:
        requested_set = set(requested_families)
        canonical_requested = tuple(
            family for family in FinancialRatioFamily if family in requested_set
        )
        if len(canonical_requested) != len(tuple(requested_families)):
            raise ValueError("requested indicator families must be unique and canonical")
        authority_inputs = (
            (
                FinancialFamilyRequirementAuthority.CANONICAL_EVIDENCE_CONTRACT,
                canonical_evidence_contract_id,
                set(canonical_evidence_contract_required_families),
            ),
            (
                FinancialFamilyRequirementAuthority.CAPABILITY_PROFILE,
                capability_profile_id,
                set(capability_profile_required_families),
            ),
            (
                FinancialFamilyRequirementAuthority.STRATEGY_RULES,
                strategy_rules_id,
                set(strategy_rules_required_families),
            ),
        )
        authoritative_required = set().union(
            *(required for _, _, required in authority_inputs)
        )
        premise_required = {
            family
            for premise in material_premises
            for family in premise.required_families
        }
        if not authoritative_required | premise_required <= requested_set:
            raise ValueError("family authorities cannot require an unrequested family")
        canonical_premises = tuple(
            sorted(material_premises, key=lambda item: item.premise_id)
        )
        if len({item.premise_id for item in canonical_premises}) != len(
            canonical_premises
        ):
            raise ValueError("material-premise identities must be unique")
        family_requirements = tuple(
            FinancialFamilyRequirement(
                family=family,
                status=(
                    FinancialFamilyRequirementStatus.REQUIRED
                    if family in authoritative_required | premise_required
                    else FinancialFamilyRequirementStatus.OPTIONAL
                ),
                authority_bindings=tuple(
                    FinancialFamilyAuthorityBinding(
                        authority=authority,
                        authority_id=authority_id,
                        required=family in required,
                    )
                    for authority, authority_id, required in authority_inputs
                ),
                material_premise_ids=tuple(
                    premise.premise_id
                    for premise in canonical_premises
                    if family in premise.required_families
                ),
            )
            for family in canonical_requested
        )
        payload = {
            "contract_version": FINANCIAL_FAMILY_REQUIREMENTS_V1,
            "eligibility_policy_version": MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
            "requested_families": [family.value for family in canonical_requested],
            "families": [item.model_dump(mode="json") for item in family_requirements],
            "material_premises": [
                item.model_dump(mode="json") for item in canonical_premises
            ],
        }
        return cls(
            **payload,
            requirements_identity=(
                "financial-family-requirements:v1:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_requirements(self) -> FinancialIndicatorEvidenceRequirements:
        canonical_requested = tuple(
            family
            for family in FinancialRatioFamily
            if family in self.requested_families
        )
        if self.requested_families != canonical_requested:
            raise ValueError("requested indicator families must be unique and canonical")
        if tuple(item.family for item in self.families) != self.requested_families:
            raise ValueError("family requirements must cover every requested family")
        if self.material_premises != tuple(
            sorted(self.material_premises, key=lambda item: item.premise_id)
        ) or len({item.premise_id for item in self.material_premises}) != len(
            self.material_premises
        ):
            raise ValueError("material premises must be unique and canonical")
        if any(
            not set(premise.required_families) <= set(self.requested_families)
            for premise in self.material_premises
        ):
            raise ValueError("material premise requires an unrequested family")
        for authority in FinancialFamilyRequirementAuthority:
            authority_ids = {
                binding.authority_id
                for family in self.families
                for binding in family.authority_bindings
                if binding.authority is authority
            }
            if len(authority_ids) != 1:
                raise ValueError("family requirement authority identity is inconsistent")
        premise_ids_by_family = {
            family: tuple(
                premise.premise_id
                for premise in self.material_premises
                if family in premise.required_families
            )
            for family in self.requested_families
        }
        if any(
            item.material_premise_ids != premise_ids_by_family[item.family]
            for item in self.families
        ):
            raise ValueError(
                "family requirement status must be derived from every authority and premise"
            )
        payload = self.model_dump(mode="json", exclude={"requirements_identity"})
        expected = "financial-family-requirements:v1:" + _canonical_digest(payload)
        if self.requirements_identity != expected:
            raise ValueError("financial family requirements identity mismatch")
        assert_safe_financial_contract_payload(payload)
        return self


class FinancialFamilyPeriodCoverage(BaseModel):
    """Payload-free measured gates and lineage for one family candidate."""

    model_config = _CLOSED_MODEL_CONFIG

    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    period: date
    disposition: FinancialPeriodDisposition
    critical_threshold: Decimal = Decimal("1")
    core_threshold: Decimal = Decimal("0.9")
    critical_coverage: Decimal = Field(ge=0, le=1)
    core_coverage: Decimal = Field(ge=0, le=1)
    declared_critical_fields: tuple[str, ...] = ()
    declared_core_fields: tuple[str, ...] = ()
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...] = ()

    @model_validator(mode="after")
    def _validate_coverage(self) -> FinancialFamilyPeriodCoverage:
        if self.critical_threshold != Decimal("1") or self.core_threshold != Decimal(
            "0.9"
        ):
            raise ValueError("family completeness thresholds are fixed")
        if self.declared_critical_fields != tuple(
            sorted(set(self.declared_critical_fields))
        ) or self.declared_core_fields != tuple(sorted(set(self.declared_core_fields))):
            raise ValueError("family coverage declarations must be canonical")
        canonical_reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.rejection_reasons
        )
        if self.rejection_reasons != canonical_reasons:
            raise ValueError("family coverage reasons must be canonical")
        return self


class FinancialMaterialPremiseFamilyBinding(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    premise_id: str = Field(pattern=_SAFE_ID_PATTERN)
    family: FinancialRatioFamily
    affected_periods: tuple[date, ...]

    @model_validator(mode="after")
    def _validate_binding(self) -> FinancialMaterialPremiseFamilyBinding:
        if self.affected_periods != _canonical_dates(
            self.affected_periods,
            reverse=True,
        ):
            raise ValueError("material-premise periods must be canonical")
        return self


class FinancialIndicatorFamilyAssessment(BaseModel):
    """Closed independent outcome for one requested indicator family."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-family-assessment-v2"] = (
        FINANCIAL_FAMILY_ASSESSMENT_V2
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] = MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
    requested_analysis_scope: FinancialAvailabilityScope
    company_type: FinancialCompanyType
    family: FinancialRatioFamily
    target_periods: tuple[date, ...]
    candidate_identities: tuple[str, ...]
    selected_periods: tuple[date, ...]
    current_only_periods: tuple[date, ...]
    rejected_periods: tuple[date, ...]
    conflicted_periods: tuple[date, ...]
    missing_periods: tuple[date, ...]
    coverage: tuple[FinancialFamilyPeriodCoverage, ...]
    requirement: FinancialFamilyRequirement
    missing_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyBinding, ...
    ] = ()
    conflicted_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyBinding, ...
    ] = ()
    complete: bool
    disposition: FinancialEvidenceDisposition
    typed_reasons: tuple[FinancialFamilyAssessmentReason, ...]
    artifact_identities: tuple[str, ...]
    selection_identities: tuple[str, ...]
    conflict_identities: tuple[str, ...]
    assessment_ref: str = Field(
        pattern=r"^financial-family-assessment=sha256:[0-9a-f]{64}$"
    )

    @classmethod
    def create(cls, **values: object) -> FinancialIndicatorFamilyAssessment:
        payload = {
            "contract_version": FINANCIAL_FAMILY_ASSESSMENT_V2,
            "eligibility_policy_version": MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
            **values,
        }
        return cls(
            **payload,
            assessment_ref=(
                "financial-family-assessment=sha256:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_family_assessment(self) -> FinancialIndicatorFamilyAssessment:
        if self.requirement.family is not self.family:
            raise ValueError("family assessment requirement binding is invalid")
        for values in (
            self.target_periods,
            self.selected_periods,
            self.current_only_periods,
            self.rejected_periods,
            self.conflicted_periods,
            self.missing_periods,
        ):
            if values != _canonical_dates(values, reverse=True):
                raise ValueError("family assessment periods must be canonical")
        if not set(self.current_only_periods) <= set(self.selected_periods):
            raise ValueError("current-only family periods must be selected")
        if (
            set(self.selected_periods) & set(self.rejected_periods)
            or set(self.selected_periods) & set(self.conflicted_periods)
            or set(self.rejected_periods) & set(self.conflicted_periods)
            or not set(self.rejected_periods) <= set(self.missing_periods)
        ):
            raise ValueError("family period outcomes are contradictory")
        if not (
            set(self.selected_periods)
            | set(self.rejected_periods)
            | set(self.conflicted_periods)
            | set(self.missing_periods)
        ) <= set(self.target_periods):
            raise ValueError("family assessment contains a non-target outcome")
        expected_missing = tuple(
            period
            for period in self.target_periods
            if period not in self.selected_periods
            and period not in self.conflicted_periods
        )
        if self.missing_periods != expected_missing:
            raise ValueError("family missing periods are inconsistent")
        expected_complete = bool(self.target_periods) and (
            set(self.selected_periods) == set(self.target_periods)
            and not self.conflicted_periods
            and not self.missing_periods
        )
        if self.complete != expected_complete:
            raise ValueError("family completeness is inconsistent")
        if self.complete:
            expected_disposition = (
                FinancialEvidenceDisposition.DEGRADED
                if self.current_only_periods
                else FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE
            )
        elif self.conflicted_periods:
            expected_disposition = FinancialEvidenceDisposition.CONFLICTED
        elif self.requirement.status is FinancialFamilyRequirementStatus.REQUIRED:
            expected_disposition = FinancialEvidenceDisposition.INSUFFICIENT
        else:
            expected_disposition = FinancialEvidenceDisposition.DEGRADED
        if self.disposition is not expected_disposition:
            raise ValueError("family evidence disposition is inconsistent")
        expected_missing_bindings = tuple(
            FinancialMaterialPremiseFamilyBinding(
                premise_id=premise_id,
                family=self.family,
                affected_periods=self.missing_periods,
            )
            for premise_id in self.requirement.material_premise_ids
            if self.missing_periods
        )
        expected_conflicted_bindings = tuple(
            FinancialMaterialPremiseFamilyBinding(
                premise_id=premise_id,
                family=self.family,
                affected_periods=self.conflicted_periods,
            )
            for premise_id in self.requirement.material_premise_ids
            if self.conflicted_periods
        )
        if (
            self.missing_material_premise_bindings != expected_missing_bindings
            or self.conflicted_material_premise_bindings
            != expected_conflicted_bindings
        ):
            raise ValueError("family material-premise bindings are inconsistent")
        reason_set: set[FinancialFamilyAssessmentReason] = set()
        if self.current_only_periods:
            reason_set.add(FinancialFamilyAssessmentReason.CURRENT_ONLY_LINEAGE)
        if self.conflicted_periods:
            reason_set.add(
                FinancialFamilyAssessmentReason.CRITICAL_FAMILY_PERIOD_CONFLICT
            )
        if not self.complete and not self.conflicted_periods:
            reason_set.add(
                FinancialFamilyAssessmentReason.REQUIRED_FAMILY_INCOMPLETE
                if self.requirement.status is FinancialFamilyRequirementStatus.REQUIRED
                else FinancialFamilyAssessmentReason.OPTIONAL_FAMILY_INCOMPLETE
            )
        if expected_missing_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_INCOMPLETE
            )
        if expected_conflicted_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_CONFLICTED
            )
        expected_reasons = tuple(
            reason for reason in FinancialFamilyAssessmentReason if reason in reason_set
        )
        if self.typed_reasons != expected_reasons:
            raise ValueError("family assessment reasons are inconsistent")
        for values, label in (
            (self.candidate_identities, "candidate"),
            (self.artifact_identities, "artifact"),
            (self.selection_identities, "selection"),
            (self.conflict_identities, "conflict"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"family {label} identities must be canonical")
        if self.coverage != tuple(
            sorted(
                self.coverage,
                key=lambda item: (item.period, item.candidate_identity),
                reverse=True,
            )
        ):
            raise ValueError("family period coverage must be canonical")
        if set(self.candidate_identities) != {
            item.candidate_identity for item in self.coverage
        }:
            raise ValueError("family candidate coverage is incomplete")
        payload = self.model_dump(mode="json", exclude={"assessment_ref"})
        expected_ref = "financial-family-assessment=sha256:" + _canonical_digest(
            payload
        )
        if self.assessment_ref != expected_ref:
            raise ValueError("financial family assessment identity mismatch")
        assert_safe_financial_projection(payload)
        return self


class FinancialIndicatorAggregateAssessment(BaseModel):
    """Material-premise-aware aggregate retaining every independent family."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-family-aggregate-v2"] = (
        FINANCIAL_FAMILY_AGGREGATE_V2
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] = MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
    requested_analysis_scope: FinancialAvailabilityScope
    company_type: FinancialCompanyType
    requirements_identity: str = Field(
        pattern=r"^financial-family-requirements:v1:[0-9a-f]{64}$"
    )
    requested_families: tuple[FinancialRatioFamily, ...] = Field(min_length=1)
    family_assessments: tuple[FinancialIndicatorFamilyAssessment, ...] = Field(
        min_length=1
    )
    complete_families: tuple[FinancialRatioFamily, ...]
    missing_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyBinding, ...
    ] = ()
    conflicted_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyBinding, ...
    ] = ()
    disposition: FinancialEvidenceDisposition
    typed_reasons: tuple[FinancialFamilyAssessmentReason, ...]
    assessment_ref: str = Field(
        pattern=r"^financial-family-aggregate=sha256:[0-9a-f]{64}$"
    )

    @classmethod
    def create(cls, **values: object) -> FinancialIndicatorAggregateAssessment:
        payload = {
            "contract_version": FINANCIAL_FAMILY_AGGREGATE_V2,
            "eligibility_policy_version": MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
            **values,
        }
        return cls(
            **payload,
            assessment_ref=(
                "financial-family-aggregate=sha256:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_aggregate(self) -> FinancialIndicatorAggregateAssessment:
        canonical_requested = tuple(
            family
            for family in FinancialRatioFamily
            if family in self.requested_families
        )
        if self.requested_families != canonical_requested or tuple(
            item.family for item in self.family_assessments
        ) != canonical_requested:
            raise ValueError("aggregate family assessments must be exact and canonical")
        expected_complete = tuple(
            item.family for item in self.family_assessments if item.complete
        )
        if self.complete_families != expected_complete:
            raise ValueError("aggregate complete families are inconsistent")
        expected_missing_bindings = tuple(
            binding
            for item in self.family_assessments
            for binding in item.missing_material_premise_bindings
        )
        expected_conflicted_bindings = tuple(
            binding
            for item in self.family_assessments
            for binding in item.conflicted_material_premise_bindings
        )
        if (
            self.missing_material_premise_bindings != expected_missing_bindings
            or self.conflicted_material_premise_bindings
            != expected_conflicted_bindings
        ):
            raise ValueError("aggregate material-premise bindings are inconsistent")
        required_conflicts = tuple(
            item
            for item in self.family_assessments
            if item.conflicted_periods
            and item.requirement.status is FinancialFamilyRequirementStatus.REQUIRED
        )
        required_incomplete = tuple(
            item
            for item in self.family_assessments
            if not item.complete
            and not item.conflicted_periods
            and item.requirement.status is FinancialFamilyRequirementStatus.REQUIRED
        )
        optional_incomplete = tuple(
            item
            for item in self.family_assessments
            if not item.complete
            and item.requirement.status is FinancialFamilyRequirementStatus.OPTIONAL
        )
        publication_failed = (
            FinancialFamilyAssessmentReason.PUBLICATION_METADATA_UNPROVEN
            in self.typed_reasons
        )
        strict_current_only = (
            FinancialFamilyAssessmentReason.STRICT_PIT_CURRENT_ONLY_CANDIDATES
            in self.typed_reasons
        )
        if publication_failed or strict_current_only or not expected_complete:
            expected_disposition = FinancialEvidenceDisposition.INSUFFICIENT
        elif required_conflicts or expected_conflicted_bindings:
            expected_disposition = FinancialEvidenceDisposition.CONFLICTED
        elif required_incomplete or expected_missing_bindings:
            expected_disposition = FinancialEvidenceDisposition.INSUFFICIENT
        elif optional_incomplete or any(
            item.current_only_periods for item in self.family_assessments
        ):
            expected_disposition = FinancialEvidenceDisposition.DEGRADED
        else:
            expected_disposition = FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE
        if self.disposition is not expected_disposition:
            raise ValueError("aggregate family disposition is inconsistent")
        reason_set: set[FinancialFamilyAssessmentReason] = set()
        if publication_failed:
            reason_set.add(FinancialFamilyAssessmentReason.PUBLICATION_METADATA_UNPROVEN)
        if strict_current_only:
            reason_set.add(
                FinancialFamilyAssessmentReason.STRICT_PIT_CURRENT_ONLY_CANDIDATES
            )
        if not expected_complete:
            reason_set.add(FinancialFamilyAssessmentReason.NO_COMPLETE_FAMILY)
        if required_incomplete:
            reason_set.add(FinancialFamilyAssessmentReason.REQUIRED_FAMILY_INCOMPLETE)
        if optional_incomplete:
            reason_set.add(FinancialFamilyAssessmentReason.OPTIONAL_FAMILY_INCOMPLETE)
        if expected_missing_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_INCOMPLETE
            )
        if required_conflicts or any(
            item.conflicted_periods for item in self.family_assessments
        ):
            reason_set.add(
                FinancialFamilyAssessmentReason.CRITICAL_FAMILY_PERIOD_CONFLICT
            )
        if expected_conflicted_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_CONFLICTED
            )
        if any(item.current_only_periods for item in self.family_assessments):
            reason_set.add(FinancialFamilyAssessmentReason.CURRENT_ONLY_LINEAGE)
        expected_reasons = tuple(
            reason for reason in FinancialFamilyAssessmentReason if reason in reason_set
        )
        if self.typed_reasons != expected_reasons:
            raise ValueError("aggregate family reasons are inconsistent")
        payload = self.model_dump(mode="json", exclude={"assessment_ref"})
        expected_ref = "financial-family-aggregate=sha256:" + _canonical_digest(
            payload
        )
        if self.assessment_ref != expected_ref:
            raise ValueError("financial family aggregate identity mismatch")
        assert_safe_financial_projection(payload)
        return self


class FinancialCompanyTypeClassifierInput(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_safe_input(self) -> FinancialCompanyTypeClassifierInput:
        _assert_safe_contract_payload((self.key, self.value))
        return self


class FinancialCompanyTypeResolution(BaseModel):
    """Evidence-bearing result of deterministic company-type resolution."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    company_type: FinancialCompanyType
    method: FinancialCompanyTypeResolutionMethod
    provider_declared_type: FinancialCompanyType | None = None
    provider_declaration_qualified: bool = False
    classifier_version: Literal["financial-company-type-classifier-v1"] = (
        "financial-company-type-classifier-v1"
    )
    classifier_inputs: tuple[FinancialCompanyTypeClassifierInput, ...]
    candidate_types: tuple[FinancialCompanyType, ...]
    ambiguous: bool
    contradictory: bool

    @model_validator(mode="after")
    def _validate_resolution(self) -> FinancialCompanyTypeResolution:
        canonical_candidates = tuple(
            company_type
            for company_type in FinancialCompanyType
            if company_type in self.candidate_types
        )
        if self.candidate_types != canonical_candidates:
            raise ValueError("company-type candidates must be unique and canonical")
        if self.classifier_inputs != tuple(
            sorted(self.classifier_inputs, key=lambda item: (item.key, item.value))
        ):
            raise ValueError("company-type classifier inputs must be canonical")
        if len(self.classifier_inputs) != len(set(self.classifier_inputs)):
            raise ValueError("company-type classifier inputs must be unique")
        for classifier_input in self.classifier_inputs:
            if classifier_input.key == "present_field":
                if not classifier_input.value.replace("_", "").isalnum():
                    raise ValueError("company-type classifier field input is invalid")
            elif classifier_input.key.startswith("metadata:"):
                if (
                    classifier_input.key.removeprefix("metadata:")
                    not in _CLASSIFIER_METADATA_KEYS
                    or classifier_input.value not in _CLASSIFIER_HINT_VALUES
                ):
                    raise ValueError("company-type classifier metadata is invalid")
            else:
                raise ValueError("company-type classifier input key is invalid")
        if self.method is FinancialCompanyTypeResolutionMethod.QUALIFIED_PROVIDER:
            if (
                not self.provider_declaration_qualified
                or self.provider_declared_type in {None, FinancialCompanyType.UNKNOWN}
                or self.company_type is not self.provider_declared_type
            ):
                raise ValueError(
                    "qualified provider resolution requires its exact declaration"
                )
            expected_contradictory = bool(
                set(self.candidate_types) - {self.provider_declared_type}
            )
            if (
                self.contradictory != expected_contradictory
                or self.ambiguous != expected_contradictory
            ):
                raise ValueError("qualified provider contradiction state is invalid")
        elif self.method is FinancialCompanyTypeResolutionMethod.CLASSIFIER:
            if (
                self.company_type is FinancialCompanyType.UNKNOWN
                or self.candidate_types != (self.company_type,)
                or self.ambiguous
                or self.contradictory
            ):
                raise ValueError("classifier resolution requires one supported type")
        elif (
            self.company_type is not FinancialCompanyType.UNKNOWN
            or len(self.candidate_types) == 1
            or not self.ambiguous
            or self.contradictory != (len(self.candidate_types) > 1)
        ):
            raise ValueError("unresolved company type state is invalid")
        return self


_COMPANY_TYPE_HINTS = {
    FinancialCompanyType.BANK: frozenset({"bank", "commercial_bank", "银行"}),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: frozenset(
        {"industrial", "manufacturing", "non_bank", "non-bank", "工业"}
    ),
    FinancialCompanyType.INSURANCE: frozenset({"insurance", "insurer", "保险"}),
    FinancialCompanyType.SECURITIES: frozenset(
        {"securities", "brokerage", "broker_dealer", "证券"}
    ),
    FinancialCompanyType.OTHER_FINANCIAL: frozenset(
        {"other_financial", "finance_company", "financial_services"}
    ),
}
_CLASSIFIER_HINT_VALUES = frozenset().union(*_COMPANY_TYPE_HINTS.values())

_COMPANY_TYPE_SIGNATURES = {
    FinancialCompanyType.BANK: frozenset(
        {"customer_deposits", "loans_and_advances", "net_interest_income"}
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: frozenset(
        {"inventory", "accounts_receivable", "cost_of_revenue"}
    ),
    FinancialCompanyType.INSURANCE: frozenset(
        {"insurance_contract_liabilities", "premiums_earned", "claims_expense"}
    ),
    FinancialCompanyType.SECURITIES: frozenset(
        {
            "customer_brokerage_deposits",
            "brokerage_commission_income",
            "financial_assets_bought_for_resale",
        }
    ),
    FinancialCompanyType.OTHER_FINANCIAL: frozenset(
        {"interest_bearing_assets", "customer_financing", "financial_service_revenue"}
    ),
}


def resolve_financial_company_type(
    *,
    provider_declared_type: FinancialCompanyType | None,
    provider_declaration_qualified: bool,
    classifier_metadata: Mapping[str, object],
    present_fields: Sequence[str],
) -> FinancialCompanyTypeResolution:
    """Prefer qualified declarations while detecting contradictory signatures."""

    normalized_metadata: dict[str, str] = {}
    for key, value in classifier_metadata.items():
        normalized_key = str(key).strip().casefold()
        if normalized_key not in _CLASSIFIER_METADATA_KEYS:
            raise ValueError("unsupported financial company-type classifier metadata")
        normalized_value = str(value).strip().casefold().replace(" ", "_")
        if normalized_value not in _CLASSIFIER_HINT_VALUES:
            raise ValueError("unsupported financial company-type classifier value")
        normalized_metadata[normalized_key] = normalized_value
    normalized_present_fields = {
        str(field).strip() for field in present_fields if str(field).strip()
    }
    if any(
        not field.replace("_", "").isalnum()
        for field in normalized_present_fields
    ):
        raise ValueError("company-type classifier field input is invalid")

    inputs = tuple(
        sorted(
            (
                *(
                    FinancialCompanyTypeClassifierInput(
                        key=f"metadata:{key}",
                        value=value,
                    )
                    for key, value in normalized_metadata.items()
                ),
                *(
                    FinancialCompanyTypeClassifierInput(
                        key="present_field",
                        value=str(field),
                    )
                    for field in normalized_present_fields
                ),
            ),
            key=lambda item: (item.key, item.value),
        )
    )
    normalized_hints = set(normalized_metadata.values())
    normalized_fields = normalized_present_fields
    candidate_set = {
        company_type
        for company_type in FinancialCompanyType
        if company_type is not FinancialCompanyType.UNKNOWN
        and (
            bool(normalized_hints & _COMPANY_TYPE_HINTS[company_type])
            or bool(normalized_fields & _COMPANY_TYPE_SIGNATURES[company_type])
        )
    }
    candidates = tuple(
        company_type
        for company_type in FinancialCompanyType
        if company_type in candidate_set
    )
    qualified_declared = (
        provider_declaration_qualified
        and provider_declared_type is not None
        and provider_declared_type is not FinancialCompanyType.UNKNOWN
    )
    if qualified_declared:
        assert provider_declared_type is not None
        contradictory = bool(candidate_set - {provider_declared_type})
        return FinancialCompanyTypeResolution(
            company_type=provider_declared_type,
            method=FinancialCompanyTypeResolutionMethod.QUALIFIED_PROVIDER,
            provider_declared_type=provider_declared_type,
            provider_declaration_qualified=True,
            classifier_inputs=inputs,
            candidate_types=candidates,
            ambiguous=contradictory,
            contradictory=contradictory,
        )
    if len(candidates) == 1:
        return FinancialCompanyTypeResolution(
            company_type=candidates[0],
            method=FinancialCompanyTypeResolutionMethod.CLASSIFIER,
            provider_declared_type=provider_declared_type,
            provider_declaration_qualified=provider_declaration_qualified,
            classifier_inputs=inputs,
            candidate_types=candidates,
            ambiguous=False,
            contradictory=False,
        )
    return FinancialCompanyTypeResolution(
        company_type=FinancialCompanyType.UNKNOWN,
        method=FinancialCompanyTypeResolutionMethod.UNRESOLVED,
        provider_declared_type=provider_declared_type,
        provider_declaration_qualified=provider_declaration_qualified,
        classifier_inputs=inputs,
        candidate_types=candidates,
        ambiguous=True,
        contradictory=len(candidates) > 1,
    )


class FinancialFieldSetDeclaration(BaseModel):
    """Static qualified field declaration; never derived from a response."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    declaration_version: Literal[
        "financial-field-declarations-v1",
        "financial-field-declarations-tushare-v2",
    ] = FINANCIAL_STATEMENT_DECLARATION_V1
    company_type: FinancialCompanyType
    statement_type: FinancialStatementType
    critical_fields: tuple[str, ...]
    core_fields: tuple[str, ...]
    normalized_unit: Literal["CNY_BASE_UNIT"] = "CNY_BASE_UNIT"

    @model_validator(mode="after")
    def _validate_declaration(self) -> FinancialFieldSetDeclaration:
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("unknown company type has no normalized field declaration")
        if self.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS:
            raise ValueError("comprehensive fundamentals is not a full statement")
        if tuple(sorted(set(self.critical_fields))) != self.critical_fields:
            raise ValueError("critical fields must be unique and canonical")
        if tuple(sorted(set(self.core_fields))) != self.core_fields:
            raise ValueError("core fields must be unique and canonical")
        if not set(self.critical_fields) <= set(self.core_fields):
            raise ValueError("critical fields must be part of the declared core")
        return self


_BALANCE_CORES_V1 = {
    FinancialCompanyType.BANK: (
        ("customer_deposits", "loans_and_advances", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_central_bank", "customer_deposits", "financial_investments", "interbank_assets", "interbank_liabilities", "loan_loss_allowance", "loans_and_advances", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: (
        ("total_assets", "total_equity", "total_liabilities"),
        ("accounts_payable", "accounts_receivable", "cash_and_equivalents", "inventory", "long_term_debt", "property_plant_equipment", "short_term_debt", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.INSURANCE: (
        ("insurance_contract_liabilities", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "claims_payable", "insurance_contract_liabilities", "investment_assets", "policyholder_deposits", "premium_receivables", "reinsurance_assets", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.SECURITIES: (
        ("customer_brokerage_deposits", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "customer_brokerage_deposits", "derivative_assets", "financial_assets_bought_for_resale", "repurchase_liabilities", "settlement_reserves", "total_assets", "total_equity", "total_liabilities", "trading_financial_assets"),
    ),
    FinancialCompanyType.OTHER_FINANCIAL: (
        ("interest_bearing_assets", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "customer_financing", "financial_investments", "interest_bearing_assets", "interest_bearing_liabilities", "loan_loss_allowance", "other_financial_assets", "total_assets", "total_equity", "total_liabilities"),
    ),
}

_INCOME_CORES_V1 = {
    FinancialCompanyType.BANK: (
        ("interest_expense", "interest_income", "net_income", "net_interest_income"),
        ("credit_impairment_loss", "fee_commission_income", "income_tax_expense", "interest_expense", "interest_income", "investment_income", "net_income", "net_interest_income", "operating_expense", "operating_profit"),
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: (
        ("net_income", "operating_profit", "operating_revenue"),
        ("cost_of_revenue", "finance_costs", "gross_profit", "income_tax_expense", "net_income", "operating_expense", "operating_profit", "operating_revenue", "other_income", "research_development_expense"),
    ),
    FinancialCompanyType.INSURANCE: (
        ("claims_expense", "net_income", "premiums_earned", "underwriting_result"),
        ("claims_expense", "fee_income", "income_tax_expense", "investment_income", "net_income", "operating_expense", "premiums_earned", "reinsurance_result", "underwriting_expense", "underwriting_result"),
    ),
    FinancialCompanyType.SECURITIES: (
        ("brokerage_commission_income", "net_income", "operating_revenue", "trading_income"),
        ("asset_management_income", "brokerage_commission_income", "income_tax_expense", "interest_income", "investment_banking_income", "investment_income", "net_income", "operating_expense", "operating_revenue", "trading_income"),
    ),
    FinancialCompanyType.OTHER_FINANCIAL: (
        ("financial_service_revenue", "interest_income", "net_income", "operating_profit"),
        ("credit_impairment_loss", "fee_income", "financial_service_revenue", "income_tax_expense", "interest_expense", "interest_income", "investment_income", "net_income", "operating_expense", "operating_profit"),
    ),
}

_CASH_FLOW_COMMON_V1 = (
    "cash_from_financing",
    "cash_from_investing",
    "cash_from_operations",
    "cash_paid_for_interest",
    "cash_paid_for_taxes",
    "cash_received_from_customers",
    "closing_cash",
    "effect_of_exchange_rates",
    "net_change_in_cash",
    "opening_cash",
)

# Tushare's qualified statement schema exposes different, semantically precise
# line items from the AKShare-Sina fallback. Keep this declaration provider-bound
# so fallback candidates and v1 checkpoints continue to use their original core.
_BALANCE_CORES_TUSHARE_V2 = {
    **_BALANCE_CORES_V1,
    FinancialCompanyType.BANK: (
        (
            "customer_deposits",
            "loans_and_advances",
            "total_assets",
            "total_equity",
            "total_liabilities",
        ),
        (
            "cash_and_central_bank",
            "customer_deposits",
            "financial_assets_at_amortized_cost",
            "financial_assets_at_fair_value",
            "interbank_assets",
            "interbank_liabilities",
            "loans_and_advances",
            "total_assets",
            "total_equity",
            "total_liabilities",
        ),
    ),
}
_INCOME_CORES_TUSHARE_V2 = {
    **_INCOME_CORES_V1,
    FinancialCompanyType.BANK: (
        (
            "interest_expense",
            "interest_income",
            "net_income",
            "operating_profit",
        ),
        (
            "fee_commission_income",
            "income_tax_expense",
            "interest_expense",
            "interest_income",
            "investment_income",
            "net_fee_commission_income",
            "net_income",
            "operating_expense",
            "operating_profit",
            "profit_before_tax",
        ),
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: (
        ("net_income", "operating_profit", "operating_revenue"),
        (
            "administrative_expense",
            "cost_of_revenue",
            "finance_costs",
            "income_tax_expense",
            "net_income",
            "operating_expense",
            "operating_profit",
            "operating_revenue",
            "profit_before_tax",
            "selling_expense",
        ),
    ),
}
_BANK_CASH_FLOW_TUSHARE_V2 = (
    "cash_from_financing",
    "cash_from_investing",
    "cash_from_operations",
    "cash_inflows_from_operations",
    "cash_paid_for_dividends_profit_and_interest",
    "cash_paid_for_taxes",
    "closing_cash",
    "effect_of_exchange_rates",
    "net_change_in_cash",
    "opening_cash",
)


def financial_statement_field_declaration(
    company_type: FinancialCompanyType,
    statement_type: FinancialStatementType,
    *,
    declaration_version: Literal[
        "financial-field-declarations-v1",
        "financial-field-declarations-tushare-v2",
    ] = FINANCIAL_STATEMENT_DECLARATION_V1,
) -> FinancialFieldSetDeclaration:
    if declaration_version == FINANCIAL_STATEMENT_DECLARATION_V1:
        balance_cores = _BALANCE_CORES_V1
        income_cores = _INCOME_CORES_V1
        cash_flow_core = _CASH_FLOW_COMMON_V1
    elif declaration_version == FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2:
        balance_cores = _BALANCE_CORES_TUSHARE_V2
        income_cores = _INCOME_CORES_TUSHARE_V2
        cash_flow_core = (
            _BANK_CASH_FLOW_TUSHARE_V2
            if company_type is FinancialCompanyType.BANK
            else _CASH_FLOW_COMMON_V1
        )
    else:
        raise ValueError("unsupported financial statement declaration version")
    if statement_type is FinancialStatementType.BALANCE_SHEET:
        critical, core = balance_cores[company_type]
    elif statement_type is FinancialStatementType.INCOME_STATEMENT:
        critical, core = income_cores[company_type]
    elif statement_type is FinancialStatementType.CASH_FLOW:
        critical = ("cash_from_operations", "closing_cash", "net_change_in_cash")
        core = cash_flow_core
    else:
        raise ValueError("full-statement declaration requires a statement type")
    return FinancialFieldSetDeclaration(
        declaration_version=declaration_version,
        company_type=company_type,
        statement_type=statement_type,
        critical_fields=tuple(sorted(critical)),
        core_fields=tuple(sorted(core)),
    )


class FinancialRatioFieldSetDeclaration(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
        "financial-ratio-declarations-baostock-v1",
    ] = FINANCIAL_RATIO_DECLARATION_V1
    company_type: FinancialCompanyType
    ratio_family: FinancialRatioFamily
    normalized_fields: tuple[str, ...]
    normalized_unit: Literal["RATIO"] = "RATIO"
    maximum_missingness: Decimal = Decimal("0.1")

    @model_validator(mode="after")
    def _validate_declaration(self) -> FinancialRatioFieldSetDeclaration:
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("unknown company type has no normalized ratio declaration")
        if tuple(sorted(set(self.normalized_fields))) != self.normalized_fields:
            raise ValueError("ratio fields must be unique and canonical")
        if not self.normalized_fields:
            raise ValueError("ratio declaration cannot be empty")
        return self


_RATIO_FAMILY_FIELDS = {
    FinancialRatioFamily.PROFIT: (
        "earnings_per_share",
        "ebitda_margin",
        "gross_margin",
        "net_margin",
        "operating_margin",
        "pre_tax_margin",
        "profit_to_cost",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
    ),
    FinancialRatioFamily.OPERATION: (
        "accounts_payable_turnover",
        "asset_turnover",
        "cash_conversion_cycle",
        "current_asset_turnover",
        "fixed_asset_turnover",
        "inventory_days",
        "inventory_turnover",
        "operating_cycle",
        "receivable_days",
        "receivables_turnover",
    ),
    FinancialRatioFamily.GROWTH: (
        "asset_growth",
        "cash_flow_growth",
        "equity_growth",
        "gross_profit_growth",
        "net_income_growth",
        "operating_profit_growth",
        "revenue_growth",
        "return_on_equity_growth",
        "total_profit_growth",
        "working_capital_growth",
    ),
    FinancialRatioFamily.BALANCE: (
        "cash_ratio",
        "current_ratio",
        "debt_to_assets",
        "debt_to_equity",
        "equity_multiplier",
        "interest_coverage",
        "long_term_debt_ratio",
        "quick_ratio",
        "tangible_asset_ratio",
        "working_capital_ratio",
    ),
    FinancialRatioFamily.CASH_FLOW: (
        "cash_flow_adequacy",
        "cash_flow_to_debt",
        "cash_flow_to_liabilities",
        "cash_flow_to_revenue",
        "cash_return_on_assets",
        "free_cash_flow_margin",
        "operating_cash_flow_ratio",
        "quality_of_income",
        "reinvestment_ratio",
        "self_financing_ratio",
    ),
    FinancialRatioFamily.DUPONT: (
        "asset_turnover_component",
        "debt_burden",
        "equity_multiplier_component",
        "financial_leverage",
        "interest_burden",
        "net_profit_margin_component",
        "operating_margin_component",
        "return_on_assets_component",
        "return_on_equity_component",
        "tax_burden",
    ),
}

_QUALIFIED_RATIO_FAMILY_FIELDS = {
    FinancialRatioFamily.PROFIT: (
        "gross_margin",
        "net_margin",
        "operating_margin",
        "pre_tax_margin",
        "profit_to_cost",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
    ),
    FinancialRatioFamily.OPERATION: (
        "asset_turnover",
        "current_asset_turnover",
        "fixed_asset_turnover",
        "receivables_turnover",
    ),
    FinancialRatioFamily.GROWTH: (
        "asset_growth",
        "cash_flow_growth",
        "equity_growth",
        "net_income_growth",
        "operating_profit_growth",
        "revenue_growth",
        "return_on_equity_growth",
        "total_profit_growth",
    ),
    FinancialRatioFamily.BALANCE: (
        "cash_ratio",
        "current_ratio",
        "debt_to_assets",
        "debt_to_equity",
        "equity_multiplier",
        "long_term_debt_ratio",
        "quick_ratio",
        "tangible_asset_ratio",
    ),
    FinancialRatioFamily.CASH_FLOW: (
        "cash_flow_to_debt",
        "cash_flow_to_revenue",
    ),
    FinancialRatioFamily.DUPONT: (
        "asset_turnover_component",
        "equity_multiplier_component",
        "net_profit_margin_component",
        "operating_margin_component",
        "return_on_assets_component",
        "return_on_equity_component",
    ),
}

_QUALIFIED_BANK_RATIO_FAMILY_FIELDS = {
    **_QUALIFIED_RATIO_FAMILY_FIELDS,
    FinancialRatioFamily.PROFIT: (
        "net_margin",
        "pre_tax_margin",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
    ),
}

_BAOSTOCK_RATIO_FAMILY_FIELDS = {
    FinancialRatioFamily.PROFIT: (
        "gross_margin",
        "net_margin",
        "return_on_equity",
    ),
    FinancialRatioFamily.OPERATION: (
        "asset_turnover",
        "current_asset_turnover",
        "inventory_turnover",
        "receivables_turnover",
    ),
    FinancialRatioFamily.GROWTH: (
        "asset_growth",
        "basic_earnings_per_share_growth",
        "equity_growth",
        "net_income_growth",
        "parent_net_income_growth",
    ),
    FinancialRatioFamily.BALANCE: (
        "cash_ratio",
        "current_ratio",
        "debt_to_assets",
        "equity_multiplier",
        "liability_growth",
        "quick_ratio",
    ),
    FinancialRatioFamily.CASH_FLOW: (
        "cash_flow_to_gross_revenue",
        "cash_flow_to_revenue",
        "current_assets_to_assets",
        "interest_coverage",
        "non_current_assets_to_assets",
        "quality_of_income",
        "tangible_asset_ratio",
    ),
    FinancialRatioFamily.DUPONT: (
        "asset_turnover_component",
        "ebit_margin_component",
        "equity_multiplier_component",
        "interest_burden",
        "net_profit_margin_component",
        "parent_net_income_share",
        "return_on_equity_component",
        "tax_burden",
    ),
}

_BAOSTOCK_BANK_RATIO_FAMILY_FIELDS = {
    **_BAOSTOCK_RATIO_FAMILY_FIELDS,
}


def financial_ratio_field_declaration(
    company_type: FinancialCompanyType,
    ratio_family: FinancialRatioFamily,
    *,
    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
        "financial-ratio-declarations-baostock-v1",
    ] = FINANCIAL_RATIO_DECLARATION_V1,
) -> FinancialRatioFieldSetDeclaration:
    if declaration_version == FINANCIAL_RATIO_DECLARATION_V1:
        normalized_fields = _RATIO_FAMILY_FIELDS[ratio_family]
    elif declaration_version == FINANCIAL_RATIO_DECLARATION_V2:
        declarations = (
            _QUALIFIED_BANK_RATIO_FAMILY_FIELDS
            if company_type is FinancialCompanyType.BANK
            else _QUALIFIED_RATIO_FAMILY_FIELDS
        )
        normalized_fields = declarations[ratio_family]
    elif declaration_version == FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1:
        declarations = (
            _BAOSTOCK_BANK_RATIO_FAMILY_FIELDS
            if company_type is FinancialCompanyType.BANK
            else _BAOSTOCK_RATIO_FAMILY_FIELDS
        )
        normalized_fields = declarations[ratio_family]
    else:
        raise ValueError("unsupported financial ratio declaration version")
    return FinancialRatioFieldSetDeclaration(
        declaration_version=declaration_version,
        company_type=company_type,
        ratio_family=ratio_family,
        normalized_fields=tuple(sorted(normalized_fields)),
    )


class FinancialFieldValue(BaseModel):
    """Original provider value paired with its explicit normalized value."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    provider_field: str = Field(pattern=_SAFE_ID_PATTERN)
    original_value: str = Field(min_length=1, max_length=512)
    original_unit: str | None = Field(default=None, max_length=64)
    normalized_field: str = Field(pattern=_SAFE_ID_PATTERN)
    normalized_value: str | None = Field(default=None, max_length=512)
    normalized_unit: str | None = Field(default=None, max_length=64)

    @field_validator("normalized_value")
    @classmethod
    def _validate_normalized_numeric_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            numeric = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("normalized financial value must be decimal") from exc
        if not numeric.is_finite():
            raise ValueError("normalized financial value must be finite")
        return value

    @model_validator(mode="after")
    def _validate_safe_value(self) -> FinancialFieldValue:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        return self


class FinancialFilingMetadata(BaseModel):
    """Provider filing metadata with independent announcement/publication dates."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    ann_date: date | None = None
    f_ann_date: date | None = None
    report_type: str | None = Field(default=None, max_length=64)
    comp_type: str | None = Field(default=None, max_length=64)
    update_flag: str | None = Field(default=None, max_length=64)
    retrieved_at: datetime
    observed_at: datetime
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_filing_revision_id: str | None = Field(
        default=None,
        pattern=_SAFE_ID_PATTERN,
    )
    provider_filing_revision_sequence: int | None = Field(default=None, ge=0)
    provider_filing_revision_sequence_qualification: Literal[
        "financial-provider-revision-sequence-qualified-v1"
    ] | None = None

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_safe_metadata(self) -> FinancialFilingMetadata:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        if (
            self.update_flag in {"0", "1"}
            and self.provider_filing_revision_id == self.update_flag
        ):
            raise ValueError("binary update_flag cannot establish restatement lineage")
        if (
            self.provider_filing_revision_sequence is not None
            and self.provider_filing_revision_id is None
        ):
            raise ValueError(
                "provider filing revision sequence requires a stable revision identity"
            )
        if (
            self.provider_filing_revision_sequence_qualification is not None
            and (
                self.provider_filing_revision_id is None
                or self.provider_filing_revision_sequence is None
            )
        ):
            raise ValueError(
                "provider filing revision qualification requires identity and sequence"
            )
        return self

    def is_strict_pit_eligible(self, *, as_of_date: date) -> bool:
        return (
            self.ann_date is not None
            and self.ann_date <= as_of_date
            and self.f_ann_date is not None
            and self.f_ann_date <= as_of_date
            and self.observed_at.date() <= as_of_date
        )

    @property
    def provider_restatement_lineage_established(self) -> bool:
        return self.provider_filing_revision_sequence_qualification is not None


def _authoritative_instrument_payload(
    identity: InstrumentIdentityEvidence,
) -> dict[str, object]:
    if not identity.is_authoritative or identity.provenance is None:
        raise ValueError("Financial Period Identity requires authoritative Instrument Identity")
    payload = {
        "canonical_symbol": identity.symbol.strip().upper(),
        "venue": identity.venue,
        "instrument_kind": identity.instrument_kind.value,
        "currency": identity.currency.upper(),
        "provenance": identity.provenance.model_dump(mode="json"),
    }
    _assert_safe_contract_payload(payload)
    return payload


def _financial_period_identity_payload(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    capability: FinancialCapability,
    statement_type: FinancialStatementType | None,
    ratio_family: FinancialRatioFamily | None,
    period_end: date,
    frequency: FinancialReportingFrequency,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    provider_revision_binding: str,
) -> dict[str, object]:
    return {
        "contract_version": FINANCIAL_CONTRACT_VERSION,
        "instrument_identity": _authoritative_instrument_payload(instrument_identity),
        "capability": capability.value,
        "statement_type": statement_type.value if statement_type is not None else None,
        "ratio_family": ratio_family.value if ratio_family is not None else None,
        "period_end": period_end.isoformat(),
        "frequency": frequency.value,
        "company_type": company_type.value,
        "consolidation_scope": consolidation_scope.value,
        "currency": currency.upper(),
        "provider_revision_binding": provider_revision_binding,
    }


class FinancialPeriodIdentity(BaseModel):
    """Canonical provider-revision-bound identity of one financial period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    instrument_identity: InstrumentIdentityEvidence
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    period_end: date
    frequency: FinancialReportingFrequency
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    provider_revision_binding: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        capability: FinancialCapability,
        statement_type: FinancialStatementType | None,
        ratio_family: FinancialRatioFamily | None,
        period_end: date,
        frequency: FinancialReportingFrequency,
        company_type: FinancialCompanyType,
        consolidation_scope: FinancialConsolidationScope,
        currency: str,
        provider_revision_binding: str,
    ) -> FinancialPeriodIdentity:
        payload = _financial_period_identity_payload(
            instrument_identity=instrument_identity,
            capability=capability,
            statement_type=statement_type,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency,
            provider_revision_binding=provider_revision_binding,
        )
        return cls(
            instrument_identity=instrument_identity,
            capability=capability,
            statement_type=statement_type,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency.upper(),
            provider_revision_binding=provider_revision_binding,
            period_identity=(
                f"{FINANCIAL_PERIOD_IDENTITY_VERSION}:{_canonical_digest(payload)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodIdentity:
        if self.capability is FinancialCapability.STATEMENT:
            if self.statement_type not in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            } or self.ratio_family is not None:
                raise ValueError("statement period identity requires one full statement")
        elif self.statement_type is not None or self.ratio_family is None:
            raise ValueError("ratio period identity requires one ratio family")
        if self.frequency is FinancialReportingFrequency.NOT_APPLICABLE:
            raise ValueError("financial periods require annual or quarterly frequency")
        payload = _financial_period_identity_payload(
            instrument_identity=self.instrument_identity,
            capability=self.capability,
            statement_type=self.statement_type,
            ratio_family=self.ratio_family,
            period_end=self.period_end,
            frequency=self.frequency,
            company_type=self.company_type,
            consolidation_scope=self.consolidation_scope,
            currency=self.currency,
            provider_revision_binding=self.provider_revision_binding,
        )
        expected = f"{FINANCIAL_PERIOD_IDENTITY_VERSION}:{_canonical_digest(payload)}"
        if self.period_identity != expected:
            raise ValueError("Financial Period Identity mismatch")
        return self


class FinancialProviderDatasetIdentity(BaseModel):
    """Stable identity of one provider endpoint/dataset schema."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    endpoint_id: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_id: str = Field(pattern=_SAFE_ID_PATTERN)
    schema_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_identity: str = Field(
        pattern=r"^financial-provider-dataset:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        endpoint_id: str,
        dataset_id: str,
        schema_identity: str,
    ) -> FinancialProviderDatasetIdentity:
        payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "provider_id": provider_id,
            "endpoint_id": endpoint_id,
            "dataset_id": dataset_id,
            "schema_identity": schema_identity,
        }
        return cls(
            **payload,
            dataset_identity=(
                f"{FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION}:"
                f"{_canonical_digest(payload)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialProviderDatasetIdentity:
        payload = self.model_dump(mode="json", exclude={"dataset_identity"})
        _assert_safe_contract_payload(payload)
        expected = (
            f"{FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION}:"
            f"{_canonical_digest(payload)}"
        )
        if self.dataset_identity != expected:
            raise ValueError("financial provider dataset identity mismatch")
        return self


class FinancialProviderArtifactIdentity(BaseModel):
    """Immutable local revision binding without retaining a provider payload."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    dataset: FinancialProviderDatasetIdentity
    canonical_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_metadata_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieved_at: datetime
    observed_at: datetime
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @classmethod
    def create(
        cls,
        *,
        dataset: FinancialProviderDatasetIdentity,
        canonical_request: Mapping[str, object],
        provider_metadata: Mapping[str, object],
        retrieved_at: datetime,
        observed_at: datetime,
        payload: object,
    ) -> FinancialProviderArtifactIdentity:
        request_identity_material = _project_identity_material(canonical_request)
        metadata_identity_material = _project_identity_material(provider_metadata)
        payload_identity_material = _project_identity_material(payload)
        _assert_safe_contract_payload(request_identity_material)
        _assert_safe_contract_payload(metadata_identity_material)
        _assert_safe_contract_payload(payload_identity_material)
        payload_fields: dict[str, Any] = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "dataset": dataset.model_dump(mode="json"),
            "canonical_request_sha256": _canonical_digest(
                request_identity_material,
                unordered_mapping_keys=_UNORDERED_REQUEST_KEYS,
            ),
            "provider_metadata_sha256": _canonical_digest(
                metadata_identity_material
            ),
            "retrieved_at": _utc_text(retrieved_at),
            "observed_at": _utc_text(observed_at),
            "payload_sha256": _canonical_digest(
                payload_identity_material,
                unordered_top_level_sequence=True,
                unordered_mapping_keys=_UNORDERED_PAYLOAD_KEYS,
            ),
        }
        return cls(
            **payload_fields,
            artifact_identity=(
                f"{FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION}:"
                f"{_canonical_digest(payload_fields)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialProviderArtifactIdentity:
        payload = self.model_dump(mode="json", exclude={"artifact_identity"})
        expected = (
            f"{FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION}:"
            f"{_canonical_digest(payload)}"
        )
        if self.artifact_identity != expected:
            raise ValueError("financial provider artifact identity mismatch")
        return self


class FinancialPeriodCandidate(BaseModel):
    """One immutable normalized candidate retained whether accepted or rejected."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    artifact: FinancialProviderArtifactIdentity
    period_identity: FinancialPeriodIdentity
    filing_metadata: FinancialFilingMetadata
    company_type_resolution: FinancialCompanyTypeResolution
    fields: tuple[FinancialFieldValue, ...]
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        artifact: FinancialProviderArtifactIdentity,
        period_identity: FinancialPeriodIdentity,
        filing_metadata: FinancialFilingMetadata,
        company_type_resolution: FinancialCompanyTypeResolution,
        fields: Sequence[FinancialFieldValue],
    ) -> FinancialPeriodCandidate:
        canonical_fields = tuple(
            sorted(fields, key=lambda field: (field.normalized_field, field.provider_field))
        )
        identity_payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "artifact_identity": artifact.artifact_identity,
            "period_identity": period_identity.period_identity,
            "filing_metadata": filing_metadata.model_dump(mode="json"),
            "company_type_resolution": company_type_resolution.model_dump(mode="json"),
            "fields": [field.model_dump(mode="json") for field in canonical_fields],
        }
        return cls(
            artifact=artifact,
            period_identity=period_identity,
            filing_metadata=filing_metadata,
            company_type_resolution=company_type_resolution,
            fields=canonical_fields,
            candidate_identity=(
                "financial-period-candidate:v1:"
                + _canonical_digest(identity_payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_candidate(self) -> FinancialPeriodCandidate:
        if self.fields != tuple(
            sorted(
                self.fields,
                key=lambda field: (field.normalized_field, field.provider_field),
            )
        ):
            raise ValueError("financial candidate fields must be canonical")
        normalized_fields = tuple(field.normalized_field for field in self.fields)
        if len(normalized_fields) != len(set(normalized_fields)):
            raise ValueError("financial candidate normalized fields must be unique")
        artifact_identity = self.artifact.artifact_identity
        if self.period_identity.provider_revision_binding != artifact_identity:
            raise ValueError("Financial Period Identity is not bound to its artifact")
        if (
            self.filing_metadata.local_provider_revision_identity
            != artifact_identity
        ):
            raise ValueError("filing metadata is not bound to its local revision")
        if (
            self.company_type_resolution.company_type
            is not self.period_identity.company_type
        ):
            raise ValueError("company-type resolution contradicts period identity")
        identity_payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "artifact_identity": artifact_identity,
            "period_identity": self.period_identity.period_identity,
            "filing_metadata": self.filing_metadata.model_dump(mode="json"),
            "company_type_resolution": self.company_type_resolution.model_dump(
                mode="json"
            ),
            "fields": [field.model_dump(mode="json") for field in self.fields],
        }
        expected = "financial-period-candidate:v1:" + _canonical_digest(
            identity_payload
        )
        if self.candidate_identity != expected:
            raise ValueError("financial period candidate identity mismatch")
        return self


class FinancialPeriodCompletenessAssessment(BaseModel):
    """Deterministic hard-gate outcome for one normalized financial period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-v2",
        "financial-period-selection-v3",
        "financial-period-selection-baostock-v1",
        "financial-period-selection-tushare-statement-v2",
    ] = FINANCIAL_PERIOD_SELECTION_V1
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    instrument_identity: InstrumentIdentityEvidence
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    period_end: date
    frequency: FinancialReportingFrequency
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    pit_as_of_date: date
    first_publication_date: date | None = None
    revision_observed_at: datetime
    disposition: FinancialPeriodDisposition
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    declared_critical_fields: tuple[str, ...]
    declared_core_fields: tuple[str, ...]
    present_critical_fields: tuple[str, ...]
    present_core_fields: tuple[str, ...]
    missing_critical_fields: tuple[str, ...]
    missing_core_fields: tuple[str, ...]
    critical_coverage: Decimal = Field(ge=0, le=1)
    core_coverage: Decimal = Field(ge=0, le=1)
    metadata_complete: bool
    usable_for_current_analysis: bool
    strict_pit_eligible: bool
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    announcement_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("revision_observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_assessment(self) -> FinancialPeriodCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        canonical_reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.rejection_reasons
        )
        if self.rejection_reasons != canonical_reasons:
            raise ValueError("financial rejection reasons must be unique and canonical")
        field_sets = (
            self.declared_critical_fields,
            self.declared_core_fields,
            self.present_critical_fields,
            self.present_core_fields,
            self.missing_critical_fields,
            self.missing_core_fields,
        )
        if any(values != tuple(sorted(set(values))) for values in field_sets):
            raise ValueError("financial assessment fields must be unique and canonical")

        expected_critical: tuple[str, ...] = ()
        expected_core: tuple[str, ...] = ()
        ratio_assessment = False
        statement_contract = (
            self.capability is FinancialCapability.STATEMENT
            and self.statement_type
            in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            }
            and self.ratio_family is None
        )
        declarable_statement = (
            statement_contract
            and self.company_type is not FinancialCompanyType.UNKNOWN
        )
        ratio_contract = (
            self.capability is FinancialCapability.RATIO_FAMILY
            and self.statement_type is None
            and self.ratio_family is not None
        )
        if declarable_statement:
            declaration_version = {
                FINANCIAL_PERIOD_SELECTION_V1: FINANCIAL_STATEMENT_DECLARATION_V1,
                FINANCIAL_PERIOD_SELECTION_TUSHARE_STATEMENT_V2: (
                    FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2
                ),
            }.get(self.contract_version)
            if declaration_version is None:
                raise ValueError(
                    "statement assessment requires a statement selection contract"
                )
            assert self.statement_type is not None
            declaration = financial_statement_field_declaration(
                self.company_type,
                self.statement_type,
                declaration_version=declaration_version,
            )
            expected_critical = declaration.critical_fields
            expected_core = declaration.core_fields
        elif (
            self.capability is FinancialCapability.RATIO_FAMILY
            and self.company_type is not FinancialCompanyType.UNKNOWN
            and self.statement_type is None
            and self.ratio_family is not None
        ):
            ratio_assessment = True
            declaration_version = {
                FINANCIAL_PERIOD_SELECTION_V1: FINANCIAL_RATIO_DECLARATION_V1,
                FINANCIAL_PERIOD_SELECTION_V2: FINANCIAL_RATIO_DECLARATION_V2,
                FINANCIAL_PERIOD_SELECTION_BAOSTOCK_V1: (
                    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1
                ),
            }[self.contract_version]
            expected_core = financial_ratio_field_declaration(
                self.company_type,
                self.ratio_family,
                declaration_version=declaration_version,
            ).normalized_fields
        elif self.declared_critical_fields or self.declared_core_fields:
            raise ValueError("unsupported assessment cannot declare normalized fields")

        if (
            self.contract_version
            in {
                FINANCIAL_PERIOD_SELECTION_V2,
                FINANCIAL_PERIOD_SELECTION_BAOSTOCK_V1,
            }
            and not ratio_contract
        ):
            raise ValueError("financial period selection v2 requires a ratio assessment")
        if (
            self.contract_version
            == FINANCIAL_PERIOD_SELECTION_TUSHARE_STATEMENT_V2
            and not statement_contract
        ):
            raise ValueError(
                "statement selection v2 requires a statement assessment"
            )

        if (
            self.declared_critical_fields != expected_critical
            or self.declared_core_fields != expected_core
        ):
            raise ValueError("financial assessment field declaration mismatch")
        if (
            set(self.present_critical_fields)
            | set(self.missing_critical_fields)
            != set(expected_critical)
            or set(self.present_critical_fields)
            & set(self.missing_critical_fields)
            or set(self.present_core_fields) | set(self.missing_core_fields)
            != set(expected_core)
            or set(self.present_core_fields) & set(self.missing_core_fields)
        ):
            raise ValueError("financial assessment field partitions are inconsistent")
        if (
            not set(self.present_critical_fields) <= set(self.present_core_fields)
            or not set(self.missing_critical_fields) <= set(self.missing_core_fields)
        ):
            raise ValueError("critical/core presence partitions are inconsistent")
        expected_critical_coverage = (
            Decimal(1)
            if ratio_assessment
            else (
                Decimal(len(self.present_critical_fields))
                / Decimal(len(expected_critical))
                if expected_critical
                else Decimal(0)
            )
        )
        expected_core_coverage = (
            Decimal(len(self.present_core_fields)) / Decimal(len(expected_core))
            if expected_core
            else Decimal(0)
        )
        if (
            self.critical_coverage != expected_critical_coverage
            or self.core_coverage != expected_core_coverage
        ):
            raise ValueError("financial assessment coverage is inconsistent")
        insufficient = bool(
            expected_core
            and (
                (not ratio_assessment and bool(self.missing_critical_fields))
                or self.core_coverage < Decimal("0.9")
            )
        )
        if insufficient != (
            FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS
            in self.rejection_reasons
        ):
            raise ValueError("financial period completeness reason is inconsistent")
        if not self.metadata_complete and (
            FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA
            not in self.rejection_reasons
        ):
            raise ValueError("missing announcement metadata must be rejected")

        v2_eligibility = (
            self.eligibility_policy_version
            == MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
        )
        if not v2_eligibility and self.announcement_date is not None:
            raise ValueError("legacy assessment carries v2 announcement eligibility")
        announcement_eligible = (
            not v2_eligibility
            or (
                self.announcement_date is not None
                and self.announcement_date <= self.pit_as_of_date
            )
        )
        announcement_after_cutoff = (
            FinancialPeriodRejectionReason.ANNOUNCEMENT_AFTER_CUTOFF
            in self.rejection_reasons
        )
        if announcement_after_cutoff != (
            v2_eligibility
            and self.announcement_date is not None
            and self.announcement_date > self.pit_as_of_date
        ):
            raise ValueError("announcement-cutoff rejection is inconsistent")
        expected_strict_pit = (
            announcement_eligible
            and self.first_publication_date is not None
            and self.first_publication_date <= self.pit_as_of_date
            and self.revision_observed_at.date() <= self.pit_as_of_date
        )
        if self.strict_pit_eligible != expected_strict_pit:
            raise ValueError("financial strict-PIT eligibility is inconsistent")
        missing_publication = (
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE
            in self.rejection_reasons
        )
        strict_missing_publication = (
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
            in self.rejection_reasons
        )
        cutoff_failed = (
            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
            in self.rejection_reasons
        )
        if strict_missing_publication:
            missing_reason_consistent = (
                self.first_publication_date is None
                and not missing_publication
                and v2_eligibility
                and self.disposition is FinancialPeriodDisposition.REJECTED
            )
        else:
            missing_reason_consistent = missing_publication == (
                self.first_publication_date is None
            )
        if not missing_reason_consistent:
            raise ValueError("first-publication rejection is inconsistent")
        if cutoff_failed != (
            self.first_publication_date is not None and not expected_strict_pit
        ):
            raise ValueError("strict-PIT cutoff rejection is inconsistent")

        pit_only_reasons = {
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT,
        }
        reason_set = set(self.rejection_reasons)
        if self.disposition is FinancialPeriodDisposition.ACCEPTED:
            if reason_set or not self.strict_pit_eligible:
                raise ValueError("accepted financial period must pass every hard gate")
        elif self.disposition is FinancialPeriodDisposition.CURRENT_ONLY:
            if not reason_set or not reason_set <= pit_only_reasons:
                raise ValueError("current-only period may fail only strict-PIT gates")
        elif self.disposition is FinancialPeriodDisposition.REJECTED:
            if not reason_set or (
                reason_set <= pit_only_reasons
                and FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                not in reason_set
                and self.eligibility_policy_version
                != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
            ):
                raise ValueError("rejected financial period requires a non-PIT reason")
        elif self.disposition is FinancialPeriodDisposition.CONFLICTED:
            if not reason_set & {
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
                FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
            }:
                raise ValueError("conflicted period requires a typed conflict")
        else:
            raise ValueError("assessment cannot use unselected disposition")
        if self.usable_for_current_analysis != (
            self.disposition
            in {
                FinancialPeriodDisposition.ACCEPTED,
                FinancialPeriodDisposition.CURRENT_ONLY,
            }
        ):
            raise ValueError("financial current-analysis usability is inconsistent")
        return self


def assess_financial_period_candidate(
    candidate: FinancialPeriodCandidate,
    *,
    requested_capability: FinancialCapability,
    requested_statement_type: FinancialStatementType | None,
    requested_ratio_family: FinancialRatioFamily | None,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_frequency: FinancialReportingFrequency,
    expected_currency: str,
    expected_consolidation_scope: FinancialConsolidationScope,
    pit_as_of_date: date,
    ratio_declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
        "financial-ratio-declarations-baostock-v1",
    ] = FINANCIAL_RATIO_DECLARATION_V1,
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = None,
) -> FinancialPeriodCompletenessAssessment:
    """Apply capability, identity, metadata, and declared completeness gates."""

    identity = candidate.period_identity
    reasons: set[FinancialPeriodRejectionReason] = set()
    if (
        identity.capability is not requested_capability
        or identity.statement_type is not requested_statement_type
        or identity.ratio_family is not requested_ratio_family
    ):
        reasons.add(FinancialPeriodRejectionReason.CAPABILITY_MISMATCH)
    if _authoritative_instrument_payload(
        identity.instrument_identity
    ) != _authoritative_instrument_payload(expected_instrument_identity):
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    if identity.frequency is not expected_frequency:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    resolution = candidate.company_type_resolution
    if (
        identity.company_type is FinancialCompanyType.UNKNOWN
        or resolution.ambiguous
        or resolution.contradictory
    ):
        reasons.add(FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE)
    if identity.currency != expected_currency.upper():
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY)
    if identity.consolidation_scope is not expected_consolidation_scope:
        reasons.add(
            FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE
        )
    if candidate.filing_metadata.ann_date is None:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    elif (
        eligibility_policy_version == MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
        and candidate.filing_metadata.ann_date > pit_as_of_date
    ):
        reasons.add(FinancialPeriodRejectionReason.ANNOUNCEMENT_AFTER_CUTOFF)

    statement_declaration_version = (
        FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2
        if candidate.artifact.dataset.provider_id == "tushare"
        else FINANCIAL_STATEMENT_DECLARATION_V1
    )
    declared_critical: tuple[str, ...] = ()
    declared_core: tuple[str, ...] = ()
    present_critical: tuple[str, ...] = ()
    present_core: tuple[str, ...] = ()
    missing_critical: tuple[str, ...] = ()
    missing_core: tuple[str, ...] = ()
    critical_coverage = Decimal(0)
    core_coverage = Decimal(0)
    if (
        identity.capability is FinancialCapability.STATEMENT
        and identity.company_type is not FinancialCompanyType.UNKNOWN
        and identity.statement_type
        in {
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.CASH_FLOW,
            FinancialStatementType.INCOME_STATEMENT,
        }
    ):
        assert identity.statement_type is not None
        declaration = financial_statement_field_declaration(
            identity.company_type,
            identity.statement_type,
            declaration_version=statement_declaration_version,
        )
        declared_critical = declaration.critical_fields
        declared_core = declaration.core_fields
        normalized_fields = {
            field.normalized_field: field
            for field in candidate.fields
            if field.normalized_value is not None
        }
        present_critical = tuple(
            field for field in declared_critical if field in normalized_fields
        )
        present_core = tuple(field for field in declared_core if field in normalized_fields)
        missing_critical = tuple(
            field for field in declared_critical if field not in normalized_fields
        )
        missing_core = tuple(field for field in declared_core if field not in normalized_fields)
        critical_coverage = Decimal(len(present_critical)) / Decimal(
            len(declared_critical)
        )
        core_coverage = Decimal(len(present_core)) / Decimal(len(declared_core))
        if any(
            normalized_fields[field].normalized_unit != declaration.normalized_unit
            for field in present_core
        ):
            reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
        if missing_critical or core_coverage < Decimal("0.9"):
            reasons.add(FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS)
    elif (
        identity.capability is FinancialCapability.RATIO_FAMILY
        and identity.company_type is not FinancialCompanyType.UNKNOWN
        and identity.ratio_family is not None
    ):
        ratio_declaration = financial_ratio_field_declaration(
            identity.company_type,
            identity.ratio_family,
            declaration_version=ratio_declaration_version,
        )
        declared_core = ratio_declaration.normalized_fields
        normalized_fields = {
            field.normalized_field: field
            for field in candidate.fields
            if field.normalized_value is not None
        }
        present_core = tuple(field for field in declared_core if field in normalized_fields)
        missing_core = tuple(field for field in declared_core if field not in normalized_fields)
        critical_coverage = Decimal(1)
        core_coverage = Decimal(len(present_core)) / Decimal(len(declared_core))
        if any(
            normalized_fields[field].normalized_unit
            != ratio_declaration.normalized_unit
            for field in present_core
        ):
            reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
        if Decimal(1) - core_coverage > ratio_declaration.maximum_missingness:
            reasons.add(FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS)

    revision_observed_at = max(
        candidate.artifact.observed_at,
        candidate.filing_metadata.observed_at,
    )
    first_publication_date = candidate.filing_metadata.f_ann_date
    strict_pit_eligible = (
        (
            eligibility_policy_version is None
            or (
                candidate.filing_metadata.ann_date is not None
                and candidate.filing_metadata.ann_date <= pit_as_of_date
            )
        )
        and first_publication_date is not None
        and first_publication_date <= pit_as_of_date
        and revision_observed_at.date() <= pit_as_of_date
    )
    if first_publication_date is None:
        reasons.add(FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE)
    elif not strict_pit_eligible:
        reasons.add(FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED)
    rejection_reasons = tuple(
        reason for reason in FinancialPeriodRejectionReason if reason in reasons
    )
    current_only_reasons = {
        FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
        FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
    }
    if not reasons:
        disposition = FinancialPeriodDisposition.ACCEPTED
    elif reasons <= current_only_reasons:
        disposition = FinancialPeriodDisposition.CURRENT_ONLY
    else:
        disposition = FinancialPeriodDisposition.REJECTED
    assessment_contract_version = (
        {
            FINANCIAL_STATEMENT_DECLARATION_V1: FINANCIAL_PERIOD_SELECTION_V1,
            FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2: (
                FINANCIAL_PERIOD_SELECTION_TUSHARE_STATEMENT_V2
            ),
        }[statement_declaration_version]
        if identity.capability is FinancialCapability.STATEMENT
        else {
            FINANCIAL_RATIO_DECLARATION_V1: FINANCIAL_PERIOD_SELECTION_V1,
            FINANCIAL_RATIO_DECLARATION_V2: FINANCIAL_PERIOD_SELECTION_V2,
            FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1: (
                FINANCIAL_PERIOD_SELECTION_BAOSTOCK_V1
            ),
        }[ratio_declaration_version]
    )
    return FinancialPeriodCompletenessAssessment(
        contract_version=assessment_contract_version,
        candidate_identity=candidate.candidate_identity,
        artifact_identity=candidate.artifact.artifact_identity,
        period_identity=identity.period_identity,
        instrument_identity=identity.instrument_identity,
        capability=identity.capability,
        statement_type=identity.statement_type,
        ratio_family=identity.ratio_family,
        period_end=identity.period_end,
        frequency=identity.frequency,
        company_type=identity.company_type,
        consolidation_scope=identity.consolidation_scope,
        currency=identity.currency,
        pit_as_of_date=pit_as_of_date,
        first_publication_date=first_publication_date,
        revision_observed_at=revision_observed_at,
        disposition=disposition,
        rejection_reasons=rejection_reasons,
        declared_critical_fields=declared_critical,
        declared_core_fields=declared_core,
        present_critical_fields=present_critical,
        present_core_fields=present_core,
        missing_critical_fields=missing_critical,
        missing_core_fields=missing_core,
        critical_coverage=critical_coverage,
        core_coverage=core_coverage,
        metadata_complete=candidate.filing_metadata.ann_date is not None,
        usable_for_current_analysis=disposition
        in {
            FinancialPeriodDisposition.ACCEPTED,
            FinancialPeriodDisposition.CURRENT_ONLY,
        },
        strict_pit_eligible=strict_pit_eligible,
        eligibility_policy_version=eligibility_policy_version,
        announcement_date=(
            candidate.filing_metadata.ann_date
            if eligibility_policy_version is not None
            else None
        ),
    )


def reject_financial_period_for_strict_pit(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    """Fail one otherwise current-usable v2 candidate closed for strict PIT."""

    if assessment.strict_pit_eligible:
        return assessment
    if assessment.disposition is not FinancialPeriodDisposition.CURRENT_ONLY:
        return assessment
    reasons = set(assessment.rejection_reasons)
    if assessment.first_publication_date is None:
        reasons.discard(
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE
        )
        reasons.add(
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
        )
    payload = assessment.model_dump(mode="python")
    payload.update(
        {
            "disposition": FinancialPeriodDisposition.REJECTED,
            "rejection_reasons": tuple(
                reason for reason in FinancialPeriodRejectionReason if reason in reasons
            ),
            "usable_for_current_analysis": False,
        }
    )
    return FinancialPeriodCompletenessAssessment.model_validate(payload)


class FinancialListingProvenance(BaseModel):
    """Authoritative listing-date provenance used by the typed exception."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    authoritative: Literal[True] = True
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    source_ref: str = Field(pattern=_SAFE_ID_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_safe_provenance(self) -> FinancialListingProvenance:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        return self


def _canonical_dates(values: Sequence[date], *, reverse: bool = False) -> tuple[date, ...]:
    return tuple(sorted(set(values), reverse=reverse))


class FinancialSinceListingException(BaseModel):
    """Typed relaxation that still requires every expected post-listing period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-since-listing-exception-v1"] = (
        "financial-since-listing-exception-v1"
    )
    listing_date: date
    listing_provenance: FinancialListingProvenance
    expected_annual_period_ends: tuple[date, ...]
    expected_reporting_period_ends: tuple[date, ...]
    covered_annual_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    ignored_pre_listing_period_ends: tuple[date, ...]
    supported: bool

    @model_validator(mode="after")
    def _validate_exception(self) -> FinancialSinceListingException:
        date_fields = (
            self.expected_annual_period_ends,
            self.expected_reporting_period_ends,
            self.covered_annual_period_ends,
            self.covered_reporting_period_ends,
            self.missing_annual_period_ends,
            self.missing_reporting_period_ends,
            self.ignored_pre_listing_period_ends,
        )
        if any(values != _canonical_dates(values) for values in date_fields):
            raise ValueError("since-listing period sets must be unique and canonical")
        if any(
            period < self.listing_date
            for period in (
                *self.expected_annual_period_ends,
                *self.expected_reporting_period_ends,
                *self.covered_annual_period_ends,
                *self.covered_reporting_period_ends,
            )
        ):
            raise ValueError("pre-listing periods cannot satisfy since-listing coverage")
        if any(
            period >= self.listing_date
            for period in self.ignored_pre_listing_period_ends
        ):
            raise ValueError("ignored since-listing periods must be pre-listing")
        if (
            set(self.covered_annual_period_ends)
            | set(self.missing_annual_period_ends)
            != set(self.expected_annual_period_ends)
            or set(self.covered_annual_period_ends)
            & set(self.missing_annual_period_ends)
            or set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.expected_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("since-listing coverage partitions are inconsistent")
        normal_target_impossible = (
            len(self.expected_annual_period_ends) < 5
            or len(self.expected_reporting_period_ends) < 8
        )
        expected_supported = (
            normal_target_impossible
            and not self.missing_annual_period_ends
            and not self.missing_reporting_period_ends
        )
        if self.supported != expected_supported:
            raise ValueError("since-listing exception support is inconsistent")
        return self


class FinancialHistoryCompletenessAssessment(BaseModel):
    """Aggregate five-annual/eight-reporting coverage result."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    statement_type: FinancialStatementType
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_annual_period_ends: tuple[date, ...]
    target_reporting_period_ends: tuple[date, ...]
    covered_annual_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    since_listing_exception: FinancialSinceListingException | None = None
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    complete: bool

    @model_validator(mode="after")
    def _validate_assessment(self) -> FinancialHistoryCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        if self.statement_type not in {
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.CASH_FLOW,
            FinancialStatementType.INCOME_STATEMENT,
        }:
            raise ValueError("statement history requires one full statement type")
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("statement history requires a resolved company type")
        target_fields = (
            self.target_annual_period_ends,
            self.target_reporting_period_ends,
            self.covered_annual_period_ends,
            self.covered_reporting_period_ends,
            self.missing_annual_period_ends,
            self.missing_reporting_period_ends,
        )
        if any(values != _canonical_dates(values, reverse=True) for values in target_fields):
            raise ValueError("history target period sets must be unique and canonical")
        if (
            set(self.covered_annual_period_ends)
            | set(self.missing_annual_period_ends)
            != set(self.target_annual_period_ends)
            or set(self.covered_annual_period_ends)
            & set(self.missing_annual_period_ends)
            or set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.target_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("history coverage partitions are inconsistent")
        expected_complete = (
            len(self.target_annual_period_ends) == 5
            and len(self.target_reporting_period_ends) == 8
            and not self.missing_annual_period_ends
            and not self.missing_reporting_period_ends
        ) or bool(
            self.since_listing_exception is not None
            and self.since_listing_exception.supported
        )
        if self.complete != expected_complete:
            raise ValueError("financial history completeness is inconsistent")
        expected_reasons = (
            ()
            if self.complete
            else (
                (
                    FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
                    FinancialPeriodRejectionReason.UNSUPPORTED_SINCE_LISTING_COVERAGE,
                )
                if self.since_listing_exception is not None
                else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
            )
        )
        if self.rejection_reasons != expected_reasons:
            raise ValueError("financial history rejection reasons are inconsistent")
        return self


def assess_financial_history_coverage(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    statement_type: FinancialStatementType,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    assessments: Sequence[FinancialPeriodCompletenessAssessment],
    eligible_annual_period_ends: Sequence[date],
    eligible_reporting_period_ends: Sequence[date],
    listing_date: date | None = None,
    listing_provenance: FinancialListingProvenance | None = None,
) -> FinancialHistoryCompletenessAssessment:
    """Assess normal targets, then the fully-covered authoritative listing exception."""

    if (listing_date is None) != (listing_provenance is None):
        raise ValueError("since-listing coverage requires date and authoritative provenance")
    expected_instrument_payload = _authoritative_instrument_payload(
        instrument_identity
    )
    usable = tuple(
        item
        for item in assessments
        if item.usable_for_current_analysis
        and item.capability is FinancialCapability.STATEMENT
        and item.statement_type is statement_type
        and item.ratio_family is None
        and item.company_type is company_type
        and item.consolidation_scope is consolidation_scope
        and item.currency == currency.upper()
        and _authoritative_instrument_payload(item.instrument_identity)
        == expected_instrument_payload
    )
    available_annual = {
        item.period_end
        for item in usable
        if item.frequency is FinancialReportingFrequency.ANNUAL
    }
    available_reporting = {
        item.period_end
        for item in usable
        if item.frequency
        in {
            FinancialReportingFrequency.ANNUAL,
            FinancialReportingFrequency.QUARTERLY,
        }
    }
    target_annual = _canonical_dates(eligible_annual_period_ends, reverse=True)[:5]
    target_reporting = _canonical_dates(
        eligible_reporting_period_ends,
        reverse=True,
    )[:8]
    covered_annual = tuple(
        period for period in target_annual if period in available_annual
    )
    covered_reporting = tuple(
        period for period in target_reporting if period in available_reporting
    )
    missing_annual = tuple(
        period for period in target_annual if period not in available_annual
    )
    missing_reporting = tuple(
        period for period in target_reporting if period not in available_reporting
    )
    normal_complete = (
        len(target_annual) == 5
        and len(target_reporting) == 8
        and not missing_annual
        and not missing_reporting
    )

    since_listing_exception: FinancialSinceListingException | None = None
    if not normal_complete and listing_date is not None and listing_provenance is not None:
        expected_annual = _canonical_dates(
            tuple(
                period
                for period in eligible_annual_period_ends
                if period >= listing_date
            )
        )
        expected_reporting = _canonical_dates(
            tuple(
                period
                for period in eligible_reporting_period_ends
                if period >= listing_date
            )
        )
        covered_since_annual = tuple(
            period for period in expected_annual if period in available_annual
        )
        covered_since_reporting = tuple(
            period for period in expected_reporting if period in available_reporting
        )
        missing_since_annual = tuple(
            period for period in expected_annual if period not in available_annual
        )
        missing_since_reporting = tuple(
            period for period in expected_reporting if period not in available_reporting
        )
        ignored_pre_listing = _canonical_dates(
            tuple(
                period
                for period in available_annual | available_reporting
                if period < listing_date
            )
        )
        normal_target_impossible = (
            len(expected_annual) < 5 or len(expected_reporting) < 8
        )
        since_listing_exception = FinancialSinceListingException(
            listing_date=listing_date,
            listing_provenance=listing_provenance,
            expected_annual_period_ends=expected_annual,
            expected_reporting_period_ends=expected_reporting,
            covered_annual_period_ends=covered_since_annual,
            covered_reporting_period_ends=covered_since_reporting,
            missing_annual_period_ends=missing_since_annual,
            missing_reporting_period_ends=missing_since_reporting,
            ignored_pre_listing_period_ends=ignored_pre_listing,
            supported=(
                normal_target_impossible
                and not missing_since_annual
                and not missing_since_reporting
            ),
        )
    complete = normal_complete or bool(
        since_listing_exception is not None and since_listing_exception.supported
    )
    reasons: tuple[FinancialPeriodRejectionReason, ...]
    if complete:
        reasons = ()
    elif since_listing_exception is not None:
        reasons = (
            FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
            FinancialPeriodRejectionReason.UNSUPPORTED_SINCE_LISTING_COVERAGE,
        )
    else:
        reasons = (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
    return FinancialHistoryCompletenessAssessment(
        instrument_identity=instrument_identity,
        statement_type=statement_type,
        company_type=company_type,
        consolidation_scope=consolidation_scope,
        currency=currency.upper(),
        target_annual_period_ends=target_annual,
        target_reporting_period_ends=target_reporting,
        covered_annual_period_ends=covered_annual,
        covered_reporting_period_ends=covered_reporting,
        missing_annual_period_ends=missing_annual,
        missing_reporting_period_ends=missing_reporting,
        since_listing_exception=since_listing_exception,
        rejection_reasons=reasons,
        complete=complete,
    )


class FinancialRatioHistoryCompletenessAssessment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    ratio_family: FinancialRatioFamily
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_reporting_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    complete: bool

    @model_validator(mode="after")
    def _validate_assessment(
        self,
    ) -> FinancialRatioHistoryCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("ratio history requires a resolved company type")
        period_sets = (
            self.target_reporting_period_ends,
            self.covered_reporting_period_ends,
            self.missing_reporting_period_ends,
        )
        if any(
            values != _canonical_dates(values, reverse=True)
            for values in period_sets
        ):
            raise ValueError("ratio history period sets must be unique and canonical")
        if (
            set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.target_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("ratio history coverage partitions are inconsistent")
        expected_complete = (
            len(self.target_reporting_period_ends) == 8
            and not self.missing_reporting_period_ends
        )
        if self.complete != expected_complete:
            raise ValueError("ratio history completeness is inconsistent")
        expected_reasons = (
            ()
            if self.complete
            else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
        )
        if self.rejection_reasons != expected_reasons:
            raise ValueError("ratio history rejection reasons are inconsistent")
        return self


def assess_financial_ratio_history_coverage(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    ratio_family: FinancialRatioFamily,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    assessments: Sequence[FinancialPeriodCompletenessAssessment],
    eligible_reporting_period_ends: Sequence[date],
) -> FinancialRatioHistoryCompletenessAssessment:
    target = _canonical_dates(eligible_reporting_period_ends, reverse=True)[:8]
    expected_instrument_payload = _authoritative_instrument_payload(
        instrument_identity
    )
    usable_periods = {
        item.period_end
        for item in assessments
        if item.usable_for_current_analysis
        and item.capability is FinancialCapability.RATIO_FAMILY
        and item.ratio_family is ratio_family
        and item.company_type is company_type
        and item.consolidation_scope is consolidation_scope
        and _authoritative_instrument_payload(item.instrument_identity)
        == expected_instrument_payload
        and item.frequency is FinancialReportingFrequency.QUARTERLY
    }
    covered = tuple(period for period in target if period in usable_periods)
    missing = tuple(period for period in target if period not in usable_periods)
    complete = len(target) == 8 and not missing
    return FinancialRatioHistoryCompletenessAssessment(
        instrument_identity=instrument_identity,
        ratio_family=ratio_family,
        company_type=company_type,
        consolidation_scope=consolidation_scope,
        currency=currency.upper(),
        target_reporting_period_ends=target,
        covered_reporting_period_ends=covered,
        missing_reporting_period_ends=missing,
        rejection_reasons=()
        if complete
        else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,),
        complete=complete,
    )


class FinancialProviderArtifactEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    artifact: FinancialProviderArtifactIdentity
    retention: Literal["retained"] = "retained"


FinancialUnavailableReason = (
    FinancialPeriodRejectionReason | AcquisitionUnavailableReason
)


class FinancialAcquisitionOutcome(BaseModel):
    """Typed payload-free outcome for one provider capability attempt."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    outcome: Literal["available", "unavailable"]
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    capability: FinancialCapability
    artifact_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$",
    )
    reason: FinancialUnavailableReason | None = None

    @classmethod
    def available(
        cls,
        *,
        provider_id: str,
        capability: FinancialCapability,
        artifact_identity: str,
    ) -> FinancialAcquisitionOutcome:
        return cls(
            outcome="available",
            provider_id=provider_id,
            capability=capability,
            artifact_identity=artifact_identity,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        capability: FinancialCapability,
        reason: FinancialUnavailableReason,
    ) -> FinancialAcquisitionOutcome:
        return cls(
            outcome="unavailable",
            provider_id=provider_id,
            capability=capability,
            reason=reason,
        )

    @model_validator(mode="after")
    def _validate_outcome(self) -> FinancialAcquisitionOutcome:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        if self.outcome == "available":
            if self.artifact_identity is None or self.reason is not None:
                raise ValueError("available financial outcome requires only an artifact")
        elif self.artifact_identity is not None or self.reason is None:
            raise ValueError("unavailable financial outcome requires only a typed reason")
        return self


class FinancialIndicatorMetadataDimensionBinding(BaseModel):
    """Bounded persisted state for one material compatibility dimension."""

    model_config = _CLOSED_MODEL_CONFIG

    normalized_field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    dimension: str = Field(pattern=_SAFE_ID_PATTERN)
    state: Literal["proven", "unproven", "not_applicable"]
    raw_provider_marker: Literal["UNKNOWN", "XXX"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    reason: str = Field(pattern=_SAFE_ID_PATTERN)


class FinancialIndicatorMetadataAssessmentBinding(BaseModel):
    """Identity/state-only compatibility projection safe for manifests and audit."""

    model_config = _CLOSED_MODEL_CONFIG

    field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] = "financial-indicator-field-semantics-v1"
    metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] = "financial-indicator-metadata-compatibility-v1"
    assessment_identity: str = Field(
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    family: FinancialRatioFamily
    compatibility_state: Literal["compatible", "incompatible"]
    current_only_metadata_eligible: bool
    strict_pit_metadata_eligible: bool
    dimensions: tuple[FinancialIndicatorMetadataDimensionBinding, ...]

    @model_validator(mode="after")
    def _validate_binding(self) -> FinancialIndicatorMetadataAssessmentBinding:
        canonical = tuple(
            sorted(
                self.dimensions,
                key=lambda item: (item.normalized_field, item.dimension),
            )
        )
        if not canonical or self.dimensions != canonical or len(canonical) != len(
            {(item.normalized_field, item.dimension) for item in canonical}
        ):
            raise ValueError("indicator metadata dimension bindings must be canonical")
        compatible = not any(item.state == "unproven" for item in canonical)
        if (self.compatibility_state == "compatible") != compatible:
            raise ValueError("indicator metadata assessment state is inconsistent")
        assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialIndicatorMetadataRejectionBinding(BaseModel):
    """Typed incompatibility projection without provider payload values."""

    model_config = _CLOSED_MODEL_CONFIG

    assessment_identity: str = Field(
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    family: FinancialRatioFamily
    typed_reasons: tuple[FinancialIndicatorMetadataDimensionBinding, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def _validate_reasons(self) -> FinancialIndicatorMetadataRejectionBinding:
        if any(item.state != "unproven" for item in self.typed_reasons):
            raise ValueError("indicator metadata rejection reason must be UNPROVEN")
        if self.typed_reasons != tuple(
            sorted(
                self.typed_reasons,
                key=lambda item: (item.normalized_field, item.dimension, item.reason),
            )
        ):
            raise ValueError("indicator metadata rejection reasons must be canonical")
        return self


class FinancialProviderCandidate(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-manifest-v1",
        "financial-provider-candidate-v2",
    ] = "financial-manifest-v1"
    candidate: FinancialPeriodCandidate
    assessment: FinancialPeriodCompletenessAssessment
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessment_identity: str | None = Field(
        default=None,
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    metadata_compatibility_state: Literal["compatible", "incompatible"] | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    metadata_selection_disposition: Literal[
        "selected",
        "unselected",
        "unresolved",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_incompatibility_reasons: tuple[
        FinancialIndicatorMetadataDimensionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _validate_binding(self) -> FinancialProviderCandidate:
        if self.assessment.candidate_identity != self.candidate.candidate_identity:
            raise ValueError("provider candidate assessment binding mismatch")
        if self.assessment.artifact_identity != self.candidate.artifact.artifact_identity:
            raise ValueError("provider candidate artifact binding mismatch")
        if self.assessment.period_identity != self.candidate.period_identity.period_identity:
            raise ValueError("provider candidate period binding mismatch")
        identity = self.candidate.period_identity
        if (
            _authoritative_instrument_payload(self.assessment.instrument_identity)
            != _authoritative_instrument_payload(identity.instrument_identity)
            or self.assessment.capability is not identity.capability
            or self.assessment.statement_type is not identity.statement_type
            or self.assessment.ratio_family is not identity.ratio_family
            or self.assessment.period_end != identity.period_end
            or self.assessment.frequency is not identity.frequency
            or self.assessment.company_type is not identity.company_type
            or self.assessment.consolidation_scope is not identity.consolidation_scope
            or self.assessment.currency != identity.currency
        ):
            raise ValueError("provider candidate assessment context mismatch")
        expected_observed_at = max(
            self.candidate.artifact.observed_at,
            self.candidate.filing_metadata.observed_at,
        )
        if (
            self.assessment.first_publication_date
            != self.candidate.filing_metadata.f_ann_date
            or self.assessment.revision_observed_at != expected_observed_at
            or self.assessment.metadata_complete
            != (self.candidate.filing_metadata.ann_date is not None)
        ):
            raise ValueError("provider candidate filing assessment mismatch")
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
            self.metadata_compatibility_assessment_identity,
            self.metadata_compatibility_state,
            self.metadata_selection_disposition,
        )
        if self.contract_version == FINANCIAL_PROVIDER_CANDIDATE_V2:
            if any(value is None for value in metadata_values):
                raise ValueError("indicator provider candidate metadata binding is incomplete")
            if (
                self.metadata_compatibility_state == "compatible"
                and self.metadata_incompatibility_reasons
            ) or (
                self.metadata_compatibility_state == "incompatible"
                and not self.metadata_incompatibility_reasons
            ):
                raise ValueError("indicator provider candidate metadata state is inconsistent")
            if (
                self.metadata_selection_disposition == "selected"
                and self.metadata_compatibility_state != "compatible"
            ):
                raise ValueError("incompatible indicator provider candidate is selected")
        elif any(value is not None for value in metadata_values) or (
            self.metadata_incompatibility_reasons
        ):
            raise ValueError("historical provider candidate carries indicator metadata")
        return self


class FinancialPeriodSelection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-v2",
        "financial-period-selection-v3",
    ] = (
        "financial-period-selection-v1"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    disposition: Literal["accepted", "current_only"]
    strict_pit_eligible: bool
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_basis: FinancialAvailabilityBasis | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    analysis_as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    ann_date: date | None = Field(default=None, exclude_if=lambda value: value is None)
    f_ann_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    local_provider_revision_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    retrieved_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    ratio_family: FinancialRatioFamily | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    period_end: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    availability_reason: FinancialPeriodRejectionReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessment_identity: str | None = Field(
        default=None,
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    metadata_compatibility_state: Literal["compatible"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    metadata_selection_disposition: Literal["selected"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    metadata_incompatibility_reasons: tuple[
        FinancialIndicatorMetadataDimensionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    selection_identity: str = Field(
        pattern=r"^financial-period-selection:v[123]:[0-9a-f]{64}$"
    )

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _canonicalize_availability_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("financial availability time must be concrete")
        return value.astimezone(timezone.utc)

    @classmethod
    def create(
        cls,
        *,
        candidate: FinancialPeriodCandidate,
        assessment: FinancialPeriodCompletenessAssessment,
        requested_analysis_scope: FinancialAvailabilityScope | None = None,
        eligibility_policy_version: Literal[
            "mainland-financial-period-eligibility-v2"
        ] | None = None,
        indicator_field_semantics_version: Literal[
            "financial-indicator-field-semantics-v1"
        ] | None = None,
        indicator_metadata_compatibility_version: Literal[
            "financial-indicator-metadata-compatibility-v1"
        ] | None = None,
        metadata_compatibility_assessment_identity: str | None = None,
    ) -> FinancialPeriodSelection:
        if assessment.disposition not in {
            FinancialPeriodDisposition.ACCEPTED,
            FinancialPeriodDisposition.CURRENT_ONLY,
        }:
            raise ValueError("only usable financial candidates may be selected")
        if (
            assessment.candidate_identity != candidate.candidate_identity
            or assessment.artifact_identity != candidate.artifact.artifact_identity
            or assessment.period_identity != candidate.period_identity.period_identity
        ):
            raise ValueError("financial selection candidate binding mismatch")
        availability_bound = eligibility_policy_version is not None
        if availability_bound != (requested_analysis_scope is not None):
            raise ValueError("financial selection availability request is incomplete")
        metadata_values = (
            indicator_field_semantics_version,
            indicator_metadata_compatibility_version,
            metadata_compatibility_assessment_identity,
        )
        metadata_bound = all(value is not None for value in metadata_values)
        if any(value is not None for value in metadata_values) and not metadata_bound:
            raise ValueError("indicator selection metadata binding is incomplete")
        payload: dict[str, object] = {
            "contract_version": (
                "financial-period-selection-v3"
                if metadata_bound
                else "financial-period-selection-v2"
                if availability_bound
                else "financial-period-selection-v1"
            ),
            "candidate_identity": candidate.candidate_identity,
            "artifact_identity": candidate.artifact.artifact_identity,
            "period_identity": candidate.period_identity.period_identity,
            "provider_id": candidate.artifact.dataset.provider_id,
            "disposition": assessment.disposition.value,
            "strict_pit_eligible": assessment.strict_pit_eligible,
        }
        if availability_bound:
            filing = candidate.filing_metadata
            if filing.ann_date is None or candidate.period_identity.ratio_family is None:
                raise ValueError("v2 financial availability requires indicator filing lineage")
            availability_scope = (
                FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                if assessment.strict_pit_eligible
                else FinancialAvailabilityScope.CURRENT_ONLY
            )
            if availability_scope is FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE:
                availability_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_FIRST_PUBLICATION_AND_OBSERVED_REVISION
                )
                availability_reason = None
            elif filing.f_ann_date is None:
                availability_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITHOUT_FIRST_PUBLICATION
                )
                availability_reason = (
                    FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                )
            else:
                availability_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE
                )
                availability_reason = FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
            payload.update(
                {
                    "requested_analysis_scope": requested_analysis_scope.value,
                    "availability_scope": availability_scope.value,
                    "availability_basis": availability_basis.value,
                    "analysis_as_of_date": assessment.pit_as_of_date.isoformat(),
                    "ann_date": filing.ann_date.isoformat(),
                    "f_ann_date": (
                        filing.f_ann_date.isoformat()
                        if filing.f_ann_date is not None
                        else None
                    ),
                    "local_provider_revision_identity": (
                        filing.local_provider_revision_identity
                    ),
                    "observed_at": _utc_text(assessment.revision_observed_at),
                    "retrieved_at": _utc_text(filing.retrieved_at),
                    "ratio_family": candidate.period_identity.ratio_family.value,
                    "period_end": candidate.period_identity.period_end.isoformat(),
                    "eligibility_policy_version": eligibility_policy_version,
                    "availability_reason": (
                        availability_reason.value
                        if availability_reason is not None
                        else None
                    ),
                }
            )
        if metadata_bound:
            payload.update(
                {
                    "indicator_field_semantics_version": (
                        indicator_field_semantics_version
                    ),
                    "indicator_metadata_compatibility_version": (
                        indicator_metadata_compatibility_version
                    ),
                    "metadata_compatibility_assessment_identity": (
                        metadata_compatibility_assessment_identity
                    ),
                    "metadata_compatibility_state": "compatible",
                    "metadata_selection_disposition": "selected",
                }
            )
        prefix = "v3" if metadata_bound else "v2" if availability_bound else "v1"
        identity_payload = {
            key: value for key, value in payload.items() if value is not None
        }
        return cls(
            **payload,
            selection_identity=(
                f"financial-period-selection:{prefix}:"
                + _canonical_digest(identity_payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodSelection:
        payload = self.model_dump(mode="json", exclude={"selection_identity"})
        prefix = {
            "financial-period-selection-v1": "v1",
            "financial-period-selection-v2": "v2",
            "financial-period-selection-v3": "v3",
        }[self.contract_version]
        expected = f"financial-period-selection:{prefix}:" + _canonical_digest(payload)
        if self.selection_identity != expected:
            raise ValueError("financial period selection identity mismatch")
        if self.strict_pit_eligible != (self.disposition == "accepted"):
            raise ValueError("financial selection PIT disposition is inconsistent")
        availability_values = (
            self.requested_analysis_scope,
            self.availability_scope,
            self.availability_basis,
            self.analysis_as_of_date,
            self.ann_date,
            self.local_provider_revision_identity,
            self.observed_at,
            self.retrieved_at,
            self.ratio_family,
            self.period_end,
            self.eligibility_policy_version,
        )
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
            self.metadata_compatibility_assessment_identity,
            self.metadata_compatibility_state,
            self.metadata_selection_disposition,
        )
        if self.contract_version == "financial-period-selection-v1":
            if any(value is not None for value in availability_values) or (
                self.f_ann_date is not None or self.availability_reason is not None
            ) or any(value is not None for value in metadata_values) or (
                self.metadata_incompatibility_reasons
            ):
                raise ValueError("legacy financial selection carries v2 availability")
            return self
        if self.contract_version == "financial-period-selection-v2" and (
            any(value is not None for value in metadata_values)
            or self.metadata_incompatibility_reasons
        ):
            raise ValueError("financial selection v2 carries indicator compatibility")
        if self.contract_version == "financial-period-selection-v3" and (
            any(value is None for value in metadata_values)
            or self.metadata_incompatibility_reasons
        ):
            raise ValueError("indicator selection metadata compatibility is incomplete")
        if (
            self.contract_version == "financial-period-selection-v3"
            and self.eligibility_policy_version is None
        ):
            if any(value is not None for value in availability_values) or (
                self.f_ann_date is not None or self.availability_reason is not None
            ):
                raise ValueError("indicator selection availability is partial")
            return self
        if any(value is None for value in availability_values):
            raise ValueError("v2 financial selection availability is incomplete")
        if self.local_provider_revision_identity != self.artifact_identity:
            raise ValueError("financial availability revision identity is contradictory")
        assert self.analysis_as_of_date is not None
        assert self.ann_date is not None
        assert self.observed_at is not None
        if self.ann_date > self.analysis_as_of_date:
            raise ValueError("selected financial announcement is after the cutoff")
        if self.availability_scope is FinancialAvailabilityScope.CURRENT_ONLY:
            if (
                self.strict_pit_eligible
                or self.requested_analysis_scope
                is not FinancialAvailabilityScope.CURRENT_ONLY
            ):
                raise ValueError("current-only financial availability is not degraded")
            if self.f_ann_date is None:
                expected_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITHOUT_FIRST_PUBLICATION
                )
                expected_reason = (
                    FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                )
            else:
                expected_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE
                )
                expected_reason = FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
                if (
                    self.f_ann_date <= self.analysis_as_of_date
                    and self.observed_at.date() <= self.analysis_as_of_date
                ):
                    raise ValueError("current-only financial PIT cutoff is satisfied")
            if (
                self.availability_basis is not expected_basis
                or self.availability_reason is not expected_reason
            ):
                raise ValueError("current-only financial availability is inconsistent")
        else:
            if (
                not self.strict_pit_eligible
                or self.f_ann_date is None
                or self.f_ann_date > self.analysis_as_of_date
                or self.observed_at.date() > self.analysis_as_of_date
                or self.availability_reason is not None
                or self.availability_basis is not (
                    FinancialAvailabilityBasis.ANN_DATE_FIRST_PUBLICATION_AND_OBSERVED_REVISION
                )
            ):
                raise ValueError("strict-PIT financial availability is incomplete")
        return self


class FinancialRejectedPeriod(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @classmethod
    def create(
        cls,
        *,
        candidate: FinancialPeriodCandidate,
        assessment: FinancialPeriodCompletenessAssessment,
    ) -> FinancialRejectedPeriod:
        if assessment.disposition not in {
            FinancialPeriodDisposition.REJECTED,
            FinancialPeriodDisposition.CONFLICTED,
        } or not assessment.rejection_reasons:
            raise ValueError("rejected-period contract requires typed rejection")
        if (
            assessment.candidate_identity != candidate.candidate_identity
            or assessment.artifact_identity != candidate.artifact.artifact_identity
            or assessment.period_identity != candidate.period_identity.period_identity
        ):
            raise ValueError("financial rejection candidate binding mismatch")
        return cls(
            candidate_identity=candidate.candidate_identity,
            artifact_identity=candidate.artifact.artifact_identity,
            period_identity=candidate.period_identity.period_identity,
            reasons=assessment.rejection_reasons,
        )

    @model_validator(mode="after")
    def _validate_reasons(self) -> FinancialRejectedPeriod:
        canonical = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if self.reasons != canonical:
            raise ValueError("rejected-period reasons must be unique and canonical")
        return self


class FinancialCriticalValueConflict(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    conflicting_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    normalized_field: str = Field(pattern=_SAFE_ID_PATTERN)
    selected_value: Decimal
    conflicting_value: Decimal
    conflict_identity: str = Field(
        pattern=r"^financial-critical-conflict:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        selected_candidate_identity: str,
        conflicting_candidate_identity: str,
        normalized_field: str,
        selected_value: Decimal,
        conflicting_value: Decimal,
    ) -> FinancialCriticalValueConflict:
        payload = {
            "contract_version": "financial-period-selection-v1",
            "selected_candidate_identity": selected_candidate_identity,
            "conflicting_candidate_identity": conflicting_candidate_identity,
            "normalized_field": normalized_field,
            "selected_value": str(selected_value),
            "conflicting_value": str(conflicting_value),
        }
        return cls(
            **payload,
            conflict_identity=(
                "financial-critical-conflict:v1:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialCriticalValueConflict:
        if self.selected_value == self.conflicting_value:
            raise ValueError("critical-value conflict requires different values")
        payload = self.model_dump(mode="json", exclude={"conflict_identity"})
        expected = "financial-critical-conflict:v1:" + _canonical_digest(payload)
        if self.conflict_identity != expected:
            raise ValueError("financial critical-value conflict identity mismatch")
        return self


class FinancialPeriodOverlapFinding(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    overlapping_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    disposition: Literal[
        "equivalent_unselected",
        "non_critical_difference",
        "critical_conflict",
        "company_type_conflict",
    ]
    compared_critical_fields: tuple[str, ...]
    compared_declared_core_fields: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    differing_non_critical_fields: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    conflict_identities: tuple[str, ...]
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...] = ()
    overlap_identity: str = Field(pattern=r"^financial-overlap:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodOverlapFinding:
        if self.compared_critical_fields != tuple(
            sorted(set(self.compared_critical_fields))
        ):
            raise ValueError("overlap critical fields must be unique and canonical")
        if self.conflict_identities != tuple(sorted(set(self.conflict_identities))):
            raise ValueError("overlap conflicts must be unique and canonical")
        if self.compared_declared_core_fields != tuple(
            sorted(set(self.compared_declared_core_fields))
        ) or self.differing_non_critical_fields != tuple(
            sorted(set(self.differing_non_critical_fields))
        ):
            raise ValueError("overlap declared-core fields must be unique and canonical")
        if not set(self.differing_non_critical_fields) <= set(
            self.compared_declared_core_fields
        ) or set(self.differing_non_critical_fields) & set(
            self.compared_critical_fields
        ):
            raise ValueError("overlap non-critical differences are inconsistent")
        canonical_reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.rejection_reasons
        )
        if self.rejection_reasons != canonical_reasons:
            raise ValueError("overlap rejection reasons must be unique and canonical")
        expected_reasons = {
            "equivalent_unselected": (),
            "non_critical_difference": (),
            "critical_conflict": (
                FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
            ),
            "company_type_conflict": (
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
            ),
        }[self.disposition]
        if (
            (self.disposition == "critical_conflict")
            != bool(self.conflict_identities)
            or self.rejection_reasons != expected_reasons
            or (
                self.disposition == "non_critical_difference"
                and not self.differing_non_critical_fields
            )
            or (
                self.disposition == "equivalent_unselected"
                and bool(self.differing_non_critical_fields)
            )
        ):
            raise ValueError("overlap disposition is inconsistent")
        payload = self.model_dump(mode="json", exclude={"overlap_identity"})
        expected = "financial-overlap:v1:" + _canonical_digest(payload)
        if self.overlap_identity != expected:
            raise ValueError("financial overlap identity mismatch")
        return self


class FinancialRevisionCandidateDisposition(BaseModel):
    """Immutable disposition of one observation within an exact selection key."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-revision-selection-v1"] = (
        "financial-revision-selection-v1"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    provider_route_rank: int = Field(ge=0)
    disposition: Literal[
        "selected",
        "operational_winner_conflicted",
        "superseded",
        "equivalent_unselected",
        "non_critical_difference_unselected",
        "conflicted_unselected",
    ]

    @model_validator(mode="after")
    def _validate_revision_binding(self) -> FinancialRevisionCandidateDisposition:
        if self.local_provider_revision_identity != self.artifact_identity:
            raise ValueError("revision disposition local identity is contradictory")
        return self


class FinancialRevisionSelection(BaseModel):
    """Deterministic operational winner and all exact-key alternatives."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-revision-selection-v1"] = (
        "financial-revision-selection-v1"
    )
    selection_key_identity: str = Field(
        pattern=r"^financial-selection-key:v1:[0-9a-f]{64}$"
    )
    instrument_identity: InstrumentIdentityEvidence
    company_type: FinancialCompanyType
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    period_end: date
    frequency: FinancialReportingFrequency
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    normalized_unit: str = Field(min_length=1, max_length=64)
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    selected_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    selected_provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    selected_provider_route_rank: int = Field(ge=0)
    ordering_basis: Literal[
        "qualified_provider_revision_sequence",
        "ann_date_observed_at_retrieved_at_local_revision_identity",
    ]
    chronology_claim: Literal["qualified_provider_sequence", "operational_only"]
    provider_selection_basis: Literal["immutable_route_rank"] = (
        "immutable_route_rank"
    )
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]
    conflicted: bool
    candidate_dispositions: tuple[FinancialRevisionCandidateDisposition, ...]
    selection_identity: str = Field(
        pattern=r"^financial-revision-selection:v1:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_selection(self) -> FinancialRevisionSelection:
        if self.capability is FinancialCapability.STATEMENT:
            if self.statement_type is None or self.ratio_family is not None:
                raise ValueError("statement revision selection family is invalid")
        elif self.statement_type is not None or self.ratio_family is None:
            raise ValueError("ratio revision selection family is invalid")
        if self.candidate_dispositions != tuple(
            sorted(
                self.candidate_dispositions,
                key=lambda item: item.candidate_identity,
            )
        ):
            raise ValueError("revision candidate dispositions must be canonical")
        disposition_by_id = {
            item.candidate_identity: item for item in self.candidate_dispositions
        }
        if len(disposition_by_id) != len(self.candidate_dispositions):
            raise ValueError("revision candidate dispositions must be unique")
        selected = disposition_by_id.get(self.selected_candidate_identity)
        expected_selected_disposition = (
            "operational_winner_conflicted" if self.conflicted else "selected"
        )
        if (
            selected is None
            or selected.artifact_identity != self.selected_artifact_identity
            or selected.provider_id != self.selected_provider_id
            or selected.provider_route_rank != self.selected_provider_route_rank
            or selected.disposition != expected_selected_disposition
        ):
            raise ValueError("revision operational winner binding is inconsistent")
        expected_claim = (
            "qualified_provider_sequence"
            if self.ordering_basis == "qualified_provider_revision_sequence"
            else "operational_only"
        )
        if self.chronology_claim != expected_claim:
            raise ValueError("revision ordering chronology claim is inconsistent")
        key_payload = {
            "instrument_identity": _authoritative_instrument_payload(
                self.instrument_identity
            ),
            "company_type": self.company_type.value,
            "capability": self.capability.value,
            "statement_type": (
                self.statement_type.value if self.statement_type is not None else None
            ),
            "ratio_family": (
                self.ratio_family.value if self.ratio_family is not None else None
            ),
            "period_end": self.period_end.isoformat(),
            "frequency": self.frequency.value,
            "consolidation_scope": self.consolidation_scope.value,
            "currency": self.currency,
            "normalized_unit": self.normalized_unit,
        }
        if self.selection_key_identity != (
            "financial-selection-key:v1:" + _canonical_digest(key_payload)
        ):
            raise ValueError("financial revision selection key identity mismatch")
        payload = self.model_dump(mode="json", exclude={"selection_identity"})
        expected = "financial-revision-selection:v1:" + _canonical_digest(payload)
        if self.selection_identity != expected:
            raise ValueError("financial revision selection identity mismatch")
        return self


def _revision_normalized_unit(
    provider_candidate: FinancialProviderCandidate,
) -> str:
    candidate = provider_candidate.candidate
    assessment = provider_candidate.assessment
    compared_fields = {
        *assessment.present_critical_fields,
        *assessment.present_core_fields,
    }
    units = {
        field.normalized_unit
        for field in candidate.fields
        if field.normalized_field in compared_fields
        and field.normalized_value is not None
        and field.normalized_unit is not None
    }
    if len(units) != 1:
        raise ValueError("eligible financial revision requires one normalized unit")
    return next(iter(units))


def _revision_selection_key_payload(
    provider_candidate: FinancialProviderCandidate,
) -> dict[str, object]:
    identity = provider_candidate.candidate.period_identity
    return {
        "instrument_identity": _authoritative_instrument_payload(
            identity.instrument_identity
        ),
        "company_type": identity.company_type.value,
        "capability": identity.capability.value,
        "statement_type": (
            identity.statement_type.value if identity.statement_type is not None else None
        ),
        "ratio_family": (
            identity.ratio_family.value if identity.ratio_family is not None else None
        ),
        "period_end": identity.period_end.isoformat(),
        "frequency": identity.frequency.value,
        "consolidation_scope": identity.consolidation_scope.value,
        "currency": identity.currency,
        "normalized_unit": _revision_normalized_unit(provider_candidate),
    }


def financial_revision_selection_key(
    provider_candidate: FinancialProviderCandidate,
) -> str:
    """Return the exact, provider-independent Ticket 05 grouping identity."""

    return "financial-selection-key:v1:" + _canonical_digest(
        _revision_selection_key_payload(provider_candidate)
    )


def reject_financial_period_for_incompatible_selection_key(
    assessment: FinancialPeriodCompletenessAssessment,
    *,
    selected_candidate: FinancialProviderCandidate,
    overlapping_candidate: FinancialProviderCandidate,
) -> FinancialPeriodCompletenessAssessment:
    """Reject exact-key mismatches before overlap or conflict comparison."""

    selected = _revision_selection_key_payload(selected_candidate)
    overlapping = _revision_selection_key_payload(overlapping_candidate)
    if selected == overlapping:
        return assessment

    mismatch_reasons: set[FinancialPeriodRejectionReason] = set()
    if selected["instrument_identity"] != overlapping["instrument_identity"]:
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    if any(
        selected[field] != overlapping[field]
        for field in ("capability", "statement_type", "ratio_family")
    ):
        mismatch_reasons.add(FinancialPeriodRejectionReason.CAPABILITY_MISMATCH)
    if any(
        selected[field] != overlapping[field]
        for field in ("period_end", "frequency")
    ):
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    if selected["company_type"] != overlapping["company_type"]:
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_COMPANY_TYPE)
    if selected["consolidation_scope"] != overlapping["consolidation_scope"]:
        mismatch_reasons.add(
            FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE
        )
    if selected["currency"] != overlapping["currency"]:
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY)
    if selected["normalized_unit"] != overlapping["normalized_unit"]:
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
    if not mismatch_reasons:
        mismatch_reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)

    reasons = tuple(
        reason
        for reason in FinancialPeriodRejectionReason
        if reason in {*assessment.rejection_reasons, *mismatch_reasons}
    )
    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.REJECTED.value,
            "rejection_reasons": [reason.value for reason in reasons],
            "usable_for_current_analysis": False,
        }
    )


def financial_provider_revision_sequence_is_qualified(
    candidates: Sequence[FinancialPeriodCandidate],
) -> bool:
    """Return whether provider revision IDs and sequence positions are one-to-one."""

    revisions = tuple(candidates)
    if not revisions or len(
        {item.artifact.dataset.provider_id for item in revisions}
    ) != 1:
        return False
    revision_ids = tuple(
        item.filing_metadata.provider_filing_revision_id for item in revisions
    )
    revision_sequences = tuple(
        item.filing_metadata.provider_filing_revision_sequence for item in revisions
    )
    return (
        all(
            item.filing_metadata.provider_filing_revision_sequence_qualification
            == "financial-provider-revision-sequence-qualified-v1"
            for item in revisions
        )
        and
        all(revision_id is not None for revision_id in revision_ids)
        and all(sequence is not None for sequence in revision_sequences)
        and len(set(revision_ids)) == len(revision_ids)
        and len(set(revision_sequences)) == len(revision_sequences)
    )


def order_financial_provider_revisions(
    provider_candidates: Sequence[FinancialProviderCandidate],
) -> tuple[
    tuple[FinancialProviderCandidate, ...],
    Literal[
        "qualified_provider_revision_sequence",
        "ann_date_observed_at_retrieved_at_local_revision_identity",
    ],
]:
    """Order one provider's eligible exact-key observations deterministically."""

    candidates = tuple(provider_candidates)
    if not candidates:
        raise ValueError("financial revision ordering requires candidates")
    key_identities = {financial_revision_selection_key(item) for item in candidates}
    provider_ids = {
        item.candidate.artifact.dataset.provider_id for item in candidates
    }
    if len(key_identities) != 1 or len(provider_ids) != 1:
        raise ValueError("financial revision ordering requires one provider and key")
    qualified_sequence = financial_provider_revision_sequence_is_qualified(
        tuple(item.candidate for item in candidates)
    )
    if qualified_sequence:
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    -int(
                        item.candidate.filing_metadata.provider_filing_revision_sequence
                    ),
                    item.candidate.filing_metadata.local_provider_revision_identity,
                ),
            )
        )
        return ordered, "qualified_provider_revision_sequence"

    def operational_key(item: FinancialProviderCandidate) -> tuple[object, ...]:
        filing = item.candidate.filing_metadata
        if filing.ann_date is None:
            raise ValueError("eligible financial revision requires ann_date")
        return (
            -filing.ann_date.toordinal(),
            -item.assessment.revision_observed_at.timestamp(),
            -filing.retrieved_at.timestamp(),
            filing.local_provider_revision_identity,
        )

    return (
        tuple(sorted(candidates, key=operational_key)),
        "ann_date_observed_at_retrieved_at_local_revision_identity",
    )


def create_financial_revision_selection(
    *,
    provider_candidates: Sequence[FinancialProviderCandidate],
    selected_candidate_identity: str,
    provider_route_ranks: Mapping[str, int],
    conflicted: bool,
    requested_analysis_scope: FinancialAvailabilityScope | None,
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ],
) -> FinancialRevisionSelection:
    """Bind an exact key's route winner and all retained alternatives."""

    candidates = tuple(provider_candidates)
    by_id = {item.candidate.candidate_identity: item for item in candidates}
    if len(by_id) != len(candidates) or selected_candidate_identity not in by_id:
        raise ValueError("financial revision selection candidates are incomplete")
    key_identities = {financial_revision_selection_key(item) for item in candidates}
    if len(key_identities) != 1:
        raise ValueError("financial revision selection mixed incompatible keys")
    selected = by_id[selected_candidate_identity]
    selected_candidate = selected.candidate
    selected_provider = selected_candidate.artifact.dataset.provider_id
    if selected_provider not in provider_route_ranks:
        raise ValueError("selected financial provider has no immutable route rank")
    selected_provider_candidates = tuple(
        item
        for item in candidates
        if item.candidate.artifact.dataset.provider_id == selected_provider
    )
    ordered_selected_provider, ordering_basis = order_financial_provider_revisions(
        selected_provider_candidates
    )
    if ordered_selected_provider[0].candidate.candidate_identity != (
        selected_candidate_identity
    ):
        raise ValueError("financial revision winner contradicts its ordering basis")
    provider_winners: list[FinancialProviderCandidate] = []
    for provider_id in sorted(
        {
            item.candidate.artifact.dataset.provider_id
            for item in candidates
        },
        key=lambda item: provider_route_ranks[item],
    ):
        ordered, _ = order_financial_provider_revisions(
            tuple(
                item
                for item in candidates
                if item.candidate.artifact.dataset.provider_id == provider_id
            )
        )
        provider_winners.append(ordered[0])
    if min(
        provider_winners,
        key=lambda item: provider_route_ranks[
            item.candidate.artifact.dataset.provider_id
        ],
    ).candidate.candidate_identity != selected_candidate_identity:
        raise ValueError("financial revision winner contradicts provider route rank")

    dispositions: list[FinancialRevisionCandidateDisposition] = []
    for item in candidates:
        candidate = item.candidate
        provider_id = candidate.artifact.dataset.provider_id
        if candidate.candidate_identity == selected_candidate_identity:
            disposition = (
                "operational_winner_conflicted" if conflicted else "selected"
            )
        else:
            same_provider = provider_id == selected_provider
            if same_provider and ordering_basis == "qualified_provider_revision_sequence":
                disposition = "superseded"
            else:
                overlap, candidate_conflicts = compare_financial_period_candidates(
                    selected_candidate,
                    candidate,
                    selected_assessment=selected.assessment,
                    overlapping_assessment=item.assessment,
                )
                if candidate_conflicts:
                    disposition = "conflicted_unselected"
                elif overlap.disposition == "equivalent_unselected":
                    disposition = "equivalent_unselected"
                else:
                    disposition = "non_critical_difference_unselected"
        dispositions.append(
            FinancialRevisionCandidateDisposition(
                candidate_identity=candidate.candidate_identity,
                artifact_identity=candidate.artifact.artifact_identity,
                local_provider_revision_identity=(
                    candidate.filing_metadata.local_provider_revision_identity
                ),
                provider_id=provider_id,
                provider_route_rank=provider_route_ranks[provider_id],
                disposition=disposition,
            )
        )
    identity = selected_candidate.period_identity
    key_payload = _revision_selection_key_payload(selected)
    payload: dict[str, object] = {
        "contract_version": "financial-revision-selection-v1",
        "selection_key_identity": next(iter(key_identities)),
        "instrument_identity": identity.instrument_identity,
        "company_type": identity.company_type,
        "capability": identity.capability,
        "statement_type": identity.statement_type,
        "ratio_family": identity.ratio_family,
        "period_end": identity.period_end,
        "frequency": identity.frequency,
        "consolidation_scope": identity.consolidation_scope,
        "currency": identity.currency,
        "normalized_unit": key_payload["normalized_unit"],
        "selected_candidate_identity": selected_candidate_identity,
        "selected_artifact_identity": selected_candidate.artifact.artifact_identity,
        "selected_provider_id": selected_provider,
        "selected_provider_route_rank": provider_route_ranks[selected_provider],
        "ordering_basis": ordering_basis,
        "chronology_claim": (
            "qualified_provider_sequence"
            if ordering_basis == "qualified_provider_revision_sequence"
            else "operational_only"
        ),
        "provider_selection_basis": "immutable_route_rank",
        "eligibility_policy_version": eligibility_policy_version,
        "conflicted": conflicted,
        "candidate_dispositions": tuple(
            sorted(dispositions, key=lambda item: item.candidate_identity)
        ),
    }
    if requested_analysis_scope is not None:
        payload["requested_analysis_scope"] = requested_analysis_scope
    return FinancialRevisionSelection(
        **payload,
        selection_identity=(
            "financial-revision-selection:v1:"
            + _canonical_digest(payload)
        ),
    )


def _period_compatibility_payload(candidate: FinancialPeriodCandidate) -> dict[str, object]:
    identity = candidate.period_identity
    return {
        "instrument_identity": _authoritative_instrument_payload(
            identity.instrument_identity
        ),
        "capability": identity.capability.value,
        "statement_type": (
            identity.statement_type.value if identity.statement_type is not None else None
        ),
        "ratio_family": (
            identity.ratio_family.value if identity.ratio_family is not None else None
        ),
        "period_end": identity.period_end.isoformat(),
        "frequency": identity.frequency.value,
        "consolidation_scope": identity.consolidation_scope.value,
        # Ratio fields are normalized to RATIO before overlap comparison. Keep
        # provider currency metadata on each candidate, but do not treat the
        # provider-specific XXX/CNY marker as a value compatibility boundary.
        "currency": (
            None
            if identity.capability is FinancialCapability.RATIO_FAMILY
            else identity.currency
        ),
    }


def compare_financial_period_candidates(
    selected: FinancialPeriodCandidate,
    overlapping: FinancialPeriodCandidate,
    *,
    selected_assessment: FinancialPeriodCompletenessAssessment | None = None,
    overlapping_assessment: FinancialPeriodCompletenessAssessment | None = None,
) -> tuple[FinancialPeriodOverlapFinding, tuple[FinancialCriticalValueConflict, ...]]:
    """Compare compatible overlap and isolate critical conflicts to one period."""

    if _period_compatibility_payload(selected) != _period_compatibility_payload(
        overlapping
    ):
        raise ValueError("financial overlap candidates have incompatible Period Identity")
    identity = selected.period_identity
    if identity.company_type is not overlapping.period_identity.company_type:
        payload = {
            "contract_version": "financial-period-selection-v1",
            "selected_candidate_identity": selected.candidate_identity,
            "overlapping_candidate_identity": overlapping.candidate_identity,
            "disposition": "company_type_conflict",
            "compared_critical_fields": [],
            "conflict_identities": [],
            "rejection_reasons": [
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE.value
            ],
        }
        return (
            FinancialPeriodOverlapFinding(
                **payload,
                overlap_identity=(
                    "financial-overlap:v1:" + _canonical_digest(payload)
                ),
            ),
            (),
        )
    if (selected_assessment is None) != (overlapping_assessment is None):
        raise ValueError("financial overlap assessments must be supplied together")
    if (
        identity.capability is FinancialCapability.STATEMENT
        and identity.statement_type is not None
    ):
        critical_fields = financial_statement_field_declaration(
            identity.company_type,
            identity.statement_type,
        ).critical_fields
    else:
        critical_fields = ()
    core_fields: tuple[str, ...] = ()
    if selected_assessment is not None and overlapping_assessment is not None:
        critical_fields = tuple(
            sorted(
                {
                    *selected_assessment.declared_critical_fields,
                    *overlapping_assessment.declared_critical_fields,
                }
            )
        )
        core_fields = tuple(
            sorted(
                {
                    *selected_assessment.declared_core_fields,
                    *overlapping_assessment.declared_core_fields,
                }
            )
        )
    selected_values = {
        field.normalized_field: field.normalized_value for field in selected.fields
    }
    overlapping_values = {
        field.normalized_field: field.normalized_value for field in overlapping.fields
    }
    compared_fields = tuple(
        field
        for field in critical_fields
        if selected_values.get(field) is not None
        and overlapping_values.get(field) is not None
    )
    compared_core_fields = tuple(
        field
        for field in core_fields
        if selected_values.get(field) is not None
        and overlapping_values.get(field) is not None
    )
    conflicts = tuple(
        FinancialCriticalValueConflict.create(
            selected_candidate_identity=selected.candidate_identity,
            conflicting_candidate_identity=overlapping.candidate_identity,
            normalized_field=field,
            selected_value=Decimal(str(selected_values[field])),
            conflicting_value=Decimal(str(overlapping_values[field])),
        )
        for field in compared_fields
        if Decimal(str(selected_values[field]))
        != Decimal(str(overlapping_values[field]))
    )
    differing_non_critical_fields = tuple(
        field
        for field in compared_core_fields
        if field not in compared_fields
        and Decimal(str(selected_values[field]))
        != Decimal(str(overlapping_values[field]))
    )
    payload = {
        "contract_version": "financial-period-selection-v1",
        "selected_candidate_identity": selected.candidate_identity,
        "overlapping_candidate_identity": overlapping.candidate_identity,
        "disposition": (
            "critical_conflict"
            if conflicts
            else (
                "non_critical_difference"
                if differing_non_critical_fields
                else "equivalent_unselected"
            )
        ),
        "compared_critical_fields": list(compared_fields),
        "conflict_identities": sorted(
            conflict.conflict_identity for conflict in conflicts
        ),
        "rejection_reasons": (
            [FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES.value]
            if conflicts
            else []
        ),
    }
    if compared_core_fields:
        payload["compared_declared_core_fields"] = list(compared_core_fields)
    if differing_non_critical_fields:
        payload["differing_non_critical_fields"] = list(
            differing_non_critical_fields
        )
    overlap = FinancialPeriodOverlapFinding(
        **payload,
        overlap_identity="financial-overlap:v1:" + _canonical_digest(payload),
    )
    return overlap, conflicts


def mark_financial_period_conflicted(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    reasons = tuple(
        reason
        for reason in FinancialPeriodRejectionReason
        if reason
        in {
            *assessment.rejection_reasons,
            FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
        }
    )
    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.CONFLICTED.value,
            "rejection_reasons": [reason.value for reason in reasons],
            "usable_for_current_analysis": False,
        }
    )


def mark_financial_period_company_type_conflicted(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    """Return a new typed conflict without mutating the retained candidate."""

    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.CONFLICTED.value,
            "rejection_reasons": [
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE.value
            ],
            "usable_for_current_analysis": False,
        }
    )


def reject_financial_period_for_company_type(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    """Reject an incompatible exact-key dimension before v2 overlap selection."""

    reasons = tuple(
        reason
        for reason in FinancialPeriodRejectionReason
        if reason
        in {
            *assessment.rejection_reasons,
            FinancialPeriodRejectionReason.INCOMPATIBLE_COMPANY_TYPE,
        }
    )
    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.REJECTED.value,
            "rejection_reasons": [reason.value for reason in reasons],
            "usable_for_current_analysis": False,
        }
    )


FinancialAggregateCompleteness = (
    FinancialHistoryCompletenessAssessment
    | FinancialRatioHistoryCompletenessAssessment
)


class FinancialRenderedArtifactIdentity(BaseModel):
    """Content identity for the bounded normalized rendering exposed to the model."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-rendered-artifact-v1"] = (
        "financial-rendered-artifact-v1"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-rendered-artifact:v1:[0-9a-f]{64}$"
    )
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    media_type: Literal["application/json"] = "application/json"
    byte_length: int = Field(ge=1)

    @classmethod
    def create(cls, rendered_value: str) -> FinancialRenderedArtifactIdentity:
        if not isinstance(rendered_value, str) or not rendered_value:
            raise ValueError("financial rendered artifact requires bounded content")
        encoded = rendered_value.encode("utf-8")
        digest = sha256(encoded).hexdigest()
        return cls(
            artifact_identity=f"financial-rendered-artifact:v1:{digest}",
            content_sha256=digest,
            byte_length=len(encoded),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialRenderedArtifactIdentity:
        if self.artifact_identity != (
            f"financial-rendered-artifact:v1:{self.content_sha256}"
        ):
            raise ValueError("financial rendered artifact identity mismatch")
        return self


def _manifest_identity_payload(
    *,
    contract_version: Literal[
        "financial-manifest-v1",
        "financial-manifest-v2",
        "financial-manifest-v3",
        "financial-manifest-v4",
    ],
    capability_routing_plan_signature: str,
    instrument_identity: InstrumentIdentityEvidence,
    requested_capability: FinancialCapability,
    requested_statement_type: FinancialStatementType | None,
    requested_ratio_family: FinancialRatioFamily | None,
    artifacts: Sequence[FinancialProviderArtifactEntry],
    acquisition_outcomes: Sequence[FinancialAcquisitionOutcome],
    provider_candidates: Sequence[FinancialProviderCandidate],
    selections: Sequence[FinancialPeriodSelection],
    rejected_periods: Sequence[FinancialRejectedPeriod],
    overlaps: Sequence[FinancialPeriodOverlapFinding],
    conflicts: Sequence[FinancialCriticalValueConflict],
    revision_selections: Sequence[FinancialRevisionSelection],
    aggregate_completeness: FinancialAggregateCompleteness,
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = None,
    physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent] = (),
    disposition: FinancialEvidenceDisposition | None = None,
    final_rendered_artifact: FinancialRenderedArtifactIdentity | None = None,
    requested_analysis_scope: FinancialAvailabilityScope | None = None,
    availability_scope: FinancialAvailabilityScope | None = None,
    availability_basis: FinancialAvailabilityBasis | None = None,
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = None,
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = None,
    availability_reason: FinancialPeriodRejectionReason | None = None,
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = None,
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = None,
    metadata_compatibility_assessments: Sequence[
        FinancialIndicatorMetadataAssessmentBinding
    ] = (),
    metadata_incompatible_rejections: Sequence[
        FinancialIndicatorMetadataRejectionBinding
    ] = (),
    selected_families: Sequence[FinancialRatioFamily] = (),
    unselected_families: Sequence[FinancialRatioFamily] = (),
    unresolved_families: Sequence[FinancialRatioFamily] = (),
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": contract_version,
        "capability_routing_plan_signature": capability_routing_plan_signature,
        "instrument_identity": _authoritative_instrument_payload(instrument_identity),
        "requested_capability": requested_capability.value,
        "requested_statement_type": (
            requested_statement_type.value
            if requested_statement_type is not None
            else None
        ),
        "requested_ratio_family": (
            requested_ratio_family.value if requested_ratio_family is not None else None
        ),
        "normalizer_version": "mainland-financial-normalizer-v1",
        "completeness_policy_version": "mainland-financial-completeness-v1",
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "acquisition_outcomes": [
            item.model_dump(mode="json") for item in acquisition_outcomes
        ],
        "provider_candidates": [
            item.model_dump(mode="json") for item in provider_candidates
        ],
        "selections": [item.model_dump(mode="json") for item in selections],
        "rejected_periods": [
            item.model_dump(mode="json") for item in rejected_periods
        ],
        "overlaps": [item.model_dump(mode="json") for item in overlaps],
        "conflicts": [item.model_dump(mode="json") for item in conflicts],
        "aggregate_completeness": aggregate_completeness.model_dump(mode="json"),
    }
    if revision_selections:
        payload["revision_selections"] = [
            item.model_dump(mode="json") for item in revision_selections
        ]
    if contract_version in {
        FINANCIAL_MANIFEST_V2,
        FINANCIAL_MANIFEST_V3,
        FINANCIAL_MANIFEST_V4,
    }:
        payload.update(
            {
                "physical_attempt_events": [
                    item.model_dump(mode="json")
                    for item in physical_attempt_events
                ],
                "disposition": (
                    disposition.value if disposition is not None else None
                ),
                "final_rendered_artifact": (
                    final_rendered_artifact.model_dump(mode="json")
                    if final_rendered_artifact is not None
                    else None
                ),
                "requested_analysis_scope": (
                    requested_analysis_scope.value
                    if requested_analysis_scope is not None
                    else None
                ),
                "availability_scope": (
                    availability_scope.value
                    if availability_scope is not None
                    else None
                ),
                "availability_basis": (
                    availability_basis.value
                    if availability_basis is not None
                    else None
                ),
                "eligibility_policy_version": eligibility_policy_version,
                "availability_reason": (
                    availability_reason.value
                    if availability_reason is not None
                    else None
                ),
            }
        )
        if financial_eligibility_binding is not None:
            payload["financial_eligibility_binding"] = (
                financial_eligibility_binding.model_dump(mode="json")
            )
    if contract_version in {FINANCIAL_MANIFEST_V3, FINANCIAL_MANIFEST_V4}:
        payload.update(
            {
                "indicator_field_semantics_version": (
                    indicator_field_semantics_version
                ),
                "indicator_metadata_compatibility_version": (
                    indicator_metadata_compatibility_version
                ),
                "metadata_compatibility_assessments": [
                    item.model_dump(mode="json")
                    for item in metadata_compatibility_assessments
                ],
                "metadata_incompatible_rejections": [
                    item.model_dump(mode="json")
                    for item in metadata_incompatible_rejections
                ],
                "selected_families": [item.value for item in selected_families],
                "unselected_families": [item.value for item in unselected_families],
                "unresolved_families": [item.value for item in unresolved_families],
            }
        )
    if contract_version == FINANCIAL_MANIFEST_V4:
        payload["temporal_eligibility_binding"] = (
            temporal_eligibility_binding.model_dump(mode="json")
            if temporal_eligibility_binding is not None
            else None
        )
    if publication_target_construction is not None:
        payload["publication_target_construction"] = (
            publication_target_construction.model_dump(mode="json")
        )
    return payload


class FinancialAcquisitionManifest(BaseModel):
    """Safe immutable operational manifest; it is not a Source Fact container."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-manifest-v1",
        "financial-manifest-v2",
        "financial-manifest-v3",
        "financial-manifest-v4",
    ] = FINANCIAL_MANIFEST_V1
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    instrument_identity: InstrumentIdentityEvidence
    requested_capability: FinancialCapability
    requested_statement_type: FinancialStatementType | None = None
    requested_ratio_family: FinancialRatioFamily | None = None
    normalizer_version: Literal["mainland-financial-normalizer-v1"] = (
        "mainland-financial-normalizer-v1"
    )
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v1"
    ] = "mainland-financial-completeness-v1"
    artifacts: tuple[FinancialProviderArtifactEntry, ...]
    acquisition_outcomes: tuple[FinancialAcquisitionOutcome, ...]
    provider_candidates: tuple[FinancialProviderCandidate, ...]
    selections: tuple[FinancialPeriodSelection, ...]
    rejected_periods: tuple[FinancialRejectedPeriod, ...]
    overlaps: tuple[FinancialPeriodOverlapFinding, ...]
    conflicts: tuple[FinancialCriticalValueConflict, ...]
    revision_selections: tuple[FinancialRevisionSelection, ...] = ()
    aggregate_completeness: FinancialAggregateCompleteness
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()
    disposition: FinancialEvidenceDisposition | None = None
    final_rendered_artifact: FinancialRenderedArtifactIdentity | None = None
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_basis: FinancialAvailabilityBasis | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    availability_reason: FinancialPeriodRejectionReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessments: tuple[
        FinancialIndicatorMetadataAssessmentBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    metadata_incompatible_rejections: tuple[
        FinancialIndicatorMetadataRejectionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    selected_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    unselected_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    unresolved_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    manifest_identity: str = Field(pattern=r"^financial-manifest:v[1234]:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        capability_routing_plan_signature: str,
        instrument_identity: InstrumentIdentityEvidence,
        requested_capability: FinancialCapability,
        requested_statement_type: FinancialStatementType | None,
        requested_ratio_family: FinancialRatioFamily | None,
        artifacts: Sequence[FinancialProviderArtifactEntry],
        acquisition_outcomes: Sequence[FinancialAcquisitionOutcome],
        provider_candidates: Sequence[FinancialProviderCandidate],
        selections: Sequence[FinancialPeriodSelection],
        rejected_periods: Sequence[FinancialRejectedPeriod],
        overlaps: Sequence[FinancialPeriodOverlapFinding],
        conflicts: Sequence[FinancialCriticalValueConflict],
        aggregate_completeness: FinancialAggregateCompleteness,
        revision_selections: Sequence[FinancialRevisionSelection] = (),
    ) -> FinancialAcquisitionManifest:
        canonical_artifacts = tuple(
            sorted(artifacts, key=lambda item: item.artifact.artifact_identity)
        )
        canonical_outcomes = tuple(
            sorted(
                acquisition_outcomes,
                key=lambda item: (
                    item.provider_id,
                    item.capability.value,
                    item.outcome,
                    item.artifact_identity or "",
                    item.reason.value if item.reason is not None else "",
                ),
            )
        )
        canonical_candidates = tuple(
            sorted(
                provider_candidates,
                key=lambda item: item.candidate.candidate_identity,
            )
        )
        canonical_selections = tuple(
            sorted(selections, key=lambda item: item.selection_identity)
        )
        canonical_rejections = tuple(
            sorted(rejected_periods, key=lambda item: item.candidate_identity)
        )
        canonical_overlaps = tuple(
            sorted(overlaps, key=lambda item: item.overlap_identity)
        )
        canonical_conflicts = tuple(
            sorted(conflicts, key=lambda item: item.conflict_identity)
        )
        canonical_revision_selections = tuple(
            sorted(
                revision_selections,
                key=lambda item: item.selection_identity,
            )
        )
        payload = _manifest_identity_payload(
            contract_version=FINANCIAL_MANIFEST_V1,
            capability_routing_plan_signature=capability_routing_plan_signature,
            instrument_identity=instrument_identity,
            requested_capability=requested_capability,
            requested_statement_type=requested_statement_type,
            requested_ratio_family=requested_ratio_family,
            artifacts=canonical_artifacts,
            acquisition_outcomes=canonical_outcomes,
            provider_candidates=canonical_candidates,
            selections=canonical_selections,
            rejected_periods=canonical_rejections,
            overlaps=canonical_overlaps,
            conflicts=canonical_conflicts,
            revision_selections=canonical_revision_selections,
            aggregate_completeness=aggregate_completeness,
        )
        return cls(
            capability_routing_plan_signature=capability_routing_plan_signature,
            instrument_identity=instrument_identity,
            requested_capability=requested_capability,
            requested_statement_type=requested_statement_type,
            requested_ratio_family=requested_ratio_family,
            artifacts=canonical_artifacts,
            acquisition_outcomes=canonical_outcomes,
            provider_candidates=canonical_candidates,
            selections=canonical_selections,
            rejected_periods=canonical_rejections,
            overlaps=canonical_overlaps,
            conflicts=canonical_conflicts,
            revision_selections=canonical_revision_selections,
            aggregate_completeness=aggregate_completeness,
            manifest_identity="financial-manifest:v1:" + _canonical_digest(payload),
        )

    def finalize(
        self,
        *,
        physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent],
        disposition: FinancialEvidenceDisposition,
        rendered_value: str,
        publication_target_construction: (
            MainlandFinancialPeriodTargetConstruction | None
        ) = None,
        requested_analysis_scope: FinancialAvailabilityScope | None = None,
        eligibility_policy_version: Literal[
            "mainland-financial-period-eligibility-v2"
        ] | None = None,
        financial_eligibility_binding: (
            MainlandFinancialEligibilityProjectionBinding | None
        ) = None,
        indicator_field_semantics_version: Literal[
            "financial-indicator-field-semantics-v1"
        ] | None = None,
        indicator_metadata_compatibility_version: Literal[
            "financial-indicator-metadata-compatibility-v1"
        ] | None = None,
        metadata_compatibility_assessments: Sequence[
            FinancialIndicatorMetadataAssessmentBinding
        ] = (),
        metadata_incompatible_rejections: Sequence[
            FinancialIndicatorMetadataRejectionBinding
        ] = (),
        selected_families: Sequence[FinancialRatioFamily] = (),
        unselected_families: Sequence[FinancialRatioFamily] = (),
        unresolved_families: Sequence[FinancialRatioFamily] = (),
        temporal_eligibility_binding: (
            FinancialTemporalEligibilityCheckpointProjection | None
        ) = None,
    ) -> FinancialAcquisitionManifest:
        """Bind the v1 selection contract to Ticket 11 operational lineage."""

        if self.contract_version != FINANCIAL_MANIFEST_V1:
            raise ValueError("only a v1 financial manifest may be finalized")
        metadata_values = (
            indicator_field_semantics_version,
            indicator_metadata_compatibility_version,
        )
        indicator_v3 = all(value is not None for value in metadata_values)
        if any(value is not None for value in metadata_values) and not indicator_v3:
            raise ValueError("indicator manifest metadata binding is incomplete")
        if indicator_v3:
            family_dispositions = (
                *selected_families,
                *unselected_families,
                *unresolved_families,
            )
            if (
                self.requested_capability is not FinancialCapability.RATIO_FAMILY
                or self.requested_ratio_family is None
                or family_dispositions != (self.requested_ratio_family,)
                or any(
                    item.contract_version != FINANCIAL_PROVIDER_CANDIDATE_V2
                    for item in self.provider_candidates
                )
                or any(
                    item.contract_version != FINANCIAL_PERIOD_SELECTION_V3
                    for item in self.selections
                )
            ):
                raise ValueError("indicator manifest v3 binding is incomplete")
        elif (
            metadata_compatibility_assessments
            or metadata_incompatible_rejections
            or selected_families
            or unselected_families
            or unresolved_families
        ):
            raise ValueError("historical manifest carries indicator metadata")
        canonical_attempts = tuple(
            sorted(
                physical_attempt_events,
                key=lambda item: item.attempt_event_id,
            )
        )
        final_rendered_artifact = FinancialRenderedArtifactIdentity.create(
            rendered_value
        )
        availability_selections = tuple(
            selection
            for selection in self.selections
            if selection.contract_version
            in {FINANCIAL_PERIOD_SELECTION_V2, FINANCIAL_PERIOD_SELECTION_V3}
            and selection.eligibility_policy_version is not None
        )
        availability_scope = (
            FinancialAvailabilityScope.CURRENT_ONLY
            if any(
                selection.availability_scope
                is FinancialAvailabilityScope.CURRENT_ONLY
                for selection in availability_selections
            )
            else (
                FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                if availability_selections
                else None
            )
        )
        availability_basis = (
            FinancialAvailabilityBasis.ANN_DATE_WITHOUT_FIRST_PUBLICATION
            if availability_scope is FinancialAvailabilityScope.CURRENT_ONLY
            and any(selection.f_ann_date is None for selection in availability_selections)
            else (
                FinancialAvailabilityBasis.ANN_DATE_FIRST_PUBLICATION_AND_OBSERVED_REVISION
                if availability_scope is FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                else (
                    FinancialAvailabilityBasis.ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE
                    if availability_scope is FinancialAvailabilityScope.CURRENT_ONLY
                    else None
                )
            )
        )
        availability_reason = next(
            (
                selection.availability_reason
                for selection in availability_selections
                if selection.availability_reason is not None
            ),
            None,
        )
        rejection_reasons = {
            reason
            for rejection in self.rejected_periods
            for reason in rejection.reasons
        }
        if availability_scope is None and requested_analysis_scope is (
            FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
        ):
            if (
                FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                in rejection_reasons
            ):
                availability_scope = FinancialAvailabilityScope.CURRENT_ONLY
                availability_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITHOUT_FIRST_PUBLICATION
                )
                availability_reason = (
                    FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                )
            elif FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED in (
                rejection_reasons
            ):
                availability_scope = FinancialAvailabilityScope.CURRENT_ONLY
                availability_basis = (
                    FinancialAvailabilityBasis.ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE
                )
                availability_reason = (
                    FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
                )
        forward_contract_version = (
            FINANCIAL_MANIFEST_V4
            if temporal_eligibility_binding is not None
            else (FINANCIAL_MANIFEST_V3 if indicator_v3 else FINANCIAL_MANIFEST_V2)
        )
        canonical_metadata_assessments = tuple(
            sorted(
                metadata_compatibility_assessments,
                key=lambda item: item.candidate_identity,
            )
        )
        canonical_metadata_rejections = tuple(
            sorted(
                metadata_incompatible_rejections,
                key=lambda item: item.candidate_identity,
            )
        )
        payload = _manifest_identity_payload(
            contract_version=forward_contract_version,
            capability_routing_plan_signature=self.capability_routing_plan_signature,
            instrument_identity=self.instrument_identity,
            requested_capability=self.requested_capability,
            requested_statement_type=self.requested_statement_type,
            requested_ratio_family=self.requested_ratio_family,
            artifacts=self.artifacts,
            acquisition_outcomes=self.acquisition_outcomes,
            provider_candidates=self.provider_candidates,
            selections=self.selections,
            rejected_periods=self.rejected_periods,
            overlaps=self.overlaps,
            conflicts=self.conflicts,
            revision_selections=self.revision_selections,
            aggregate_completeness=self.aggregate_completeness,
            publication_target_construction=publication_target_construction,
            physical_attempt_events=canonical_attempts,
            disposition=disposition,
            final_rendered_artifact=final_rendered_artifact,
            requested_analysis_scope=requested_analysis_scope,
            availability_scope=availability_scope,
            availability_basis=availability_basis,
            eligibility_policy_version=eligibility_policy_version,
            financial_eligibility_binding=financial_eligibility_binding,
            availability_reason=availability_reason,
            indicator_field_semantics_version=indicator_field_semantics_version,
            indicator_metadata_compatibility_version=(
                indicator_metadata_compatibility_version
            ),
            metadata_compatibility_assessments=(
                canonical_metadata_assessments
            ),
            metadata_incompatible_rejections=canonical_metadata_rejections,
            selected_families=selected_families,
            unselected_families=unselected_families,
            unresolved_families=unresolved_families,
            temporal_eligibility_binding=temporal_eligibility_binding,
        )
        return FinancialAcquisitionManifest(
            **self.model_dump(
                mode="python",
                exclude={
                    "contract_version",
                    "physical_attempt_events",
                    "disposition",
                    "final_rendered_artifact",
                    "manifest_identity",
                    "publication_target_construction",
                    "requested_analysis_scope",
                    "availability_scope",
                    "availability_basis",
                    "eligibility_policy_version",
                    "financial_eligibility_binding",
                    "availability_reason",
                    "indicator_field_semantics_version",
                    "indicator_metadata_compatibility_version",
                    "metadata_compatibility_assessments",
                    "metadata_incompatible_rejections",
                    "selected_families",
                    "unselected_families",
                    "unresolved_families",
                    "temporal_eligibility_binding",
                },
            ),
            contract_version=forward_contract_version,
            publication_target_construction=publication_target_construction,
            physical_attempt_events=canonical_attempts,
            disposition=disposition,
            final_rendered_artifact=final_rendered_artifact,
            requested_analysis_scope=requested_analysis_scope,
            availability_scope=availability_scope,
            availability_basis=availability_basis,
            eligibility_policy_version=eligibility_policy_version,
            financial_eligibility_binding=financial_eligibility_binding,
            availability_reason=availability_reason,
            indicator_field_semantics_version=indicator_field_semantics_version,
            indicator_metadata_compatibility_version=(
                indicator_metadata_compatibility_version
            ),
            metadata_compatibility_assessments=(
                canonical_metadata_assessments
            ),
            metadata_incompatible_rejections=canonical_metadata_rejections,
            selected_families=tuple(selected_families),
            unselected_families=tuple(unselected_families),
            unresolved_families=tuple(unresolved_families),
            temporal_eligibility_binding=temporal_eligibility_binding,
            manifest_identity=(
                "financial-manifest:"
                + (
                    "v4:"
                    if temporal_eligibility_binding is not None
                    else ("v3:" if indicator_v3 else "v2:")
                )
                + _canonical_digest(payload)
            ),
        )

    def bind_temporal_eligibility(
        self,
        temporal_eligibility_binding: FinancialTemporalEligibilityCheckpointProjection,
    ) -> FinancialAcquisitionManifest:
        """Promote an already-finalized v2/v3 manifest to the temporal v4 shape."""

        if self.contract_version == FINANCIAL_MANIFEST_V4:
            if self.temporal_eligibility_binding != temporal_eligibility_binding:
                raise ValueError("financial manifest temporal binding mismatch")
            return self
        if self.contract_version not in {FINANCIAL_MANIFEST_V2, FINANCIAL_MANIFEST_V3}:
            raise ValueError("only a finalized financial manifest may bind temporal state")
        payload = _manifest_identity_payload(
            contract_version=FINANCIAL_MANIFEST_V4,
            capability_routing_plan_signature=self.capability_routing_plan_signature,
            instrument_identity=self.instrument_identity,
            requested_capability=self.requested_capability,
            requested_statement_type=self.requested_statement_type,
            requested_ratio_family=self.requested_ratio_family,
            artifacts=self.artifacts,
            acquisition_outcomes=self.acquisition_outcomes,
            provider_candidates=self.provider_candidates,
            selections=self.selections,
            rejected_periods=self.rejected_periods,
            overlaps=self.overlaps,
            conflicts=self.conflicts,
            revision_selections=self.revision_selections,
            aggregate_completeness=self.aggregate_completeness,
            publication_target_construction=self.publication_target_construction,
            physical_attempt_events=self.physical_attempt_events,
            disposition=self.disposition,
            final_rendered_artifact=self.final_rendered_artifact,
            requested_analysis_scope=self.requested_analysis_scope,
            availability_scope=self.availability_scope,
            availability_basis=self.availability_basis,
            eligibility_policy_version=self.eligibility_policy_version,
            financial_eligibility_binding=self.financial_eligibility_binding,
            availability_reason=self.availability_reason,
            indicator_field_semantics_version=self.indicator_field_semantics_version,
            indicator_metadata_compatibility_version=(
                self.indicator_metadata_compatibility_version
            ),
            metadata_compatibility_assessments=(
                self.metadata_compatibility_assessments
            ),
            metadata_incompatible_rejections=self.metadata_incompatible_rejections,
            selected_families=self.selected_families,
            unselected_families=self.unselected_families,
            unresolved_families=self.unresolved_families,
            temporal_eligibility_binding=temporal_eligibility_binding,
        )
        return FinancialAcquisitionManifest.model_validate(
            {
                **self.model_dump(
                    mode="python",
                    exclude={
                        "contract_version",
                        "manifest_identity",
                        "temporal_eligibility_binding",
                    },
                ),
                "contract_version": FINANCIAL_MANIFEST_V4,
                "temporal_eligibility_binding": temporal_eligibility_binding,
                "manifest_identity": (
                    "financial-manifest:v4:" + _canonical_digest(payload)
                ),
            }
        )

    @model_validator(mode="after")
    def _validate_manifest(self) -> FinancialAcquisitionManifest:
        _assert_safe_contract_payload(
            self.model_dump(mode="json", exclude={"manifest_identity"})
        )
        if self.requested_capability is FinancialCapability.STATEMENT:
            if self.requested_statement_type not in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            } or self.requested_ratio_family is not None:
                raise ValueError("statement manifest request identity is invalid")
            if (
                not isinstance(
                    self.aggregate_completeness,
                    FinancialHistoryCompletenessAssessment,
                )
                or self.aggregate_completeness.statement_type
                is not self.requested_statement_type
            ):
                raise ValueError("statement manifest completeness context is invalid")
            if self.contract_version == FINANCIAL_MANIFEST_V3:
                raise ValueError("statement manifest cannot use indicator v3")
        elif self.requested_statement_type is not None or self.requested_ratio_family is None:
            raise ValueError("ratio manifest request identity is invalid")
        elif (
            not isinstance(
                self.aggregate_completeness,
                FinancialRatioHistoryCompletenessAssessment,
            )
            or self.aggregate_completeness.ratio_family
            is not self.requested_ratio_family
        ):
            raise ValueError("ratio manifest completeness context is invalid")
        if _authoritative_instrument_payload(
            self.aggregate_completeness.instrument_identity
        ) != _authoritative_instrument_payload(self.instrument_identity):
            raise ValueError("manifest completeness Instrument Identity mismatch")
        if self.artifacts != tuple(
            sorted(self.artifacts, key=lambda item: item.artifact.artifact_identity)
        ):
            raise ValueError("manifest artifacts must be canonical")
        if self.acquisition_outcomes != tuple(
            sorted(
                self.acquisition_outcomes,
                key=lambda item: (
                    item.provider_id,
                    item.capability.value,
                    item.outcome,
                    item.artifact_identity or "",
                    item.reason.value if item.reason is not None else "",
                ),
            )
        ):
            raise ValueError("manifest acquisition outcomes must be canonical")
        if self.provider_candidates != tuple(
            sorted(
                self.provider_candidates,
                key=lambda item: item.candidate.candidate_identity,
            )
        ):
            raise ValueError("manifest provider candidates must be canonical")
        if self.selections != tuple(
            sorted(self.selections, key=lambda item: item.selection_identity)
        ):
            raise ValueError("manifest selections must be canonical")
        if self.rejected_periods != tuple(
            sorted(self.rejected_periods, key=lambda item: item.candidate_identity)
        ):
            raise ValueError("manifest rejected periods must be canonical")
        if self.overlaps != tuple(
            sorted(self.overlaps, key=lambda item: item.overlap_identity)
        ):
            raise ValueError("manifest overlaps must be canonical")
        if self.conflicts != tuple(
            sorted(self.conflicts, key=lambda item: item.conflict_identity)
        ):
            raise ValueError("manifest conflicts must be canonical")
        if self.revision_selections != tuple(
            sorted(
                self.revision_selections,
                key=lambda item: item.selection_identity,
            )
        ):
            raise ValueError("manifest revision selections must be canonical")
        if self.contract_version == FINANCIAL_MANIFEST_V1:
            if (
                self.physical_attempt_events
                or self.disposition is not None
                or self.final_rendered_artifact is not None
                or self.publication_target_construction is not None
                or self.requested_analysis_scope is not None
                or self.availability_scope is not None
                or self.availability_basis is not None
                or self.eligibility_policy_version is not None
                or self.financial_eligibility_binding is not None
                or self.availability_reason is not None
                or self.indicator_field_semantics_version is not None
                or self.indicator_metadata_compatibility_version is not None
                or self.metadata_compatibility_assessments
                or self.metadata_incompatible_rejections
                or self.selected_families
                or self.unselected_families
                or self.unresolved_families
                or self.temporal_eligibility_binding is not None
            ):
                raise ValueError("legacy financial manifest carries v2 lineage")
        else:
            if self.disposition is None or self.final_rendered_artifact is None:
                raise ValueError("financial manifest v2 operational lineage is incomplete")
            if self.physical_attempt_events != tuple(
                sorted(
                    self.physical_attempt_events,
                    key=lambda item: item.attempt_event_id,
                )
            ):
                raise ValueError("financial manifest attempts must be canonical")
            availability_values = (
                self.requested_analysis_scope,
                self.availability_scope,
                self.availability_basis,
                self.eligibility_policy_version,
            )
            if self.eligibility_policy_version is None:
                if any(value is not None for value in availability_values) or (
                    self.availability_reason is not None
                ):
                    raise ValueError("pre-v2 manifest carries eligibility availability")
            elif (
                self.eligibility_policy_version
                != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
                or (
                    self.requested_capability
                    is FinancialCapability.RATIO_FAMILY
                    and self.requested_analysis_scope is None
                )
            ):
                raise ValueError("v2 manifest availability lineage is incomplete")
            elif (self.availability_scope is None) != (
                self.availability_basis is None
            ):
                raise ValueError("manifest availability scope and basis are incomplete")
            if self.financial_eligibility_binding is not None and (
                self.eligibility_policy_version
                != self.financial_eligibility_binding.eligibility_policy_version
                or (
                    self.contract_version == FINANCIAL_MANIFEST_V2
                    and self.financial_eligibility_binding.financial_manifest_version
                    != self.contract_version
                )
                or (
                    self.contract_version
                    in {FINANCIAL_MANIFEST_V3, FINANCIAL_MANIFEST_V4}
                    and self.financial_eligibility_binding.financial_manifest_version
                    != FINANCIAL_MANIFEST_V2
                )
            ):
                raise ValueError(
                    "financial manifest eligibility binding is contradictory"
                )
            attempt_ids = tuple(
                item.attempt_event_id for item in self.physical_attempt_events
            )
            if len(attempt_ids) != len(set(attempt_ids)):
                raise ValueError("financial manifest attempt identities must be unique")
            metadata_values = (
                self.indicator_field_semantics_version,
                self.indicator_metadata_compatibility_version,
            )
            if self.contract_version == FINANCIAL_MANIFEST_V2:
                if (
                    any(value is not None for value in metadata_values)
                    or self.metadata_compatibility_assessments
                    or self.metadata_incompatible_rejections
                    or self.selected_families
                    or self.unselected_families
                    or self.unresolved_families
                ):
                    raise ValueError("financial manifest v2 carries indicator compatibility")
            elif self.contract_version == FINANCIAL_MANIFEST_V3 and (
                any(value is None for value in metadata_values)
            ):
                raise ValueError("indicator manifest v3 metadata binding is incomplete")
            if self.contract_version == FINANCIAL_MANIFEST_V4:
                if self.temporal_eligibility_binding is None:
                    raise ValueError("financial manifest v4 temporal binding is incomplete")
            elif self.temporal_eligibility_binding is not None:
                raise ValueError("historical financial manifest carries temporal binding")
        artifact_ids = tuple(item.artifact.artifact_identity for item in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("manifest artifact identities must be unique")
        artifact_id_set = set(artifact_ids)
        candidate_by_id = {
            item.candidate.candidate_identity: item for item in self.provider_candidates
        }
        if len(candidate_by_id) != len(self.provider_candidates):
            raise ValueError("manifest candidate identities must be unique")
        if self.contract_version in {FINANCIAL_MANIFEST_V3, FINANCIAL_MANIFEST_V4} and (
            self.requested_capability is FinancialCapability.RATIO_FAMILY
        ):
            assert self.requested_ratio_family is not None
            family_order = tuple(FinancialRatioFamily)
            family_dispositions = (
                *self.selected_families,
                *self.unselected_families,
                *self.unresolved_families,
            )
            if (
                family_dispositions != (self.requested_ratio_family,)
                or any(
                    families
                    != tuple(family for family in family_order if family in families)
                    for families in (
                        self.selected_families,
                        self.unselected_families,
                        self.unresolved_families,
                    )
                )
                or bool(self.selected_families)
                != bool(self.aggregate_completeness.complete)
                or bool(self.unresolved_families)
                == bool(self.aggregate_completeness.complete)
                or self.unselected_families
            ):
                raise ValueError("indicator manifest family disposition is inconsistent")
            assessment_bindings = {
                item.candidate_identity: item
                for item in self.metadata_compatibility_assessments
            }
            if (
                len(assessment_bindings)
                != len(self.metadata_compatibility_assessments)
                or set(assessment_bindings) != set(candidate_by_id)
            ):
                raise ValueError("indicator manifest assessment candidates mismatch")
            rejection_bindings = {
                item.candidate_identity: item
                for item in self.metadata_incompatible_rejections
            }
            if len(rejection_bindings) != len(self.metadata_incompatible_rejections):
                raise ValueError("indicator manifest metadata rejections are duplicated")
            selected_candidate_ids = {
                selection.candidate_identity for selection in self.selections
            }
            for candidate_id, wrapper in candidate_by_id.items():
                binding = assessment_bindings[candidate_id]
                reasons = tuple(
                    item for item in binding.dimensions if item.state == "unproven"
                )
                expected_disposition = (
                    "selected"
                    if candidate_id in selected_candidate_ids
                    else "unselected"
                    if binding.compatibility_state == "incompatible"
                    else "unresolved"
                    if self.unresolved_families
                    else "unselected"
                )
                if (
                    wrapper.contract_version != FINANCIAL_PROVIDER_CANDIDATE_V2
                    or wrapper.metadata_compatibility_assessment_identity
                    != binding.assessment_identity
                    or wrapper.metadata_compatibility_state
                    != binding.compatibility_state
                    or wrapper.metadata_selection_disposition
                    != expected_disposition
                    or wrapper.metadata_incompatibility_reasons != reasons
                    or wrapper.candidate.artifact.artifact_identity
                    != binding.artifact_identity
                    or wrapper.candidate.period_identity.period_identity
                    != binding.period_identity
                ):
                    raise ValueError("indicator manifest candidate assessment mismatch")
                rejection = rejection_bindings.get(candidate_id)
                if binding.compatibility_state == "incompatible":
                    if (
                        rejection is None
                        or rejection.assessment_identity != binding.assessment_identity
                        or rejection.typed_reasons != reasons
                    ):
                        raise ValueError("indicator manifest typed rejection mismatch")
                elif rejection is not None:
                    raise ValueError("compatible indicator carries metadata rejection")
            for selection in self.selections:
                binding = assessment_bindings[selection.candidate_identity]
                if (
                    selection.contract_version != FINANCIAL_PERIOD_SELECTION_V3
                    or selection.metadata_compatibility_assessment_identity
                    != binding.assessment_identity
                    or binding.compatibility_state != "compatible"
                ):
                    raise ValueError("indicator manifest selection compatibility mismatch")
        if any(
            item.candidate.artifact.artifact_identity not in artifact_id_set
            for item in self.provider_candidates
        ):
            raise ValueError("manifest candidate artifact was not retained")
        if any(
            outcome.outcome == "available"
            and outcome.artifact_identity not in artifact_id_set
            for outcome in self.acquisition_outcomes
        ):
            raise ValueError("available manifest outcome artifact was not retained")
        for selection in self.selections:
            provider_candidate = candidate_by_id.get(selection.candidate_identity)
            if provider_candidate is None:
                raise ValueError("manifest selection candidate is absent")
            candidate = provider_candidate.candidate
            assessment = provider_candidate.assessment
            if (
                selection.artifact_identity != assessment.artifact_identity
                or selection.period_identity != assessment.period_identity
                or selection.provider_id != candidate.artifact.dataset.provider_id
                or selection.disposition != assessment.disposition.value
                or selection.strict_pit_eligible != assessment.strict_pit_eligible
            ):
                raise ValueError("manifest selection artifact binding mismatch")
            aggregate = self.aggregate_completeness
            identity = candidate.period_identity
            if (
                identity.company_type is not aggregate.company_type
                or identity.consolidation_scope is not aggregate.consolidation_scope
                or (
                    identity.capability is not FinancialCapability.RATIO_FAMILY
                    and identity.currency != aggregate.currency
                )
                or identity.capability is not self.requested_capability
                or identity.statement_type is not self.requested_statement_type
                or identity.ratio_family is not self.requested_ratio_family
                or _authoritative_instrument_payload(identity.instrument_identity)
                != _authoritative_instrument_payload(aggregate.instrument_identity)
            ):
                raise ValueError("manifest selection completeness context mismatch")
        rejection_by_id = {
            item.candidate_identity: item for item in self.rejected_periods
        }
        if len(rejection_by_id) != len(self.rejected_periods):
            raise ValueError("manifest rejected-period identities must be unique")
        for candidate_id, provider_candidate in candidate_by_id.items():
            if provider_candidate.assessment.disposition in {
                FinancialPeriodDisposition.REJECTED,
                FinancialPeriodDisposition.CONFLICTED,
            } and candidate_id not in rejection_by_id:
                raise ValueError("manifest rejected candidate lacks typed rejection")
        if any(
            rejection.candidate_identity not in candidate_by_id
            or rejection.artifact_identity not in artifact_id_set
            for rejection in self.rejected_periods
        ):
            raise ValueError("manifest rejected-period binding is absent")
        if any(
            rejection.period_identity
            != candidate_by_id[rejection.candidate_identity].assessment.period_identity
            or rejection.reasons
            != candidate_by_id[rejection.candidate_identity].assessment.rejection_reasons
            for rejection in self.rejected_periods
        ):
            raise ValueError("manifest rejected-period assessment mismatch")
        conflict_by_id = {
            item.conflict_identity: item for item in self.conflicts
        }
        if len(conflict_by_id) != len(self.conflicts):
            raise ValueError("manifest critical-conflict identities must be unique")
        if any(
            conflict.selected_candidate_identity not in candidate_by_id
            or conflict.conflicting_candidate_identity not in candidate_by_id
            for conflict in self.conflicts
        ):
            raise ValueError("manifest critical conflict candidate is absent")
        overlap_by_id = {item.overlap_identity: item for item in self.overlaps}
        if len(overlap_by_id) != len(self.overlaps):
            raise ValueError("manifest overlap identities must be unique")
        if any(
            overlap.selected_candidate_identity not in candidate_by_id
            or overlap.overlapping_candidate_identity not in candidate_by_id
            or not set(overlap.conflict_identities) <= set(conflict_by_id)
            for overlap in self.overlaps
        ):
            raise ValueError("manifest overlap binding is absent")
        selection_candidate_ids = {
            item.candidate_identity for item in self.selections
        }
        revision_rank_by_provider: dict[str, int] = {}
        revision_provider_by_rank: dict[int, str] = {}
        for revision_selection in self.revision_selections:
            revision_candidate_ids = {
                item.candidate_identity
                for item in revision_selection.candidate_dispositions
            }
            if (
                not revision_candidate_ids <= set(candidate_by_id)
                or revision_selection.selected_candidate_identity
                not in revision_candidate_ids
                or revision_selection.selected_candidate_identity
                not in selection_candidate_ids
            ):
                raise ValueError("manifest revision selection candidate is absent")
            provider_route_ranks: dict[str, int] = {}
            for disposition in revision_selection.candidate_dispositions:
                candidate = candidate_by_id[disposition.candidate_identity].candidate
                provider_id = candidate.artifact.dataset.provider_id
                if (
                    disposition.artifact_identity
                    != candidate.artifact.artifact_identity
                    or disposition.local_provider_revision_identity
                    != candidate.filing_metadata.local_provider_revision_identity
                    or disposition.provider_id != provider_id
                ):
                    raise ValueError(
                        "manifest revision selection provenance mismatch"
                    )
                provider_rank = provider_route_ranks.setdefault(
                    provider_id,
                    disposition.provider_route_rank,
                )
                if provider_rank != disposition.provider_route_rank:
                    raise ValueError(
                        "manifest revision selection route rank mismatch"
                    )
                existing_rank = revision_rank_by_provider.setdefault(
                    provider_id,
                    disposition.provider_route_rank,
                )
                existing_provider = revision_provider_by_rank.setdefault(
                    disposition.provider_route_rank,
                    provider_id,
                )
                if (
                    existing_rank != disposition.provider_route_rank
                    or existing_provider != provider_id
                ):
                    raise ValueError(
                        "manifest revision selection route rank mismatch"
                    )
            conflict_present = any(
                {
                    conflict.selected_candidate_identity,
                    conflict.conflicting_candidate_identity,
                }
                <= revision_candidate_ids
                for conflict in self.conflicts
            )
            if revision_selection.conflicted != conflict_present:
                raise ValueError(
                    "manifest revision selection conflict binding mismatch"
                )
            expected_revision_selection = create_financial_revision_selection(
                provider_candidates=tuple(
                    candidate_by_id[candidate_id]
                    for candidate_id in revision_candidate_ids
                ),
                selected_candidate_identity=(
                    revision_selection.selected_candidate_identity
                ),
                provider_route_ranks=provider_route_ranks,
                conflicted=revision_selection.conflicted,
                requested_analysis_scope=(
                    revision_selection.requested_analysis_scope
                ),
                eligibility_policy_version=(
                    revision_selection.eligibility_policy_version
                ),
            )
            if expected_revision_selection != revision_selection:
                raise ValueError(
                    "manifest revision selection provenance mismatch"
                )
        payload = _manifest_identity_payload(
            contract_version=self.contract_version,
            capability_routing_plan_signature=self.capability_routing_plan_signature,
            instrument_identity=self.instrument_identity,
            requested_capability=self.requested_capability,
            requested_statement_type=self.requested_statement_type,
            requested_ratio_family=self.requested_ratio_family,
            artifacts=self.artifacts,
            acquisition_outcomes=self.acquisition_outcomes,
            provider_candidates=self.provider_candidates,
            selections=self.selections,
            rejected_periods=self.rejected_periods,
            overlaps=self.overlaps,
            conflicts=self.conflicts,
            revision_selections=self.revision_selections,
            aggregate_completeness=self.aggregate_completeness,
            publication_target_construction=self.publication_target_construction,
            physical_attempt_events=self.physical_attempt_events,
            disposition=self.disposition,
            final_rendered_artifact=self.final_rendered_artifact,
            requested_analysis_scope=self.requested_analysis_scope,
            availability_scope=self.availability_scope,
            availability_basis=self.availability_basis,
            eligibility_policy_version=self.eligibility_policy_version,
            financial_eligibility_binding=self.financial_eligibility_binding,
            availability_reason=self.availability_reason,
            indicator_field_semantics_version=(
                self.indicator_field_semantics_version
            ),
            indicator_metadata_compatibility_version=(
                self.indicator_metadata_compatibility_version
            ),
            metadata_compatibility_assessments=(
                self.metadata_compatibility_assessments
            ),
            metadata_incompatible_rejections=(
                self.metadata_incompatible_rejections
            ),
            selected_families=self.selected_families,
            unselected_families=self.unselected_families,
            unresolved_families=self.unresolved_families,
            temporal_eligibility_binding=self.temporal_eligibility_binding,
        )
        version = {
            FINANCIAL_MANIFEST_V1: "v1",
            FINANCIAL_MANIFEST_V2: "v2",
            FINANCIAL_MANIFEST_V3: "v3",
            FINANCIAL_MANIFEST_V4: "v4",
        }[self.contract_version]
        expected = "financial-manifest:" + version + ":" + _canonical_digest(payload)
        if self.manifest_identity != expected:
            raise ValueError("financial acquisition manifest identity mismatch")
        return self


__all__ = [
    "FINANCIAL_PERIOD_SELECTION_V1",
    "FINANCIAL_PERIOD_SELECTION_V2",
    "FINANCIAL_PERIOD_SELECTION_V3",
    "FINANCIAL_PERIOD_SELECTION_BAOSTOCK_V1",
    "FINANCIAL_PERIOD_SELECTION_TUSHARE_STATEMENT_V2",
    "FINANCIAL_STATEMENT_DECLARATION_V1",
    "FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2",
    "FINANCIAL_MANIFEST_V1",
    "FINANCIAL_MANIFEST_V2",
    "FINANCIAL_MANIFEST_V3",
    "FINANCIAL_PROVIDER_CANDIDATE_V2",
    "FINANCIAL_RATIO_DECLARATION_V1",
    "FINANCIAL_RATIO_DECLARATION_V2",
    "FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1",
    "FinancialAcquisitionManifest",
    "FinancialAcquisitionOutcome",
    "FinancialAvailabilityBasis",
    "FinancialAvailabilityScope",
    "FinancialCapability",
    "FinancialCompanyType",
    "FinancialCompanyTypeResolution",
    "FinancialCompanyTypeResolutionMethod",
    "FinancialConsolidationScope",
    "FinancialCriticalValueConflict",
    "FinancialEvidenceDisposition",
    "FinancialFieldValue",
    "FinancialFieldSetDeclaration",
    "FinancialFilingMetadata",
    "FinancialHistoryCompletenessAssessment",
    "FinancialListingProvenance",
    "FinancialPeriodCandidate",
    "FinancialPeriodCompletenessAssessment",
    "FinancialPeriodDisposition",
    "FinancialPeriodIdentity",
    "FinancialPeriodOverlapFinding",
    "FinancialPeriodRejectionReason",
    "FinancialPeriodSelection",
    "FinancialProviderArtifactEntry",
    "FinancialProviderArtifactIdentity",
    "FinancialProviderCandidate",
    "FinancialProviderDatasetIdentity",
    "FinancialRejectedPeriod",
    "FinancialRenderedArtifactIdentity",
    "FinancialRevisionCandidateDisposition",
    "FinancialRevisionSelection",
    "FinancialRatioFieldSetDeclaration",
    "FinancialRatioFamily",
    "FinancialRatioHistoryCompletenessAssessment",
    "FinancialReportingFrequency",
    "FinancialStatementType",
    "FinancialSinceListingException",
    "FinancialUnavailableReason",
    "assess_financial_period_candidate",
    "assess_financial_history_coverage",
    "assess_financial_ratio_history_coverage",
    "assert_safe_financial_contract_payload",
    "assert_safe_financial_projection",
    "compare_financial_period_candidates",
    "create_financial_revision_selection",
    "financial_provider_revision_sequence_is_qualified",
    "financial_revision_selection_key",
    "financial_ratio_field_declaration",
    "financial_statement_field_declaration",
    "mark_financial_period_conflicted",
    "mark_financial_period_company_type_conflicted",
    "order_financial_provider_revisions",
    "reject_financial_period_for_company_type",
    "reject_financial_period_for_incompatible_selection_key",
    "reject_financial_period_for_strict_pit",
    "resolve_financial_company_type",
]
