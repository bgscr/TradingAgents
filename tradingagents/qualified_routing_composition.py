"""Immutable blueprint and run-owned sources for qualified routing composition.

Provider adapters are bound without opening sessions or stores. Graph integration,
checkpoint restoration, and runtime lifecycle ownership remain later factory phases.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import time
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from inspect import signature as inspect_signature
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.asset_configuration import (
    RunAssetConfiguration,
    RunAssetConfigurationProjection,
)
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingPreflightError,
    MainlandCapabilityRoutingRunProjection,
    TushareCapability,
    TushareEndpointPacingIdentity,
    mainland_capability_routing_failure,
)
from tradingagents.dataflows.akshare_sina_statements import (
    AkshareSinaStatementAdapter,
)
from tradingagents.dataflows.baostock_capabilities import (
    BAOSTOCK_CAPABILITY_NORMALIZER_VERSION,
    BaoStockCapabilityAdapter,
)
from tradingagents.dataflows.baostock_ratios import (
    BAOSTOCK_RATIO_NORMALIZER_VERSION,
    BaoStockRatioFamilyAdapter,
    assess_baostock_ratio_family_results,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCapabilityRoutingRequest,
    FinancialCapabilitySource,
    FinancialCompanyTypeEvidenceProviderResponse,
    FinancialCompanyTypeEvidenceRoutingRequest,
    FinancialCompanyTypeEvidenceSource,
    FinancialCompanyTypeEvidenceSourceProjectionKind,
    FinancialIndicatorProviderGapRequest,
    FinancialIndicatorRoutingRequest,
    FinancialIndicatorSource,
    FinancialPeriodSource,
    FinancialProviderGapRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
    financial_period_response_from_indicator_adapter,
    financial_period_response_from_statement_adapter,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_MANIFEST_V2,
    FinancialAvailabilityScope,
    FinancialConsolidationScope,
    FinancialRatioFamily,
    FinancialStatementType,
)
from tradingagents.dataflows.financial_dispatch import (
    FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
    FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
    FINANCIAL_DISPATCH_LEDGER_VERSION,
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialDispatchCheckpointLedger,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityRequestBinding,
    FinancialListingMetadataProjection,
    financial_eligibility_acquisition_context_identity,
    validate_financial_eligibility_acquisition_context_binding,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityProjectionBinding,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_indicator_routing_request,
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestCacheCheckpoint,
    ProviderSubrequestCacheError,
    ProviderSubrequestCacheFailureReason,
    ProviderSubrequestInstrumentIdentity,
    ProviderSubrequestKey,
    ProviderSubrequestResult,
)
from tradingagents.dataflows.qualified_production_sources import (
    AKSHARE_FACTOR_NORMALIZER_VERSION,
    AKSHARE_INDICATOR_NORMALIZER_VERSION,
    AKSHARE_NAME_NORMALIZER_VERSION,
    YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
    YFINANCE_PROFILE_NORMALIZER_VERSION,
    YFINANCE_STATEMENT_NORMALIZER_VERSION,
    acquire_akshare_factor_payload,
    acquire_akshare_indicator_payload,
    acquire_akshare_name_payload,
    acquire_yahoo_derived_factor_payload,
    acquire_yahoo_profile_payload,
    acquire_yahoo_statement_payload,
    decode_akshare_factor_response,
    decode_akshare_indicator_response,
    decode_akshare_name_response,
    decode_yahoo_company_type_evidence_response,
    decode_yahoo_derived_factor_response,
    decode_yahoo_profile_response,
    decode_yahoo_statement_response,
)
from tradingagents.dataflows.tushare_factors_names import (
    TushareAdjustmentFactorAdapter,
    TushareNameEventAdapter,
)
from tradingagents.dataflows.tushare_indicators import TushareFinancialIndicatorAdapter
from tradingagents.dataflows.tushare_statements import TushareStatementAdapter
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceState,
    InstrumentKind,
)
from tradingagents.market_history.config import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
)
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptNotMade,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestPriority,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleDiagnostic,
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
    ProductionQualifiedRuntimeOwner,
)
from tradingagents.qualified_routing_provider_clients import (
    ProductionBaoStockRunTransport,
    production_baostock_process_transport,
    production_module_run_facade,
    production_tushare_run_client,
)

PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION = (
    "production-qualified-routing-composition-factory-v1"
)
PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION = (
    "production-qualified-routing-composition-blueprint-v1"
)

_BLUEPRINT_IDENTITY_PREFIX = "production-qualified-routing-blueprint:v1:"
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_IDENTITY_PATTERN = r"^[a-z][a-z0-9._:=-]*$"
_SOURCE_SECRET_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)
_REQUIRED_DEPENDENCY_IDS = ("akshare", "baostock", "tushare", "yfinance")
_PRODUCTION_PROVIDER_SOURCE_IDS = frozenset(
    {
        "akshare",
        "akshare_sina",
        "baostock",
        "baostock_qualified_families",
        "tushare",
        "yfinance",
        "yfinance_derived",
    }
)
_CONFIG_FIELDS = (
    "mainland_capability_routing_mode",
    "mainland_capability_routing_plan_signature",
    "tushare_enabled_capabilities",
    "tushare_qualification_profile",
    "asset_configuration_signature",
    "data_usage_mode",
    "tushare_account_scope_label",
    "tushare_calls_per_minute",
    "tushare_operator_safety_ceiling_calls_per_minute",
    "data_cache_dir",
    "market_history_database_path",
    "market_history_payload_root",
    "market_history_backup_root",
    "results_dir",
)
_ROOT_FIELDS = (
    ("runtime", "data_cache_dir", "qualified-runtime-root:v1:", False),
    (
        "database",
        "market_history_database_path",
        "market-history-database:v1:",
        True,
    ),
    (
        "provider_artifacts",
        "market_history_payload_root",
        "provider-artifact-root:v1:",
        False,
    ),
    (
        "backups",
        "market_history_backup_root",
        "market-history-backup-root:v1:",
        False,
    ),
    ("reports", "results_dir", "qualified-report-root:v1:", False),
)


class ProductionRuntimeRootIdentity(BaseModel):
    """Machine-independent identity for one validated deployment-local root."""

    model_config = _CLOSED_MODEL_CONFIG

    role: Literal[
        "runtime",
        "database",
        "provider_artifacts",
        "backups",
        "reports",
    ]
    identity: str = Field(pattern=_SAFE_IDENTITY_PATTERN)


class ProductionQualifiedRoutingFactoryContract(BaseModel):
    """Closed safe material accepted by the shipped phase-one factory."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "production-qualified-routing-composition-factory-v1"
    ] = PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION
    validation_intent: Literal["qualified_validation"] = "qualified_validation"
    mode: Literal[MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW]
    disposition: Literal[
        MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    ]
    plan_signature: str = Field(pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$")
    asset_configuration_signature: str = Field(
        pattern=r"^asset-config:v1:[0-9a-f]{64}$"
    )
    asset_configuration_version: Literal["1.0"]
    instrument_symbol: str = Field(pattern=r"^[0-9]{6}\.(SS|SZ)$")
    reference_market: Literal["XSHG", "XSHE"]
    instrument_kind: Literal[InstrumentKind.EQUITY]
    currency: Literal["CNY"]
    registry_id: str = Field(pattern=_SAFE_IDENTITY_PATTERN)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_profile_identity: str = Field(pattern=_SAFE_IDENTITY_PATTERN)
    adjustment_basis: str = Field(pattern=_SAFE_IDENTITY_PATTERN)
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    data_usage_mode: Literal[DataUsageMode.PERSONAL_RESEARCH]
    qualification_profile: Literal["cn-a-2000-20260729-v1"]
    enabled_capabilities: tuple[TushareCapability, ...] = Field(min_length=1)
    required_provider_source_ids: tuple[str, ...] = Field(min_length=1)
    dependency_ids: tuple[str, ...] = Field(min_length=1)
    credential_boundary_identity: Literal[
        "tushare-environment-boundary:v1"
    ] = "tushare-environment-boundary:v1"
    account_scope_label: str = Field(pattern=_SAFE_IDENTITY_PATTERN)
    endpoint_pacing_identities: tuple[TushareEndpointPacingIdentity, ...]
    runtime_root_identities: tuple[ProductionRuntimeRootIdentity, ...]

    @model_validator(mode="after")
    def _validate_canonical_contract(
        self,
    ) -> ProductionQualifiedRoutingFactoryContract:
        canonical_capabilities = tuple(
            capability
            for capability in TushareCapability
            if capability in set(self.enabled_capabilities)
        )
        if self.enabled_capabilities != canonical_capabilities:
            raise ValueError("factory enabled capabilities are not canonical")
        if self.required_provider_source_ids != tuple(
            sorted(set(self.required_provider_source_ids))
        ) or any(
            re.fullmatch(_SAFE_IDENTITY_PATTERN, source_id) is None
            for source_id in self.required_provider_source_ids
        ):
            raise ValueError("factory provider source coverage is not canonical")
        if self.dependency_ids != _REQUIRED_DEPENDENCY_IDS:
            raise ValueError("factory dependency inventory is not canonical")
        endpoint_ids = tuple(
            identity.endpoint_id for identity in self.endpoint_pacing_identities
        )
        if endpoint_ids != tuple(sorted(set(endpoint_ids))):
            raise ValueError("factory endpoint pacing inventory is not canonical")
        expected_root_roles = tuple(role for role, *_rest in _ROOT_FIELDS)
        actual_root_roles = tuple(root.role for root in self.runtime_root_identities)
        if actual_root_roles != expected_root_roles or any(
            re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", root.identity)
            is None
            for root, (_role, _key, prefix, _is_database) in zip(
                self.runtime_root_identities,
                _ROOT_FIELDS,
                strict=True,
            )
        ):
            raise ValueError("factory runtime-root inventory is not canonical")
        return self


class ProductionQualifiedRoutingCompositionBlueprint(BaseModel):
    """Immutable, secret-free plan for a later run-owned composition."""

    model_config = _CLOSED_MODEL_CONFIG

    blueprint_version: Literal[
        "production-qualified-routing-composition-blueprint-v1"
    ] = PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION
    factory_contract: ProductionQualifiedRoutingFactoryContract
    routing_plan: MainlandCapabilityRoutingPlan = Field(repr=False)
    blueprint_identity: str = Field(
        pattern=r"^production-qualified-routing-blueprint:v1:[0-9a-f]{64}$"
    )
    runtime_root_path: Path = Field(exclude=True, repr=False)
    database_path: Path = Field(exclude=True, repr=False)
    provider_artifact_root_path: Path = Field(exclude=True, repr=False)
    backup_root_path: Path = Field(exclude=True, repr=False)
    report_root_path: Path = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def _validate_bound_blueprint(
        self,
    ) -> ProductionQualifiedRoutingCompositionBlueprint:
        contract = self.factory_contract
        if (
            contract.plan_signature != self.routing_plan.plan_signature
            or contract.mode is not self.routing_plan.mode
            or contract.enabled_capabilities
            != self.routing_plan.enabled_tushare_capabilities
            or contract.financial_eligibility_binding
            != self.routing_plan.financial_eligibility_binding
            or contract.account_scope_label != self.routing_plan.account_scope_label
            or contract.endpoint_pacing_identities
            != self.routing_plan.endpoint_pacing_identities
        ):
            raise ValueError("blueprint routing plan binding is invalid")
        root_paths = (
            self.runtime_root_path,
            self.database_path,
            self.provider_artifact_root_path,
            self.backup_root_path,
            self.report_root_path,
        )
        if any(
            not path.is_absolute()
            or "?" in str(path)
            or "://" in str(path)
            or any(part == ".." for part in path.parts)
            for path in root_paths
        ) or self.database_path.suffix.casefold() != ".sqlite3":
            raise ValueError("blueprint runtime-root binding is invalid")
        identity_payload = {
            "blueprint_version": self.blueprint_version,
            "factory_contract": contract.model_dump(mode="json"),
        }
        expected_identity = (
            f"{_BLUEPRINT_IDENTITY_PREFIX}{_canonical_digest(identity_payload)}"
        )
        if self.blueprint_identity != expected_identity:
            raise ValueError("blueprint identity mismatch")
        return self

    @property
    def factory_contract_version(self) -> str:
        return self.factory_contract.contract_version

    @property
    def mode(self) -> MainlandCapabilityRoutingMode:
        return self.factory_contract.mode

    @property
    def validation_intent(self) -> str:
        return self.factory_contract.validation_intent

    @property
    def disposition(self) -> MainlandCapabilityRoutingDisposition:
        return self.factory_contract.disposition

    @property
    def enabled_capabilities(self) -> tuple[TushareCapability, ...]:
        return self.factory_contract.enabled_capabilities

    @property
    def plan_signature(self) -> str:
        return self.factory_contract.plan_signature

    @property
    def asset_configuration_signature(self) -> str:
        return self.factory_contract.asset_configuration_signature


class ManagedQualifiedRoutingComposition(Protocol):
    """Single owner-capable boundary for one qualified analysis lifetime."""

    blueprint: ProductionQualifiedRoutingCompositionBlueprint

    def bind_run(self, run_binding: object) -> object: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


def _validated_retained_financial_metadata(
    company_type_evidence: Sequence[object],
    listing_metadata: Sequence[object],
) -> tuple[
    tuple[FinancialCompanyTypeEvidenceProjection, ...],
    tuple[FinancialListingMetadataProjection, ...],
]:
    try:
        return (
            tuple(
                FinancialCompanyTypeEvidenceProjection.model_validate(value)
                for value in company_type_evidence
            ),
            tuple(
                FinancialListingMetadataProjection.model_validate(value)
                for value in listing_metadata
            ),
        )
    except (AttributeError, TypeError, ValueError):
        raise ProductionQualifiedRoutingLifecycleError(
            ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
        ) from None


@dataclass(frozen=True, slots=True)
class ProductionFinancialEligibilityRuntimeInputs:
    """Validated one-way handoff from managed composition to the run host."""

    financial_routing: object = field(repr=False, compare=False)
    metadata_acquirer: object = field(repr=False, compare=False)
    retained_company_type_evidence: tuple[
        FinancialCompanyTypeEvidenceProjection, ...
    ] = field(default=(), repr=False, compare=False)
    retained_listing_metadata: tuple[FinancialListingMetadataProjection, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.financial_routing is None or not callable(
            getattr(self.metadata_acquirer, "acquire", None)
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        company_type_evidence, listing_metadata = (
            _validated_retained_financial_metadata(
                self.retained_company_type_evidence,
                self.retained_listing_metadata,
            )
        )
        object.__setattr__(
            self,
            "retained_company_type_evidence",
            company_type_evidence,
        )
        object.__setattr__(self, "retained_listing_metadata", listing_metadata)


class ProductionQualifiedRoutingRunBinding(BaseModel):
    """Canonical post-checkpoint inputs required before run resources may open."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    run_id: str = Field(pattern=r"^run:[0-9a-f]{64}$")
    asset_configuration: RunAssetConfiguration = Field(repr=False)
    routing_plan: MainlandCapabilityRoutingPlan = Field(repr=False)
    checkpoint_compatibility_known: Literal[True]
    resume_from_checkpoint: bool = False
    checkpoint_thread_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
    )
    checkpoint_state: Mapping[str, Any] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    restore_financial_dispatcher: Callable[..., object] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    install_financial_eligibility_runtime: Callable[[object], object] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    restore_financial_eligibility_context: (
        Callable[[object], object] | None
    ) = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _validate_authoritative_bindings(self) -> ProductionQualifiedRoutingRunBinding:
        if (
            self.asset_configuration.asset_configuration_signature
            == ""
            or self.routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
        ):
            raise ValueError("qualified runtime binding is invalid")
        if self.checkpoint_thread_id is not None and not self.resume_from_checkpoint:
            expected_run_id = "run:" + sha256(
                f"checkpoint:{self.checkpoint_thread_id}".encode()
            ).hexdigest()
            if self.run_id != expected_run_id:
                raise ValueError("qualified checkpoint run identity is invalid")
        if self.resume_from_checkpoint:
            if (
                self.checkpoint_thread_id is None
                or not isinstance(self.checkpoint_state, Mapping)
                or not callable(self.restore_financial_dispatcher)
                or self.install_financial_eligibility_runtime is not None
                or not callable(self.restore_financial_eligibility_context)
            ):
                raise ValueError("qualified checkpoint restoration binding is invalid")
        elif (
            self.checkpoint_state is not None
            or self.restore_financial_eligibility_context is not None
        ):
            raise ValueError("fresh qualified binding cannot carry checkpoint state")
        return self


@dataclass(frozen=True, slots=True)
class ProductionQualifiedRoutingRuntimeAssembly:
    """Ticket-02 graph wiring plus the run-owned objects it created."""

    financial_routing: object = field(repr=False, compare=False)
    owned_resources: tuple[tuple[str, object], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    source_composition: object | None = field(default=None, repr=False, compare=False)
    dependencies: object | None = field(default=None, repr=False, compare=False)
    eligibility_metadata_acquirer: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    retained_company_type_evidence: tuple[FinancialCompanyTypeEvidenceProjection, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    retained_listing_metadata: tuple[FinancialListingMetadataProjection, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        company_type_evidence, listing_metadata = (
            _validated_retained_financial_metadata(
                self.retained_company_type_evidence,
                self.retained_listing_metadata,
            )
        )
        if any(
            not _safe_source_token(resource_kind) or resource is None
            for resource_kind, resource in self.owned_resources
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        object.__setattr__(
            self,
            "retained_company_type_evidence",
            company_type_evidence,
        )
        object.__setattr__(self, "retained_listing_metadata", listing_metadata)


def _noop_runtime_phase_hook(_phase: str, _value: object) -> None:
    return None


@dataclass(frozen=True, slots=True)
class ProductionQualifiedRoutingRuntimeFactories:
    """Composition-local acquisition seams for deterministic run binding."""

    open_coordinator_store: Callable[[object], object] = field(repr=False)
    build_coordinator: Callable[[object, threading.Event], object] = field(repr=False)
    build_artifact_store: Callable[[object], object] = field(repr=False)
    build_subrequest_cache: Callable[
        [object, object, str, threading.Event], object
    ] = field(repr=False)
    build_tushare_client: Callable[[object, threading.Event], object] = field(
        repr=False
    )
    build_akshare_facade: Callable[[object, threading.Event], object] = field(
        repr=False
    )
    build_yahoo_transport: Callable[[object, threading.Event], object] = field(
        repr=False
    )
    build_baostock_transport: Callable[[object, threading.Event], object] = field(
        repr=False
    )
    assemble_runtime: Callable[..., ProductionQualifiedRoutingRuntimeAssembly] = field(
        repr=False
    )
    phase_hook: Callable[[str, object], None] = field(
        default=_noop_runtime_phase_hook,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        callbacks = (
            self.open_coordinator_store,
            self.build_coordinator,
            self.build_artifact_store,
            self.build_subrequest_cache,
            self.build_tushare_client,
            self.build_akshare_facade,
            self.build_yahoo_transport,
            self.build_baostock_transport,
            self.assemble_runtime,
            self.phase_hook,
        )
        if any(not callable(callback) for callback in callbacks):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )


def production_qualified_routing_runtime_factories(
) -> ProductionQualifiedRoutingRuntimeFactories:
    """Return lazy production acquisitions without opening any run resource."""

    def runtime_blueprint(value: object) -> ProductionQualifiedRoutingCompositionBlueprint:
        if not isinstance(value, ProductionQualifiedRoutingCompositionBlueprint):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        return value

    def open_store(value: object) -> MarketHistoryStore:
        blueprint = runtime_blueprint(value)
        config = MarketHistoryConfig(
            mode=MarketHistoryMode.SHADOW,
            database_path=blueprint.database_path,
            payload_root=blueprint.provider_artifact_root_path,
            backup_root=blueprint.backup_root_path,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        )
        try:
            return MarketHistoryStore.open_provider_request_authority(config)
        except ProductionQualifiedRoutingLifecycleError:
            raise
        except Exception:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            ) from None

    def build_coordinator(
        store: object,
        stop_signal: threading.Event,
    ) -> ProviderRequestCoordinator:
        if not isinstance(store, MarketHistoryStore):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        try:
            coordinator = ProviderRequestCoordinator(store, stop_signal=stop_signal)
            contract = runtime_blueprint_value.factory_contract
            services = (
                ("akshare_sina", ""),
                ("baostock", ""),
                ("tushare", contract.account_scope_label),
                ("yfinance", ""),
            )
            for provider_id, account_scope in services:
                service_id, service_name = upstream_service_identity_for_provider(
                    provider_id,
                    account_scope=account_scope,
                )
                coordinator.register_upstream_service(
                    service_id,
                    service_name,
                    account_scope=account_scope,
                )
            tushare_service_id, _ = upstream_service_identity_for_provider(
                "tushare",
                account_scope=contract.account_scope_label,
            )
            coordinator.configure_operator_ceiling(
                upstream_service_id=tushare_service_id,
                minimum_interval=timedelta(
                    seconds=60.0
                    / min(
                        identity.operator_calls_per_minute
                        for identity in contract.endpoint_pacing_identities
                    )
                ),
                allow_prewarming=False,
                configured_at=datetime.now(timezone.utc),
            )
            return coordinator
        except ProductionQualifiedRoutingLifecycleError:
            raise
        except Exception:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            ) from None

    def build_artifact_store(value: object) -> ProviderSubrequestArtifactStore:
        blueprint = runtime_blueprint(value)
        try:
            return ProviderSubrequestArtifactStore(
                blueprint.provider_artifact_root_path
                / "qualified-provider-subrequests"
            )
        except Exception:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            ) from None

    def build_cache(
        coordinator: object,
        artifact_store: object,
        run_id: str,
        stop_signal: threading.Event,
    ) -> ProviderSubrequestCache:
        if not isinstance(coordinator, ProviderRequestCoordinator) or not isinstance(
            artifact_store,
            ProviderSubrequestArtifactStore,
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        try:
            return ProviderSubrequestCache(
                coordinator=coordinator,
                artifact_store=artifact_store,
                run_scope_id=run_id,
                stop_signal=stop_signal,
            )
        except Exception:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            ) from None

    runtime_blueprint_value: ProductionQualifiedRoutingCompositionBlueprint

    def remember_blueprint(value: object) -> MarketHistoryStore:
        nonlocal runtime_blueprint_value
        runtime_blueprint_value = runtime_blueprint(value)
        return open_store(runtime_blueprint_value)

    def build_tushare(
        value: object,
        stop_signal: threading.Event,
    ) -> object:
        runtime_blueprint(value)
        return production_tushare_run_client(stop_signal)

    def build_module_facade(
        provider_id: str,
        module_name: str,
        value: object,
        stop_signal: threading.Event,
    ) -> object:
        runtime_blueprint(value)
        return production_module_run_facade(
            provider_id=provider_id,
            module_name=module_name,
            stop_signal=stop_signal,
        )

    def build_baostock(
        value: object,
        stop_signal: threading.Event,
    ) -> object:
        runtime_blueprint(value)
        return production_baostock_process_transport(stop_signal)

    return ProductionQualifiedRoutingRuntimeFactories(
        open_coordinator_store=remember_blueprint,
        build_coordinator=build_coordinator,
        build_artifact_store=build_artifact_store,
        build_subrequest_cache=build_cache,
        build_tushare_client=build_tushare,
        build_akshare_facade=lambda value, stop_signal: build_module_facade(
            "akshare",
            "akshare",
            value,
            stop_signal,
        ),
        build_yahoo_transport=lambda value, stop_signal: build_module_facade(
            "yfinance",
            "yfinance",
            value,
            stop_signal,
        ),
        build_baostock_transport=build_baostock,
        assemble_runtime=_assemble_production_qualified_runtime,
    )


@dataclass(frozen=True, slots=True)
class ProductionQualifiedRoutingRuntime:
    """Safe public handle for one completely bound run-owned composition."""

    run_id: str
    financial_routing: object | None = field(repr=False, compare=False)
    stop_signal: threading.Event = field(repr=False, compare=False)
    resource_kinds: tuple[str, ...]
    source_composition: object | None = field(default=None, repr=False, compare=False)
    dependencies: object | None = field(default=None, repr=False, compare=False)
    def _require_financial_routing(self) -> object:
        if self.financial_routing is None:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
            )
        return self.financial_routing

    @property
    def router(self) -> object:
        return self._require_financial_routing().router

    @property
    def statement_request_factory(self) -> object:
        return self._require_financial_routing().statement_request_factory

    @property
    def indicator_request_factory(self) -> object:
        return self._require_financial_routing().indicator_request_factory

    def __repr__(self) -> str:
        return (
            "ProductionQualifiedRoutingRuntime("
            f"run_id={self.run_id!r}, resource_kinds={self.resource_kinds!r})"
        )


@dataclass(frozen=True, slots=True)
class _QualifiedCheckpointRestoration:
    context: FinancialEligibilityAcquisitionContext
    shadow_ledger: FinancialDispatchCheckpointLedger
    cache_checkpoint: ProviderSubrequestCacheCheckpoint


def _physical_provider_id(route_provider_id: str) -> str:
    return {
        "akshare": "akshare_sina",
        "baostock_qualified_families": "baostock",
        "yfinance_derived": "yfinance",
    }.get(route_provider_id, route_provider_id)


def _cache_normalizer_is_compatible(
    key: ProviderSubrequestKey,
    plan: MainlandCapabilityRoutingPlan,
) -> bool:
    if key.provider_id == "tushare":
        return key.normalizer_version == plan.normalizer_version
    if key.provider_id == "baostock":
        return key.normalizer_version in {
            BAOSTOCK_CAPABILITY_NORMALIZER_VERSION,
            BAOSTOCK_RATIO_NORMALIZER_VERSION,
        }
    if key.provider_id == "akshare_sina":
        external_normalizer = {
            "adjustment_factors": AKSHARE_FACTOR_NORMALIZER_VERSION,
            "financial_indicators": AKSHARE_INDICATOR_NORMALIZER_VERSION,
            "issuer_name_events": AKSHARE_NAME_NORMALIZER_VERSION,
        }.get(key.capacity_scope)
        return key.normalizer_version in {
            plan.normalizer_version,
            external_normalizer,
            f"{key.capacity_scope}-terminal-v1",
        }
    if key.provider_id == "yfinance":
        external_normalizer = {
            "current_financial_profile": YFINANCE_PROFILE_NORMALIZER_VERSION,
            "derived_adjustment_factors": (
                YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION
            ),
            "financial_statements": YFINANCE_STATEMENT_NORMALIZER_VERSION,
        }.get(key.capacity_scope)
        return key.normalizer_version in {
            external_normalizer,
            f"{key.capacity_scope}-terminal-v1",
        }
    return False


def _validate_cache_provider_calls(
    ledger: FinancialDispatchCheckpointLedger,
    cache_checkpoint: ProviderSubrequestCacheCheckpoint,
    plan: MainlandCapabilityRoutingPlan,
) -> None:
    cache_entries = {
        entry.key.subrequest_key: entry for entry in cache_checkpoint.entries
    }
    allowed_logical_providers = {
        provider_id
        for route in plan.routes
        for provider_id in route.providers
    }
    selections = (
        *ledger.qualified_statement_selections,
        *ledger.qualified_indicator_selections,
    )
    for selection in selections:
        for call in selection.terminal.routing_result.provider_calls:
            if (
                call.provider_id not in allowed_logical_providers
                or len(call.subrequest_keys) != len(set(call.subrequest_keys))
                or len(call.physical_attempt_ids)
                != len(set(call.physical_attempt_ids))
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_CHAIN_MISMATCH
                )
            try:
                entries = tuple(
                    cache_entries[subrequest_key]
                    for subrequest_key in call.subrequest_keys
                )
            except KeyError as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                ) from exc
            expected_physical_provider = _physical_provider_id(call.provider_id)
            expected_events = {
                event.attempt_event_id: event
                for entry in entries
                for event in entry.attempt_events
            }
            actual_events = {
                event.attempt_event_id: event
                for event in call.physical_attempt_events
            }
            if (
                any(
                    entry.key.provider_id != expected_physical_provider
                    for entry in entries
                )
                or set(call.physical_attempt_ids) != set(expected_events)
                or actual_events != expected_events
                or any(
                    outcome.provider_id != call.provider_id
                    or outcome.subrequest_key not in call.subrequest_keys
                    for outcome in call.subrequest_outcomes
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                )


def _restore_checkpoint_eligibility_context(
    state: Mapping[str, Any],
    run_binding: ProductionQualifiedRoutingRunBinding,
) -> FinancialEligibilityAcquisitionContext:
    raw_shadow_ledger = state.get("financial_dispatch_shadow_ledger")
    if not isinstance(raw_shadow_ledger, Mapping):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
        )
    raw_ledger_version = raw_shadow_ledger.get("contract_version")
    if raw_ledger_version != FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
            if raw_ledger_version == FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION
            else FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_VERSION_UNSUPPORTED
        )
    raw_context = raw_shadow_ledger.get("financial_eligibility_context")
    if raw_context is None:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
        )
    if not isinstance(raw_context, Mapping):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_INVALID
        )
    if raw_context.get("contract_version") != (
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_VERSION_UNSUPPORTED
        )
    raw_identity = raw_context.get("context_identity")
    try:
        identity_material = dict(raw_context)
        identity_material.pop("context_identity", None)
        expected_identity = financial_eligibility_acquisition_context_identity(
            identity_material
        )
    except (TypeError, ValueError) as exc:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_INVALID
        ) from exc
    if raw_identity != expected_identity:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_IDENTITY_MISMATCH
        )
    try:
        context = FinancialEligibilityAcquisitionContext.model_validate(raw_context)
    except (TypeError, ValueError) as exc:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_INVALID
        ) from exc
    checkpoint_thread_id = run_binding.checkpoint_thread_id
    trade_date = state.get("trade_date")
    if (
        checkpoint_thread_id is None
        or raw_shadow_ledger.get("checkpoint_thread_id") != checkpoint_thread_id
        or trade_date != context.as_of_date.isoformat()
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_BINDING_MISMATCH
        )
    try:
        validate_financial_eligibility_acquisition_context_binding(
            context,
            canonical_run_id=run_binding.run_id,
            asset_configuration_signature=(
                run_binding.asset_configuration.asset_configuration_signature
            ),
            instrument_identity=run_binding.asset_configuration.instrument_identity,
            as_of_date=context.as_of_date,
            availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
            acquisition_purpose="foreground_current_analysis",
            financial_eligibility_binding=(
                run_binding.routing_plan.financial_eligibility_binding
            ),
            routing_plan_signature=run_binding.routing_plan.plan_signature,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        )
    except ValueError as exc:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_BINDING_MISMATCH
        ) from exc
    return context


def _validated_checkpoint_restoration(
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    run_binding: ProductionQualifiedRoutingRunBinding,
) -> _QualifiedCheckpointRestoration | None:
    """Authenticate the complete qualified projection before resources open."""

    if not run_binding.resume_from_checkpoint:
        return None
    state = run_binding.checkpoint_state
    if not isinstance(state, Mapping):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.MALFORMED
        )
    checkpoint_thread_id = run_binding.checkpoint_thread_id
    expected_run_id = (
        "run:"
        + sha256(f"checkpoint:{checkpoint_thread_id}".encode()).hexdigest()
        if checkpoint_thread_id is not None
        else None
    )
    if expected_run_id != run_binding.run_id:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
        )
    context = _restore_checkpoint_eligibility_context(state, run_binding)
    try:
        expected_asset = RunAssetConfigurationProjection.model_validate(
            run_binding.asset_configuration.model_dump(mode="json")
        )
        restored_asset = RunAssetConfigurationProjection.model_validate(
            state.get("asset_configuration")
        )
        restored_plan = MainlandCapabilityRoutingPlan.model_validate(
            state.get("capability_routing_plan")
        )
        restored_rollout = MainlandCapabilityRoutingRunProjection.model_validate(
            state.get("capability_routing_rollout")
        )
        restored_binding = (
            MainlandFinancialEligibilityProjectionBinding.model_validate(
                state.get("financial_eligibility_binding")
            )
        )
        evidence = EvidenceState.model_validate(state.get("evidence_state"))
        authoritative_ledger = FinancialDispatchCheckpointLedger.model_validate(
            state.get("financial_dispatch_ledger")
        )
        shadow_ledger = FinancialDispatchCheckpointLedger.model_validate(
            state.get("financial_dispatch_shadow_ledger")
        )
    except FinancialDispatchCheckpointError:
        raise
    except (TypeError, ValueError) as exc:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.MALFORMED
        ) from exc

    plan = run_binding.routing_plan
    expected_binding = plan.financial_eligibility_binding
    expected_rollout = MainlandCapabilityRoutingRunProjection.from_plan(plan)
    if restored_asset != expected_asset:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
        )
    if (
        evidence.instrument_identity
        != run_binding.asset_configuration.instrument_identity
        or evidence.instrument_identity is None
        or ProviderSubrequestInstrumentIdentity.from_authoritative(
            evidence.instrument_identity
        )
        != ProviderSubrequestInstrumentIdentity.from_authoritative(
            run_binding.asset_configuration.instrument_identity
        )
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
        )
    if (
        restored_plan != plan
        or restored_plan != blueprint.routing_plan
        or restored_rollout != expected_rollout
        or restored_rollout.disposition
        is not MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
        )
    if (
        expected_binding is None
        or restored_binding != expected_binding
        or evidence.financial_eligibility_binding != expected_binding
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
        )
    if state.get("run_id") != run_binding.run_id:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
        )
    if (
        authoritative_ledger.contract_version != FINANCIAL_DISPATCH_LEDGER_VERSION
        or authoritative_ledger.run_asset_configuration_version
        != expected_asset.asset_configuration_version
        or authoritative_ledger.run_asset_configuration_signature
        != expected_asset.asset_configuration_signature
        or (
            authoritative_ledger.provider_subrequest_cache is not None
            and authoritative_ledger.provider_subrequest_cache
            != shadow_ledger.provider_subrequest_cache
        )
        or authoritative_ledger.qualified_statement_selections
        or authoritative_ledger.qualified_indicator_selections
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.PROVIDER_CHAIN_MISMATCH
        )
    cache_checkpoint = shadow_ledger.provider_subrequest_cache
    expected_context_binding = FinancialEligibilityRequestBinding.from_context(
        context
    )
    selection_context_bindings = tuple(
        selection.terminal.selection_request.financial_eligibility_context_binding
        for selection in (
            *shadow_ledger.qualified_statement_selections,
            *shadow_ledger.qualified_indicator_selections,
        )
    )
    if (
        shadow_ledger.contract_version
        != FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION
        or shadow_ledger.run_asset_configuration_version
        != expected_asset.asset_configuration_version
        or shadow_ledger.run_asset_configuration_signature
        != expected_asset.asset_configuration_signature
        or shadow_ledger.capability_routing_plan_signature != plan.plan_signature
        or shadow_ledger.financial_manifest_version != FINANCIAL_MANIFEST_V2
        or shadow_ledger.financial_eligibility_binding != expected_binding
        or shadow_ledger.run_scope_id != run_binding.run_id
        or cache_checkpoint is None
        or cache_checkpoint.run_scope_id != run_binding.run_id
        or shadow_ledger.checkpoint_thread_id != checkpoint_thread_id
        or shadow_ledger.financial_eligibility_context != context
        or not selection_context_bindings
        or any(
            binding != expected_context_binding
            for binding in selection_context_bindings
        )
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_DISPATCHER_MISMATCH
        )
    if (
        cache_checkpoint.contract_version != "1.1"
        or cache_checkpoint.financial_eligibility_context_contract_version
        != context.contract_version
        or cache_checkpoint.financial_eligibility_context_identity
        != context.context_identity
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_CACHE_MISMATCH
        )

    expected_identity = ProviderSubrequestInstrumentIdentity.from_authoritative(
        run_binding.asset_configuration.instrument_identity
    )
    allowed_physical_providers = {
        _physical_provider_id(provider_id)
        for route in plan.routes
        for provider_id in route.providers
    }
    for entry in cache_checkpoint.entries:
        key = entry.key
        expected_account_scope = (
            plan.account_scope_label
            if key.provider_id == "tushare"
            else "not-applicable"
            if key.provider_id == "baostock"
            else "public"
        )
        expected_upstream_id, _ = upstream_service_identity_for_provider(
            key.provider_id,
            account_scope=(
                expected_account_scope if key.provider_id == "tushare" else ""
            ),
        )
        if (
            key.provider_id not in allowed_physical_providers
            or key.instrument_identity != expected_identity
            or key.canonical_symbol != expected_identity.canonical_symbol
            or key.account_scope != expected_account_scope
            or key.upstream_service_id != expected_upstream_id
            or key.qualification_profile != plan.qualification_profile
            or key.data_usage_mode is not blueprint.factory_contract.data_usage_mode
            or not _cache_normalizer_is_compatible(key, plan)
            or (
                entry.artifact is not None
                and entry.artifact.media_type != "application/json"
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
            )
    _validate_cache_provider_calls(shadow_ledger, cache_checkpoint, plan)
    return _QualifiedCheckpointRestoration(
        context=context,
        shadow_ledger=shadow_ledger,
        cache_checkpoint=cache_checkpoint,
    )


@dataclass(frozen=True, slots=True)
class RegisteredProductionQualifiedRoutingComposition:
    """Resource-free blueprint that becomes the sole owner when a run binds."""

    blueprint: ProductionQualifiedRoutingCompositionBlueprint
    runtime_factories: ProductionQualifiedRoutingRuntimeFactories | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _state_lock: threading.RLock = field(
        init=False,
        repr=False,
        compare=False,
        default_factory=threading.RLock,
    )
    _runtime: ProductionQualifiedRoutingRuntime | None = field(
        init=False,
        repr=False,
        compare=False,
        default=None,
    )
    _owner: ProductionQualifiedRuntimeOwner | None = field(
        init=False,
        repr=False,
        compare=False,
        default=None,
    )
    _closed: bool = field(init=False, repr=False, compare=False, default=False)

    def __post_init__(self) -> None:
        if not isinstance(
            self.blueprint,
            ProductionQualifiedRoutingCompositionBlueprint,
        ):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
            )
        factories = self.runtime_factories
        if factories is None:
            factories = production_qualified_routing_runtime_factories()
            object.__setattr__(self, "runtime_factories", factories)
        if not isinstance(factories, ProductionQualifiedRoutingRuntimeFactories):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )

    @property
    def factory_identity(self) -> str:
        return self.blueprint.factory_contract_version

    def bind_run(
        self,
        run_binding: ProductionQualifiedRoutingAssemblyBinding
        | ProductionQualifiedRoutingRunBinding,
    ) -> object:
        """Bind injected Ticket-02 wiring or one managed canonical run runtime."""

        if isinstance(run_binding, ProductionQualifiedRoutingAssemblyBinding):
            return self._bind_injected_assembly(run_binding)
        if not isinstance(run_binding, ProductionQualifiedRoutingRunBinding):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
            )
        self._validate_run_binding(run_binding)
        with self._state_lock:
            if self._closed:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.CLOSED
                )
            if self._owner is not None and self._owner.interrupted:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
                )
            if self._runtime is not None:
                if self._runtime.run_id == run_binding.run_id:
                    return self._runtime
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.DIFFERENT_RUN_ACTIVE
                )
            owner = ProductionQualifiedRuntimeOwner()
            object.__setattr__(self, "_owner", owner)
            try:
                runtime = self._construct_runtime(run_binding, owner)
            except BaseException:
                owner.interrupt()
                owner.close()
                object.__setattr__(self, "_closed", True)
                raise
            object.__setattr__(self, "_runtime", runtime)
            return runtime

    def _bind_injected_assembly(
        self,
        run_binding: ProductionQualifiedRoutingAssemblyBinding,
    ) -> object:
        source_composition = assemble_production_qualified_provider_sources(
            blueprint=self.blueprint,
            dependencies=run_binding.dependencies,
            source_registrations=run_binding.source_registrations,
        )
        from tradingagents.graph.financial_tools import (
            QualifiedFinancialRoutingComposition,
        )

        try:
            return QualifiedFinancialRoutingComposition(
                router=source_composition.router,
                statement_request_factory=run_binding.statement_request_factory,
                indicator_request_factory=run_binding.indicator_request_factory,
            )
        except (AttributeError, TypeError, ValueError):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
            )

    def _validate_run_binding(
        self,
        run_binding: ProductionQualifiedRoutingRunBinding,
    ) -> None:
        if (
            run_binding.routing_plan != self.blueprint.routing_plan
            or run_binding.routing_plan.plan_signature != self.blueprint.plan_signature
            or run_binding.asset_configuration.asset_configuration_signature
            != self.blueprint.asset_configuration_signature
            or run_binding.routing_plan.financial_eligibility_binding
            != self.blueprint.factory_contract.financial_eligibility_binding
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )

    def _construct_runtime(
        self,
        run_binding: ProductionQualifiedRoutingRunBinding,
        owner: ProductionQualifiedRuntimeOwner,
    ) -> ProductionQualifiedRoutingRuntime:
        factories = self.runtime_factories
        assert factories is not None
        restoration = _validated_checkpoint_restoration(self.blueprint, run_binding)
        restored_context_owner = None
        if restoration is not None:
            restore_context = run_binding.restore_financial_eligibility_context
            assert restore_context is not None
            restored_context_owner = restore_context(restoration.context)
            if restored_context_owner is None:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
                )
            factories.phase_hook(
                "eligibility_context_restore",
                restoration.context,
            )

        def acquire(resource_kind: str, value: object) -> object:
            owner.own(resource_kind, value)
            factories.phase_hook(resource_kind, value)
            return value

        coordinator_store = acquire(
            "coordinator_store",
            factories.open_coordinator_store(self.blueprint),
        )
        coordinator = acquire(
            "coordinator",
            factories.build_coordinator(coordinator_store, owner.stop_signal),
        )
        artifact_store = acquire(
            "artifact_store",
            factories.build_artifact_store(self.blueprint),
        )
        subrequest_cache = acquire(
            "subrequest_cache",
            factories.build_subrequest_cache(
                coordinator,
                artifact_store,
                run_binding.run_id,
                owner.stop_signal,
            ),
        )
        restored_shadow_dispatcher = None
        if restoration is not None:
            checkpoint_state = run_binding.checkpoint_state
            restore_dispatcher = run_binding.restore_financial_dispatcher
            assert checkpoint_state is not None
            assert restore_dispatcher is not None
            if not isinstance(subrequest_cache, ProviderSubrequestCache):
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
                )
            try:
                subrequest_cache.bind_financial_eligibility_context(
                    context_contract_version=restoration.context.contract_version,
                    context_identity=restoration.context.context_identity,
                )
                subrequest_cache.restore_checkpoint(restoration.cache_checkpoint)
            except ProviderSubrequestCacheError as exc:
                if exc.reason is (
                    ProviderSubrequestCacheFailureReason.CONTEXT_MISMATCH
                ):
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_CACHE_MISMATCH
                    ) from exc
                raise
            factories.phase_hook("subrequest_cache_restore", subrequest_cache)
            restored_shadow_dispatcher = restore_dispatcher(
                checkpoint_state,
                provider_subrequest_cache=subrequest_cache,
                routing_plan=run_binding.routing_plan,
            )
            from tradingagents.dataflows.financial_dispatch import (
                FinancialToolDispatcher,
            )

            if not isinstance(restored_shadow_dispatcher, FinancialToolDispatcher):
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
                )
            if (
                restored_shadow_dispatcher.financial_eligibility_context
                != restoration.context
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_DISPATCHER_MISMATCH
                )
            factories.phase_hook(
                "checkpoint_restore",
                restored_shadow_dispatcher,
            )
        tushare_client = acquire(
            "tushare_client",
            factories.build_tushare_client(self.blueprint, owner.stop_signal),
        )
        akshare_facade = acquire(
            "akshare_facade",
            factories.build_akshare_facade(self.blueprint, owner.stop_signal),
        )
        yahoo_transport = acquire(
            "yahoo_transport",
            factories.build_yahoo_transport(self.blueprint, owner.stop_signal),
        )
        baostock_transport = acquire(
            "baostock_transport",
            factories.build_baostock_transport(self.blueprint, owner.stop_signal),
        )
        assembly = factories.assemble_runtime(
            blueprint=self.blueprint,
            run_binding=run_binding,
            coordinator=coordinator,
            coordinator_store=coordinator_store,
            artifact_store=artifact_store,
            subrequest_cache=subrequest_cache,
            provider_clients=MappingProxyType(
                {
                    "akshare": akshare_facade,
                    "baostock": baostock_transport,
                    "tushare": tushare_client,
                    "yfinance": yahoo_transport,
                }
            ),
            stop_signal=owner.stop_signal,
            acquire_owned_resource=acquire,
        )
        if not isinstance(assembly, ProductionQualifiedRoutingRuntimeAssembly):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        from tradingagents.graph.financial_tools import (
            QualifiedFinancialRoutingComposition,
        )

        if not isinstance(
            assembly.financial_routing,
            QualifiedFinancialRoutingComposition,
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        financial_routing = assembly.financial_routing
        if restored_shadow_dispatcher is not None:
            restored_shadow_dispatcher.bind_qualified_statement_router(
                financial_routing.router
            )
        financial_routing = QualifiedFinancialRoutingComposition(
            router=financial_routing.router,
            statement_request_factory=financial_routing.statement_request_factory,
            indicator_request_factory=financial_routing.indicator_request_factory,
            provider_subrequest_cache=subrequest_cache,
            restored_shadow_dispatcher=restored_shadow_dispatcher,
        )
        for resource_kind, resource in assembly.owned_resources:
            acquire(resource_kind, resource)
        if restored_context_owner is not None:
            install_restored_routing = getattr(
                restored_context_owner,
                "install_restored_financial_routing",
                None,
            )
            if not callable(install_restored_routing):
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
                )
            install_restored_routing(financial_routing)
            factories.phase_hook(
                "eligibility_runtime_exposed",
                restored_context_owner,
            )
        install_eligibility_runtime = (
            run_binding.install_financial_eligibility_runtime
        )
        eligibility_runtime_installed = False
        if (
            install_eligibility_runtime is not None
            and assembly.eligibility_metadata_acquirer is not None
        ):
            eligibility_inputs = ProductionFinancialEligibilityRuntimeInputs(
                financial_routing=financial_routing,
                metadata_acquirer=assembly.eligibility_metadata_acquirer,
                retained_company_type_evidence=(
                    assembly.retained_company_type_evidence
                ),
                retained_listing_metadata=assembly.retained_listing_metadata,
            )
            installed_owner = install_eligibility_runtime(eligibility_inputs)
            if installed_owner is None:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
                )
            eligibility_runtime_installed = True
        return ProductionQualifiedRoutingRuntime(
            run_id=run_binding.run_id,
            financial_routing=(
                None
                if restored_context_owner is not None
                or eligibility_runtime_installed
                else financial_routing
            ),
            stop_signal=owner.stop_signal,
            resource_kinds=owner.resource_kinds,
            source_composition=(
                None
                if eligibility_runtime_installed
                or restored_context_owner is not None
                else assembly.source_composition
            ),
            dependencies=(
                None
                if eligibility_runtime_installed
                or restored_context_owner is not None
                else assembly.dependencies
            ),
        )

    @property
    def runtime(self) -> ProductionQualifiedRoutingRuntime | None:
        with self._state_lock:
            return self._runtime

    @property
    def cleanup_diagnostics(
        self,
    ) -> tuple[ProductionQualifiedRoutingLifecycleDiagnostic, ...]:
        with self._state_lock:
            return self._owner.diagnostics if self._owner is not None else ()

    def interrupt(self) -> None:
        with self._state_lock:
            owner = self._owner
        if owner is not None:
            owner.interrupt()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            object.__setattr__(self, "_closed", True)
            owner = self._owner
        if owner is not None:
            owner.close()


@dataclass(frozen=True, slots=True)
class ProductionQualifiedRoutingAssemblyBinding:
    """Run-owned inputs consumed by Ticket 02 assembly at the binding seam."""

    dependencies: object = field(repr=False, compare=False)
    source_registrations: tuple[ProductionQualifiedSourceRegistration, ...]
    statement_request_factory: Callable[[object], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    indicator_request_factory: Callable[[object], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def build_registered_production_qualified_routing_composition(
    *,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    runtime_factories: ProductionQualifiedRoutingRuntimeFactories | None = None,
) -> RegisteredProductionQualifiedRoutingComposition:
    """Consume one validated blueprint without opening run-owned resources."""

    if not isinstance(blueprint, ProductionQualifiedRoutingCompositionBlueprint):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
        )
    return RegisteredProductionQualifiedRoutingComposition(
        blueprint=blueprint,
        runtime_factories=runtime_factories,
    )


class ProductionQualifiedSourceKind(str, Enum):
    """Public router seam implemented by one plan-named source."""

    STATEMENT = "statement"
    INDICATOR = "indicator"
    CAPABILITY = "capability"
    COMPANY_TYPE_EVIDENCE = "company_type_evidence"


_STATEMENT_CAPABILITIES = (
    MainlandCapability.BALANCE_SHEET,
    MainlandCapability.INCOME_STATEMENT,
    MainlandCapability.CASH_FLOW,
)
_ROUTABLE_CAPABILITIES = (
    MainlandCapability.ADJUSTMENT_FACTORS,
    MainlandCapability.SUSPENSION_STATUS,
    MainlandCapability.NAME_EVENTS,
    MainlandCapability.ISSUER_LIFECYCLE,
)


def _safe_source_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(_SAFE_IDENTITY_PATTERN, value) is not None
        and not any(marker in value.casefold() for marker in _SOURCE_SECRET_MARKERS)
        and "?" not in value
        and "://" not in value
        and not Path(value).is_absolute()
    )


def _source_kind_accepts(
    source_kind: ProductionQualifiedSourceKind,
    capability: MainlandCapability,
) -> bool:
    if source_kind is ProductionQualifiedSourceKind.STATEMENT:
        return capability in _STATEMENT_CAPABILITIES
    if source_kind is ProductionQualifiedSourceKind.INDICATOR:
        return capability is MainlandCapability.FINANCIAL_INDICATORS
    if source_kind is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE:
        return capability is MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
    return capability in _ROUTABLE_CAPABILITIES


def _ordinary_plan_pairs(
    plan: MainlandCapabilityRoutingPlan,
) -> tuple[tuple[MainlandCapability, str], ...]:
    """Project only source kinds implemented by the ordinary financial router."""

    return tuple(
        (route.capability, provider_id)
        for route in plan.routes
        if route.capability is not MainlandCapability.DAILY_MARKET_SNAPSHOT
        and (
            route.capability in _STATEMENT_CAPABILITIES
            or route.capability is MainlandCapability.FINANCIAL_INDICATORS
            or route.capability
            is MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
            or route.capability in _ROUTABLE_CAPABILITIES
        )
        for provider_id in route.providers
    )


def _physical_provider_is_compatible(
    *,
    route_provider_id: str,
    physical_provider_id: str,
) -> bool:
    expected_physical_provider_id = (
        "yfinance"
        if route_provider_id == "yfinance_derived"
        else "akshare_sina"
        if route_provider_id == "akshare"
        else route_provider_id
    )
    return physical_provider_id == expected_physical_provider_id


@dataclass(frozen=True, slots=True)
class ProductionQualifiedSourceRegistration:
    """Lazy, safe registration for an already-qualified run-owned source."""

    provider_id: str
    endpoint_id: str
    source_kind: ProductionQualifiedSourceKind
    capabilities: tuple[MainlandCapability, ...]
    source_factory: Callable[
        [ProductionQualifiedRoutingRunDependencies],
        ProductionQualifiedBoundSource,
    ] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        canonical = tuple(
            capability
            for capability in MainlandCapability
            if capability in self.capabilities
        )
        if (
            not _safe_source_token(self.provider_id)
            or not _safe_source_token(self.endpoint_id)
            or not isinstance(self.source_kind, ProductionQualifiedSourceKind)
            or not self.capabilities
            or self.capabilities != canonical
            or len(self.capabilities) != len(set(self.capabilities))
            or self.source_kind is ProductionQualifiedSourceKind.CAPABILITY
            and len(self.capabilities) != 1
            or any(
                not _source_kind_accepts(self.source_kind, capability)
                for capability in self.capabilities
            )
            or not callable(self.source_factory)
        ):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
            )

    @property
    def registration_id(self) -> str:
        return f"{self.source_kind.value}:{self.provider_id}:{self.endpoint_id}"


@dataclass(frozen=True, slots=True)
class ProductionQualifiedRoutingRunDependencies:
    """Safe run-owned references shared by every assembled adapter and source."""

    coordinator: ProviderRequestCoordinator = field(repr=False, compare=False)
    subrequest_cache: ProviderSubrequestCache = field(repr=False, compare=False)
    artifact_store: ProviderSubrequestArtifactStore = field(repr=False, compare=False)
    provider_clients: Mapping[str, object] = field(repr=False, compare=False)
    now: Callable[[], datetime] = field(repr=False, compare=False)
    sleep: Callable[[float], None] = field(repr=False, compare=False)
    owner_id: str
    lease_duration: timedelta = field(repr=False)

    def __post_init__(self) -> None:
        try:
            clients = MappingProxyType(
                dict(sorted(dict(self.provider_clients).items()))
            )
        except Exception:
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DEPENDENCY_MISSING
            )
        if (
            not isinstance(self.coordinator, ProviderRequestCoordinator)
            or not isinstance(self.subrequest_cache, ProviderSubrequestCache)
            or not isinstance(self.artifact_store, ProviderSubrequestArtifactStore)
            or not callable(self.now)
            or not callable(self.sleep)
            or not _safe_source_token(self.owner_id)
            or not isinstance(self.lease_duration, timedelta)
            or self.lease_duration <= timedelta(0)
            or any(
                not _safe_source_token(provider_id) or client is None
                for provider_id, client in clients.items()
            )
        ):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DEPENDENCY_MISSING
            )
        object.__setattr__(self, "provider_clients", clients)

    def __repr__(self) -> str:
        provider_ids = tuple(self.provider_clients)
        return (
            "ProductionQualifiedRoutingRunDependencies("
            f"owner_id={self.owner_id!r}, provider_ids={provider_ids!r})"
        )


@dataclass(frozen=True, slots=True)
class ProductionQualifiedBoundSource:
    """One external source operation bound to the run cache and coordinator."""

    physical_provider_id: str
    physical_request: Callable[
        [object], bytes | PhysicalAttemptNotMade[bytes]
    ] = field(repr=False, compare=False)
    response_decoder: (
        Callable[[object, bytes], object]
        | Callable[[object, bytes, ProviderSubrequestResult], object]
    ) = field(repr=False, compare=False)
    normalizer_version: str
    max_physical_attempts: int
    coordinator: ProviderRequestCoordinator = field(repr=False, compare=False)
    subrequest_cache: ProviderSubrequestCache = field(repr=False, compare=False)
    artifact_store: ProviderSubrequestArtifactStore = field(repr=False, compare=False)
    adapter: object = field(repr=False, compare=False)
    now: Callable[[], datetime] = field(repr=False, compare=False)
    sleep: Callable[[float], None] = field(repr=False, compare=False)
    owner_id: str = field(repr=False)
    lease_duration: timedelta = field(repr=False)

    @classmethod
    def bind(
        cls,
        *,
        dependencies: ProductionQualifiedRoutingRunDependencies,
        physical_provider_id: str,
        physical_request: Callable[
            [object], bytes | PhysicalAttemptNotMade[bytes]
        ],
        response_decoder: (
            Callable[[object, bytes], object]
            | Callable[[object, bytes, ProviderSubrequestResult], object]
        ),
        adapter: object,
        normalizer_version: str,
        max_physical_attempts: int = 1,
    ) -> ProductionQualifiedBoundSource:
        return cls(
            physical_provider_id=physical_provider_id,
            physical_request=physical_request,
            response_decoder=response_decoder,
            normalizer_version=normalizer_version,
            max_physical_attempts=max_physical_attempts,
            coordinator=dependencies.coordinator,
            subrequest_cache=dependencies.subrequest_cache,
            artifact_store=dependencies.artifact_store,
            adapter=adapter,
            now=dependencies.now,
            sleep=dependencies.sleep,
            owner_id=dependencies.owner_id,
            lease_duration=dependencies.lease_duration,
        )

    def __post_init__(self) -> None:
        if (
            not _safe_source_token(self.physical_provider_id)
            or not callable(self.physical_request)
            or not callable(self.response_decoder)
            or not _safe_source_token(self.normalizer_version)
            or not isinstance(self.max_physical_attempts, int)
            or isinstance(self.max_physical_attempts, bool)
            or not 1 <= self.max_physical_attempts <= 10
            or self.adapter is None
            or not isinstance(self.coordinator, ProviderRequestCoordinator)
            or not isinstance(self.subrequest_cache, ProviderSubrequestCache)
            or not isinstance(self.artifact_store, ProviderSubrequestArtifactStore)
            or not callable(self.now)
            or not callable(self.sleep)
            or not _safe_source_token(self.owner_id)
            or not isinstance(self.lease_duration, timedelta)
            or self.lease_duration <= timedelta(0)
            or not self.subrequest_cache.is_bound_to(
                coordinator=self.coordinator,
                artifact_store=self.artifact_store,
            )
        ):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
            )

    def is_bound_to(
        self,
        dependencies: ProductionQualifiedRoutingRunDependencies,
    ) -> bool:
        return (
            self.coordinator is dependencies.coordinator
            and self.subrequest_cache is dependencies.subrequest_cache
            and self.artifact_store is dependencies.artifact_store
            and self.now is dependencies.now
            and self.sleep is dependencies.sleep
            and self.owner_id == dependencies.owner_id
            and self.lease_duration == dependencies.lease_duration
            and (
                self.physical_provider_id in dependencies.provider_clients
                or self.physical_provider_id == "akshare_sina"
                and "akshare" in dependencies.provider_clients
            )
        )

    def __repr__(self) -> str:
        return (
            "ProductionQualifiedBoundSource("
            f"physical_provider_id={self.physical_provider_id!r}, "
            "run_authority_bound=True)"
        )


class ProductionQualifiedSourceUnavailableError(RuntimeError):
    """Carry one already-sanitized typed source outcome across an adapter seam."""

    def __init__(
        self,
        response: (
            FinancialProviderPeriodResponse
            | FinancialCapabilityProviderResponse
            | FinancialCompanyTypeEvidenceProviderResponse
        ),
    ) -> None:
        if (
            not isinstance(
                response,
                (
                    FinancialProviderPeriodResponse,
                    FinancialCapabilityProviderResponse,
                    FinancialCompanyTypeEvidenceProviderResponse,
                ),
            )
            or response.kind.value != "unavailable"
            or any(
                not _safe_source_token(identity)
                for identity in (
                    *response.subrequest_keys,
                    *response.physical_attempt_ids,
                )
            )
        ):
            raise ValueError(
                "qualified provider source failure response is invalid"
            ) from None
        self.response = response
        super().__init__("qualified_provider_source_unavailable")


@dataclass(frozen=True, slots=True)
class ProductionQualifiedSourceBinding:
    """Immutable safe identity plus one hidden executable source function."""

    provider_id: str
    endpoint_id: str
    source_kind: ProductionQualifiedSourceKind
    capabilities: tuple[MainlandCapability, ...]
    source: Callable[[object], object] = field(repr=False, compare=False)
    coordinator: ProviderRequestCoordinator = field(repr=False, compare=False)
    subrequest_cache: ProviderSubrequestCache = field(repr=False, compare=False)
    artifact_store: ProviderSubrequestArtifactStore = field(repr=False, compare=False)
    adapter: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        canonical = tuple(
            capability
            for capability in MainlandCapability
            if capability in self.capabilities
        )
        if (
            not _safe_source_token(self.provider_id)
            or not _safe_source_token(self.endpoint_id)
            or not isinstance(self.source_kind, ProductionQualifiedSourceKind)
            or not self.capabilities
            or self.capabilities != canonical
            or len(self.capabilities) != len(set(self.capabilities))
            or any(
                not _source_kind_accepts(self.source_kind, capability)
                for capability in self.capabilities
            )
            or not callable(self.source)
            or self.adapter is None
            or not self.subrequest_cache.is_bound_to(
                coordinator=self.coordinator,
                artifact_store=self.artifact_store,
            )
        ):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
            )

    @property
    def binding_id(self) -> str:
        capability_id = ".".join(capability.value for capability in self.capabilities)
        return (
            f"{self.source_kind.value}:{capability_id}:"
            f"{self.provider_id}:{self.endpoint_id}"
        )

    def __repr__(self) -> str:
        return f"ProductionQualifiedSourceBinding(binding_id={self.binding_id!r})"


@dataclass(frozen=True, slots=True)
class ProductionQualifiedProviderSourceComposition:
    """Immutable plan-bound topology for the existing qualified router."""

    blueprint: ProductionQualifiedRoutingCompositionBlueprint = field(
        repr=False,
        compare=False,
    )
    dependencies: ProductionQualifiedRoutingRunDependencies = field(
        repr=False,
        compare=False,
    )
    bindings: tuple[ProductionQualifiedSourceBinding, ...]
    router: MainlandFinancialCapabilityRouter = field(repr=False, compare=False)
    statement_source_ids: tuple[str, ...]
    indicator_source_ids: tuple[str, ...]
    company_type_evidence_source_ids: tuple[str, ...]
    capability_source_ids: tuple[tuple[MainlandCapability, tuple[str, ...]], ...]

    @property
    def source_binding_ids(self) -> tuple[str, ...]:
        return tuple(binding.binding_id for binding in self.bindings)

    def __repr__(self) -> str:
        return (
            "ProductionQualifiedProviderSourceComposition("
            f"blueprint_identity={self.blueprint.blueprint_identity!r}, "
            f"source_binding_ids={self.source_binding_ids!r})"
        )


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _asset_projection(
    asset_configuration: RunAssetConfiguration,
    *,
    token_value: str,
) -> RunAssetConfigurationProjection | None:
    try:
        signature_payload = asset_configuration.signature_payload()
        if _contains_unsafe_asset_material(
            signature_payload,
            token_value=token_value,
        ) or _credential_material_in_text(
            asset_configuration.registry_artifact,
            token_value,
        ):
            return None
        projection = RunAssetConfigurationProjection.model_validate(
            {
                **signature_payload,
                "asset_configuration_signature": (
                    asset_configuration.asset_configuration_signature
                ),
                "asset_type": asset_configuration.asset_type,
            }
        )
    except (AttributeError, TypeError, ValueError):
        return None
    identity = projection.instrument_identity
    if (
        not identity.is_authoritative
        or projection.instrument_kind is not InstrumentKind.EQUITY
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or projection.reference_market not in {"XSHG", "XSHE"}
        or identity.venue != projection.reference_market
        or identity.currency.upper() != "CNY"
        or projection.capability_profile.instrument_kind is not InstrumentKind.EQUITY
    ):
        return None
    return projection


def _contains_unsafe_asset_material(
    value: object,
    *,
    token_value: str,
    parent_key: str = "",
) -> bool:
    if isinstance(value, BaseModel):
        return _contains_unsafe_asset_material(
            value.model_dump(mode="json"),
            token_value=token_value,
            parent_key=parent_key,
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if any(
                marker in normalized_key
                for marker in (
                    "token",
                    "credential",
                    "password",
                    "secret",
                    "raw_text",
                    "raw_payload",
                    "raw_error",
                    "provider_error",
                    "exception",
                )
            ):
                return True
            if _contains_unsafe_asset_material(
                item,
                token_value=token_value,
                parent_key=normalized_key,
            ):
                return True
        return False
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(
            _contains_unsafe_asset_material(
                item,
                token_value=token_value,
                parent_key=parent_key,
            )
            for item in value
        )
    if not isinstance(value, str):
        return False
    if (
        _credential_material_in_text(value, token_value)
        or "?" in value
        or "://" in value
    ):
        return True
    if parent_key == "source_ref" and re.fullmatch(
        _SAFE_IDENTITY_PATTERN,
        value,
    ) is None:
        return True
    try:
        return Path(value).is_absolute()
    except (OSError, RuntimeError, ValueError):
        return True


def _credential_material_in_text(value: str, token_value: str) -> bool:
    token_digest = sha256(token_value.encode("utf-8")).hexdigest()
    if token_value in value or token_digest in value.casefold():
        return True
    if len(token_value) < 8:
        return False
    return any(
        token_value[index : index + 8] in value
        for index in range(len(token_value) - 7)
    )


def _required_provider_source_ids(
    plan: MainlandCapabilityRoutingPlan,
) -> tuple[str, ...]:
    sources = {
        provider
        for route in plan.routes
        if route.capability is not MainlandCapability.DAILY_MARKET_SNAPSHOT
        for provider in route.providers
    }
    return tuple(sorted(sources))


def production_qualified_routing_dependency_availability() -> dict[str, bool]:
    """Resolve the static production dependency inventory without importing it."""

    return {
        dependency: importlib.util.find_spec(dependency) is not None
        for dependency in _REQUIRED_DEPENDENCY_IDS
    }


def production_qualified_routing_provider_source_coverage(
    plan: MainlandCapabilityRoutingPlan,
) -> dict[str, bool]:
    """Project whether Ticket 02 covers every source named by the immutable plan."""

    return {
        source_id: source_id in _PRODUCTION_PROVIDER_SOURCE_IDS
        for source_id in _required_provider_source_ids(plan)
    }


def _validated_root_paths(config: Mapping[str, Any]) -> tuple[Path, ...] | None:
    paths: list[Path] = []
    for _role, key, _identity, is_database in _ROOT_FIELDS:
        value = config.get(key)
        if not isinstance(value, (str, Path)):
            return None
        raw = str(value).strip()
        try:
            path = Path(raw)
            contains_parent_traversal = any(part == ".." for part in path.parts)
        except (OSError, RuntimeError, ValueError):
            return None
        if (
            not raw
            or "?" in raw
            or "://" in raw
            or raw.startswith("~")
            or contains_parent_traversal
            or not path.is_absolute()
        ):
            return None
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        if is_database and resolved.suffix.casefold() != ".sqlite3":
            return None
        paths.append(resolved)
    return tuple(paths)


def _runtime_root_identities(
    paths: tuple[Path, ...],
) -> tuple[ProductionRuntimeRootIdentity, ...]:
    return tuple(
        ProductionRuntimeRootIdentity(
            role=role,
            identity=(
                prefix
                + sha256(
                    os.path.normcase(str(path)).encode("utf-8")
                ).hexdigest()
            ),
        )
        for path, (role, _key, prefix, _is_database) in zip(
            paths,
            _ROOT_FIELDS,
            strict=True,
        )
    )


def _snapshot_mapping(
    value: object,
    keys: tuple[str, ...],
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return {key: value.get(key) for key in keys}
    except Exception:  # noqa: BLE001 - raw boundary failures are discarded
        return None


def _fail(reason: MainlandCapabilityRoutingFailureReason) -> None:
    raise MainlandCapabilityRoutingPreflightError(
        mainland_capability_routing_failure(reason)
    ) from None


def _plan_is_valid(plan: MainlandCapabilityRoutingPlan) -> bool:
    try:
        restored = MainlandCapabilityRoutingPlan.model_validate(
            plan.model_dump(mode="json")
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return restored == plan


def _binding_is_valid(
    binding: object,
    plan: MainlandCapabilityRoutingPlan,
) -> bool:
    if (
        not isinstance(binding, MainlandFinancialEligibilityProjectionBinding)
        or plan.financial_eligibility_binding is None
        or plan.financial_eligibility_policy is None
        or binding != plan.financial_eligibility_binding
    ):
        return False
    try:
        restored = MainlandFinancialEligibilityProjectionBinding.model_validate(
            binding.model_dump(mode="json")
        )
    except (TypeError, ValueError):
        return False
    return (
        restored == binding
        and restored.policy_signature
        == plan.financial_eligibility_policy.policy_signature
    )


def _configured_capabilities(value: object) -> tuple[TushareCapability, ...] | None:
    try:
        if isinstance(value, str):
            raw_values: tuple[object, ...] = tuple(
                item.strip() for item in value.split(",") if item.strip()
            )
        elif isinstance(value, (tuple, list, set, frozenset)):
            raw_values = tuple(value)
        else:
            return None
        enabled = {
            item if isinstance(item, TushareCapability) else TushareCapability(str(item))
            for item in raw_values
        }
    except (TypeError, ValueError):
        return None
    return tuple(capability for capability in TushareCapability if capability in enabled)


def _safe_account_scope_matches(
    config: Mapping[str, Any],
    plan: MainlandCapabilityRoutingPlan,
    token_value: str,
) -> bool:
    value = config.get("tushare_account_scope_label")
    if not isinstance(value, str) or value != plan.account_scope_label:
        return False
    normalized = value.casefold()
    return bool(
        re.fullmatch(_SAFE_IDENTITY_PATTERN, value)
        and len(value) <= 64
        and not _credential_material_in_text(value, token_value)
        and not any(
            marker in normalized
            for marker in (
                "token",
                "credential",
                "secret",
                "password",
                "api-key",
                "api_key",
                "apikey",
            )
        )
    )


def _exact_positive_rate(config: Mapping[str, Any], key: str) -> int | None:
    value = config.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 200
    ):
        return None
    return value


_FACTORY_CONTRACT_TYPE = ProductionQualifiedRoutingFactoryContract
_BLUEPRINT_TYPE = ProductionQualifiedRoutingCompositionBlueprint


def build_production_qualified_routing_composition_blueprint(
    *,
    plan: MainlandCapabilityRoutingPlan,
    asset_configuration: RunAssetConfiguration,
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding,
    config: Mapping[str, Any],
    validation_workload: bool,
    disposition: MainlandCapabilityRoutingDisposition,
    environment: Mapping[str, str],
    dependency_availability: Mapping[str, bool],
    provider_source_coverage: Mapping[str, bool],
    factory_contract_version: str,
) -> ProductionQualifiedRoutingCompositionBlueprint:
    """Validate safe construction inputs and return the resource-free blueprint."""

    if factory_contract_version != PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_CONTRACT_INVALID
        )
    if validation_workload is not True:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_VALIDATION_WORKLOAD_REQUIRED
        )
    if not isinstance(plan, MainlandCapabilityRoutingPlan):
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_PLAN_INVALID)
    if plan.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1:
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED)
    if plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW:
        _fail(MainlandCapabilityRoutingFailureReason.INVALID_ROUTING_MODE)
    if disposition is not (
        MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DISPOSITION_INVALID
        )
    if not plan.enabled_tushare_capabilities:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_PLAN_NOT_APPLICABLE
        )
    if not _plan_is_valid(plan):
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_PLAN_INVALID)
    config_snapshot = _snapshot_mapping(config, _CONFIG_FIELDS)
    if config_snapshot is None:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_CONTRACT_INVALID
        )
    if config_snapshot.get("mainland_capability_routing_mode") != plan.mode.value:
        _fail(MainlandCapabilityRoutingFailureReason.INVALID_ROUTING_MODE)
    if (
        config_snapshot.get("mainland_capability_routing_plan_signature")
        != plan.plan_signature
    ):
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_PLAN_INVALID)
    if (
        _configured_capabilities(
            config_snapshot.get("tushare_enabled_capabilities")
        )
        != plan.enabled_tushare_capabilities
    ):
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_PLAN_INVALID)
    if (
        config_snapshot.get("tushare_qualification_profile")
        != plan.qualification_profile
    ):
        _fail(MainlandCapabilityRoutingFailureReason.QUALIFICATION_PROFILE_MISMATCH)

    environment_snapshot = _snapshot_mapping(environment, ("TUSHARE_TOKEN",))
    token_value = (
        environment_snapshot.get("TUSHARE_TOKEN")
        if environment_snapshot is not None
        else None
    )
    if not isinstance(token_value, str) or not token_value.strip():
        _fail(MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING)

    if not isinstance(asset_configuration, RunAssetConfiguration):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_ASSET_BINDING_INVALID
        )
    asset_projection = _asset_projection(
        asset_configuration,
        token_value=token_value,
    )
    if (
        asset_projection is None
        or config_snapshot.get("asset_configuration_signature")
        != asset_configuration.asset_configuration_signature
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_ASSET_BINDING_INVALID
        )
    if not _binding_is_valid(financial_eligibility_binding, plan):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_ELIGIBILITY_BINDING_INVALID
        )
    if (
        config_snapshot.get("data_usage_mode")
        != DataUsageMode.PERSONAL_RESEARCH.value
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.DATA_USAGE_MODE_NOT_PERSONAL_RESEARCH
        )
    dependency_snapshot = _snapshot_mapping(
        dependency_availability,
        _REQUIRED_DEPENDENCY_IDS,
    )
    if dependency_snapshot is None or any(
        dependency_snapshot.get(dependency) is not True
        for dependency in _REQUIRED_DEPENDENCY_IDS
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DEPENDENCY_MISSING
    )

    required_provider_source_ids = _required_provider_source_ids(plan)
    provider_source_snapshot = _snapshot_mapping(
        provider_source_coverage,
        required_provider_source_ids,
    )
    if provider_source_snapshot is None or any(
        provider_source_snapshot.get(source_id) is not True
        for source_id in required_provider_source_ids
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
        )
    if not _safe_account_scope_matches(config_snapshot, plan, token_value):
        _fail(MainlandCapabilityRoutingFailureReason.TUSHARE_ACCOUNT_SCOPE_INVALID)
    calls_per_minute = _exact_positive_rate(
        config_snapshot,
        "tushare_calls_per_minute",
    )
    if calls_per_minute is None or any(
        identity.calls_per_minute != calls_per_minute
        for identity in plan.endpoint_pacing_identities
    ):
        _fail(MainlandCapabilityRoutingFailureReason.TUSHARE_PACING_INVALID)
    operator_calls_per_minute = _exact_positive_rate(
        config_snapshot,
        "tushare_operator_safety_ceiling_calls_per_minute",
    )
    if operator_calls_per_minute is None or any(
        identity.operator_calls_per_minute != operator_calls_per_minute
        for identity in plan.endpoint_pacing_identities
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID
        )
    runtime_paths = _validated_root_paths(config_snapshot)
    if runtime_paths is None:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_RUNTIME_ROOT_INVALID
        )
    runtime_root_identities = _runtime_root_identities(runtime_paths)
    contract: ProductionQualifiedRoutingFactoryContract | None = None
    try:
        contract = ProductionQualifiedRoutingFactoryContract(
            mode=plan.mode,
            disposition=disposition,
            plan_signature=plan.plan_signature,
            asset_configuration_signature=(
                asset_configuration.asset_configuration_signature
            ),
            asset_configuration_version=(
                asset_projection.asset_configuration_version
            ),
            instrument_symbol=asset_projection.instrument_identity.symbol,
            reference_market=asset_projection.reference_market,
            instrument_kind=asset_projection.instrument_kind,
            currency=asset_projection.instrument_identity.currency.upper(),
            registry_id=asset_projection.registry_id,
            registry_digest=asset_projection.registry_digest,
            capability_profile_identity=(
                asset_projection.capability_profile.profile_id
            ),
            adjustment_basis=asset_projection.adjustment_basis,
            financial_eligibility_binding=financial_eligibility_binding,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            qualification_profile=plan.qualification_profile,
            enabled_capabilities=plan.enabled_tushare_capabilities,
            required_provider_source_ids=required_provider_source_ids,
            dependency_ids=_REQUIRED_DEPENDENCY_IDS,
            account_scope_label=plan.account_scope_label,
            endpoint_pacing_identities=plan.endpoint_pacing_identities,
            runtime_root_identities=runtime_root_identities,
        )
        identity_payload = {
            "blueprint_version": PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION,
            "factory_contract": contract.model_dump(mode="json"),
        }
        blueprint_identity = (
            f"{_BLUEPRINT_IDENTITY_PREFIX}{_canonical_digest(identity_payload)}"
        )
        blueprint = ProductionQualifiedRoutingCompositionBlueprint(
            factory_contract=contract,
            routing_plan=plan,
            blueprint_identity=blueprint_identity,
            runtime_root_path=runtime_paths[0],
            database_path=runtime_paths[1],
            provider_artifact_root_path=runtime_paths[2],
            backup_root_path=runtime_paths[3],
            report_root_path=runtime_paths[4],
        )
    except (TypeError, ValueError):
        blueprint = None
    if (
        blueprint is None
        or type(contract) is not _FACTORY_CONTRACT_TYPE
        or type(blueprint) is not _BLUEPRINT_TYPE
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
        )
    return blueprint


def _internal_source_endpoint(
    capability: MainlandCapability,
    provider_id: str,
) -> tuple[ProductionQualifiedSourceKind, str] | None:
    if capability is MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE:
        if provider_id == "tushare":
            return (
                ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE,
                "balancesheet_company_type_evidence",
            )
        if provider_id == "akshare_sina":
            return (
                ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE,
                "stock_financial_report_sina_company_type_evidence",
            )
    if capability in _STATEMENT_CAPABILITIES:
        if provider_id == "tushare":
            endpoint_id = {
                MainlandCapability.BALANCE_SHEET: "balancesheet",
                MainlandCapability.INCOME_STATEMENT: "income",
                MainlandCapability.CASH_FLOW: "cashflow",
            }[capability]
            return ProductionQualifiedSourceKind.STATEMENT, endpoint_id
        if provider_id == "akshare_sina":
            return ProductionQualifiedSourceKind.STATEMENT, "stock_financial_report_sina"
    if capability is MainlandCapability.FINANCIAL_INDICATORS:
        if provider_id == "tushare":
            return ProductionQualifiedSourceKind.INDICATOR, "fina_indicator"
        if provider_id == "baostock_qualified_families":
            return ProductionQualifiedSourceKind.INDICATOR, "qualified_ratio_families"
    if capability is MainlandCapability.ADJUSTMENT_FACTORS:
        if provider_id == "baostock":
            return ProductionQualifiedSourceKind.CAPABILITY, "provider_history_bundle"
        if provider_id == "tushare":
            return ProductionQualifiedSourceKind.CAPABILITY, "adj_factor"
    if capability is MainlandCapability.SUSPENSION_STATUS and provider_id == "baostock":
        return ProductionQualifiedSourceKind.CAPABILITY, "session_status"
    if capability is MainlandCapability.NAME_EVENTS and provider_id == "tushare":
        return ProductionQualifiedSourceKind.CAPABILITY, "namechange"
    if capability is MainlandCapability.ISSUER_LIFECYCLE and provider_id == "baostock":
        return ProductionQualifiedSourceKind.CAPABILITY, "issuer_lifecycle"
    return None


def _statement_capability(statement_type: FinancialStatementType) -> MainlandCapability:
    return {
        FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
    }[statement_type]


def _statement_adapter_source(
    *,
    provider_id: Literal["tushare", "akshare_sina"],
    adapter: TushareStatementAdapter | AkshareSinaStatementAdapter,
) -> FinancialPeriodSource:
    def source(request: FinancialProviderGapRequest):
        if request.provider_id != provider_id:
            raise ValueError("statement source request provider mismatch")
        period_ends = (
            *request.missing_annual_period_ends,
            *request.missing_reporting_period_ends,
            *request.conflicted_annual_period_ends,
            *request.conflicted_reporting_period_ends,
        )
        if not period_ends:
            raise ValueError("statement source requires an unresolved period")
        if request.statement_type is FinancialStatementType.BALANCE_SHEET:
            result = adapter.acquire_balance_sheet(
                instrument_identity=request.instrument_identity,
                range_start=min(period_ends),
                as_of_date=request.as_of_date,
            )
        elif request.statement_type is FinancialStatementType.INCOME_STATEMENT:
            result = adapter.acquire_income_statement(
                instrument_identity=request.instrument_identity,
                range_start=min(period_ends),
                as_of_date=request.as_of_date,
            )
        else:
            result = adapter.acquire_cash_flow(
                instrument_identity=request.instrument_identity,
                range_start=min(period_ends),
                as_of_date=request.as_of_date,
            )
        return financial_period_response_from_statement_adapter(
            provider_id=provider_id,
            result=result,
        )

    return source


def _statement_company_type_evidence_source(
    *,
    provider_id: Literal["tushare", "akshare_sina"],
    adapter: TushareStatementAdapter | AkshareSinaStatementAdapter,
) -> FinancialCompanyTypeEvidenceSource:
    """Project safe company-type inputs from the existing statement adapter."""

    projection_kind = {
        "tushare": FinancialCompanyTypeEvidenceSourceProjectionKind.TUSHARE_STATEMENT,
        "akshare_sina": (
            FinancialCompanyTypeEvidenceSourceProjectionKind.AKSHARE_SINA_STATEMENT
        ),
    }[provider_id]

    def source(
        request: FinancialCompanyTypeEvidenceRoutingRequest,
    ) -> FinancialCompanyTypeEvidenceProviderResponse:
        as_of_date = request.metadata_binding.as_of_date
        target_period = date(as_of_date.year - 1, 12, 31)
        period_response = _statement_adapter_source(
            provider_id=provider_id,
            adapter=adapter,
        )(
            FinancialProviderGapRequest(
                provider_id=provider_id,
                instrument_identity=request.metadata_binding.instrument_identity,
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=as_of_date,
                missing_annual_period_ends=(target_period,),
                missing_reporting_period_ends=(),
            )
        )
        if period_response.kind.value != "available" or not period_response.candidates:
            return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
                provider_id=provider_id,
                metadata_binding=request.metadata_binding,
                reason=(
                    period_response.reason
                    or AcquisitionUnavailableReason.NO_DATA
                ),
                subrequest_keys=period_response.subrequest_keys,
                physical_attempt_ids=period_response.physical_attempt_ids,
            )
        candidate = max(
            period_response.candidates,
            key=lambda item: (
                item.period_identity.period_end,
                item.candidate_identity,
            ),
        )
        resolution = candidate.company_type_resolution
        evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
            instrument_identity=request.metadata_binding.instrument_identity,
            as_of_date=as_of_date,
            provider_id=provider_id,
            artifact_identity=candidate.artifact.artifact_identity,
            source_ref=candidate.artifact.artifact_identity,
            observed_at=candidate.filing_metadata.observed_at,
            retrieved_at=candidate.filing_metadata.retrieved_at,
            provider_declared_type=resolution.provider_declared_type,
            provider_declaration_qualified=(
                resolution.provider_declaration_qualified
            ),
            normalized_resolver_inputs=resolution.classifier_inputs,
        )
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id=provider_id,
            metadata_binding=request.metadata_binding,
            source_projection_kind=projection_kind,
            evidence=evidence,
            subrequest_keys=period_response.subrequest_keys,
            physical_attempt_ids=period_response.physical_attempt_ids,
        )

    return source


def _disabled_company_type_evidence_source(
    provider_id: str,
) -> FinancialCompanyTypeEvidenceSource:
    """Keep disabled optional Tushare statement capability typed and callable."""

    def source(
        request: FinancialCompanyTypeEvidenceRoutingRequest,
    ) -> FinancialCompanyTypeEvidenceProviderResponse:
        return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
            provider_id=provider_id,
            metadata_binding=request.metadata_binding,
            reason=AcquisitionUnavailableReason.USAGE_NOT_ENTITLED,
        )

    return source


def _tushare_indicator_source(
    adapter: TushareFinancialIndicatorAdapter,
) -> FinancialIndicatorSource:
    def source(request: FinancialIndicatorProviderGapRequest):
        if request.provider_id != "tushare":
            raise ValueError("indicator source request provider mismatch")
        period_ends = tuple(
            period
            for _family, periods in request.missing_periods_by_family
            for period in periods
        )
        if not period_ends:
            raise ValueError("indicator source requires an unresolved period")
        if request.company_type_resolution is None:
            return FinancialProviderPeriodResponse.unavailable(
                provider_id="tushare",
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            )
        result = adapter.acquire_financial_indicators(
            instrument_identity=request.instrument_identity,
            company_type_resolution=request.company_type_resolution,
            range_start=min(period_ends),
            as_of_date=request.as_of_date,
            eligible_reporting_period_ends=tuple(
                sorted(set(period_ends), reverse=True)
            ),
        )
        return financial_period_response_from_indicator_adapter(
            provider_id="tushare",
            result=result,
        )

    return source


def _quarter(period_end: date) -> int:
    quarter_by_month = {3: 1, 6: 2, 9: 3, 12: 4}
    quarter = quarter_by_month.get(period_end.month)
    if (
        quarter is None
        or period_end.day != monthrange(period_end.year, period_end.month)[1]
    ):
        raise ValueError("BaoStock ratio target is not a reporting quarter end")
    return quarter


def _baostock_ratio_source(
    adapter: BaoStockRatioFamilyAdapter,
) -> FinancialIndicatorSource:
    def source(request: FinancialIndicatorProviderGapRequest):
        if request.provider_id != "baostock_qualified_families":
            raise ValueError("indicator source request provider mismatch")
        methods = {
            FinancialRatioFamily.PROFIT: adapter.acquire_profit,
            FinancialRatioFamily.OPERATION: adapter.acquire_operation,
            FinancialRatioFamily.GROWTH: adapter.acquire_growth,
            FinancialRatioFamily.BALANCE: adapter.acquire_balance_ratios,
            FinancialRatioFamily.CASH_FLOW: adapter.acquire_cash_flow_ratios,
            FinancialRatioFamily.DUPONT: adapter.acquire_dupont,
        }
        company_type_resolution = request.company_type_resolution
        if company_type_resolution is None:
            return FinancialProviderPeriodResponse.unavailable(
                provider_id="baostock_qualified_families",
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            )
        results = tuple(
            methods[family](
                instrument_identity=request.instrument_identity,
                company_type_resolution=company_type_resolution,
                year=period_end.year,
                quarter=_quarter(period_end),
                as_of_date=request.as_of_date,
            )
            for family, period_ends in request.missing_periods_by_family
            for period_end in period_ends
        )
        eligible_periods = tuple(
            sorted(
                {
                    period_end
                    for _family, period_ends in request.missing_periods_by_family
                    for period_end in period_ends
                },
                reverse=True,
            )
        )
        aggregate = assess_baostock_ratio_family_results(
            instrument_identity=request.instrument_identity,
            company_type=request.company_type,
            consolidation_scope=request.consolidation_scope,
            currency=request.currency,
            results=results,
            eligible_reporting_period_ends=eligible_periods,
        )
        return financial_period_response_from_indicator_adapter(
            provider_id="baostock_qualified_families",
            result=aggregate,
        )

    return source


def _adapter_unavailable_reason(outcome: object) -> AcquisitionUnavailableReason:
    value = str(getattr(getattr(outcome, "kind", None), "value", "provider_error"))
    return {
        "permission_denied": AcquisitionUnavailableReason.PERMISSION_DENIED,
        "rate_limited": AcquisitionUnavailableReason.RATE_LIMITED,
        "authentication": AcquisitionUnavailableReason.AUTHENTICATION,
        "empty": AcquisitionUnavailableReason.EMPTY_FRAME,
        "malformed": AcquisitionUnavailableReason.MALFORMED_RESPONSE,
        "timeout": AcquisitionUnavailableReason.TIMEOUT,
        "disconnect": AcquisitionUnavailableReason.DISCONNECT,
        "upstream_busy": AcquisitionUnavailableReason.UPSTREAM_BUSY,
    }.get(value, AcquisitionUnavailableReason.PROVIDER_ERROR)


def _attempt_ids(results: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(event.attempt_event_id)
            for result in results
            if result is not None
            for event in getattr(result, "attempt_events", ())
        )
    )


def _artifact_ids(results: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(artifact.artifact_identity)
            for result in results
            if result is not None
            for artifact in (getattr(result, "artifact", None),)
            if artifact is not None
        )
    )


def _subrequest_keys(results: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(key)
            for result in results
            if result is not None
            for key in (getattr(result, "subrequest_key", None),)
            if key is not None
        )
    )


def _capability_adapter_response(
    *,
    provider_id: str,
    capability: MainlandCapability,
    result: object,
    current_tradeability: Literal["unknown", "tradeable", "suspended"] = "unknown",
) -> FinancialCapabilityProviderResponse:
    results = (result,)
    artifact_ids = _artifact_ids(results)
    candidates = tuple(getattr(result, "candidates", ()))
    common = {
        "provider_id": provider_id,
        "capability": capability,
        "subrequest_keys": _subrequest_keys(results),
        "physical_attempt_ids": _attempt_ids(results),
    }
    if artifact_ids and (candidates or current_tradeability != "unknown"):
        return FinancialCapabilityProviderResponse.available(
            **common,
            retained_artifact_identities=artifact_ids,
            candidate_count=len(candidates),
            current_tradeability=current_tradeability,
            listing_metadata=getattr(result, "listing_metadata", None),
        )
    if artifact_ids:
        return FinancialCapabilityProviderResponse.rejected(
            **common,
            retained_artifact_identities=artifact_ids,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )
    return FinancialCapabilityProviderResponse.unavailable(
        **common,
        reason=_adapter_unavailable_reason(getattr(result, "outcome", None)),
    )


def _baostock_factor_source(
    adapter: BaoStockCapabilityAdapter,
) -> FinancialCapabilitySource:
    def source(request):
        bundle = adapter.acquire_provider_history_bundle(
            instrument_identity=request.instrument_identity,
            range_start=request.range_start,
            as_of_date=request.as_of_date,
        )
        results = tuple(
            result
            for result in (bundle.raw, bundle.factors, bundle.statuses)
            if result is not None
        )
        artifact_ids = _artifact_ids(results)
        common = {
            "provider_id": "baostock",
            "capability": MainlandCapability.ADJUSTMENT_FACTORS,
            "subrequest_keys": _subrequest_keys(results),
            "physical_attempt_ids": _attempt_ids(results),
        }
        if bundle.provider_history_bundle_identity is not None:
            assert len(artifact_ids) == 3
            assert bundle.factors is not None
            return FinancialCapabilityProviderResponse.available(
                **common,
                retained_artifact_identities=artifact_ids,
                candidate_count=len(bundle.factors.candidates),
                provider_history_bundle_identity=(
                    bundle.provider_history_bundle_identity
                ),
                strict_bundle_component_artifact_identities=artifact_ids,
            )
        if artifact_ids:
            return FinancialCapabilityProviderResponse.rejected(
                **common,
                retained_artifact_identities=artifact_ids,
                reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY,
            )
        first = results[0] if results else None
        return FinancialCapabilityProviderResponse.unavailable(
            **common,
            reason=_adapter_unavailable_reason(getattr(first, "outcome", None)),
        )

    return source


def _baostock_status_source(
    adapter: BaoStockCapabilityAdapter,
) -> FinancialCapabilitySource:
    def source(request):
        result = adapter.acquire_session_status(
            instrument_identity=request.instrument_identity,
            range_start=request.range_start,
            as_of_date=request.as_of_date,
        )
        return _capability_adapter_response(
            provider_id="baostock",
            capability=MainlandCapability.SUSPENSION_STATUS,
            result=result,
            current_tradeability=result.current_tradeability.value,
        )

    return source


def _baostock_lifecycle_source(
    adapter: BaoStockCapabilityAdapter,
) -> FinancialCapabilitySource:
    def source(request):
        result = adapter.acquire_lifecycle(
            instrument_identity=request.instrument_identity,
            as_of_date=request.as_of_date,
        )
        return _capability_adapter_response(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            result=result,
        )

    return source


def _tushare_factor_source(
    adapter: TushareAdjustmentFactorAdapter,
) -> FinancialCapabilitySource:
    def source(request):
        result = adapter.acquire_adjustment_factors(
            instrument_identity=request.instrument_identity,
            range_start=request.range_start,
            as_of_date=request.as_of_date,
        )
        return _capability_adapter_response(
            provider_id="tushare",
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            result=result,
        )

    return source


def _tushare_name_source(
    adapter: TushareNameEventAdapter,
) -> FinancialCapabilitySource:
    def source(request):
        result = adapter.acquire_name_events(
            instrument_identity=request.instrument_identity,
            range_start=request.range_start,
            as_of_date=request.as_of_date,
        )
        return _capability_adapter_response(
            provider_id="tushare",
            capability=MainlandCapability.NAME_EVENTS,
            result=result,
        )

    return source


def _build_internal_adapter(
    *,
    capability: MainlandCapability,
    provider_id: str,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    dependencies: ProductionQualifiedRoutingRunDependencies,
    adapter_cache: dict[str, object],
) -> tuple[object, Callable[[object], object]]:
    common = {
        "routing_plan": blueprint.routing_plan,
        "subrequest_cache": dependencies.subrequest_cache,
        "owner_id": dependencies.owner_id,
        "now": dependencies.now,
        "sleep": dependencies.sleep,
        "lease_duration": dependencies.lease_duration,
    }
    if capability is MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE:
        if provider_id == "tushare":
            if (
                TushareCapability.STATEMENTS
                not in blueprint.routing_plan.enabled_tushare_capabilities
            ):
                client = dependencies.provider_clients["tushare"]
                return client, _disabled_company_type_evidence_source("tushare")
            adapter = adapter_cache.get("tushare_statements")
            if adapter is None:
                adapter = TushareStatementAdapter(
                    **common,
                    sdk_client=dependencies.provider_clients["tushare"],
                )
                adapter_cache["tushare_statements"] = adapter
        elif provider_id == "akshare_sina":
            adapter = adapter_cache.get("akshare_sina_statements")
            if adapter is None:
                adapter = AkshareSinaStatementAdapter(
                    **common,
                    sdk_client=dependencies.provider_clients["akshare"],
                )
                adapter_cache["akshare_sina_statements"] = adapter
        else:
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
            )
        return adapter, _statement_company_type_evidence_source(
            provider_id=provider_id,  # type: ignore[arg-type]
            adapter=adapter,  # type: ignore[arg-type]
        )
    if capability in _STATEMENT_CAPABILITIES and provider_id == "tushare":
        adapter = adapter_cache.get("tushare_statements")
        if adapter is None:
            adapter = TushareStatementAdapter(
                **common,
                sdk_client=dependencies.provider_clients["tushare"],
            )
            adapter_cache["tushare_statements"] = adapter
        return adapter, _statement_adapter_source(
            provider_id="tushare",
            adapter=adapter,  # type: ignore[arg-type]
        )
    if capability in _STATEMENT_CAPABILITIES and provider_id == "akshare_sina":
        adapter = adapter_cache.get("akshare_sina_statements")
        if adapter is None:
            adapter = AkshareSinaStatementAdapter(
                **common,
                sdk_client=dependencies.provider_clients["akshare"],
            )
            adapter_cache["akshare_sina_statements"] = adapter
        return adapter, _statement_adapter_source(
            provider_id="akshare_sina",
            adapter=adapter,  # type: ignore[arg-type]
        )
    if capability is MainlandCapability.FINANCIAL_INDICATORS and provider_id == "tushare":
        adapter = adapter_cache.get("tushare_indicators")
        if adapter is None:
            adapter = TushareFinancialIndicatorAdapter(
                **common,
                sdk_client=dependencies.provider_clients["tushare"],
            )
            adapter_cache["tushare_indicators"] = adapter
        return adapter, _tushare_indicator_source(adapter)  # type: ignore[arg-type]
    if (
        capability is MainlandCapability.FINANCIAL_INDICATORS
        and provider_id == "baostock_qualified_families"
    ):
        adapter = adapter_cache.get("baostock_ratios")
        if adapter is None:
            adapter = BaoStockRatioFamilyAdapter(
                **common,
                transport=dependencies.provider_clients["baostock"],
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            )
            adapter_cache["baostock_ratios"] = adapter
        return adapter, _baostock_ratio_source(adapter)  # type: ignore[arg-type]
    if provider_id == "baostock":
        adapter = adapter_cache.get("baostock_capabilities")
        if adapter is None:
            adapter = BaoStockCapabilityAdapter(
                **common,
                transport=dependencies.provider_clients["baostock"],
            )
            adapter_cache["baostock_capabilities"] = adapter
        if capability is MainlandCapability.ADJUSTMENT_FACTORS:
            source = _baostock_factor_source(adapter)  # type: ignore[arg-type]
        elif capability is MainlandCapability.SUSPENSION_STATUS:
            source = _baostock_status_source(adapter)  # type: ignore[arg-type]
        else:
            source = _baostock_lifecycle_source(adapter)  # type: ignore[arg-type]
        return adapter, source
    if capability is MainlandCapability.ADJUSTMENT_FACTORS and provider_id == "tushare":
        adapter = adapter_cache.get("tushare_factors")
        if adapter is None:
            adapter = TushareAdjustmentFactorAdapter(
                **common,
                sdk_client=dependencies.provider_clients["tushare"],
            )
            adapter_cache["tushare_factors"] = adapter
        return adapter, _tushare_factor_source(adapter)  # type: ignore[arg-type]
    if capability is MainlandCapability.NAME_EVENTS and provider_id == "tushare":
        adapter = adapter_cache.get("tushare_names")
        if adapter is None:
            adapter = TushareNameEventAdapter(
                **common,
                sdk_client=dependencies.provider_clients["tushare"],
            )
            adapter_cache["tushare_names"] = adapter
        return adapter, _tushare_name_source(adapter)  # type: ignore[arg-type]
    _fail(
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
    )


def _validate_run_dependencies(
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    dependencies: ProductionQualifiedRoutingRunDependencies,
) -> None:
    if (
        not isinstance(blueprint, ProductionQualifiedRoutingCompositionBlueprint)
        or not isinstance(dependencies, ProductionQualifiedRoutingRunDependencies)
        or tuple(dependencies.provider_clients)
        != blueprint.factory_contract.dependency_ids
        or not dependencies.subrequest_cache.is_bound_to(
            coordinator=dependencies.coordinator,
            artifact_store=dependencies.artifact_store,
        )
    ):
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DEPENDENCY_MISSING
        )


def _external_request_material(
    registration: ProductionQualifiedSourceRegistration,
    request: object,
) -> tuple[object, date, date, tuple[str, ...]]:
    if registration.endpoint_id in {
        "current_financial_profile",
        "current_profile_company_type_evidence",
    }:
        if isinstance(request, FinancialIndicatorProviderGapRequest):
            identity = request.instrument_identity
            as_of_date = request.as_of_date
        elif isinstance(request, FinancialCompanyTypeEvidenceRoutingRequest):
            identity = request.metadata_binding.instrument_identity
            as_of_date = request.metadata_binding.as_of_date
        else:
            raise ValueError("qualified Yahoo profile request is invalid")
        return (
            identity,
            as_of_date,
            as_of_date,
            ("profile:company_type_classifier",),
        )
    if registration.source_kind is ProductionQualifiedSourceKind.STATEMENT:
        if not isinstance(request, FinancialProviderGapRequest):
            raise ValueError("qualified statement source request is invalid")
        capability = _statement_capability(request.statement_type)
        periods = tuple(
            sorted(
                {
                    *request.missing_annual_period_ends,
                    *request.missing_reporting_period_ends,
                    *request.conflicted_annual_period_ends,
                    *request.conflicted_reporting_period_ends,
                }
            )
        )
        if not periods:
            raise ValueError("qualified statement source requires an unresolved period")
        fields = (
            f"capability:{capability.value}",
            f"statement:{request.statement_type.value}",
            *(f"period:{period.isoformat()}" for period in periods),
        )
        return request.instrument_identity, periods[0], request.as_of_date, fields
    if registration.source_kind is ProductionQualifiedSourceKind.INDICATOR:
        if not isinstance(request, FinancialIndicatorProviderGapRequest):
            raise ValueError("qualified indicator source request is invalid")
        targets = tuple(
            (family, period)
            for family, periods in request.missing_periods_by_family
            for period in periods
        )
        if not targets:
            raise ValueError("qualified indicator source requires an unresolved period")
        periods = tuple(period for _family, period in targets)
        fields = (
            f"capability:{MainlandCapability.FINANCIAL_INDICATORS.value}",
            *(f"target:{family.value}:{period.isoformat()}" for family, period in targets),
        )
        return request.instrument_identity, min(periods), request.as_of_date, fields
    if registration.source_kind is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE:
        if not isinstance(request, FinancialCompanyTypeEvidenceRoutingRequest):
            raise ValueError("qualified company-type evidence request is invalid")
        binding = request.metadata_binding
        fields = (
            f"capability:{MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE.value}",
            f"purpose:{binding.acquisition_purpose}",
        )
        return (
            binding.instrument_identity,
            binding.as_of_date,
            binding.as_of_date,
            fields,
        )
    if not isinstance(request, FinancialCapabilityRoutingRequest):
        raise ValueError("qualified capability source request is invalid")
    fields = (f"capability:{request.capability.value}",)
    return request.instrument_identity, request.range_start, request.as_of_date, fields


def _external_subrequest_key(
    *,
    registration: ProductionQualifiedSourceRegistration,
    bound_source: ProductionQualifiedBoundSource,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    request: object,
) -> ProviderSubrequestKey:
    instrument_identity, range_start, range_end, requested_fields = (
        _external_request_material(registration, request)
    )
    upstream_service_id, _service_name = upstream_service_identity_for_provider(
        bound_source.physical_provider_id
    )
    return ProviderSubrequestKey.create(
        provider_id=bound_source.physical_provider_id,
        upstream_service_id=upstream_service_id,
        account_scope="public",
        capacity_scope=(
            "current_financial_profile"
            if registration.endpoint_id
            == "current_profile_company_type_evidence"
            else registration.endpoint_id
        ),
        instrument_identity=instrument_identity,
        requested_range_start=range_start.isoformat(),
        requested_range_end=range_end.isoformat(),
        requested_fields=requested_fields,
        as_of_date=range_end,
        qualification_profile=blueprint.routing_plan.qualification_profile,
        normalizer_version=bound_source.normalizer_version,
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _external_unavailable_response(
    registration: ProductionQualifiedSourceRegistration,
    request: object,
    *,
    reason: AcquisitionUnavailableReason,
    subrequest_keys: tuple[str, ...] = (),
    physical_attempt_ids: tuple[str, ...] = (),
    physical_attempt_events: tuple[object, ...] = (),
):
    if registration.source_kind in {
        ProductionQualifiedSourceKind.STATEMENT,
        ProductionQualifiedSourceKind.INDICATOR,
    }:
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=registration.provider_id,
            reason=reason,
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=physical_attempt_ids,
            physical_attempt_events=physical_attempt_events,
        )
    if registration.source_kind is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE:
        if not isinstance(request, FinancialCompanyTypeEvidenceRoutingRequest):
            raise ValueError("qualified company-type evidence request is invalid")
        return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
            provider_id=registration.provider_id,
            metadata_binding=request.metadata_binding,
            reason=reason,
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=physical_attempt_ids,
        )
    capability = getattr(request, "capability", None)
    if capability not in registration.capabilities:
        capability = registration.capabilities[0]
    return FinancialCapabilityProviderResponse.unavailable(
        provider_id=registration.provider_id,
        capability=capability,
        reason=reason,
        subrequest_keys=subrequest_keys,
        physical_attempt_ids=physical_attempt_ids,
    )


def _external_response_with_attempts(
    *,
    registration: ProductionQualifiedSourceRegistration,
    request: object,
    response: object,
    result: ProviderSubrequestResult,
):
    subrequest_keys = (result.key.subrequest_key,)
    attempt_ids = tuple(event.attempt_event_id for event in result.attempt_events)
    if not isinstance(
        response,
        (
            FinancialProviderPeriodResponse,
            FinancialCapabilityProviderResponse,
            FinancialCompanyTypeEvidenceProviderResponse,
        ),
    ):
        return _external_unavailable_response(
            registration,
            request,
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=attempt_ids,
            physical_attempt_events=result.attempt_events,
        )
    payload = response.model_dump(mode="python")
    payload["subrequest_keys"] = subrequest_keys
    payload["physical_attempt_ids"] = attempt_ids
    if isinstance(response, FinancialProviderPeriodResponse):
        payload["physical_attempt_events"] = result.attempt_events
        payload["subrequest_outcomes"] = ()
        return FinancialProviderPeriodResponse.model_validate(payload)
    if isinstance(response, FinancialCompanyTypeEvidenceProviderResponse):
        return FinancialCompanyTypeEvidenceProviderResponse.model_validate(payload)
    return FinancialCapabilityProviderResponse.model_validate(payload)


def _execute_external_source(
    *,
    registration: ProductionQualifiedSourceRegistration,
    bound_source: ProductionQualifiedBoundSource,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    request: object,
):
    key = _external_subrequest_key(
        registration=registration,
        bound_source=bound_source,
        blueprint=blueprint,
        request=request,
    )
    result = bound_source.subrequest_cache.execute(
        key,
        owner_id=bound_source.owner_id,
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=bound_source.now,
        sleep=bound_source.sleep,
        lease_duration=bound_source.lease_duration,
        max_physical_attempts=bound_source.max_physical_attempts,
        operation=f"qualified_{registration.endpoint_id}",
        media_type="application/json",
        physical_request=lambda: bound_source.physical_request(request),
    )
    if result.value is None:
        return _external_unavailable_response(
            registration,
            request,
            reason=_adapter_unavailable_reason(result.outcome),
            subrequest_keys=(result.key.subrequest_key,),
            physical_attempt_ids=tuple(
                event.attempt_event_id for event in result.attempt_events
            ),
            physical_attempt_events=result.attempt_events,
        )
    try:
        decoder = bound_source.response_decoder
        try:
            decoder_signature = inspect_signature(decoder)
        except (TypeError, ValueError):
            response = decoder(request, result.value)  # type: ignore[call-arg]
        else:
            try:
                decoder_signature.bind(request, result.value, result)
            except TypeError:
                try:
                    decoder_signature.bind(request, result.value)
                except TypeError:
                    raise ValueError(
                        "qualified response decoder signature is incompatible"
                    ) from None
                response = decoder(request, result.value)  # type: ignore[call-arg]
            else:
                response = decoder(  # type: ignore[call-arg]
                    request,
                    result.value,
                    result,
                )
    except ProductionQualifiedSourceUnavailableError as failure:
        response = failure.response
    except Exception:
        response = None
    return _external_response_with_attempts(
        registration=registration,
        request=request,
        response=response,
        result=result,
    )


def _safe_external_source(
    registration: ProductionQualifiedSourceRegistration,
    bound_source: ProductionQualifiedBoundSource,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
) -> Callable[[object], object]:
    def unavailable(request: object, response: object | None = None):
        subrequest_keys = tuple(getattr(response, "subrequest_keys", ()))
        physical_attempt_ids = tuple(getattr(response, "physical_attempt_ids", ()))
        physical_attempt_events = tuple(
            getattr(response, "physical_attempt_events", ())
        )
        return _external_unavailable_response(
            registration,
            request,
            reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=physical_attempt_ids,
            physical_attempt_events=physical_attempt_events,
        )

    def safe_source(request: object):
        request_provider = getattr(request, "provider_id", registration.provider_id)
        if registration.source_kind is ProductionQualifiedSourceKind.STATEMENT:
            statement_type = getattr(request, "statement_type", None)
            try:
                request_capability = _statement_capability(statement_type)
            except (KeyError, TypeError):
                return unavailable(request)
        elif registration.source_kind is ProductionQualifiedSourceKind.INDICATOR:
            request_capability = MainlandCapability.FINANCIAL_INDICATORS
        elif (
            registration.source_kind
            is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE
        ):
            request_capability = MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
        else:
            request_capability = getattr(request, "capability", None)
        if (
            request_provider != registration.provider_id
            or request_capability not in registration.capabilities
        ):
            return unavailable(request)
        try:
            response = _execute_external_source(
                registration=registration,
                bound_source=bound_source,
                blueprint=blueprint,
                request=request,
            )
        except Exception:
            return unavailable(request)
        if registration.source_kind in {
            ProductionQualifiedSourceKind.STATEMENT,
            ProductionQualifiedSourceKind.INDICATOR,
        }:
            if (
                not isinstance(response, FinancialProviderPeriodResponse)
                or response.provider_id != registration.provider_id
            ):
                return unavailable(request, response)
            if (
                registration.source_kind is ProductionQualifiedSourceKind.INDICATOR
                and registration.provider_id == "yfinance"
                and response.kind.value == "available"
                and (
                    not response.degraded_current_profile
                    or bool(response.candidates)
                )
            ):
                return unavailable(request, response)
            return response
        if (
            registration.source_kind
            is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE
        ):
            if (
                not isinstance(response, FinancialCompanyTypeEvidenceProviderResponse)
                or response.provider_id != registration.provider_id
                or not isinstance(request, FinancialCompanyTypeEvidenceRoutingRequest)
                or response.metadata_binding != request.metadata_binding
            ):
                return unavailable(request, response)
            return response
        if (
            not isinstance(response, FinancialCapabilityProviderResponse)
            or response.provider_id != registration.provider_id
            or response.capability is not request_capability
        ):
            return unavailable(request, response)
        if (
            registration.provider_id == "yfinance_derived"
            and response.kind.value == "available"
            and (not response.derived or not response.degraded)
        ):
            return unavailable(request, response)
        return response

    return safe_source


class _ProductionQualifiedRequestFactories:
    """Lifecycle wrapper around the pure Ticket 02C two-input projections."""

    def __init__(self, run_binding: ProductionQualifiedRoutingRunBinding) -> None:
        self._asset_configuration = run_binding.asset_configuration
        self._routing_plan = run_binding.routing_plan
        self._closed = False
        self._lock = threading.Lock()

    def _require_open(self) -> None:
        with self._lock:
            if self._closed:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.CLOSED
                )

    def statement(
        self,
        request: object,
        context: object | None = None,
    ) -> FinancialStatementRoutingRequest:
        self._require_open()
        if not isinstance(request, FinancialToolRequest) or not isinstance(
            context,
            FinancialEligibilityAcquisitionContext,
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        return build_financial_statement_routing_request(request, context)

    def indicator(
        self,
        request: object,
        context: object | None = None,
    ) -> FinancialIndicatorRoutingRequest:
        self._require_open()
        if not isinstance(request, FinancialToolRequest) or not isinstance(
            context,
            FinancialEligibilityAcquisitionContext,
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        return build_financial_indicator_routing_request(request, context)

    def interrupt(self) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __repr__(self) -> str:
        return "ProductionQualifiedRequestFactories(run_owned=True)"


class _ProductionQualifiedAdapterSet:
    """Explicit ownership marker for the immutable set of bound adapters."""

    def __init__(self, adapters: tuple[object, ...]) -> None:
        self._adapters = adapters
        self._closed = False
        self._lock = threading.Lock()

    def interrupt(self) -> None:
        for adapter in reversed(self._adapters):
            interrupt = getattr(adapter, "interrupt", None)
            if callable(interrupt):
                interrupt()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

    def __repr__(self) -> str:
        return "ProductionQualifiedAdapterSet(run_owned=True)"


def _production_executable_source_registrations(
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
) -> tuple[ProductionQualifiedSourceRegistration, ...]:
    """Replace six terminal seams and add Yahoo's required evidence projection."""

    plan_pairs = {
        pair
        for pair in _ordinary_plan_pairs(blueprint.routing_plan)
        if _internal_source_endpoint(*pair) is None
    }
    statements = tuple(
        capability
        for capability in _STATEMENT_CAPABILITIES
        if (capability, "yfinance") in plan_pairs
    )
    specifications: list[
        tuple[
            str,
            str,
            ProductionQualifiedSourceKind,
            tuple[MainlandCapability, ...],
        ]
    ] = []
    if statements:
        specifications.append(
            (
                "yfinance",
                "financial_statements",
                ProductionQualifiedSourceKind.STATEMENT,
                statements,
            )
        )
    candidates = (
        (
            "akshare",
            "financial_indicators",
            ProductionQualifiedSourceKind.INDICATOR,
            (MainlandCapability.FINANCIAL_INDICATORS,),
        ),
        (
            "yfinance",
            "current_financial_profile",
            ProductionQualifiedSourceKind.INDICATOR,
            (MainlandCapability.FINANCIAL_INDICATORS,),
        ),
        (
            "yfinance",
            "current_profile_company_type_evidence",
            ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE,
            (MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE,),
        ),
        (
            "akshare",
            "adjustment_factors",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.ADJUSTMENT_FACTORS,),
        ),
        (
            "yfinance_derived",
            "derived_adjustment_factors",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.ADJUSTMENT_FACTORS,),
        ),
        (
            "akshare",
            "issuer_name_events",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.NAME_EVENTS,),
        ),
    )
    specifications.extend(
        specification
        for specification in candidates
        if all(
            (capability, specification[0]) in plan_pairs
            for capability in specification[3]
        )
    )

    registrations: list[ProductionQualifiedSourceRegistration] = []
    for provider_id, endpoint_id, source_kind, capabilities in specifications:
        physical_provider_id = (
            "yfinance"
            if provider_id == "yfinance_derived"
            else "akshare_sina"
            if provider_id == "akshare"
            else provider_id
        )
        client_id = "akshare" if provider_id == "akshare" else physical_provider_id
        def source_factory(
            dependencies: ProductionQualifiedRoutingRunDependencies,
            *,
            _physical_provider_id=physical_provider_id,
            _client_id=client_id,
            _endpoint_id=endpoint_id,
        ) -> ProductionQualifiedBoundSource:
            client = dependencies.provider_clients[_client_id]

            if _endpoint_id == "financial_statements":
                def yahoo_statement_request(request: object) -> bytes:
                    if not isinstance(request, FinancialProviderGapRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="yfinance_statement_request_incompatible",
                        )
                    return acquire_yahoo_statement_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def yahoo_statement_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialProviderPeriodResponse:
                    if not isinstance(request, FinancialProviderGapRequest):
                        raise ValueError("Yahoo statement request is incompatible")
                    return decode_yahoo_statement_response(request, payload, result)

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=yahoo_statement_request,
                    response_decoder=yahoo_statement_decoder,
                    adapter=client,
                    normalizer_version=YFINANCE_STATEMENT_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "financial_indicators":
                def akshare_indicator_request(request: object) -> bytes:
                    if not isinstance(request, FinancialIndicatorProviderGapRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="akshare_indicator_request_incompatible",
                        )
                    return acquire_akshare_indicator_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def akshare_indicator_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialProviderPeriodResponse:
                    if not isinstance(request, FinancialIndicatorProviderGapRequest):
                        raise ValueError("AKShare indicator request is incompatible")
                    return decode_akshare_indicator_response(request, payload, result)

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=akshare_indicator_request,
                    response_decoder=akshare_indicator_decoder,
                    adapter=client,
                    normalizer_version=AKSHARE_INDICATOR_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "current_financial_profile":
                def yahoo_profile_request(request: object) -> bytes:
                    if not isinstance(request, FinancialIndicatorProviderGapRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="yfinance_profile_request_incompatible",
                        )
                    return acquire_yahoo_profile_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def yahoo_profile_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialProviderPeriodResponse:
                    if not isinstance(request, FinancialIndicatorProviderGapRequest):
                        raise ValueError("Yahoo profile request is incompatible")
                    return decode_yahoo_profile_response(request, payload, result)

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=yahoo_profile_request,
                    response_decoder=yahoo_profile_decoder,
                    adapter=client,
                    normalizer_version=YFINANCE_PROFILE_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "current_profile_company_type_evidence":
                def yahoo_company_type_request(request: object) -> bytes:
                    if not isinstance(
                        request,
                        FinancialCompanyTypeEvidenceRoutingRequest,
                    ):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="yfinance_company_type_request_incompatible",
                        )
                    return acquire_yahoo_profile_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def yahoo_company_type_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialCompanyTypeEvidenceProviderResponse:
                    if not isinstance(
                        request,
                        FinancialCompanyTypeEvidenceRoutingRequest,
                    ):
                        raise ValueError("Yahoo company-type request is incompatible")
                    return decode_yahoo_company_type_evidence_response(
                        request,
                        payload,
                        result,
                    )

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=yahoo_company_type_request,
                    response_decoder=yahoo_company_type_decoder,
                    adapter=client,
                    normalizer_version=YFINANCE_PROFILE_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "adjustment_factors":
                def akshare_factor_request(request: object) -> bytes:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="akshare_factor_request_incompatible",
                        )
                    return acquire_akshare_factor_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def akshare_factor_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialCapabilityProviderResponse:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise ValueError("AKShare factor request is incompatible")
                    return decode_akshare_factor_response(
                        request,
                        payload,
                        result,
                        qualification_profile=blueprint.routing_plan.qualification_profile,
                    )

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=akshare_factor_request,
                    response_decoder=akshare_factor_decoder,
                    adapter=client,
                    normalizer_version=AKSHARE_FACTOR_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "derived_adjustment_factors":
                def yahoo_factor_request(request: object) -> bytes:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="yfinance_factor_request_incompatible",
                        )
                    return acquire_yahoo_derived_factor_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def yahoo_factor_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialCapabilityProviderResponse:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise ValueError("Yahoo factor request is incompatible")
                    return decode_yahoo_derived_factor_response(
                        request,
                        payload,
                        result,
                        qualification_profile=blueprint.routing_plan.qualification_profile,
                    )

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=yahoo_factor_request,
                    response_decoder=yahoo_factor_decoder,
                    adapter=client,
                    normalizer_version=YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            if _endpoint_id == "issuer_name_events":
                def akshare_name_request(request: object) -> bytes:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                            retryable=False,
                            error_code="akshare_name_request_incompatible",
                        )
                    return acquire_akshare_name_payload(
                        facade=client,
                        request=request,
                        now=dependencies.now,
                    )

                def akshare_name_decoder(
                    request: object,
                    payload: bytes,
                    result: ProviderSubrequestResult,
                ) -> FinancialCapabilityProviderResponse:
                    if not isinstance(request, FinancialCapabilityRoutingRequest):
                        raise ValueError("AKShare name request is incompatible")
                    return decode_akshare_name_response(
                        request,
                        payload,
                        result,
                        qualification_profile=blueprint.routing_plan.qualification_profile,
                    )

                return ProductionQualifiedBoundSource.bind(
                    dependencies=dependencies,
                    physical_provider_id=_physical_provider_id,
                    physical_request=akshare_name_request,
                    response_decoder=akshare_name_decoder,
                    adapter=client,
                    normalizer_version=AKSHARE_NAME_NORMALIZER_VERSION,
                    max_physical_attempts=1,
                )
            raise ValueError("unknown qualified production source endpoint")

        registrations.append(
            ProductionQualifiedSourceRegistration(
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                source_kind=source_kind,
                capabilities=capabilities,
                source_factory=source_factory,
            )
        )
    return tuple(registrations)


