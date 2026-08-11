"""Provider-neutral contracts for qualified financial eligibility acquisition."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from typing_extensions import Self

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.capability_routing import (
    CAPABILITY_ROUTING_PLAN_VERSION,
    CAPABILITY_ROUTING_PLAN_VERSION_1_3,
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeClassifierInput,
    FinancialCompanyTypeResolution,
    FinancialCompanyTypeResolutionMethod,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    assert_safe_financial_projection,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityProjectionBinding,
    build_mainland_financial_eligibility_policy_v2,
)
from tradingagents.dataflows.financial_period_targets import (
    derive_mainland_financial_acquisition_period_supersets,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    EvidenceEffectiveAsOfDerivation,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    MarketSessionEligibilityBinding,
    ProviderPublicationDateDerivation,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEligibilityRejectionReason,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION = (
    "financial-company-type-evidence-binding-v1"
)
FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION = (
    "financial-company-type-evidence-binding-v3"
)
FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION_V2 = (
    "financial-company-type-evidence-binding-v2"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION = (
    "financial-eligibility-acquisition-context-v1"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2 = (
    "financial-eligibility-acquisition-context-v2"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION = (
    "financial-eligibility-acquisition-result-v1"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION_V2 = (
    "financial-eligibility-acquisition-result-v2"
)
FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION = (
    "financial-eligibility-request-binding-v1"
)
FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION_V2 = (
    "financial-eligibility-request-binding-v2"
)

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    revalidate_instances="always",
)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_ARTIFACT_ID_PATTERN = (
    r"^(?:financial-)?provider-artifact:v1:[0-9a-f]{64}$"
)
_LISTING_ARTIFACT_ID_PATTERN = r"^provider-artifact:v1:[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^run:[0-9a-f]{64}$"
_ASSET_SIGNATURE_PATTERN = r"^asset-config:v1:[0-9a-f]{64}$"
_PLAN_SIGNATURE_PATTERN = r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
_CONTEXT_IDENTITY_PREFIX = "financial-eligibility-acquisition-context:v1:"
_CONTEXT_IDENTITY_PREFIX_V2 = "financial-eligibility-acquisition-context:v2:"
_EVIDENCE_IDENTITY_PREFIX = "financial-company-type-evidence:v1:"
_URL_QUERY = re.compile(r"https?://\S*\?", re.IGNORECASE)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("financial eligibility observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _is_machine_local_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _assert_safe_projection(value: object) -> None:
    projected = _canonical_value(value)
    assert_safe_financial_projection(projected)
    if isinstance(projected, Mapping):
        for key, item in projected.items():
            if str(key).casefold() == "raw_text":
                raise ValueError("financial eligibility projection contains raw text")
            _assert_safe_projection(item)
        return
    if isinstance(projected, Sequence) and not isinstance(
        projected,
        (str, bytes, bytearray),
    ):
        for item in projected:
            _assert_safe_projection(item)
        return
    if isinstance(projected, str) and (
        _URL_QUERY.search(projected) is not None
        or _is_machine_local_absolute_path(projected)
    ):
        raise ValueError("financial eligibility projection contains an unsafe locator")


def assert_safe_financial_eligibility_projection(value: object) -> None:
    """Validate checkpoint-safe financial eligibility or request material."""

    _assert_safe_projection(value)


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_datetime(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _canonical_digest(value: object) -> str:
    _assert_safe_projection(value)
    encoded = json.dumps(
        _canonical_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def financial_eligibility_acquisition_context_identity(
    material: Mapping[str, object],
) -> str:
    """Return the deterministic identity of complete safe context material."""

    prefix = (
        _CONTEXT_IDENTITY_PREFIX_V2
        if material.get("contract_version")
        == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
        else _CONTEXT_IDENTITY_PREFIX
    )
    return f"{prefix}{_canonical_digest(material)}"


def _evidence_identity(material: Mapping[str, object]) -> str:
    return f"{_EVIDENCE_IDENTITY_PREFIX}{_canonical_digest(material)}"


def _model_payload(value: BaseModel | Mapping[str, Any]) -> Mapping[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


class FinancialCompanyTypeEvidenceBinding(BaseModel):
    """Safe artifact-bound inputs accepted by the existing company-type resolver."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-company-type-evidence-binding-v1"
    ] = FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    artifact_identity: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    source_ref: str = Field(pattern=_SAFE_ID_PATTERN)
    observed_at: datetime
    retrieved_at: datetime
    provider_declared_type: FinancialCompanyType | None = None
    provider_declaration_qualified: bool = False
    normalized_resolver_inputs: tuple[FinancialCompanyTypeClassifierInput, ...]
    qualified_provenance: Literal[True] = True
    resolution_input_identity: str = Field(
        pattern=r"^financial-company-type-evidence:v1:[0-9a-f]{64}$"
    )

    @field_validator("normalized_resolver_inputs", mode="before")
    @classmethod
    def _canonicalize_resolver_inputs(cls, value: object) -> object:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return value
        parsed = tuple(
            item
            if isinstance(item, FinancialCompanyTypeClassifierInput)
            else FinancialCompanyTypeClassifierInput.model_validate(item)
            for item in value
        )
        return tuple(sorted(parsed, key=lambda item: (item.key, item.value)))

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        return _utc_datetime(value)

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"resolution_input_identity"})

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if self.observed_at > self.retrieved_at:
            raise ValueError("company-type evidence retrieval precedes observation")
        if len(self.normalized_resolver_inputs) != len(
            set(self.normalized_resolver_inputs)
        ):
            raise ValueError("company-type resolver inputs must be unique")
        if self.provider_declaration_qualified and self.provider_declared_type in {
            None,
            FinancialCompanyType.UNKNOWN,
        }:
            raise ValueError("qualified evidence requires a supported declaration")
        if self.resolution_input_identity != _evidence_identity(
            self.identity_payload()
        ):
            raise ValueError("company-type evidence identity mismatch")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        artifact_identity: str,
        source_ref: str,
        observed_at: datetime,
        retrieved_at: datetime,
        provider_declared_type: FinancialCompanyType | None,
        provider_declaration_qualified: bool,
        normalized_resolver_inputs: Sequence[
            FinancialCompanyTypeClassifierInput | Mapping[str, object]
        ],
    ) -> Self:
        parsed_inputs = tuple(
            sorted(
                (
                    item
                    if isinstance(item, FinancialCompanyTypeClassifierInput)
                    else FinancialCompanyTypeClassifierInput.model_validate(item)
                    for item in normalized_resolver_inputs
                ),
                key=lambda item: (item.key, item.value),
            )
        )
        payload: dict[str, object] = {
            "contract_version": FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION,
            "provider_id": provider_id,
            "artifact_identity": artifact_identity,
            "source_ref": source_ref,
            "observed_at": _utc_datetime(observed_at),
            "retrieved_at": _utc_datetime(retrieved_at),
            "provider_declared_type": provider_declared_type,
            "provider_declaration_qualified": provider_declaration_qualified,
            "normalized_resolver_inputs": parsed_inputs,
            "qualified_provenance": True,
        }
        return cls(
            **payload,
            resolution_input_identity=_evidence_identity(payload),
        )


