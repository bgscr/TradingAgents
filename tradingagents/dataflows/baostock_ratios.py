"""Qualified BaoStock financial ratio-family adapters.

The adapters in this module expose explicit, coordinated family/period
subrequests only.  Ticket 10 owns missing-family routing and provider selection.
Ratio artifacts never enter BaoStock's strict market-history bundle.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodCompletenessAssessment,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialRatioHistoryCompletenessAssessment,
    FinancialReportingFrequency,
    FinancialStatementType,
    assess_financial_period_candidate,
    assess_financial_ratio_history_coverage,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactRef,
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
    ProviderSubrequestOutcome,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
)

BAOSTOCK_RATIO_ADAPTER_VERSION = "baostock-ratio-family-adapter-v1"
BAOSTOCK_RATIO_NORMALIZER_VERSION = "baostock-ratio-family-normalizer-v1"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CREDENTIAL_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "credential",
    "password",
    "secret",
    "token",
)


class BaoStockRatioFamilyTransport(Protocol):
    """Deterministic transport boundary matching BaoStock family endpoints."""

    def query_profit_data(self, **kwargs: object) -> pd.DataFrame: ...

    def query_operation_data(self, **kwargs: object) -> pd.DataFrame: ...

    def query_growth_data(self, **kwargs: object) -> pd.DataFrame: ...

    def query_balance_data(self, **kwargs: object) -> pd.DataFrame: ...

    def query_cash_flow_data(self, **kwargs: object) -> pd.DataFrame: ...

    def query_dupont_data(self, **kwargs: object) -> pd.DataFrame: ...


class BaoStockRatioAdapterFailureReason(str, Enum):
    NOT_ENABLED = "baostock_ratio_families_not_enabled"
    INCOHERENT_PLAN = "baostock_ratio_family_plan_incoherent"
    DATA_USAGE_MODE_NOT_PERMITTED = "baostock_ratio_data_usage_mode_not_permitted"


class BaoStockRatioAdapterConfigurationError(ValueError):
    """Typed construction failure raised before any provider work."""

    def __init__(self, reason: BaoStockRatioAdapterFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class BaoStockRatioOutcomeKind(str, Enum):
    AVAILABLE = "available"
    PERMISSION_DENIED = "permission_denied"
    AUTHENTICATION_FAILURE = "authentication_failure"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    EMPTY_NO_DATA = "empty_no_data"
    MALFORMED_RESPONSE = "malformed_response"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    AMBIGUOUS_COMPANY_TYPE = "ambiguous_company_type"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    INCOMPATIBLE_CURRENCY = "incompatible_currency"
    INSUFFICIENT_COMPLETENESS = "insufficient_completeness"
    CAPABILITY_MISMATCH = "capability_mismatch"
    PROVIDER_ERROR = "provider_error"


class BaoStockRatioCapabilityOutcome(BaseModel):
    """Typed payload-free adapter outcome for one exact family request."""

    model_config = _CLOSED_MODEL_CONFIG

    kind: BaoStockRatioOutcomeKind
    retryable: bool = False


class BaoStockRatioRowRejection(BaseModel):
    """Payload-free rejection of one retained BaoStock ratio row."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["baostock-ratio-family-adapter-v1"] = (
        BAOSTOCK_RATIO_ADAPTER_VERSION
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    ratio_family: FinancialRatioFamily
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @model_validator(mode="after")
    def _validate_reasons(self) -> BaoStockRatioRowRejection:
        canonical = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.reasons
        )
        if not canonical or canonical != self.reasons:
            raise ValueError("BaoStock ratio rejection reasons must be canonical")
        return self


