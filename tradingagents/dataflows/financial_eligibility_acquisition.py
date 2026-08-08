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
from zoneinfo import ZoneInfo

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
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION = (
    "financial-company-type-evidence-binding-v1"
)
FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION = (
    "financial-company-type-evidence-binding-v2"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION = (
    "financial-eligibility-acquisition-context-v1"
)
FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION = (
    "financial-eligibility-acquisition-result-v1"
)
FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION = (
    "financial-eligibility-request-binding-v1"
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
_EVIDENCE_IDENTITY_PREFIX = "financial-company-type-evidence:v1:"
_MAINLAND_TIMEZONE = ZoneInfo("Asia/Shanghai")
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

    return f"{_CONTEXT_IDENTITY_PREFIX}{_canonical_digest(material)}"


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


class FinancialCompanyTypeAcquisitionEvidenceBinding(
    FinancialCompanyTypeEvidenceBinding
):
    """Additive source-bound evidence required by fresh metadata acquisition."""

    contract_version: Literal[
        "financial-company-type-evidence-binding-v2"
    ] = FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date

    @model_validator(mode="after")
    def _validate_acquisition_binding(self) -> Self:
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or self.retrieved_at.astimezone(_MAINLAND_TIMEZONE).date()
            != self.as_of_date
        ):
            raise ValueError("company-type evidence acquisition binding is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        as_of_date: date,
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
            "contract_version": (
                FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
            ),
            "instrument_identity": instrument_identity,
            "as_of_date": as_of_date,
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


FinancialCompanyTypeContextEvidence = (
    FinancialCompanyTypeAcquisitionEvidenceBinding
    | FinancialCompanyTypeEvidenceBinding
)


class FinancialEligibilityMetadataBinding(BaseModel):
    """Exact safe binding shared by freshly acquired and retained metadata."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-eligibility-metadata-binding-v1"] = (
        "financial-eligibility-metadata-binding-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"] = (
        "foreground_current_analysis"
    )
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str = Field(pattern=_PLAN_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_metadata_binding(self) -> Self:
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
        ):
            raise ValueError("eligibility metadata requires a CNY XSHG/XSHE Equity")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialCompanyTypeEvidenceProjection(BaseModel):
    """Exact retained-safe company-type evidence and acquisition binding."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-company-type-evidence-projection-v1"] = (
        "financial-company-type-evidence-projection-v1"
    )
    metadata_binding: FinancialEligibilityMetadataBinding
    evidence: FinancialCompanyTypeAcquisitionEvidenceBinding

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        if (
            self.evidence.instrument_identity
            != self.metadata_binding.instrument_identity
            or self.evidence.as_of_date != self.metadata_binding.as_of_date
        ):
            raise ValueError("company-type evidence acquisition binding mismatch")
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

    contract_version: Literal["financial-listing-metadata-v1"] = (
        "financial-listing-metadata-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
    listing_date: date
    listing_provenance: FinancialListingProvenance

    @model_validator(mode="after")
    def _validate_listing_metadata(self) -> Self:
        provenance = FinancialListingProvenance.model_validate(
            self.listing_provenance.model_dump(mode="json")
        )
        identity = InstrumentIdentityEvidence.model_validate(
            self.instrument_identity.model_dump(mode="json")
        )
        if (
            not identity.is_authoritative
            or identity.instrument_kind is not InstrumentKind.EQUITY
            or identity.venue not in {"XSHG", "XSHE"}
            or identity.currency != "CNY"
            or self.listing_date > self.as_of_date
            or not provenance.authoritative
            or provenance.provider_id != "baostock"
            or re.fullmatch(_LISTING_ARTIFACT_ID_PATTERN, provenance.source_ref) is None
        ):
            raise ValueError("listing metadata is not BaoStock authoritative")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialListingMetadataProjection(BaseModel):
    """Exact retained-safe listing metadata and acquisition binding."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-listing-metadata-projection-v1"] = (
        "financial-listing-metadata-projection-v1"
    )
    metadata_binding: FinancialEligibilityMetadataBinding
    metadata: FinancialListingMetadata

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        if (
            self.metadata.instrument_identity
            != self.metadata_binding.instrument_identity
            or self.metadata.as_of_date != self.metadata_binding.as_of_date
        ):
            raise ValueError("listing metadata acquisition binding mismatch")
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialEligibilityAcquisitionContext(BaseModel):
    """Immutable provider-neutral authoritative metadata for one qualified run."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-context-v1"
    ] = FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
    context_identity: str = Field(
        pattern=r"^financial-eligibility-acquisition-context:v1:[0-9a-f]{64}$"
    )
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    asset_configuration_signature: str = Field(pattern=_ASSET_SIGNATURE_PATTERN)
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
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
            or evidence.as_of_date != self.as_of_date
        ):
            raise ValueError("company-type acquisition evidence context mismatch")
        listing = FinancialListingProvenance.model_validate(
            self.listing_provenance.model_dump(mode="json")
        )
        if (
            not listing.authoritative
            or listing.provider_id != "baostock"
            or re.fullmatch(_LISTING_ARTIFACT_ID_PATTERN, listing.source_ref) is None
            or self.listing_date > self.as_of_date
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
                run_as_of_date=self.as_of_date,
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


class FinancialEligibilityRequestBinding(BaseModel):
    """Complete safe context binding carried by a fresh routing request."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-eligibility-request-binding-v1"] = (
        FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION
    )
    context_contract_version: Literal[
        "financial-eligibility-acquisition-context-v1"
    ]
    context_identity: str = Field(
        pattern=r"^financial-eligibility-acquisition-context:v1:[0-9a-f]{64}$"
    )
    canonical_run_id: str = Field(pattern=_RUN_ID_PATTERN)
    asset_configuration_signature: str = Field(pattern=_ASSET_SIGNATURE_PATTERN)
    instrument_identity: InstrumentIdentityEvidence
    canonical_symbol: str = Field(min_length=1, max_length=64)
    as_of_date: date
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

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
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
        if self.listing_date > self.as_of_date:
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
            context_contract_version=context.contract_version,
            context_identity=context.context_identity,
            canonical_run_id=context.run_id,
            asset_configuration_signature=context.asset_configuration_signature,
            instrument_identity=context.instrument_identity,
            canonical_symbol=context.instrument_identity.symbol.strip().upper(),
            as_of_date=context.as_of_date,
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
        )


class FinancialEligibilityAcquisitionFailureReason(str, Enum):
    COMPANY_TYPE_EVIDENCE_UNAVAILABLE = "company_type_evidence_unavailable"
    COMPANY_TYPE_UNRESOLVED = "company_type_unresolved"
    COMPANY_TYPE_AMBIGUOUS = "company_type_ambiguous"
    COMPANY_TYPE_CONTRADICTORY = "company_type_contradictory"
    LISTING_PROVENANCE_UNAVAILABLE = "listing_provenance_unavailable"
    CONTEXT_BINDING_MISMATCH = "context_binding_mismatch"
    INTERRUPTED_ACQUISITION = "interrupted_acquisition"


class FinancialEligibilityAcquisitionFailure(BaseModel):
    """Bounded failure with no provider payload, raw error, or partial context."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-result-v1"
    ] = FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION
    reason: FinancialEligibilityAcquisitionFailureReason
    diagnostic_code: FinancialEligibilityAcquisitionFailureReason
    company_type_resolution: FinancialCompanyTypeResolution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
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
        _assert_safe_projection(self.model_dump(mode="json"))
        return self