class FinancialCompanyTypeProviderTemporalProofBinding(BaseModel):
    """Exact provider-origin proof slice bound beside temporal company evidence."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-company-type-provider-temporal-proof-binding-v1"
    ] = "financial-company-type-provider-temporal-proof-binding-v1"
    provider_id: Literal["tushare", "akshare_sina"]
    provider_effective_source_field: Literal[
        "end_date",
        "报告日",
        "报告日期",
    ]
    evidence_class: TemporalEvidenceClass
    artifact: TemporalEvidenceArtifactBinding
    evidence_effective_as_of: date
    evidence_effective_as_of_derivation: EvidenceEffectiveAsOfDerivation
    ann_date: date
    ann_date_derivation: ProviderPublicationDateDerivation
    f_ann_date: date | None = None
    f_ann_date_derivation: ProviderPublicationDateDerivation | None = None

    @model_validator(mode="after")
    def _validate_provider_proof(self) -> Self:
        expected_class = {
            "tushare": TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
            "akshare_sina": (
                TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE
            ),
        }[self.provider_id]
        qualified_provider_effective_fields = {
            "tushare": frozenset({"end_date"}),
            "akshare_sina": frozenset({"end_date", "报告日", "报告日期"}),
        }[self.provider_id]
        if (
            self.evidence_class is not expected_class
            or self.provider_effective_source_field
            not in qualified_provider_effective_fields
            or self.evidence_effective_as_of_derivation.artifact != self.artifact
            or self.ann_date_derivation.artifact != self.artifact
            or not self.evidence_effective_as_of_derivation.qualified_for(
                self.evidence_class
            )
            or not self.ann_date_derivation.qualified_for(
                self.evidence_class,
                date_kind=PublicationDateKind.ANN_DATE,
            )
            or (self.f_ann_date is None) != (self.f_ann_date_derivation is None)
        ):
            raise ValueError("company-type provider temporal proof is invalid")
        if self.f_ann_date_derivation is not None and (
            self.f_ann_date_derivation.artifact != self.artifact
            or not self.f_ann_date_derivation.qualified_for(
                self.evidence_class,
                date_kind=PublicationDateKind.F_ANN_DATE,
            )
        ):
            raise ValueError("company-type first-publication proof is invalid")
        return self

    @classmethod
    def from_temporal_evidence(
        cls,
        *,
        provider_id: str,
        evidence: FinancialMetadataTemporalEvidence,
        provider_effective_source_field: str | None = None,
    ) -> Self:
        effective_derivation = evidence.evidence_effective_as_of_derivation
        ann_derivation = evidence.ann_date_derivation
        if (
            evidence.evidence_effective_as_of is None
            or effective_derivation is None
            or evidence.ann_date is None
            or ann_derivation is None
        ):
            raise ValueError("company-type provider temporal proof is unavailable")
        exact_effective_source = provider_effective_source_field
        if exact_effective_source is None:
            if provider_id != "tushare" or effective_derivation.source_field != "end_date":
                raise ValueError(
                    "company-type exact provider effective field is unavailable"
                )
            exact_effective_source = "end_date"
        return cls(
            provider_id=provider_id,
            provider_effective_source_field=exact_effective_source,
            evidence_class=evidence.evidence_class,
            artifact=evidence.artifact,
            evidence_effective_as_of=evidence.evidence_effective_as_of,
            evidence_effective_as_of_derivation=effective_derivation,
            ann_date=evidence.ann_date,
            ann_date_derivation=ann_derivation,
            f_ann_date=evidence.f_ann_date,
            f_ann_date_derivation=evidence.f_ann_date_derivation,
        )


class FinancialCompanyTypeAcquisitionEvidenceBinding(
    FinancialCompanyTypeEvidenceBinding
):
    """Additive source-bound evidence required by fresh metadata acquisition."""

    contract_version: Literal[
        "financial-company-type-evidence-binding-v2",
        "financial-company-type-evidence-binding-v3",
    ] = FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION_V2
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    temporal_evidence: FinancialMetadataTemporalEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    provider_temporal_proof: (
        FinancialCompanyTypeProviderTemporalProofBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_acquisition_binding(self) -> Self:
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        temporal = (
            self.contract_version
            == FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
        )
        historical_shape = self.as_of_date is not None and self.requested_session is None
        temporal_shape = self.as_of_date is None and self.requested_session is not None
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or (temporal and not temporal_shape)
            or (not temporal and not historical_shape)
            or temporal != (self.temporal_evidence is not None)
            or temporal != (self.provider_temporal_proof is not None)
        ):
            raise ValueError("company-type evidence acquisition binding is invalid")
        if temporal:
            proof = self.temporal_evidence
            provider_proof = self.provider_temporal_proof
            assert proof is not None
            assert provider_proof is not None
            expected_class = {
                "tushare": TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
                "akshare_sina": (
                    TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE
                ),
                "yfinance": TemporalEvidenceClass.YFINANCE_CURRENT_PROFILE_COMPANY_TYPE,
            }.get(self.provider_id)
            if (
                proof.evidence_class is not expected_class
                or proof.artifact.artifact_identity != self.artifact_identity
                or proof.observed_at != self.observed_at
                or proof.retrieved_at != self.retrieved_at
                or provider_proof.provider_id != self.provider_id
                or provider_proof.evidence_class is not proof.evidence_class
                or provider_proof.artifact != proof.artifact
                or provider_proof.evidence_effective_as_of
                != proof.evidence_effective_as_of
                or provider_proof.evidence_effective_as_of_derivation
                != proof.evidence_effective_as_of_derivation
                or provider_proof.ann_date != proof.ann_date
                or provider_proof.ann_date_derivation != proof.ann_date_derivation
                or provider_proof.f_ann_date != proof.f_ann_date
                or provider_proof.f_ann_date_derivation
                != proof.f_ann_date_derivation
            ):
                raise ValueError("company-type temporal proof binding is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        as_of_date: date | None = None,
        requested_session: date | None = None,
        temporal_evidence: FinancialMetadataTemporalEvidence | None = None,
        provider_temporal_proof: (
            FinancialCompanyTypeProviderTemporalProofBinding | None
        ) = None,
        provider_id: str,
        artifact_identity: str,
        source_ref: str,
        observed_at: datetime,
        retrieved_at: datetime,
        provider_declared_type: FinancialCompanyType | None,
        provider_declaration_qualified: bool,
        normalized_resolver_inputs: Sequence[
            FinancialCompanyTypeClassifierInput | Mapping[str, object]
        ],
    ) -> Self:
        parsed_inputs = tuple(
            sorted(
                (
                    item
                    if isinstance(item, FinancialCompanyTypeClassifierInput)
                    else FinancialCompanyTypeClassifierInput.model_validate(item)
                    for item in normalized_resolver_inputs
                ),
                key=lambda item: (item.key, item.value),
            )
        )
        temporal = temporal_evidence is not None or requested_session is not None
        payload: dict[str, object] = {
            "contract_version": (
                FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
                if temporal
                else FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION_V2
            ),
            "instrument_identity": instrument_identity,
            "provider_id": provider_id,
            "artifact_identity": artifact_identity,
            "source_ref": source_ref,
            "observed_at": _utc_datetime(observed_at),
            "retrieved_at": _utc_datetime(retrieved_at),
            "provider_declared_type": provider_declared_type,
            "provider_declaration_qualified": provider_declaration_qualified,
            "normalized_resolver_inputs": parsed_inputs,
            "qualified_provenance": True,
        }
        if temporal:
            if temporal_evidence is None:
                raise ValueError("company-type temporal proof is unavailable")
            exact_provider_proof = (
                provider_temporal_proof
                if provider_temporal_proof is not None
                else FinancialCompanyTypeProviderTemporalProofBinding.from_temporal_evidence(
                    provider_id=provider_id,
                    evidence=temporal_evidence,
                )
            )
            payload["requested_session"] = requested_session
            payload["temporal_evidence"] = temporal_evidence
            payload["provider_temporal_proof"] = exact_provider_proof
        else:
            payload["as_of_date"] = as_of_date
        return cls(
            **payload,
            resolution_input_identity=_evidence_identity(payload),
        )


FinancialCompanyTypeContextEvidence = (
    FinancialCompanyTypeAcquisitionEvidenceBinding
    | FinancialCompanyTypeEvidenceBinding
)


class FinancialEligibilityMetadataBinding(BaseModel):
    """Exact safe binding shared by freshly acquired and retained metadata."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-metadata-binding-v1",
        "financial-eligibility-metadata-binding-v2",
    ] = "financial-eligibility-metadata-binding-v1"
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"] = (
        "foreground_current_analysis"
    )
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str = Field(pattern=_PLAN_SIGNATURE_PATTERN)
    temporal_mode: TemporalEligibilityMode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_evaluated_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    calendar_binding: MarketSessionEligibilityBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    strict_cutoff: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("eligibility_evaluated_at", "strict_cutoff")
    @classmethod
    def _normalize_temporal_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc_datetime(value)

    @model_validator(mode="after")
    def _validate_metadata_binding(self) -> Self:
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        temporal = self.contract_version == "financial-eligibility-metadata-binding-v2"
        historical_shape = self.as_of_date is not None and self.requested_session is None
        temporal_shape = self.as_of_date is None and self.requested_session is not None
        temporal_fields = (
            self.temporal_mode,
            self.eligibility_evaluated_at,
            self.calendar_binding,
        )
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or (temporal and (not temporal_shape or not all(temporal_fields)))
            or (
                not temporal
                and (
                    not historical_shape
                    or any((*temporal_fields, self.strict_cutoff))
                )
            )
        ):
            raise ValueError("eligibility metadata requires a CNY XSHG/XSHE Equity")
        if temporal:
            assert self.calendar_binding is not None
            assert self.requested_session is not None
            assert self.eligibility_evaluated_at is not None
            assert self.temporal_mode is not None
            strict = self.temporal_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
            expected_scope = (
                FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                if strict
                else FinancialAvailabilityScope.CURRENT_ONLY
            )
            if (
                not self.calendar_binding.has_valid_identity()
                or self.calendar_binding.requested_session != self.requested_session
                or self.calendar_binding.eligibility_evaluated_at
                != self.eligibility_evaluated_at
                or self.availability_scope is not expected_scope
                or strict != (self.strict_cutoff is not None)
            ):
                raise ValueError("eligibility metadata temporal binding is invalid")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialCompanyTypeEvidenceProjection(BaseModel):
    """Exact retained-safe company-type evidence and acquisition binding."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-company-type-evidence-projection-v1",
        "financial-company-type-evidence-projection-v2",
    ] = "financial-company-type-evidence-projection-v1"
    metadata_binding: FinancialEligibilityMetadataBinding
    evidence: FinancialCompanyTypeAcquisitionEvidenceBinding
    temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        temporal = self.contract_version == "financial-company-type-evidence-projection-v2"
        if (
            self.evidence.instrument_identity
            != self.metadata_binding.instrument_identity
            or (
                self.evidence.requested_session != self.metadata_binding.requested_session
                if temporal
                else self.evidence.as_of_date != self.metadata_binding.as_of_date
            )
            or temporal
            != (
                self.evidence.contract_version
                == FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
                and self.metadata_binding.contract_version
                == "financial-eligibility-metadata-binding-v2"
                and self.temporal_decision is not None
            )
            or temporal != (self.temporal_decision is not None)
        ):
            raise ValueError("company-type evidence acquisition binding mismatch")
        if temporal:
            proof = self.evidence.temporal_evidence
            decision = self.temporal_decision
            assert proof is not None and decision is not None
            if (
                decision.immutable_artifact_identity
                != proof.artifact.artifact_identity
                or decision.immutable_revision_identity
                != proof.artifact.revision_identity
                or decision.immutable_row_identity != proof.artifact.row_identity
                or decision.calendar_binding != self.metadata_binding.calendar_binding
                or decision.mode != self.metadata_binding.temporal_mode.value
                or decision.strict_cutoff != self.metadata_binding.strict_cutoff
            ):
                raise ValueError("company-type temporal decision binding mismatch")
        if self.evidence.provider_id == "yfinance" and (
            self.evidence.provider_declared_type is not None
            or self.evidence.provider_declaration_qualified
        ):
            raise ValueError("Yahoo company-type evidence is classifier-only")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialListingMetadata(BaseModel):
    """Provider-neutral authoritative listing metadata projected by lifecycle."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-listing-metadata-v1",
        "financial-listing-metadata-v2",
    ] = "financial-listing-metadata-v1"
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    listing_date: date
    listing_provenance: FinancialListingProvenance
    temporal_evidence: FinancialMetadataTemporalEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_listing_metadata(self) -> Self:
        provenance = FinancialListingProvenance.model_validate(
            self.listing_provenance.model_dump(mode="json")
        )
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        temporal = self.contract_version == "financial-listing-metadata-v2"
        analysis_session = self.requested_session if temporal else self.as_of_date
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or analysis_session is None
            or self.listing_date > analysis_session
            or not provenance.authoritative
            or provenance.provider_id != "baostock"
            or re.fullmatch(_LISTING_ARTIFACT_ID_PATTERN, provenance.source_ref) is None
            or temporal != (self.temporal_evidence is not None)
            or temporal != (self.as_of_date is None and self.requested_session is not None)
            or (not temporal and self.requested_session is not None)
        ):
            raise ValueError("listing metadata is not BaoStock authoritative")
        if temporal:
            proof = self.temporal_evidence
            assert proof is not None
            if (
                proof.evidence_class
                is not TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING
                or proof.artifact.artifact_identity != provenance.source_ref
                or proof.evidence_effective_as_of != self.listing_date
                or proof.observed_at != provenance.observed_at
            ):
                raise ValueError("listing temporal proof binding is invalid")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialListingMetadataProjection(BaseModel):
    """Exact retained-safe listing metadata and acquisition binding."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-listing-metadata-projection-v1",
        "financial-listing-metadata-projection-v2",
    ] = "financial-listing-metadata-projection-v1"
    metadata_binding: FinancialEligibilityMetadataBinding
    metadata: FinancialListingMetadata
    temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        temporal = self.contract_version == "financial-listing-metadata-projection-v2"
        if (
            self.metadata.instrument_identity
            != self.metadata_binding.instrument_identity
            or (
                self.metadata.requested_session
                != self.metadata_binding.requested_session
                if temporal
                else self.metadata.as_of_date != self.metadata_binding.as_of_date
            )
            or temporal
            != (
                self.metadata.contract_version == "financial-listing-metadata-v2"
                and self.metadata_binding.contract_version
                == "financial-eligibility-metadata-binding-v2"
                and self.temporal_decision is not None
            )
            or temporal != (self.temporal_decision is not None)
        ):
            raise ValueError("listing metadata acquisition binding mismatch")
        if temporal:
            proof = self.metadata.temporal_evidence
            decision = self.temporal_decision
            assert proof is not None and decision is not None
            if (
                decision.immutable_artifact_identity
                != proof.artifact.artifact_identity
                or decision.immutable_revision_identity
                != proof.artifact.revision_identity
                or decision.immutable_row_identity != proof.artifact.row_identity
                or decision.calendar_binding != self.metadata_binding.calendar_binding
                or decision.mode != self.metadata_binding.temporal_mode.value
                or decision.strict_cutoff != self.metadata_binding.strict_cutoff
            ):
                raise ValueError("listing temporal decision binding mismatch")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialEligibilityTemporalContextBinding(BaseModel):
    """Exact temporal inputs and accepted prerequisite decisions for context v2."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-eligibility-temporal-binding-v1"] = (
        "financial-eligibility-temporal-binding-v1"
    )
    capability_routing_plan_version: Literal["1.3"] = "1.3"
    temporal_policy_version: Literal[
        "provider-neutral-temporal-eligibility-v1"
    ] = "provider-neutral-temporal-eligibility-v1"
    calendar_eligibility_binding_version: Literal[
        "market-session-eligibility-binding-v1"
    ] = "market-session-eligibility-binding-v1"
    temporal_decision_version: Literal[
        "financial-metadata-temporal-eligibility-decision-v1"
    ] = "financial-metadata-temporal-eligibility-decision-v1"
    requested_session: date
    temporal_mode: TemporalEligibilityMode
    eligibility_evaluated_at: datetime
    calendar_binding: MarketSessionEligibilityBinding
    latest_completed_session: date | None = None
    company_type_temporal_decision: FinancialMetadataTemporalEligibilityDecision
    listing_temporal_decision: FinancialMetadataTemporalEligibilityDecision
    strict_cutoff: datetime | None = None

    @field_validator("eligibility_evaluated_at", "strict_cutoff")
    @classmethod
    def _validate_temporal_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return None if value is None else _utc_datetime(value)

    @model_validator(mode="after")
    def _validate_temporal_binding(self) -> Self:
        decisions = (
            self.company_type_temporal_decision,
            self.listing_temporal_decision,
        )
        if (
            not self.calendar_binding.has_valid_identity()
            or self.requested_session != self.calendar_binding.requested_session
            or self.eligibility_evaluated_at
            != self.calendar_binding.eligibility_evaluated_at
            or self.latest_completed_session
            != self.calendar_binding.latest_completed_session
            or any(
                decision.result != "accepted"
                or decision.mode != self.temporal_mode.value
                or decision.calendar_binding != self.calendar_binding
                or decision.eligible_session != self.requested_session
                for decision in decisions
            )
        ):
            raise ValueError("financial eligibility temporal binding is inconsistent")
        if (
            self.company_type_temporal_decision.evidence_class
            not in {
                TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
                TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE,
            }
            or self.listing_temporal_decision.evidence_class
            is not TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING
        ):
            raise ValueError("financial eligibility temporal evidence roles mismatch")
        strict = self.temporal_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
        if strict != (self.strict_cutoff is not None) or any(
            decision.strict_cutoff != self.strict_cutoff for decision in decisions
        ):
            raise ValueError("financial eligibility strict cutoff binding is inconsistent")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


