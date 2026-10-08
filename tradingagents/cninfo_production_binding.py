"""Explicit, resource-free production binding of the qualified CNINFO policy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.dataflows.cninfo_listing_publication import (
    CNINFO_ANNOUNCEMENT_QUERY_URL,
    CNINFO_CANDIDATE_PROFILE,
    CNINFO_SECURITY_LOOKUP_URL,
    CNINFO_SOURCE_POLICY_VERSION,
    CninfoListingBoard,
    CninfoSourcePolicy,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.coordinator import upstream_service_identity_for_provider

if TYPE_CHECKING:
    from tradingagents.capability_routing import MainlandCapabilityRoutingPlan


PRODUCTION_PLAN_VERSION_CONFIG_KEY = "mainland_capability_routing_plan_version"
CNINFO_PRODUCTION_CONFIG_KEYS = (
    "cninfo_listing_publication_profile",
    "cninfo_source_policy_version",
    "cninfo_operator_minimum_interval_seconds",
)
CNINFO_PRODUCTION_DEPENDENCY_IDS = ("lxml", "requests")
CNINFO_PROVIDER_SCOPES = ("cninfo_metadata", "cninfo_documents")
CNINFO_UPSTREAM_SERVICE_IDS = tuple(
    upstream_service_identity_for_provider(provider_scope)[0]
    for provider_scope in CNINFO_PROVIDER_SCOPES
)
_CLOSED = ConfigDict(
    extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
)


def _identity(namespace: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return namespace + sha256(encoded).hexdigest()


class MainlandProductionPlanVersion(str, Enum):
    """Allowlisted production selections; historical readers remain separate."""

    PLAN_1_4 = "1.4"
    PLAN_1_5 = "1.5"


def authoritative_cninfo_listing_board(
    identity: InstrumentIdentityEvidence,
) -> CninfoListingBoard:
    """Map canonical equity code families only after authoritative venue binding."""

    identity = InstrumentIdentityEvidence.model_validate(identity.model_dump(mode="json"))
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
    ):
        raise ValueError("CNINFO requires authoritative mainland equity identity")
    routes = (
        ("XSHG", r"60[0135][0-9]{3}\.SS", CninfoListingBoard.XSHG_MAIN),
        ("XSHE", r"30[01][0-9]{3}\.SZ", CninfoListingBoard.XSHE_CHINEXT),
        ("XSHG", r"688[0-9]{3}\.SS", CninfoListingBoard.XSHG_STAR),
    )
    for venue, pattern, board in routes:
        if identity.venue == venue and re.fullmatch(pattern, identity.symbol):
            return board
    raise ValueError("CNINFO listing board is unsupported or contradicts identity")


class CninfoUpstreamPacingIdentity(BaseModel):
    model_config = _CLOSED

    provider_scope: Literal["cninfo_metadata", "cninfo_documents"]
    upstream_service_id: str = Field(pattern=r"^upstream-service=sha256:[0-9a-f]{64}$")
    capacity_scope: Literal["cninfo.metadata", "cninfo.documents"]
    minimum_interval_seconds: float = Field(ge=1, le=60)
    operator_safety_ceiling_identity: Literal["cninfo-operator-safety-ceiling-v1"]
    max_in_flight: Literal[1]
    allow_prewarming: Literal[False]


class CninfoProductionProfile(BaseModel):
    """Instance of the existing candidate profile, with no pacing default."""

    model_config = _CLOSED

    contract_version: Literal["cninfo-listing-publication-production-profile-v1"]
    profile_id: Literal["cninfo-listing-publication-candidate-v1"]
    plan_version: Literal["1.5"]
    capability: Literal["listing_publication_evidence"]
    capability_contract_version: Literal["listing-publication-evidence-v1"]
    provider_id: Literal["cninfo"]
    source_policy: CninfoSourcePolicy
    source_policy_identity: str = Field(pattern=r"^cninfo-source-policy:v1:[0-9a-f]{64}$")
    upstream_pacing: tuple[CninfoUpstreamPacingIdentity, CninfoUpstreamPacingIdentity]
    metadata_endpoints: tuple[str, str]
    document_origin: Literal["https://static.cninfo.com.cn/"]
    lookup_policy: Literal["exact_instrument_bounded_date_window_dynamic_org_id"]
    metadata_policy: Literal["metadata_first"]
    html_policy: Literal["ambiguous_metadata_only"]
    html_parser_version: Literal["cninfo_legacy_html_v1+lxml-6.1.1"]
    pdf_policy: Literal["unqualified_unavailable"]
    profile_identity: str = Field(pattern=r"^cninfo-production-profile:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_qualified_profile(self) -> CninfoProductionProfile:
        policy = CninfoSourcePolicy.model_validate(self.source_policy.model_dump(mode="json"))
        if not policy.eligible:
            raise ValueError("CNINFO production binding requires personal research")
        if self.metadata_endpoints != (CNINFO_SECURITY_LOOKUP_URL, CNINFO_ANNOUNCEMENT_QUERY_URL):
            raise ValueError("CNINFO endpoint binding is invalid")
        if (
            tuple(item.upstream_service_id for item in self.upstream_pacing)
            != CNINFO_UPSTREAM_SERVICE_IDS
            or tuple(item.provider_scope for item in self.upstream_pacing) != CNINFO_PROVIDER_SCOPES
            or any(
                item.minimum_interval_seconds
                != self.source_policy.operator_minimum_interval_seconds
                or item.capacity_scope != item.provider_scope.replace("_", ".")
                for item in self.upstream_pacing
            )
        ):
            raise ValueError("CNINFO pacing and source policy contradict")
        if self.source_policy_identity != _identity(
            "cninfo-source-policy:v1:", self.source_policy.model_dump(mode="json")
        ) or self.profile_identity != _identity(
            "cninfo-production-profile:v1:",
            self.model_dump(mode="json", exclude={"profile_identity"}),
        ):
            raise ValueError("CNINFO profile identity is invalid")
        return self

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> CninfoProductionProfile:
        if (
            config.get("cninfo_listing_publication_profile") != CNINFO_CANDIDATE_PROFILE
            or config.get("cninfo_source_policy_version") != CNINFO_SOURCE_POLICY_VERSION
        ):
            raise ValueError("CNINFO profile and source policy must be explicitly selected")
        interval = config.get("cninfo_operator_minimum_interval_seconds")
        if isinstance(interval, bool) or not isinstance(interval, (str, int, float)):
            raise ValueError("CNINFO requires an explicit Operator Safety Ceiling")
        # The qualified policy owns the numeric bounds and fixed request limits.
        # The operator supplies the interval; neither source nor environment has
        # an implicit default that can select provider traffic for a launch.
        policy = CninfoSourcePolicy(
            contract_version=config["cninfo_source_policy_version"],
            data_usage_mode=config.get("data_usage_mode"),
            operator_minimum_interval_seconds=interval,
        )
        pacing = tuple(
            CninfoUpstreamPacingIdentity(
                provider_scope=provider_scope,
                upstream_service_id=upstream_service_identity_for_provider(provider_scope)[0],
                capacity_scope=provider_scope.replace("_", "."),
                minimum_interval_seconds=policy.operator_minimum_interval_seconds,
                operator_safety_ceiling_identity="cninfo-operator-safety-ceiling-v1",
                max_in_flight=1,
                allow_prewarming=False,
            )
            for provider_scope in CNINFO_PROVIDER_SCOPES
        )
        payload = {
            "contract_version": "cninfo-listing-publication-production-profile-v1",
            "profile_id": CNINFO_CANDIDATE_PROFILE,
            "plan_version": "1.5",
            "capability": "listing_publication_evidence",
            "capability_contract_version": "listing-publication-evidence-v1",
            "provider_id": "cninfo",
            "source_policy": policy.model_dump(mode="json"),
            "source_policy_identity": _identity(
                "cninfo-source-policy:v1:", policy.model_dump(mode="json")
            ),
            "upstream_pacing": [item.model_dump(mode="json") for item in pacing],
            "metadata_endpoints": [CNINFO_SECURITY_LOOKUP_URL, CNINFO_ANNOUNCEMENT_QUERY_URL],
            "document_origin": "https://static.cninfo.com.cn/",
            "lookup_policy": "exact_instrument_bounded_date_window_dynamic_org_id",
            "metadata_policy": "metadata_first",
            "html_policy": "ambiguous_metadata_only",
            "html_parser_version": "cninfo_legacy_html_v1+lxml-6.1.1",
            "pdf_policy": "unqualified_unavailable",
        }
        return cls(**payload, profile_identity=_identity("cninfo-production-profile:v1:", payload))


class CninfoProductionBinding(BaseModel):
    """Closed per-instrument binding carried by the production blueprint."""

    model_config = _CLOSED

    contract_version: Literal["cninfo-listing-publication-production-binding-v1"]
    plan_signature: str = Field(pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$")
    profile: CninfoProductionProfile
    instrument_identity: InstrumentIdentityEvidence
    asset_configuration_signature: str = Field(pattern=r"^asset-config:v1:[0-9a-f]{64}$")
    authoritative_listing_board: CninfoListingBoard
    listing_board_binding_version: Literal["instrument-identity-cninfo-listing-board-v1"]
    legacy_authoritative: Literal[True]
    qualified_v1_authoritative: Literal[False]
    qualified_shadow_authoritative: Literal[False]
    binding_identity: str = Field(pattern=r"^cninfo-production-binding:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_identity_and_authority(self) -> CninfoProductionBinding:
        if self.authoritative_listing_board is not authoritative_cninfo_listing_board(
            self.instrument_identity
        ):
            raise ValueError("CNINFO listing board contradicts Instrument Identity")
        if self.binding_identity != _identity(
            "cninfo-production-binding:v1:",
            self.model_dump(mode="json", exclude={"binding_identity"}),
        ):
            raise ValueError("CNINFO production binding identity is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        plan: MainlandCapabilityRoutingPlan,
        asset_configuration: RunAssetConfiguration,
        config: Mapping[str, Any],
    ) -> CninfoProductionBinding:
        if (
            config.get(PRODUCTION_PLAN_VERSION_CONFIG_KEY) != "1.5"
            or plan.plan_version != "1.5"
            or plan.mode.value != "qualified_v1_shadow"
        ):
            raise ValueError("CNINFO production binding requires explicit shadow plan 1.5")
        profile = CninfoProductionProfile.from_config(config)
        if plan.listing_publication_candidate_profile != profile.profile_id:
            raise ValueError("CNINFO profile and routing plan contradict")
        payload = {
            "contract_version": "cninfo-listing-publication-production-binding-v1",
            "plan_signature": plan.plan_signature,
            "profile": profile.model_dump(mode="json"),
            "instrument_identity": asset_configuration.instrument_identity.model_dump(mode="json"),
            "asset_configuration_signature": asset_configuration.asset_configuration_signature,
            "authoritative_listing_board": authoritative_cninfo_listing_board(
                asset_configuration.instrument_identity
            ).value,
            "listing_board_binding_version": "instrument-identity-cninfo-listing-board-v1",
            "legacy_authoritative": True,
            "qualified_v1_authoritative": False,
            "qualified_shadow_authoritative": False,
        }
        return cls(**payload, binding_identity=_identity("cninfo-production-binding:v1:", payload))


def select_production_mainland_capability_routing_plan(
    predecessor: MainlandCapabilityRoutingPlan,
    *,
    asset_configuration: RunAssetConfiguration,
    config: Mapping[str, Any],
) -> MainlandCapabilityRoutingPlan:
    """Apply only explicit production selections to the unchanged base preflight."""

    from tradingagents.capability_routing import (
        prepare_mainland_listing_publication_candidate_plan,
        prepare_mainland_temporal_capability_routing_plan,
    )

    selected = config.get(PRODUCTION_PLAN_VERSION_CONFIG_KEY)
    has_cninfo_config = any(config.get(key) is not None for key in CNINFO_PRODUCTION_CONFIG_KEYS)
    if selected is None:
        if has_cninfo_config:
            raise ValueError("CNINFO configuration requires explicit plan 1.5")
        return predecessor
    if not isinstance(selected, str):
        raise ValueError("production routing plan selection is invalid")
    version = MainlandProductionPlanVersion(selected)
    plan = prepare_mainland_temporal_capability_routing_plan(predecessor)
    if version is MainlandProductionPlanVersion.PLAN_1_5:
        if not predecessor.enabled_tushare_capabilities:
            raise ValueError("CNINFO requires the production qualified shadow composition")
        CninfoProductionProfile.from_config(config)
        authoritative_cninfo_listing_board(asset_configuration.instrument_identity)
        plan = prepare_mainland_listing_publication_candidate_plan(plan)
    elif has_cninfo_config:
        raise ValueError("plan 1.4 cannot carry CNINFO configuration")
    signature = config.get("mainland_capability_routing_plan_signature")
    if signature is not None and signature != plan.plan_signature:
        raise ValueError("configured production plan signature does not match selection")
    return plan