class BaoStockRatioFamilyAdapterResult(BaseModel):
    """One exact family/year/quarter result with no raw payload surface."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["baostock-ratio-family-adapter-v1"] = (
        BAOSTOCK_RATIO_ADAPTER_VERSION
    )
    request: ProviderSubrequestKey
    ratio_family: FinancialRatioFamily
    year: int = Field(ge=1990, le=9999)
    quarter: int = Field(ge=1, le=4)
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: FinancialProviderArtifactIdentity | None = None
    row_artifacts: tuple[FinancialProviderArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    capability_outcome: BaoStockRatioCapabilityOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    candidates: tuple[FinancialPeriodCandidate, ...] = ()
    period_completeness: tuple[FinancialPeriodCompletenessAssessment, ...] = ()
    row_rejections: tuple[BaoStockRatioRowRejection, ...] = ()
    source_facts_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False
    strict_history_bundle_component: Literal[False] = False
    current_tradeability_changed: Literal[False] = False
    adjustment_basis_changed: Literal[False] = False
    lifecycle_evidence_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_result(self) -> BaoStockRatioFamilyAdapterResult:
        if (
            self.request.provider_id != "baostock"
            or self.request.capacity_scope != f"ratio_{self.ratio_family.value}"
            or self.request.requested_range_start != f"{self.year:04d}-Q{self.quarter}"
            or self.request.requested_range_end != f"{self.year:04d}-Q{self.quarter}"
        ):
            raise ValueError("BaoStock ratio result contradicts its exact request")
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("BaoStock ratio artifact contradicts transport outcome")
        if not available and self.capability_outcome != _transport_capability_outcome(
            self.outcome
        ):
            raise ValueError("BaoStock ratio capability outcome contradicts transport")
        if not available and (
            self.row_artifacts
            or self.candidates
            or self.period_completeness
            or self.row_rejections
        ):
            raise ValueError("unavailable BaoStock ratio cannot emit projections")
        row_ids = {item.artifact_identity for item in self.row_artifacts}
        if len(row_ids) != len(self.row_artifacts):
            raise ValueError("BaoStock ratio row artifacts must be distinct")
        if any(
            candidate.period_identity.capability
            is not FinancialCapability.RATIO_FAMILY
            or candidate.period_identity.statement_type is not None
            or candidate.period_identity.ratio_family is not self.ratio_family
            or candidate.artifact.artifact_identity not in row_ids
            for candidate in self.candidates
        ):
            raise ValueError("BaoStock ratio candidate crosses a capability boundary")
        if tuple(item.candidate_identity for item in self.period_completeness) != tuple(
            item.candidate_identity for item in self.candidates
        ):
            raise ValueError("BaoStock ratio completeness is not candidate-aligned")
        disposed = {
            item.artifact.artifact_identity for item in self.candidates
        } | {item.artifact_identity for item in self.row_rejections}
        if disposed != row_ids:
            raise ValueError("BaoStock ratio row artifact has no disposition")
        return self


class BaoStockRatioFamilyAggregateResult(BaseModel):
    """Independent completeness and salvage across explicitly supplied families."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["baostock-ratio-family-adapter-v1"] = (
        BAOSTOCK_RATIO_ADAPTER_VERSION
    )
    operational_results: tuple[BaoStockRatioFamilyAdapterResult, ...]
    family_completeness: tuple[
        FinancialRatioHistoryCompletenessAssessment, ...
    ]
    qualified_families: tuple[FinancialRatioFamily, ...]
    rejected_families: tuple[FinancialRatioFamily, ...]
    usable_candidates: tuple[FinancialPeriodCandidate, ...]
    source_facts_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_aggregate(self) -> BaoStockRatioFamilyAggregateResult:
        family_order = tuple(FinancialRatioFamily)
        if self.qualified_families != tuple(
            family for family in family_order if family in self.qualified_families
        ) or self.rejected_families != tuple(
            family for family in family_order if family in self.rejected_families
        ):
            raise ValueError("BaoStock ratio aggregate families must be canonical")
        assessed_families = tuple(
            item.ratio_family for item in self.family_completeness
        )
        if assessed_families != tuple(
            family for family in family_order if family in assessed_families
        ):
            raise ValueError("BaoStock ratio completeness must be family-canonical")
        if set(self.qualified_families) | set(self.rejected_families) != set(
            assessed_families
        ) or set(self.qualified_families) & set(self.rejected_families):
            raise ValueError("BaoStock ratio family dispositions are incomplete")
        if any(
            item.period_identity.ratio_family not in self.qualified_families
            for item in self.usable_candidates
        ):
            raise ValueError("rejected BaoStock family leaked a usable candidate")
        return self