def _decision_matches_temporal_evidence(
    decision: FinancialMetadataTemporalEligibilityDecision,
    evidence: FinancialMetadataTemporalEvidence,
) -> bool:
    """Bind a context decision to the exact proof transaction it evaluated."""

    return (
        decision.evidence_class is evidence.evidence_class
        and decision.immutable_artifact_identity
        == evidence.artifact.artifact_identity
        and decision.immutable_revision_identity
        == evidence.artifact.revision_identity
        and decision.immutable_row_identity == evidence.artifact.row_identity
        and decision.evidence_effective_as_of == evidence.evidence_effective_as_of
        and decision.evidence_effective_as_of_derivation
        == evidence.evidence_effective_as_of_derivation
        and decision.ann_date == evidence.ann_date
        and decision.ann_date_derivation == evidence.ann_date_derivation
        and decision.f_ann_date == evidence.f_ann_date
        and decision.f_ann_date_derivation == evidence.f_ann_date_derivation
        and decision.observed_at == evidence.observed_at
        and decision.retrieved_at == evidence.retrieved_at
        and decision.exact_revision_availability_proof
        == evidence.exact_revision_availability_proof
    )


class FinancialEligibilityAcquisitionContext(BaseModel):
    """Immutable provider-neutral authoritative metadata for one qualified run."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-context-v1",
        "financial-eligibility-acquisition-context-v2",
    ] = FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
    context_identity: str = Field(
        pattern=r"^financial-eligibility-acquisition-context:v[12]:[0-9a-f]{64}$"
    )
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    asset_configuration_signature: str = Field(pattern=_ASSET_SIGNATURE_PATTERN)
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"] = (
        "foreground_current_analysis"
    )
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str = Field(pattern=_PLAN_SIGNATURE_PATTERN)
    company_type_resolution: FinancialCompanyTypeResolution
    company_type_evidence: FinancialCompanyTypeContextEvidence
    listing_date: date
    listing_provenance: FinancialListingProvenance
    currency: Literal["CNY"] = "CNY"
    consolidation_scope: FinancialConsolidationScope
    acquisition_annual_period_ends: tuple[date, ...]
    acquisition_reporting_period_ends: tuple[date, ...]
    temporal_eligibility_binding: FinancialEligibilityTemporalContextBinding | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    listing_temporal_evidence: FinancialMetadataTemporalEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator(
        "acquisition_annual_period_ends",
        "acquisition_reporting_period_ends",
        mode="before",
    )
    @classmethod
    def _canonicalize_periods(cls, value: object) -> object:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(sorted(set(value), reverse=True))
        return value

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"context_identity"})

    def safe_projection(self) -> dict[str, Any]:
        projection = self.model_dump(mode="json")
        _assert_safe_projection(projection)
        return projection

    @model_validator(mode="after")
    def _validate_context(self) -> Self:
        temporal_bound = (
            self.contract_version
            == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
        )
        historical_shape = (
            self.as_of_date is not None and self.requested_session is None
        )
        temporal_shape = (
            self.as_of_date is None and self.requested_session is not None
        )
        if (temporal_bound and not temporal_shape) or (
            not temporal_bound and not historical_shape
        ):
            raise ValueError(
                "financial eligibility context requested-session shape mismatch"
            )
        if temporal_bound != (self.temporal_eligibility_binding is not None) or (
            temporal_bound != (self.listing_temporal_evidence is not None)
        ):
            raise ValueError("financial eligibility context temporal version mismatch")
        analysis_session = (
            self.requested_session if temporal_bound else self.as_of_date
        )
        assert analysis_session is not None
        if temporal_bound:
            temporal = self.temporal_eligibility_binding
            listing_proof = self.listing_temporal_evidence
            assert temporal is not None
            assert listing_proof is not None
            expected_scope = (
                FinancialAvailabilityScope.CURRENT_ONLY
                if temporal.temporal_mode
                is TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY
                else FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
            )
            if (
                analysis_session != temporal.requested_session
                or self.availability_scope is not expected_scope
                or not isinstance(
                    self.company_type_evidence,
                    FinancialCompanyTypeAcquisitionEvidenceBinding,
                )
                or self.company_type_evidence.contract_version
                != FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
                or self.company_type_evidence.temporal_evidence is None
                or not _decision_matches_temporal_evidence(
                    temporal.company_type_temporal_decision,
                    self.company_type_evidence.temporal_evidence,
                )
                or not _decision_matches_temporal_evidence(
                    temporal.listing_temporal_decision,
                    listing_proof,
                )
                or listing_proof.artifact.artifact_identity
                != self.listing_provenance.source_ref
                or listing_proof.evidence_effective_as_of != self.listing_date
                or listing_proof.observed_at != self.listing_provenance.observed_at
            ):
                raise ValueError("financial eligibility context temporal binding mismatch")
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or self.currency != identity.currency
        ):
            raise ValueError(
                "financial eligibility context requires a CNY XSHG/XSHE Equity"
            )
        resolution = FinancialCompanyTypeResolution.model_validate(
            self.company_type_resolution.model_dump(mode="json")
        )
        evidence = self.company_type_evidence
        if (
            resolution.company_type is FinancialCompanyType.UNKNOWN
            or resolution.method is FinancialCompanyTypeResolutionMethod.UNRESOLVED
            or resolution.ambiguous
            or resolution.contradictory
        ):
            raise ValueError("financial eligibility context requires resolved company type")
        if (
            evidence.normalized_resolver_inputs != resolution.classifier_inputs
            or evidence.provider_declared_type is not resolution.provider_declared_type
            or evidence.provider_declaration_qualified
            != resolution.provider_declaration_qualified
        ):
            raise ValueError("company-type resolution and evidence binding mismatch")
        if isinstance(
            evidence, FinancialCompanyTypeAcquisitionEvidenceBinding
        ) and (
            evidence.instrument_identity != identity
            or (
                evidence.requested_session != analysis_session
                if temporal_bound
                else evidence.as_of_date != analysis_session
            )
        ):
            raise ValueError("company-type acquisition evidence context mismatch")
        listing = FinancialListingProvenance.model_validate(
            self.listing_provenance.model_dump(mode="json")
        )
        if (
            not listing.authoritative
            or listing.provider_id != "baostock"
            or re.fullmatch(_LISTING_ARTIFACT_ID_PATTERN, listing.source_ref) is None
            or self.listing_date > analysis_session
        ):
            raise ValueError("financial listing provenance is not BaoStock authoritative")
        policy = build_mainland_financial_eligibility_policy_v2()
        expected_binding = MainlandFinancialEligibilityProjectionBinding.from_policy(
            policy
        )
        if self.financial_eligibility_binding != expected_binding:
            raise ValueError("financial eligibility context policy binding mismatch")
        expected_annual, expected_reporting = (
            derive_mainland_financial_acquisition_period_supersets(
                run_as_of_date=analysis_session,
                listing_date=self.listing_date,
                policy=policy,
            )
        )
        if (
            self.acquisition_annual_period_ends != expected_annual
            or self.acquisition_reporting_period_ends != expected_reporting
        ):
            raise ValueError("financial eligibility acquisition periods mismatch")
        if self.context_identity != financial_eligibility_acquisition_context_identity(
            self.identity_payload()
        ):
            raise ValueError("financial eligibility acquisition context identity mismatch")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


def validate_financial_eligibility_acquisition_context_binding(
    context: FinancialEligibilityAcquisitionContext,
    *,
    canonical_run_id: str,
    asset_configuration_signature: str,
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding,
    routing_plan_signature: str,
    instrument_identity: InstrumentIdentityEvidence | None = None,
    as_of_date: date | None = None,
    availability_scope: FinancialAvailabilityScope | None = None,
    acquisition_purpose: Literal["foreground_current_analysis"] | None = None,
    consolidation_scope: FinancialConsolidationScope | None = None,
) -> FinancialEligibilityAcquisitionContext:
    """Validate one immutable context against its authoritative host bindings."""

    if not isinstance(context, FinancialEligibilityAcquisitionContext):
        raise ValueError("financial eligibility context binding is invalid")
    try:
        validated_context = FinancialEligibilityAcquisitionContext.model_validate(
            context.model_dump(mode="json")
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("financial eligibility context binding is invalid") from None
    if (
        validated_context != context
        or context.run_id != canonical_run_id
        or context.asset_configuration_signature != asset_configuration_signature
        or context.financial_eligibility_binding != financial_eligibility_binding
        or context.routing_plan_signature != routing_plan_signature
        or (
            instrument_identity is not None
            and context.instrument_identity != instrument_identity
        )
        or (
            as_of_date is not None
            and (
                context.requested_session
                if context.contract_version
                == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
                else context.as_of_date
            )
            != as_of_date
        )
        or (
            availability_scope is not None
            and context.availability_scope is not availability_scope
        )
        or (
            acquisition_purpose is not None
            and context.acquisition_purpose != acquisition_purpose
        )
        or (
            consolidation_scope is not None
            and context.consolidation_scope is not consolidation_scope
        )
    ):
        raise ValueError("financial eligibility context binding is invalid")
    return context


class FinancialEligibilityRequestBinding(BaseModel):
    """Complete safe context binding carried by a fresh routing request."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-request-binding-v1",
        "financial-eligibility-request-binding-v2",
    ] = FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION
    context_contract_version: Literal[
        "financial-eligibility-acquisition-context-v1",
        "financial-eligibility-acquisition-context-v2",
    ]
    context_identity: str = Field(
        pattern=r"^financial-eligibility-acquisition-context:v[12]:[0-9a-f]{64}$"
    )
    canonical_run_id: str = Field(pattern=_RUN_ID_PATTERN)
    asset_configuration_signature: str = Field(pattern=_ASSET_SIGNATURE_PATTERN)
    instrument_identity: InstrumentIdentityEvidence
    canonical_symbol: str = Field(min_length=1, max_length=64)
    as_of_date: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"]
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str = Field(pattern=_PLAN_SIGNATURE_PATTERN)
    company_type_resolution: FinancialCompanyTypeResolution
    company_type_evidence: FinancialCompanyTypeContextEvidence
    listing_date: date
    listing_provenance: FinancialListingProvenance
    currency: Literal["CNY"]
    consolidation_scope: FinancialConsolidationScope
    acquisition_annual_period_ends: tuple[date, ...]
    acquisition_reporting_period_ends: tuple[date, ...]
    temporal_eligibility_binding: FinancialEligibilityTemporalContextBinding | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        temporal_bound = (
            self.contract_version == FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION_V2
        )
        historical_shape = (
            self.as_of_date is not None and self.requested_session is None
        )
        temporal_shape = (
            self.as_of_date is None and self.requested_session is not None
        )
        if (
            temporal_bound
            != (
                self.context_contract_version
                == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
            )
            or temporal_bound != (self.temporal_eligibility_binding is not None)
            or (temporal_bound and not temporal_shape)
            or (not temporal_bound and not historical_shape)
        ):
            raise ValueError("request binding temporal version is inconsistent")
        if temporal_bound and (
            self.temporal_eligibility_binding is None
            or self.requested_session
            != self.temporal_eligibility_binding.requested_session
        ):
            raise ValueError("request binding temporal decision is inconsistent")
        if self.canonical_symbol != self.instrument_identity.symbol.strip().upper():
            raise ValueError("request binding symbol contradicts Instrument Identity")
        if self.currency != self.instrument_identity.currency:
            raise ValueError("request binding currency contradicts Instrument Identity")
        if self.company_type_resolution.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("request binding requires resolved company type")
        if (
            self.company_type_evidence.normalized_resolver_inputs
            != self.company_type_resolution.classifier_inputs
            or self.company_type_evidence.provider_declared_type
            is not self.company_type_resolution.provider_declared_type
            or self.company_type_evidence.provider_declaration_qualified
            != self.company_type_resolution.provider_declaration_qualified
        ):
            raise ValueError("request binding company-type evidence is inconsistent")
        analysis_session = (
            self.requested_session if temporal_bound else self.as_of_date
        )
        assert analysis_session is not None
        if self.listing_date > analysis_session:
            raise ValueError("request binding listing date is after its as-of date")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_context(
        cls,
        context: FinancialEligibilityAcquisitionContext,
    ) -> Self:
        """Project the complete immutable context without recomputing policy."""

        return cls(
            contract_version=(
                FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION_V2
                if context.contract_version
                == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
                else FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION
            ),
            context_contract_version=context.contract_version,
            context_identity=context.context_identity,
            canonical_run_id=context.run_id,
            asset_configuration_signature=context.asset_configuration_signature,
            instrument_identity=context.instrument_identity,
            canonical_symbol=context.instrument_identity.symbol.strip().upper(),
            as_of_date=context.as_of_date,
            requested_session=context.requested_session,
            availability_scope=context.availability_scope,
            acquisition_purpose=context.acquisition_purpose,
            financial_eligibility_binding=context.financial_eligibility_binding,
            routing_plan_signature=context.routing_plan_signature,
            company_type_resolution=context.company_type_resolution,
            company_type_evidence=context.company_type_evidence,
            listing_date=context.listing_date,
            listing_provenance=context.listing_provenance,
            currency=context.currency,
            consolidation_scope=context.consolidation_scope,
            acquisition_annual_period_ends=context.acquisition_annual_period_ends,
            acquisition_reporting_period_ends=(
                context.acquisition_reporting_period_ends
            ),
            temporal_eligibility_binding=context.temporal_eligibility_binding,
        )