class FinancialEligibilityAcquisitionResult(BaseModel):
    """Closed terminal result containing either one context or one safe failure."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-acquisition-result-v1"
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
        _assert_safe_projection(self.model_dump(mode="json"))
        return self

    @classmethod
    def succeeded(
        cls,
        context: FinancialEligibilityAcquisitionContext,
    ) -> Self:
        return cls(outcome="success", context=context)

    @classmethod
    def failed(
        cls,
        reason: FinancialEligibilityAcquisitionFailureReason,
        *,
        company_type_resolution: FinancialCompanyTypeResolution | None = None,
    ) -> Self:
        return cls(
            outcome="failure",
            failure=FinancialEligibilityAcquisitionFailure(
                reason=reason,
                diagnostic_code=reason,
                company_type_resolution=company_type_resolution,
            ),
        )


def _failure(
    reason: FinancialEligibilityAcquisitionFailureReason,
    *,
    company_type_resolution: FinancialCompanyTypeResolution | None = None,
) -> FinancialEligibilityAcquisitionResult:
    return FinancialEligibilityAcquisitionResult.failed(
        reason,
        company_type_resolution=company_type_resolution,
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
    authoritative_as_of_date: date,
    as_of_date: date,
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
    currency: str,
    consolidation_scope: FinancialConsolidationScope,
    acquisition_annual_period_ends: Sequence[date],
    acquisition_reporting_period_ends: Sequence[date],
    interrupted: bool = False,
) -> FinancialEligibilityAcquisitionResult:
    """Atomically validate authoritative bindings and return one terminal result."""

    if interrupted:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
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
        )
    if bool(getattr(resolution, "contradictory", False)):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY,
            company_type_resolution=resolution,
        )
    if bool(getattr(resolution, "ambiguous", False)):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
            company_type_resolution=resolution,
        )
    if company_type_evidence is None:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
        )
    try:
        evidence_payload = _model_payload(company_type_evidence)
        evidence_type = (
            FinancialCompanyTypeAcquisitionEvidenceBinding
            if evidence_payload.get("contract_version")
            == FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION
            else FinancialCompanyTypeEvidenceBinding
        )
        evidence = evidence_type.model_validate(evidence_payload)
    except (TypeError, ValidationError, ValueError):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
        )
    if listing_provenance is None:
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
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
            FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
        )
    try:
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
                run_as_of_date=authoritative_as_of_date,
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
            or as_of_date != authoritative_as_of_date
            or validated_scope is not validated_authoritative_scope
            or acquisition_purpose != "foreground_current_analysis"
            or routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
            or routing_plan.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
            or routing_plan_signature != routing_plan.plan_signature
            or routing_plan.financial_eligibility_binding is None
            or validated_binding != routing_plan.financial_eligibility_binding
            or evidence.provider_id not in expected_route
            or _canonical_period_tuple(acquisition_annual_period_ends)
            != expected_annual
            or _canonical_period_tuple(acquisition_reporting_period_ends)
            != expected_reporting
            or listing_date > authoritative_as_of_date
            or validated_consolidation is FinancialConsolidationScope.UNKNOWN
        ):
            raise ValueError("financial eligibility acquisition binding mismatch")
        payload: dict[str, object] = {
            "contract_version": FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
            "run_id": run_id,
            "asset_configuration_signature": asset_configuration_signature,
            "instrument_identity": validated_identity,
            "as_of_date": as_of_date,
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
        context = FinancialEligibilityAcquisitionContext(
            **payload,
            context_identity=(
                financial_eligibility_acquisition_context_identity(payload)
            ),
        )
    except (AttributeError, KeyError, TypeError, ValidationError, ValueError):
        return _failure(
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
        )
    return FinancialEligibilityAcquisitionResult.succeeded(context)


__all__ = [
    "FINANCIAL_COMPANY_TYPE_ACQUISITION_EVIDENCE_BINDING_VERSION",
    "FINANCIAL_COMPANY_TYPE_EVIDENCE_BINDING_VERSION",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_RESULT_VERSION",
    "FINANCIAL_ELIGIBILITY_REQUEST_BINDING_VERSION",
    "FinancialCompanyTypeAcquisitionEvidenceBinding",
    "FinancialCompanyTypeContextEvidence",
    "FinancialCompanyTypeEvidenceBinding",
    "FinancialCompanyTypeEvidenceProjection",
    "FinancialEligibilityAcquisitionContext",
    "FinancialEligibilityAcquisitionFailure",
    "FinancialEligibilityAcquisitionFailureReason",
    "FinancialEligibilityMetadataBinding",
    "FinancialEligibilityRequestBinding",
    "FinancialEligibilityAcquisitionResult",
    "FinancialListingMetadata",
    "FinancialListingMetadataProjection",
    "build_financial_eligibility_acquisition_context",
    "derive_mainland_financial_acquisition_period_supersets",
    "financial_eligibility_acquisition_context_identity",
    "assert_safe_financial_eligibility_projection",
]