class BaoStockRatioStatementBoundaryResult(BaseModel):
    """Typed proof that a ratio artifact cannot satisfy a full statement."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["baostock-ratio-family-adapter-v1"] = (
        BAOSTOCK_RATIO_ADAPTER_VERSION
    )
    ratio_family: FinancialRatioFamily
    requested_statement_type: FinancialStatementType
    retained_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    capability_outcome: BaoStockRatioCapabilityOutcome
    candidate_assessments: tuple[FinancialPeriodCompletenessAssessment, ...]
    statement_candidates: tuple[FinancialPeriodCandidate, ...] = ()
    source_facts_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_boundary(self) -> BaoStockRatioStatementBoundaryResult:
        if self.requested_statement_type not in {
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.INCOME_STATEMENT,
            FinancialStatementType.CASH_FLOW,
        }:
            raise ValueError("ratio boundary requires a full-statement request")
        if (
            self.capability_outcome.kind
            is not BaoStockRatioOutcomeKind.CAPABILITY_MISMATCH
            or self.statement_candidates
            or any(
                FinancialPeriodRejectionReason.CAPABILITY_MISMATCH
                not in assessment.rejection_reasons
                for assessment in self.candidate_assessments
            )
        ):
            raise ValueError("BaoStock ratio statement boundary is inconsistent")
        return self


class _SourceField(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider_field: str = Field(pattern=_SAFE_ID_PATTERN)
    original_unit: Literal["PERCENT", "RATIO"]
    divisor: Decimal


_PROFIT_FIELDS = {
    "gross_margin": _SourceField(
        provider_field="gpMargin",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "net_margin": _SourceField(
        provider_field="npMargin",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "return_on_equity": _SourceField(
        provider_field="roeAvg",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_BANK_PROFIT_FIELDS = _PROFIT_FIELDS

_OPERATION_FIELDS = {
    "asset_turnover": _SourceField(
        provider_field="AssetTurnRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "current_asset_turnover": _SourceField(
        provider_field="CATurnRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "inventory_turnover": _SourceField(
        provider_field="INVTurnRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "receivables_turnover": _SourceField(
        provider_field="NRTurnRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_GROWTH_FIELDS = {
    "asset_growth": _SourceField(
        provider_field="YOYAsset",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "basic_earnings_per_share_growth": _SourceField(
        provider_field="YOYEPSBasic",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "equity_growth": _SourceField(
        provider_field="YOYEquity",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "net_income_growth": _SourceField(
        provider_field="YOYNI",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "parent_net_income_growth": _SourceField(
        provider_field="YOYPNI",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_BALANCE_FIELDS = {
    "cash_ratio": _SourceField(
        provider_field="cashRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "current_ratio": _SourceField(
        provider_field="currentRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "debt_to_assets": _SourceField(
        provider_field="liabilityToAsset",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "equity_multiplier": _SourceField(
        provider_field="assetToEquity",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "liability_growth": _SourceField(
        provider_field="YOYLiability",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "quick_ratio": _SourceField(
        provider_field="quickRatio",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_CASH_FLOW_FIELDS = {
    "cash_flow_to_gross_revenue": _SourceField(
        provider_field="CFOToGr",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "cash_flow_to_revenue": _SourceField(
        provider_field="CFOToOR",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "current_assets_to_assets": _SourceField(
        provider_field="CAToAsset",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "interest_coverage": _SourceField(
        provider_field="ebitToInterest",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "non_current_assets_to_assets": _SourceField(
        provider_field="NCAToAsset",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "quality_of_income": _SourceField(
        provider_field="CFOToNP",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "tangible_asset_ratio": _SourceField(
        provider_field="tangibleAssetToAsset",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_DUPONT_FIELDS = {
    "asset_turnover_component": _SourceField(
        provider_field="dupontAssetTurn",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "ebit_margin_component": _SourceField(
        provider_field="dupontEbittogr",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "equity_multiplier_component": _SourceField(
        provider_field="dupontAssetStoEquity",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "interest_burden": _SourceField(
        provider_field="dupontIntburden",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "net_profit_margin_component": _SourceField(
        provider_field="dupontNitogr",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "parent_net_income_share": _SourceField(
        provider_field="dupontPnitoni",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "return_on_equity_component": _SourceField(
        provider_field="dupontROE",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
    "tax_burden": _SourceField(
        provider_field="dupontTaxBurden",
        original_unit="RATIO",
        divisor=Decimal(1),
    ),
}

_BAOSTOCK_RATIO_ARTIFACT_FIELDS = {
    FinancialRatioFamily.PROFIT: (
        "roeAvg",
        "npMargin",
        "gpMargin",
        "netProfit",
        "epsTTM",
        "MBRevenue",
        "totalShare",
        "liqaShare",
    ),
    FinancialRatioFamily.OPERATION: (
        "NRTurnRatio",
        "NRTurnDays",
        "INVTurnRatio",
        "INVTurnDays",
        "CATurnRatio",
        "AssetTurnRatio",
    ),
    FinancialRatioFamily.GROWTH: (
        "YOYEquity",
        "YOYAsset",
        "YOYNI",
        "YOYEPSBasic",
        "YOYPNI",
    ),
    FinancialRatioFamily.BALANCE: (
        "currentRatio",
        "quickRatio",
        "cashRatio",
        "YOYLiability",
        "liabilityToAsset",
        "assetToEquity",
    ),
    FinancialRatioFamily.CASH_FLOW: (
        "CAToAsset",
        "NCAToAsset",
        "tangibleAssetToAsset",
        "ebitToInterest",
        "CFOToOR",
        "CFOToNP",
        "CFOToGr",
    ),
    FinancialRatioFamily.DUPONT: (
        "dupontROE",
        "dupontAssetStoEquity",
        "dupontAssetTurn",
        "dupontPnitoni",
        "dupontNitogr",
        "dupontTaxBurden",
        "dupontIntburden",
        "dupontEbittogr",
    ),
}


class BaoStockRatioFamilyAdapter:
    """Acquire one explicitly requested BaoStock ratio family period."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        transport: BaoStockRatioFamilyTransport,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
        data_usage_mode: DataUsageMode,
        normalizer_version: str = BAOSTOCK_RATIO_NORMALIZER_VERSION,
    ) -> None:
        _validate_plan(routing_plan)
        if data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH:
            raise BaoStockRatioAdapterConfigurationError(
                BaoStockRatioAdapterFailureReason.DATA_USAGE_MODE_NOT_PERMITTED
            )
        if not normalizer_version or any(
            marker in normalizer_version.casefold() for marker in _CREDENTIAL_MARKERS
        ):
            raise ValueError("BaoStock ratio normalizer identity is unsafe")
        self._plan = routing_plan
        self._cache = subrequest_cache
        self._transport = transport
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration
        self._data_usage_mode = data_usage_mode
        self._normalizer_version = normalizer_version

    def acquire_profit(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.PROFIT,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_profit_data",
            fields=(
                _BANK_PROFIT_FIELDS
                if company_type_resolution.company_type is FinancialCompanyType.BANK
                else _PROFIT_FIELDS
            ),
            request=lambda code: self._transport.query_profit_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def acquire_operation(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.OPERATION,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_operation_data",
            fields=_OPERATION_FIELDS,
            request=lambda code: self._transport.query_operation_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def acquire_growth(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.GROWTH,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_growth_data",
            fields=_GROWTH_FIELDS,
            request=lambda code: self._transport.query_growth_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def acquire_balance_ratios(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.BALANCE,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_balance_data",
            fields=_BALANCE_FIELDS,
            request=lambda code: self._transport.query_balance_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def acquire_cash_flow_ratios(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.CASH_FLOW,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_cash_flow_data",
            fields=_CASH_FLOW_FIELDS,
            request=lambda code: self._transport.query_cash_flow_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def acquire_dupont(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        year: int,
        quarter: int,
        as_of_date: date,
    ) -> BaoStockRatioFamilyAdapterResult:
        return self._acquire(
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=FinancialRatioFamily.DUPONT,
            year=year,
            quarter=quarter,
            as_of_date=as_of_date,
            endpoint_id="query_dupont_data",
            fields=_DUPONT_FIELDS,
            request=lambda code: self._transport.query_dupont_data(
                code=code,
                year=year,
                quarter=quarter,
            ),
        )

    def _acquire(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        ratio_family: FinancialRatioFamily,
        year: int,
        quarter: int,
        as_of_date: date,
        endpoint_id: str,
        fields: Mapping[str, _SourceField],
        request: Callable[[str], pd.DataFrame],
    ) -> BaoStockRatioFamilyAdapterResult:
        provider_code = _validate_identity(instrument_identity)
        period_end = _period_end(year=year, quarter=quarter)
        if period_end > as_of_date:
            raise ValueError("BaoStock ratio period cannot end after the as-of date")
        period_token = f"{year:04d}-Q{quarter}"
        upstream_id, _ = upstream_service_identity_for_provider("baostock")
        requested_fields = tuple(
            {
                "code",
                "pubDate",
                "statDate",
                *_BAOSTOCK_RATIO_ARTIFACT_FIELDS[ratio_family],
            }
        )
        key = ProviderSubrequestKey.create(
            provider_id="baostock",
            upstream_service_id=upstream_id,
            account_scope="not-applicable",
            capacity_scope=f"ratio_{ratio_family.value}",
            instrument_identity=instrument_identity,
            requested_range_start=period_token,
            requested_range_end=period_token,
            requested_fields=requested_fields,
            as_of_date=as_of_date,
            qualification_profile=self._plan.qualification_profile,
            normalizer_version=self._normalizer_version,
            data_usage_mode=self._data_usage_mode,
        )

        def physical_request() -> bytes:
            try:
                frame = request(provider_code)
            except PhysicalAttemptFailure:
                raise
            except Exception as exc:
                raise _sanitized_transport_failure(exc) from None
            return _encode_artifact(
                frame=frame,
                key=key,
                ratio_family=ratio_family,
                year=year,
                quarter=quarter,
                endpoint_id=endpoint_id,
                observed_at=_require_utc(self._now()),
            )

        subrequest = self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation=f"baostock_ratio_{ratio_family.value}",
            media_type="application/json",
            physical_request=physical_request,
        )
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return BaoStockRatioFamilyAdapterResult(
                request=key,
                ratio_family=ratio_family,
                year=year,
                quarter=quarter,
                outcome=subrequest.outcome,
                capability_outcome=_transport_capability_outcome(
                    subrequest.outcome
                ),
                sequence_id=subrequest.sequence_id,
                attempt_events=subrequest.attempt_events,
            )
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = _decode_artifact(
            subrequest.value,
            key=key,
            ratio_family=ratio_family,
            year=year,
            quarter=quarter,
            endpoint_id=endpoint_id,
        )
        artifact = _artifact_identity(key=key, payload=payload)
        row_artifacts = _row_artifact_identities(
            key=key,
            response_artifact=artifact,
            payload=payload,
        )
        candidates, assessments, rejections = _project_candidates(
            payload=payload,
            row_artifacts=row_artifacts,
            instrument_identity=instrument_identity,
            company_type_resolution=company_type_resolution,
            ratio_family=ratio_family,
            expected_provider_code=provider_code,
            expected_period_end=period_end,
            pit_as_of_date=as_of_date,
            fields=fields,
        )
        return BaoStockRatioFamilyAdapterResult(
            request=key,
            ratio_family=ratio_family,
            year=year,
            quarter=quarter,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            outcome=subrequest.outcome,
            capability_outcome=_normalized_capability_outcome(
                row_artifacts=row_artifacts,
                assessments=assessments,
                rejections=rejections,
            ),
            sequence_id=subrequest.sequence_id,
            attempt_events=subrequest.attempt_events,
            candidates=candidates,
            period_completeness=assessments,
            row_rejections=rejections,
        )


def assess_baostock_ratio_family_results(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    results: Sequence[BaoStockRatioFamilyAdapterResult],
    eligible_reporting_period_ends: Sequence[date],
) -> BaoStockRatioFamilyAggregateResult:
    """Apply the Ticket 02 eight-period gate independently per supplied family."""

    by_key: dict[str, BaoStockRatioFamilyAdapterResult] = {}
    for result in results:
        existing = by_key.get(result.request.subrequest_key)
        if existing is not None and existing != result:
            raise ValueError("conflicting BaoStock results share one exact request")
        by_key[result.request.subrequest_key] = result
    family_index = {family: index for index, family in enumerate(FinancialRatioFamily)}
    operational_results = tuple(
        sorted(
            by_key.values(),
            key=lambda item: (
                family_index[item.ratio_family],
                item.year,
                item.quarter,
                item.request.subrequest_key,
            ),
        )
    )
    present_families = tuple(
        family
        for family in FinancialRatioFamily
        if any(result.ratio_family is family for result in operational_results)
    )
    assessment_by_candidate = {
        assessment.candidate_identity: assessment
        for result in operational_results
        for assessment in result.period_completeness
    }
    family_completeness = tuple(
        assess_financial_ratio_history_coverage(
            instrument_identity=instrument_identity,
            ratio_family=family,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency,
            assessments=tuple(
                assessment
                for result in operational_results
                if result.ratio_family is family
                for assessment in result.period_completeness
            ),
            eligible_reporting_period_ends=eligible_reporting_period_ends,
        )
        for family in present_families
    )
    qualified_families = tuple(
        item.ratio_family for item in family_completeness if item.complete
    )
    rejected_families = tuple(
        item.ratio_family for item in family_completeness if not item.complete
    )
    usable_candidates = tuple(
        sorted(
            (
                candidate
                for result in operational_results
                if result.ratio_family in qualified_families
                for candidate in result.candidates
                if assessment_by_candidate[candidate.candidate_identity]
                .usable_for_current_analysis
            ),
            key=lambda item: (
                family_index[item.period_identity.ratio_family],
                item.period_identity.period_end,
                item.candidate_identity,
            ),
        )
    )
    return BaoStockRatioFamilyAggregateResult(
        operational_results=operational_results,
        family_completeness=family_completeness,
        qualified_families=qualified_families,
        rejected_families=rejected_families,
        usable_candidates=usable_candidates,
    )


def offer_baostock_ratio_to_statement(
    *,
    result: BaoStockRatioFamilyAdapterResult,
    statement_type: FinancialStatementType,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_currency: str,
    expected_consolidation_scope: FinancialConsolidationScope,
    pit_as_of_date: date,
) -> BaoStockRatioStatementBoundaryResult:
    """Retain the artifact while rejecting every ratio candidate as a statement."""

    if statement_type not in {
        FinancialStatementType.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW,
    }:
        raise ValueError("BaoStock ratio can be offered only to a full statement")
    if result.artifact is None:
        raise ValueError("BaoStock ratio statement boundary requires an artifact")
    assessments = tuple(
        assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=statement_type,
            requested_ratio_family=None,
            expected_instrument_identity=expected_instrument_identity,
            expected_frequency=FinancialReportingFrequency.QUARTERLY,
            expected_currency=expected_currency,
            expected_consolidation_scope=expected_consolidation_scope,
            pit_as_of_date=pit_as_of_date,
            ratio_declaration_version=FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
        )
        for candidate in result.candidates
    )
    return BaoStockRatioStatementBoundaryResult(
        ratio_family=result.ratio_family,
        requested_statement_type=statement_type,
        retained_artifact_identity=result.artifact.artifact_identity,
        capability_outcome=BaoStockRatioCapabilityOutcome(
            kind=BaoStockRatioOutcomeKind.CAPABILITY_MISMATCH
        ),
        candidate_assessments=assessments,
    )


class _RatioNormalizationError(ValueError):
    def __init__(self, reason: FinancialPeriodRejectionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _project_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: Sequence[FinancialProviderArtifactIdentity],
    instrument_identity: InstrumentIdentityEvidence,
    company_type_resolution: FinancialCompanyTypeResolution,
    ratio_family: FinancialRatioFamily,
    expected_provider_code: str,
    expected_period_end: date,
    pit_as_of_date: date,
    fields: Mapping[str, _SourceField],
) -> tuple[
    tuple[FinancialPeriodCandidate, ...],
    tuple[FinancialPeriodCompletenessAssessment, ...],
    tuple[BaoStockRatioRowRejection, ...],
]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    candidates: list[FinancialPeriodCandidate] = []
    assessments: list[FinancialPeriodCompletenessAssessment] = []
    rejections: list[BaoStockRatioRowRejection] = []
    for row, row_artifact in zip(rows, row_artifacts, strict=True):
        assert isinstance(row, dict)
        try:
            if str(row.get("code", "")).strip() != expected_provider_code:
                raise ValueError("BaoStock ratio symbol is incompatible")
            if date.fromisoformat(str(row.get("statDate", "")).strip()) != expected_period_end:
                raise ValueError("BaoStock ratio period is incompatible")
            normalized_fields = _normalized_fields(row=row, fields=fields)
            currency_text = str(row.get("currency", "") or "").strip().upper()
            currency = instrument_identity.currency
            if currency_text and currency_text != currency:
                raise _RatioNormalizationError(
                    FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY
                )
            period_identity = FinancialPeriodIdentity.create(
                instrument_identity=instrument_identity,
                capability=FinancialCapability.RATIO_FAMILY,
                statement_type=None,
                ratio_family=ratio_family,
                period_end=expected_period_end,
                frequency=FinancialReportingFrequency.QUARTERLY,
                company_type=company_type_resolution.company_type,
                consolidation_scope=FinancialConsolidationScope.UNKNOWN,
                currency=currency,
                provider_revision_binding=row_artifact.artifact_identity,
            )
            filing_metadata = FinancialFilingMetadata(
                ann_date=date.fromisoformat(str(row.get("pubDate", "")).strip()),
                f_ann_date=None,
                report_type=None,
                comp_type=None,
                update_flag=None,
                retrieved_at=row_artifact.retrieved_at,
                observed_at=row_artifact.observed_at,
                local_provider_revision_identity=row_artifact.artifact_identity,
                provider_filing_revision_id=None,
            )
            candidate = FinancialPeriodCandidate.create(
                artifact=row_artifact,
                period_identity=period_identity,
                filing_metadata=filing_metadata,
                company_type_resolution=company_type_resolution,
                fields=normalized_fields,
            )
            assessment = assess_financial_period_candidate(
                candidate,
                requested_capability=FinancialCapability.RATIO_FAMILY,
                requested_statement_type=None,
                requested_ratio_family=ratio_family,
                expected_instrument_identity=instrument_identity,
                expected_frequency=FinancialReportingFrequency.QUARTERLY,
                expected_currency=instrument_identity.currency,
                expected_consolidation_scope=FinancialConsolidationScope.UNKNOWN,
                pit_as_of_date=pit_as_of_date,
                ratio_declaration_version=(
                    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1
                ),
            )
        except _RatioNormalizationError as exc:
            rejections.append(
                BaoStockRatioRowRejection(
                    artifact_identity=row_artifact.artifact_identity,
                    ratio_family=ratio_family,
                    reasons=(exc.reason,),
                )
            )
            continue
        except (InvalidOperation, TypeError, ValueError):
            rejections.append(
                BaoStockRatioRowRejection(
                    artifact_identity=row_artifact.artifact_identity,
                    ratio_family=ratio_family,
                    reasons=(
                        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
                    ),
                )
            )
            continue
        candidates.append(candidate)
        assessments.append(assessment)
    return tuple(candidates), tuple(assessments), tuple(rejections)


def _normalized_fields(
    *,
    row: Mapping[str, object],
    fields: Mapping[str, _SourceField],
) -> tuple[FinancialFieldValue, ...]:
    normalized: list[FinancialFieldValue] = []
    for normalized_field, source in fields.items():
        original = row.get(source.provider_field)
        if original is None or str(original).strip() == "":
            continue
        explicit_unit = next(
            (
                str(row.get(unit_field)).strip().upper()
                for unit_field in (
                    f"{source.provider_field}Unit",
                    f"{source.provider_field}_unit",
                )
                if row.get(unit_field) is not None
                and str(row.get(unit_field)).strip()
            ),
            None,
        )
        if explicit_unit is not None and explicit_unit != source.original_unit:
            raise _RatioNormalizationError(
                FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT
            )
        numeric = Decimal(str(original))
        if not numeric.is_finite():
            raise ValueError("BaoStock ratio value is malformed")
        normalized.append(
            FinancialFieldValue(
                provider_field=source.provider_field,
                original_value=str(original),
                original_unit=source.original_unit,
                normalized_field=normalized_field,
                normalized_value=format(numeric / source.divisor, "f"),
                normalized_unit="RATIO",
            )
        )
    return tuple(normalized)


def _encode_artifact(
    *,
    frame: object,
    key: ProviderSubrequestKey,
    ratio_family: FinancialRatioFamily,
    year: int,
    quarter: int,
    endpoint_id: str,
    observed_at: datetime,
) -> bytes:
    if not isinstance(frame, pd.DataFrame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_ratio_response_not_frame",
        )
    columns = [str(column) for column in frame.columns]
    if not columns or len(columns) != len(set(columns)):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_ratio_columns_invalid",
        )
    if _contains_credential_data(frame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_ratio_response_unsafe",
        )
    try:
        rows = [
            {
                column: _json_value(value)
                for column, value in zip(columns, values, strict=True)
            }
            for values in frame.itertuples(index=False, name=None)
        ]
        payload = {
            "artifact_contract_version": BAOSTOCK_RATIO_ADAPTER_VERSION,
            "provider_id": "baostock",
            "endpoint_id": endpoint_id,
            "dataset_id": f"baostock.{ratio_family.value}.quarterly.v1",
            "schema_identity": f"baostock_{ratio_family.value}_quarterly_v1",
            "capability": FinancialCapability.RATIO_FAMILY.value,
            "ratio_family": ratio_family.value,
            "canonical_symbol": key.canonical_symbol,
            "instrument_identity": key.instrument_identity.model_dump(mode="json"),
            "year": year,
            "quarter": quarter,
            "retrieved_at": observed_at.isoformat().replace("+00:00", "Z"),
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "qualification_profile": key.qualification_profile,
            "normalizer_version": key.normalizer_version,
            "exact_request_identity": key.subrequest_key,
            "columns": columns,
            "rows": rows,
        }
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_ratio_response_not_serializable",
        ) from None


def _decode_artifact(
    value: bytes,
    *,
    key: ProviderSubrequestKey,
    ratio_family: FinancialRatioFamily,
    year: int,
    quarter: int,
    endpoint_id: str,
) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("BaoStock ratio artifact is malformed") from exc
    required = {
        "artifact_contract_version",
        "provider_id",
        "endpoint_id",
        "dataset_id",
        "schema_identity",
        "capability",
        "ratio_family",
        "canonical_symbol",
        "instrument_identity",
        "year",
        "quarter",
        "retrieved_at",
        "observed_at",
        "qualification_profile",
        "normalizer_version",
        "exact_request_identity",
        "columns",
        "rows",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload["artifact_contract_version"] != BAOSTOCK_RATIO_ADAPTER_VERSION
        or payload["provider_id"] != "baostock"
        or payload["endpoint_id"] != endpoint_id
        or payload["dataset_id"]
        != f"baostock.{ratio_family.value}.quarterly.v1"
        or payload["schema_identity"]
        != f"baostock_{ratio_family.value}_quarterly_v1"
        or payload["capability"] != FinancialCapability.RATIO_FAMILY.value
        or payload["ratio_family"] != ratio_family.value
        or payload["canonical_symbol"] != key.canonical_symbol
        or payload["instrument_identity"]
        != key.instrument_identity.model_dump(mode="json")
        or payload["year"] != year
        or payload["quarter"] != quarter
        or payload["qualification_profile"] != key.qualification_profile
        or payload["normalizer_version"] != key.normalizer_version
        or payload["exact_request_identity"] != key.subrequest_key
        or not isinstance(payload["columns"], list)
        or not isinstance(payload["rows"], list)
        or any(
            not isinstance(row, dict) or set(row) != set(payload["columns"])
            for row in payload["rows"]
        )
    ):
        raise ValueError("BaoStock ratio artifact is malformed")
    return payload


def _artifact_identity(
    *,
    key: ProviderSubrequestKey,
    payload: Mapping[str, object],
) -> FinancialProviderArtifactIdentity:
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="baostock",
        endpoint_id=str(payload["endpoint_id"]),
        dataset_id=str(payload["dataset_id"]),
        schema_identity=str(payload["schema_identity"]),
    )
    return FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request=key.model_dump(mode="json"),
        provider_metadata={
            "provider_id": "baostock",
            "endpoint_id": payload["endpoint_id"],
            "ratio_family": payload["ratio_family"],
            "year": payload["year"],
            "quarter": payload["quarter"],
            "schema_identity": payload["schema_identity"],
        },
        retrieved_at=_payload_time(payload, "retrieved_at"),
        observed_at=_payload_time(payload, "observed_at"),
        payload=payload["rows"],
    )