class FinancialEligibilityAcquisitionFailureReason(str, Enum):
    COMPANY_TYPE_EVIDENCE_UNAVAILABLE = "company_type_evidence_unavailable"
    COMPANY_TYPE_UNRESOLVED = "company_type_unresolved"
    COMPANY_TYPE_AMBIGUOUS = "company_type_ambiguous"
    COMPANY_TYPE_CONTRADICTORY = "company_type_contradictory"
    LISTING_PROVENANCE_UNAVAILABLE = "listing_provenance_unavailable"
    TEMPORAL_PROOF_UNAVAILABLE = "temporal_proof_unavailable"
    TEMPORAL_ELIGIBILITY_REJECTED = "temporal_eligibility_rejected"
    CONTEXT_BINDING_MISMATCH = "context_binding_mismatch"
    INTERRUPTED_ACQUISITION = "interrupted_acquisition"


class FinancialEligibilityAcquisitionFailure(BaseModel):
    """Bounded failure with no provider payload, raw error, or partial context."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-result-v1",
        "financial-eligibility-acquisition-result-v2",
    ] = FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION
    reason: FinancialEligibilityAcquisitionFailureReason
    diagnostic_code: FinancialEligibilityAcquisitionFailureReason
    company_type_resolution: FinancialCompanyTypeResolution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    rejected_temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    temporal_rejection_reasons: tuple[TemporalEligibilityRejectionReason, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.reason is not self.diagnostic_code:
            raise ValueError("financial eligibility failure diagnostic mismatch")
        resolution_failure = self.reason in {
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED,
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY,
        }
        if resolution_failure != (self.company_type_resolution is not None):
            raise ValueError(
                "company-type resolution failure requires only its safe resolution"
            )
        resolution = self.company_type_resolution
        if resolution is not None:
            reason_matches_resolution = (
                self.reason
                is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED
                and resolution.method
                is FinancialCompanyTypeResolutionMethod.UNRESOLVED
            ) or (
                self.reason
                is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS
                and resolution.ambiguous
            ) or (
                self.reason
                is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY
                and resolution.contradictory
            )
            if not reason_matches_resolution:
                raise ValueError("company-type failure contradicts its safe resolution")
        temporal_failure = self.reason in {
            FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
            FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
        }
        if temporal_failure and (
            self.contract_version
            != FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION_V2
        ):
            raise ValueError("temporal acquisition failure version is inconsistent")
        if self.reason is FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE:
            if self.rejected_temporal_decision is not None or self.temporal_rejection_reasons:
                raise ValueError("unavailable temporal proof cannot carry a decision")
        elif self.reason is FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED:
            decision = self.rejected_temporal_decision
            if (
                decision is None
                or decision.result != "rejected"
                or not decision.reasons
                or self.temporal_rejection_reasons != decision.reasons
            ):
                raise ValueError("temporal rejection failure is inconsistent")
        elif self.rejected_temporal_decision is not None or self.temporal_rejection_reasons:
            raise ValueError("non-temporal acquisition failure carries temporal material")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialEligibilityAcquisitionResult(BaseModel):
    """Closed terminal result containing either one context or one safe failure."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-result-v1",
        "financial-eligibility-acquisition-result-v2",
    ] = FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION
    outcome: Literal["success", "failure"]
    context: FinancialEligibilityAcquisitionContext | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    failure: FinancialEligibilityAcquisitionFailure | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_terminal_result(self) -> Self:
        successful = self.context is not None and self.failure is None
        failed = self.context is None and self.failure is not None
        if not (successful ^ failed) or (self.outcome == "success") != successful:
            raise ValueError("financial eligibility result requires exactly one terminal value")
        expected_version = (
            self.context.contract_version.replace("context", "result")
            if self.context is not None
            else self.failure.contract_version
            if self.failure is not None
            else None
        )
        if self.contract_version != expected_version:
            raise ValueError("financial eligibility result version mismatch")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self

    @classmethod
    def succeeded(
        cls,
        context: FinancialEligibilityAcquisitionContext,
    ) -> Self:
        version = (
            FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION_V2
            if context.contract_version
            == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
            else FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION
        )
        return cls(contract_version=version, outcome="success", context=context)

    @classmethod
    def failed(
        cls,
        reason: FinancialEligibilityAcquisitionFailureReason,
        *,
        company_type_resolution: FinancialCompanyTypeResolution | None = None,
        rejected_temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = None,
        temporal_proof_unavailable: bool = False,
        temporal_context: bool = False,
    ) -> Self:
        temporal = (
            temporal_context
            or rejected_temporal_decision is not None
            or temporal_proof_unavailable
            or reason
            in {
                FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
                FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
            }
        )
        version = (
            FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION_V2
            if temporal
            else FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION
        )
        return cls(
            contract_version=version,
            outcome="failure",
            failure=FinancialEligibilityAcquisitionFailure(
                contract_version=version,
                reason=reason,
                diagnostic_code=reason,
                company_type_resolution=company_type_resolution,
                rejected_temporal_decision=rejected_temporal_decision,
                temporal_rejection_reasons=(
                    rejected_temporal_decision.reasons
                    if rejected_temporal_decision is not None
                    else ()
                ),
            ),
        )