def _assemble_production_qualified_runtime(
    *,
    blueprint: object,
    run_binding: object,
    coordinator: object,
    coordinator_store: object,
    artifact_store: object,
    subrequest_cache: object,
    provider_clients: Mapping[str, object],
    stop_signal: threading.Event,
    acquire_owned_resource: Callable[[str, object], object],
) -> ProductionQualifiedRoutingRuntimeAssembly:
    if (
        not isinstance(blueprint, ProductionQualifiedRoutingCompositionBlueprint)
        or not isinstance(run_binding, ProductionQualifiedRoutingRunBinding)
        or not isinstance(coordinator, ProviderRequestCoordinator)
        or not isinstance(coordinator_store, MarketHistoryStore)
        or not isinstance(artifact_store, ProviderSubrequestArtifactStore)
        or not isinstance(subrequest_cache, ProviderSubrequestCache)
        or not callable(acquire_owned_resource)
    ):
        raise ProductionQualifiedRoutingLifecycleError(
            ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
        )
    dependencies = ProductionQualifiedRoutingRunDependencies(
        coordinator=coordinator,
        subrequest_cache=subrequest_cache,
        artifact_store=artifact_store,
        provider_clients=provider_clients,
        now=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
        owner_id=run_binding.run_id,
        lease_duration=timedelta(seconds=30),
    )
    source_composition = assemble_production_qualified_provider_sources(
        blueprint=blueprint,
        dependencies=dependencies,
        source_registrations=_production_executable_source_registrations(blueprint),
    )
    unique_adapters: list[object] = []
    client_ids = {id(client) for client in provider_clients.values()}
    for binding in source_composition.bindings:
        if id(binding.adapter) not in client_ids and all(
            binding.adapter is not adapter for adapter in unique_adapters
        ):
            unique_adapters.append(binding.adapter)
    adapter_set = _ProductionQualifiedAdapterSet(tuple(unique_adapters))
    acquire_owned_resource("provider_adapters", adapter_set)
    request_factories = _ProductionQualifiedRequestFactories(run_binding)
    acquire_owned_resource("request_factories", request_factories)
    from tradingagents.graph.financial_tools import (
        QualifiedFinancialRoutingComposition,
    )

    enabled = set(blueprint.enabled_capabilities)
    financial_routing = QualifiedFinancialRoutingComposition(
        router=source_composition.router,
        statement_request_factory=(
            request_factories.statement
            if TushareCapability.STATEMENTS in enabled
            else None
        ),
        indicator_request_factory=(
            request_factories.indicator
            if TushareCapability.FINANCIAL_INDICATORS in enabled
            else None
        ),
    )
    eligibility_metadata_acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=blueprint.routing_plan,
        router=source_composition.router,
        stop_signal=stop_signal,
    )
    return ProductionQualifiedRoutingRuntimeAssembly(
        financial_routing=financial_routing,
        source_composition=source_composition,
        dependencies=dependencies,
        eligibility_metadata_acquirer=eligibility_metadata_acquirer,
    )