def _row_artifact_identities(
    *,
    key: ProviderSubrequestKey,
    response_artifact: FinancialProviderArtifactIdentity,
    payload: Mapping[str, object],
) -> tuple[FinancialProviderArtifactIdentity, ...]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    counts: dict[str, int] = {}
    result: list[FinancialProviderArtifactIdentity] = []
    for row in rows:
        canonical = json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        occurrence = counts.get(canonical, 0)
        counts[canonical] = occurrence + 1
        result.append(
            FinancialProviderArtifactIdentity.create(
                dataset=response_artifact.dataset,
                canonical_request=key.model_dump(mode="json"),
                provider_metadata={
                    "provider_id": "baostock",
                    "endpoint_id": payload["endpoint_id"],
                    "ratio_family": payload["ratio_family"],
                    "year": payload["year"],
                    "quarter": payload["quarter"],
                    "response_payload_sha256": response_artifact.payload_sha256,
                    "row_payload_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
                    "duplicate_occurrence": occurrence,
                },
                retrieved_at=response_artifact.retrieved_at,
                observed_at=response_artifact.observed_at,
                payload=row,
            )
        )
    return tuple(result)


def _validate_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if plan.mode not in {
        MainlandCapabilityRoutingMode.QUALIFIED_V1,
        MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
    }:
        raise BaoStockRatioAdapterConfigurationError(
            BaoStockRatioAdapterFailureReason.NOT_ENABLED
        )
    if (
        "baostock_qualified_families"
        not in plan.route_for(MainlandCapability.FINANCIAL_INDICATORS)
        or plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT)
        != ("akshare", "baostock", "yfinance")
    ):
        raise BaoStockRatioAdapterConfigurationError(
            BaoStockRatioAdapterFailureReason.INCOHERENT_PLAN
        )