def _failure(
    reason: FinancialEligibilityAcquisitionFailureReason,
    *,
    company_type_resolution: FinancialCompanyTypeResolution | None = None,
    temporal_context: bool = False,
) -> FinancialEligibilityAcquisitionResult:
    return FinancialEligibilityAcquisitionResult.failed(
        reason,
        company_type_resolution=company_type_resolution,
        temporal_context=temporal_context,
    )


def _canonical_period_tuple(values: Sequence[date]) -> tuple[date, ...]:
    return tuple(sorted(set(values), reverse=True))


def build_financial_eligibility_acquisition_context(
    *,
    canonical_run_id: str,
    run_id: str,
    asset_configuration: RunAssetConfiguration,
    asset_configuration_signature: str,
    instrument_identity: InstrumentIdentityEvidence,
    authoritative_as_of_date: date | None,
    as_of_date: date | None,
    requested_session: date | None = None,
    authoritative_availability_scope: FinancialAvailabilityScope,
    availability_scope: FinancialAvailabilityScope,
    acquisition_purpose: str,
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | Mapping[str, Any] | None
    ),
    routing_plan: MainlandCapabilityRoutingPlan,
    routing_plan_signature: str,
    company_type_resolution: FinancialCompanyTypeResolution,
    company_type_evidence: (
        FinancialCompanyTypeContextEvidence | Mapping[str, Any] | None
    ),
    listing_date: date,
    listing_provenance: FinancialListingProvenance | Mapping[str, Any] | None,
    temporal_mode: TemporalEligibilityMode | None = None,
    eligibility_evaluated_at: datetime | None = None,
    calendar_binding: MarketSessionEligibilityBinding | None = None,
    company_type_temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = None,
    listing_temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = None,
    listing_temporal_evidence: FinancialMetadataTemporalEvidence | None = None,
    strict_cutoff: datetime | None = None,
    currency: str,
    consolidation_scope: FinancialConsolidationScope,
    acquisition_annual_period_ends: Sequence[date],
    acquisition_reporting_period_ends: Sequence[date],
    interrupted: bool = False,
) -> FinancialEligibilityAcquisitionResult:
    """Atomically validate authoritative bindings and return one terminal result."""

    temporal_context = requested_session is not None
    if interrupted:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION,
            temporal_context=temporal_context,
        )
    resolution = company_type_resolution
    if (
        getattr(resolution, "company_type", None) is FinancialCompanyType.UNKNOWN
        or getattr(resolution, "method", None)
        is FinancialCompanyTypeResolutionMethod.UNRESOLVED
    ):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED,
            company_type_resolution=resolution,
            temporal_context=temporal_context,
        )
    if bool(getattr(resolution, "contradictory", False)):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY,
            company_type_resolution=resolution,
            temporal_context=temporal_context,
        )
    if bool(getattr(resolution, "ambiguous", False)):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
            company_type_resolution=resolution,
            temporal_context=temporal_context,
        )
    if company_type_evidence is None:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE,
            temporal_context=temporal_context,
        )
    try:
        evidence_payload = _model_payload(company_type_evidence)
        evidence_type = (
            FinancialCompanyTypeAcquisitionEvidenceBinding
            if evidence_payload.get("contract_version")
            in {
                FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION,
                FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION_V2,
            }
            else FinancialCompanyTypeEvidenceBinding
        )
        evidence = evidence_type.model_validate(evidence_payload)
    except (TypeError, ValidationError, ValueError):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE,
            temporal_context=temporal_context,
        )
    if listing_provenance is None:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE,
            temporal_context=temporal_context,
        )
    try:
        listing = FinancialListingProvenance.model_validate(
            _model_payload(listing_provenance)
        )
        if (
            not listing.authoritative
            or listing.provider_id != "baostock"
            or re.fullmatch(_LISTING_ARTIFACT_ID_PATTERN, listing.source_ref) is None
        ):
            raise ValueError("listing provenance is not authoritative")
    except (TypeError, ValidationError, ValueError):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE,
            temporal_context=temporal_context,
        )
    try:
        temporal = requested_session is not None
        analysis_session = requested_session if temporal else as_of_date
        if analysis_session is None:
            raise ValueError("financial eligibility analysis session is unavailable")
        validated_resolution = FinancialCompanyTypeResolution.model_validate(
            resolution.model_dump(mode="json")
        )
        validated_identity = InstrumentIdentityEvidence.model_validate(
            instrument_identity.model_dump(mode="json")
        )
        validated_binding = MainlandFinancialEligibilityProjectionBinding.model_validate(
            _model_payload(financial_eligibility_binding)  # type: ignore[arg-type]
        )
        validated_scope = FinancialAvailabilityScope(availability_scope)
        validated_authoritative_scope = FinancialAvailabilityScope(
            authoritative_availability_scope
        )
        validated_consolidation = FinancialConsolidationScope(consolidation_scope)
        expected_route = routing_plan.route_for(
            MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
        )
        expected_annual, expected_reporting = (
            derive_mainland_financial_acquisition_period_supersets(
                run_as_of_date=analysis_session,
                listing_date=listing_date,
                policy=routing_plan.financial_eligibility_policy,
            )
        )
        if (
            re.fullmatch(_RUN_ID_PATTERN, canonical_run_id) is None
            or run_id != canonical_run_id
            or re.fullmatch(_RUN_ID_PATTERN, run_id) is None
            or not isinstance(asset_configuration, RunAssetConfiguration)
            or asset_configuration_signature
            != asset_configuration.asset_configuration_signature
            or validated_identity != asset_configuration.instrument_identity
            or not validated_identity.is_authoritative
            or validated_identity.instrument_kind is not InstrumentKind.EQUITY
            or validated_identity.venue not in {"XSHG", "XSHE"}
            or validated_identity.currency != "CNY"
            or currency != validated_identity.currency
            or (
                as_of_date != authoritative_as_of_date
                if not temporal
                else authoritative_as_of_date is not None or as_of_date is not None
            )
            or validated_scope is not validated_authoritative_scope
            or acquisition_purpose != "foreground_current_analysis"
            or routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
            or routing_plan.plan_version
            != (
                CAPABILITY_ROUTING_PLAN_VERSION_1_3
                if temporal
                else CAPABILITY_ROUTING_PLAN_VERSION
            )
            or routing_plan_signature != routing_plan.plan_signature
            or routing_plan.financial_eligibility_binding is None
            or validated_binding != routing_plan.financial_eligibility_binding
            or evidence.provider_id not in expected_route
            or _canonical_period_tuple(acquisition_annual_period_ends)
            != expected_annual
            or _canonical_period_tuple(acquisition_reporting_period_ends)
            != expected_reporting
            or listing_date > analysis_session
            or validated_consolidation is FinancialConsolidationScope.UNKNOWN
        ):
            raise ValueError("financial eligibility acquisition binding mismatch")
        temporal_binding = None
        if temporal:
            if (
                temporal_mode is None
                or eligibility_evaluated_at is None
                or calendar_binding is None
                or company_type_temporal_decision is None
                or listing_temporal_decision is None
                or listing_temporal_evidence is None
            ):
                raise ValueError("financial eligibility temporal inputs are incomplete")
            temporal_binding = FinancialEligibilityTemporalContextBinding(
                requested_session=analysis_session,
                temporal_mode=temporal_mode,
                eligibility_evaluated_at=eligibility_evaluated_at,
                calendar_binding=calendar_binding,
                latest_completed_session=calendar_binding.latest_completed_session,
                company_type_temporal_decision=company_type_temporal_decision,
                listing_temporal_decision=listing_temporal_decision,
                strict_cutoff=strict_cutoff,
            )
        elif any(
            value is not None
            for value in (
                temporal_mode,
                eligibility_evaluated_at,
                calendar_binding,
                company_type_temporal_decision,
                listing_temporal_decision,
                listing_temporal_evidence,
                strict_cutoff,
            )
        ):
            raise ValueError("historical context cannot carry temporal inputs")
        payload: dict[str, object] = {
            "contract_version": (
                FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2
                if temporal
                else FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
            ),
            "run_id": run_id,
            "asset_configuration_signature": asset_configuration_signature,
            "instrument_identity": validated_identity,
            "availability_scope": validated_scope,
            "acquisition_purpose": acquisition_purpose,
            "financial_eligibility_binding": validated_binding,
            "routing_plan_signature": routing_plan_signature,
            "company_type_resolution": validated_resolution,
            "company_type_evidence": evidence,
            "listing_date": listing_date,
            "listing_provenance": listing,
            "currency": currency,
            "consolidation_scope": validated_consolidation,
            "acquisition_annual_period_ends": expected_annual,
            "acquisition_reporting_period_ends": expected_reporting,
        }
        if temporal:
            payload["requested_session"] = analysis_session
            payload["temporal_eligibility_binding"] = temporal_binding
            payload["listing_temporal_evidence"] = listing_temporal_evidence
        else:
            payload["as_of_date"] = as_of_date
        context = FinancialEligibilityAcquisitionContext(
            **payload,
            context_identity=(
                financial_eligibility_acquisition_context_identity(payload)
            ),
        )
    except (AttributeError, KeyError, TypeError, ValidationError, ValueError):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH,
            temporal_context=temporal_context,
        )
    return FinancialEligibilityAcquisitionResult.succeeded(context)


__all__ = [
    "FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION",
    "FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION",
    "FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION",
    "FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION_V2",
    "FinancialCompanyTypeAcquisitionEvidenceBinding",
    "FinancialCompanyTypeContextEvidence",
    "FinancialCompanyTypeEvidenceBinding",
    "FinancialCompanyTypeEvidenceProjection",
    "FinancialEligibilityAcquisitionContext",
    "FinancialEligibilityAcquisitionFailure",
    "FinancialEligibilityAcquisitionFailureReason",
    "FinancialEligibilityMetadataBinding",
    "FinancialEligibilityRequestBinding",
    "FinancialEligibilityTemporalContextBinding",
    "FinancialEligibilityAcquisitionResult",
    "FinancialListingMetadata",
    "FinancialListingMetadataProjection",
    "build_financial_eligibility_acquisition_context",
    "derive_mainland_financial_acquisition_period_supersets",
    "financial_eligibility_acquisition_context_identity",
    "assert_safe_financial_eligibility_projection",
]