def assemble_production_qualified_provider_sources(
    *,
    blueprint: ProductionQualifiedRoutingCompositionBlueprint,
    dependencies: ProductionQualifiedRoutingRunDependencies,
    source_registrations: tuple[ProductionQualifiedSourceRegistration, ...],
) -> ProductionQualifiedProviderSourceComposition:
    """Bind the plan's exact non-market routes without physical provider work."""

    _validate_run_dependencies(blueprint, dependencies)
    plan_pairs = _ordinary_plan_pairs(blueprint.routing_plan)
    external_pairs = {
        pair
        for pair in plan_pairs
        if _internal_source_endpoint(*pair) is None
    }
    registration_by_pair: dict[
        tuple[MainlandCapability, str],
        ProductionQualifiedSourceRegistration,
    ] = {}
    registration_ids: set[str] = set()
    try:
        registrations = tuple(source_registrations)
    except Exception:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
        )
    for registration in registrations:
        if not isinstance(registration, ProductionQualifiedSourceRegistration):
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
            )
        if registration.registration_id in registration_ids:
            _fail(
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
            )
        registration_ids.add(registration.registration_id)
        for capability in registration.capabilities:
            pair = (capability, registration.provider_id)
            if pair not in external_pairs or pair in registration_by_pair:
                _fail(
                    MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
                )
            registration_by_pair[pair] = registration
    if set(registration_by_pair) != external_pairs:
        _fail(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
        )

    built_registration_sources: dict[
        int,
        tuple[Callable[[object], object], ProductionQualifiedBoundSource],
    ] = {}
    pair_sources: dict[tuple[MainlandCapability, str], Callable[[object], object]] = {}
    bindings: list[ProductionQualifiedSourceBinding] = []
    adapter_cache: dict[str, object] = {}
    internal_binding_keys: set[tuple[MainlandCapability, str]] = set()
    external_binding_ids: set[int] = set()
    for capability, provider_id in plan_pairs:
        internal = _internal_source_endpoint(capability, provider_id)
        if internal is not None:
            source_kind, endpoint_id = internal
            adapter, source = _build_internal_adapter(
                capability=capability,
                provider_id=provider_id,
                blueprint=blueprint,
                dependencies=dependencies,
                adapter_cache=adapter_cache,
            )
            pair_sources[(capability, provider_id)] = source
            binding_key = (capability, provider_id)
            if binding_key not in internal_binding_keys:
                internal_binding_keys.add(binding_key)
                bindings.append(
                    ProductionQualifiedSourceBinding(
                        provider_id=provider_id,
                        endpoint_id=endpoint_id,
                        source_kind=source_kind,
                        capabilities=(capability,),
                        source=source,
                        coordinator=dependencies.coordinator,
                        subrequest_cache=dependencies.subrequest_cache,
                        artifact_store=dependencies.artifact_store,
                        adapter=adapter,
                    )
                )
            continue

        registration = registration_by_pair[(capability, provider_id)]
        registration_key = id(registration)
        if registration_key not in built_registration_sources:
            try:
                bound_source = registration.source_factory(dependencies)
            except Exception:
                _fail(
                    MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
                )
            if (
                not isinstance(bound_source, ProductionQualifiedBoundSource)
                or not bound_source.is_bound_to(dependencies)
                or not _physical_provider_is_compatible(
                    route_provider_id=registration.provider_id,
                    physical_provider_id=bound_source.physical_provider_id,
                )
            ):
                _fail(
                    MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
                )
            built_registration_sources[registration_key] = (
                _safe_external_source(registration, bound_source, blueprint),
                bound_source,
            )
        source, bound_source = built_registration_sources[registration_key]
        pair_sources[(capability, provider_id)] = source
        if registration_key not in external_binding_ids:
            external_binding_ids.add(registration_key)
            bindings.append(
                ProductionQualifiedSourceBinding(
                    provider_id=registration.provider_id,
                    endpoint_id=registration.endpoint_id,
                    source_kind=registration.source_kind,
                    capabilities=registration.capabilities,
                    source=source,
                    coordinator=dependencies.coordinator,
                    subrequest_cache=dependencies.subrequest_cache,
                    artifact_store=dependencies.artifact_store,
                    adapter=bound_source.adapter,
                )
            )

    statement_source_ids = blueprint.routing_plan.route_for(
        MainlandCapability.BALANCE_SHEET
    )

    def statement_source(provider_id: str) -> FinancialPeriodSource:
        def dispatch(request: FinancialProviderGapRequest):
            capability = _statement_capability(request.statement_type)
            return pair_sources[(capability, provider_id)](request)

        return dispatch

    statement_sources = MappingProxyType(
        {
            provider_id: statement_source(provider_id)
            for provider_id in statement_source_ids
        }
    )
    indicator_source_ids = blueprint.routing_plan.route_for(
        MainlandCapability.FINANCIAL_INDICATORS
    )
    indicator_sources: Mapping[str, FinancialIndicatorSource] = MappingProxyType(
        {
            provider_id: pair_sources[
                (MainlandCapability.FINANCIAL_INDICATORS, provider_id)
            ]
            for provider_id in indicator_source_ids
        }
    )
    capability_source_ids = tuple(
        (capability, blueprint.routing_plan.route_for(capability))
        for capability in _ROUTABLE_CAPABILITIES
    )
    capability_sources: Mapping[
        MainlandCapability,
        Mapping[str, FinancialCapabilitySource],
    ] = MappingProxyType(
        {
            capability: MappingProxyType(
                {
                    provider_id: pair_sources[(capability, provider_id)]
                    for provider_id in provider_ids
                }
            )
            for capability, provider_ids in capability_source_ids
        }
    )
    company_type_evidence_source_ids = blueprint.routing_plan.route_for(
        MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
    )
    company_type_evidence_sources: Mapping[
        str,
        FinancialCompanyTypeEvidenceSource,
    ] = MappingProxyType(
        {
            provider_id: pair_sources[
                (
                    MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE,
                    provider_id,
                )
            ]
            for provider_id in company_type_evidence_source_ids
        }
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=blueprint.routing_plan,
        statement_sources=statement_sources,
        indicator_sources=indicator_sources,
        capability_sources=capability_sources,
        company_type_evidence_sources=company_type_evidence_sources,
    )
    return ProductionQualifiedProviderSourceComposition(
        blueprint=blueprint,
        dependencies=dependencies,
        bindings=tuple(bindings),
        router=router,
        statement_source_ids=statement_source_ids,
        indicator_source_ids=indicator_source_ids,
        company_type_evidence_source_ids=company_type_evidence_source_ids,
        capability_source_ids=capability_source_ids,
    )