def _transport_capability_outcome(
    outcome: ProviderSubrequestOutcome,
) -> BaoStockRatioCapabilityOutcome:
    kind = {
        ProviderSubrequestOutcomeKind.AVAILABLE: BaoStockRatioOutcomeKind.AVAILABLE,
        ProviderSubrequestOutcomeKind.PERMISSION_DENIED: (
            BaoStockRatioOutcomeKind.PERMISSION_DENIED
        ),
        ProviderSubrequestOutcomeKind.AUTHENTICATION: (
            BaoStockRatioOutcomeKind.AUTHENTICATION_FAILURE
        ),
        ProviderSubrequestOutcomeKind.RATE_LIMITED: (
            BaoStockRatioOutcomeKind.RATE_LIMITED
        ),
        ProviderSubrequestOutcomeKind.TIMEOUT: BaoStockRatioOutcomeKind.TIMEOUT,
        ProviderSubrequestOutcomeKind.DISCONNECT: (
            BaoStockRatioOutcomeKind.DISCONNECT
        ),
        ProviderSubrequestOutcomeKind.EMPTY: (
            BaoStockRatioOutcomeKind.EMPTY_NO_DATA
        ),
        ProviderSubrequestOutcomeKind.MALFORMED: (
            BaoStockRatioOutcomeKind.MALFORMED_RESPONSE
        ),
    }.get(outcome.kind, BaoStockRatioOutcomeKind.PROVIDER_ERROR)
    return BaoStockRatioCapabilityOutcome(kind=kind, retryable=outcome.retryable)


