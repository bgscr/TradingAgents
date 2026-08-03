"""Phase-one contract for production qualified-routing composition.

This module validates and freezes construction material only.  Provider adapters,
stores, graphs, models, checkpoints, and lifecycle ownership are deliberately not
constructed here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
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
    TushareCapability,
    TushareEndpointPacingIdentity,
    mainland_capability_routing_failure,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityProjectionBinding,
)
from tradingagents.evidence import InstrumentKind
from tradingagents.market_history.config import DataUsageMode

PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION = (
    "production-qualified-routing-composition-factory-v1"
)
PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION = (
    "production-qualified-routing-composition-blueprint-v1"
)

_BLUEPRINT_IDENTITY_PREFIX = "production-qualified-routing-blueprint:v1:"
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_IDENTITY_PATTERN = r"^[a-z][a-z0-9._:-]*$"
_REQUIRED_DEPENDENCY_IDS = ("akshare", "baostock", "tushare", "yfinance")
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
    """Single owner-capable boundary implemented by later lifecycle tickets."""

    blueprint: ProductionQualifiedRoutingCompositionBlueprint

    def bind_run(self, run_binding: object) -> object: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


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


__all__ = [
    "ManagedQualifiedRoutingComposition",
    "PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION",
    "PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION",
    "ProductionQualifiedRoutingCompositionBlueprint",
    "ProductionQualifiedRoutingFactoryContract",
    "ProductionRuntimeRootIdentity",
    "build_production_qualified_routing_composition_blueprint",
]