__all__ = [
    "ManagedQualifiedRoutingComposition",
    "PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION",
    "PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION",
    "ProductionFinancialEligibilityRuntimeInputs",
    "ProductionQualifiedBoundSource",
    "ProductionBaoStockRunTransport",
    "ProductionQualifiedRoutingCompositionBlueprint",
    "ProductionQualifiedRoutingFactoryContract",
    "ProductionQualifiedRoutingLifecycleDiagnostic",
    "ProductionQualifiedRoutingLifecycleError",
    "ProductionQualifiedRoutingLifecycleFailureReason",
    "ProductionQualifiedRoutingAssemblyBinding",
    "ProductionQualifiedRoutingRuntime",
    "ProductionQualifiedRoutingRuntimeAssembly",
    "ProductionQualifiedRoutingRuntimeFactories",
    "ProductionQualifiedRoutingRunBinding",
    "RegisteredProductionQualifiedRoutingComposition",
    "ProductionQualifiedRoutingRunDependencies",
    "ProductionQualifiedProviderSourceComposition",
    "ProductionQualifiedSourceBinding",
    "ProductionQualifiedSourceKind",
    "ProductionQualifiedSourceRegistration",
    "ProductionQualifiedSourceUnavailableError",
    "ProductionRuntimeRootIdentity",
    "assemble_production_qualified_provider_sources",
    "build_production_qualified_routing_composition_blueprint",
    "build_registered_production_qualified_routing_composition",
    "production_qualified_routing_dependency_availability",
    "production_qualified_routing_provider_source_coverage",
]