def _normalized_capability_outcome(
    *,
    row_artifacts: Sequence[FinancialProviderArtifactIdentity],
    assessments: Sequence[FinancialPeriodCompletenessAssessment],
    rejections: Sequence[BaoStockRatioRowRejection],
) -> BaoStockRatioCapabilityOutcome:
    if not row_artifacts:
        return BaoStockRatioCapabilityOutcome(
            kind=BaoStockRatioOutcomeKind.EMPTY_NO_DATA
        )
    if rejections:
        rejection_reasons = {
            reason for rejection in rejections for reason in rejection.reasons
        }
        if FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in rejection_reasons:
            return BaoStockRatioCapabilityOutcome(
                kind=BaoStockRatioOutcomeKind.INCOMPATIBLE_UNIT
            )
        if FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in rejection_reasons:
            return BaoStockRatioCapabilityOutcome(
                kind=BaoStockRatioOutcomeKind.INCOMPATIBLE_CURRENCY
            )
        return BaoStockRatioCapabilityOutcome(
            kind=BaoStockRatioOutcomeKind.INCOMPATIBLE_METADATA
        )
    reason_set = {
        reason for assessment in assessments for reason in assessment.rejection_reasons
    }
    priority = (
        (
            FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
            BaoStockRatioOutcomeKind.AMBIGUOUS_COMPANY_TYPE,
        ),
        (
            FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT,
            BaoStockRatioOutcomeKind.INCOMPATIBLE_UNIT,
        ),
        (
            FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY,
            BaoStockRatioOutcomeKind.INCOMPATIBLE_CURRENCY,
        ),
        (
            FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
            BaoStockRatioOutcomeKind.INCOMPATIBLE_METADATA,
        ),
        (
            FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
            BaoStockRatioOutcomeKind.INSUFFICIENT_COMPLETENESS,
        ),
        (
            FinancialPeriodRejectionReason.CAPABILITY_MISMATCH,
            BaoStockRatioOutcomeKind.CAPABILITY_MISMATCH,
        ),
    )
    for reason, outcome in priority:
        if reason in reason_set:
            return BaoStockRatioCapabilityOutcome(kind=outcome)
    if any(item.usable_for_current_analysis for item in assessments):
        return BaoStockRatioCapabilityOutcome(kind=BaoStockRatioOutcomeKind.AVAILABLE)
    return BaoStockRatioCapabilityOutcome(
        kind=BaoStockRatioOutcomeKind.MALFORMED_RESPONSE
    )


def _validate_identity(identity: InstrumentIdentityEvidence) -> str:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("BaoStock ratios require a mainland Equity identity")
    resolved = resolve_mainland_instrument(identity.symbol)
    if resolved is None or resolved.instrument_kind != "equity":
        raise ValueError("BaoStock ratio Instrument Identity is provider-incompatible")
    return resolved.baostock_code


def _period_end(*, year: int, quarter: int) -> date:
    if not isinstance(year, int) or isinstance(year, bool) or not 1990 <= year <= 9999:
        raise ValueError("BaoStock ratio year is invalid")
    try:
        month, day = {
            1: (3, 31),
            2: (6, 30),
            3: (9, 30),
            4: (12, 31),
        }[quarter]
    except (KeyError, TypeError):
        raise ValueError("BaoStock ratio quarter is invalid") from None
    return date(year, month, day)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("BaoStock ratio observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _payload_time(payload: Mapping[str, object], field: str) -> datetime:
    return datetime.fromisoformat(str(payload[field]).replace("Z", "+00:00"))


def _json_value(value: object) -> object:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        value = value.item()
    if isinstance(value, datetime):
        return _require_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool):
        return value
    raise TypeError("unsupported BaoStock ratio provider value")


def _contains_credential_data(frame: pd.DataFrame) -> bool:
    return any(
        marker in str(column).casefold()
        for column in frame.columns
        for marker in _CREDENTIAL_MARKERS
    ) or any(
        marker in value.casefold()
        for row in frame.itertuples(index=False, name=None)
        for value in row
        if isinstance(value, str)
        for marker in _CREDENTIAL_MARKERS
    )


def _sanitized_transport_failure(exc: Exception) -> PhysicalAttemptFailure:
    type_name = type(exc).__name__.casefold()
    message = str(exc)[:512].casefold()
    provider_error_code = str(getattr(exc, "error_code", "") or "").strip().casefold()
    retry_after = _retry_after_seconds(exc)
    if isinstance(exc, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code="baostock_ratio_timeout",
        )
    if provider_error_code == "10001005" or any(
        marker in message
        for marker in ("10001005", "rate limit", "too many", "capacity")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
            error_code="baostock_ratio_rate_limited",
            rate_limit_scope=(
                RateLimitScope.UPSTREAM
                if provider_error_code == "10001005"
                or "10001005" in message
                or _upstream_rate_limit(exc)
                else RateLimitScope.CAPACITY
            ),
        )
    if any(marker in message for marker in ("permission", "access denied", "权限")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="baostock_ratio_permission_denied",
        )
    if any(
        marker in message
        for marker in ("authentication", "auth", "login failed", "认证")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="baostock_ratio_authentication",
        )
    if any(
        marker in message
        for marker in ("disconnect", "connection reset", "connection aborted", "socket")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
            error_code="baostock_ratio_disconnect",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code="baostock_ratio_provider_error",
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if value is None:
        value = getattr(exc, "retry_after", None)
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _upstream_rate_limit(exc: Exception) -> bool:
    value = getattr(exc, "rate_limit_scope", None)
    return value is RateLimitScope.UPSTREAM or (
        isinstance(value, str)
        and value.strip().casefold() in {"account", "global", "upstream"}
    )


__all__ = [
    "BAOSTOCK_RATIO_ADAPTER_VERSION",
    "BAOSTOCK_RATIO_NORMALIZER_VERSION",
    "BaoStockRatioAdapterConfigurationError",
    "BaoStockRatioAdapterFailureReason",
    "BaoStockRatioCapabilityOutcome",
    "BaoStockRatioFamilyAdapter",
    "BaoStockRatioFamilyAdapterResult",
    "BaoStockRatioFamilyAggregateResult",
    "BaoStockRatioFamilyTransport",
    "BaoStockRatioOutcomeKind",
    "BaoStockRatioRowRejection",
    "BaoStockRatioStatementBoundaryResult",
    "assess_baostock_ratio_family_results",
    "offer_baostock_ratio_to_statement",
]
