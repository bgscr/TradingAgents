"""Authoritative immutable asset semantics resolved before a run is built."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from tradingagents.dataflows.crypto_universe import is_crypto_pair_syntax
from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    RegistryFailureReason,
    resolve_authoritative_crypto_identity,
    resolve_authoritative_instrument_identity,
)
from tradingagents.decision_policy import DecisionHorizon
from tradingagents.evidence import (
    CapabilityProfile,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    capability_profile_for,
)
from tradingagents.strategy_registry import (
    CRYPTO_DECISION_HORIZON,
    DEFAULT_DECISION_HORIZON,
    market_return_calculation_id,
)

ASSET_CONFIGURATION_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")


class ObservationCalendarKind(str, Enum):
    MARKET_SESSIONS = "market_sessions"
    CONSECUTIVE_DAILY = "consecutive_daily"


class RunAssetConfigurationError(ValueError):
    """Typed deterministic failure to establish one coherent run asset."""

    def __init__(
        self,
        *,
        reason: RegistryFailureReason,
        diagnostic_code: str,
        source_ref: str,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = diagnostic_code
        self.source_ref = source_ref
        super().__init__(diagnostic_code)


class RunAssetConfiguration(BaseModel):
    """Closed run-level identity, capability, calendar, and rule selection."""

    model_config = _CLOSED_MODEL_CONFIG

    asset_configuration_version: Literal["1.0"] = ASSET_CONFIGURATION_VERSION
    instrument_identity: InstrumentIdentityEvidence
    instrument_kind: InstrumentKind
    registry_id: str = Field(min_length=1)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_market: str = Field(min_length=1)
    capability_profile: CapabilityProfile
    observation_calendar_kind: ObservationCalendarKind
    calculation_id: str = Field(min_length=1)
    strategy_rule_ids: tuple[str, ...] = Field(min_length=1)
    horizon: DecisionHorizon
    observations_required: int = Field(ge=1)
    adjustment_basis: str = Field(min_length=1)
    registry_source_ref: str = Field(min_length=1, exclude=True, repr=False)
    registry_artifact: str = Field(min_length=1, exclude=True, repr=False)

    @model_validator(mode="after")
    def _validate_coherent_semantics(self) -> RunAssetConfiguration:
        if not self.instrument_identity.is_authoritative:
            raise ValueError("run asset identity must be authoritative")
        if self.instrument_identity.instrument_kind is not self.instrument_kind:
            raise ValueError("run asset instrument kind contradicts its identity")
        if self.instrument_identity.venue != self.reference_market:
            raise ValueError("run asset Reference Market contradicts its identity")
        if self.capability_profile.instrument_kind is not self.instrument_kind:
            raise ValueError("run asset Capability Profile has the wrong instrument kind")
        if self.instrument_kind is InstrumentKind.CRYPTO:
            expected_rules = (
                "market.return_20d.crypto.buy",
                "market.return_20d.crypto.hold",
                "market.return_20d.crypto.sell",
            )
            if self.reference_market != "CCC":
                raise ValueError("crypto run asset must use the CCC Reference Market")
            if self.capability_profile.profile_id != "crypto.v1":
                raise ValueError("crypto run asset must use the crypto Capability Profile")
            if self.observation_calendar_kind is not (
                ObservationCalendarKind.CONSECUTIVE_DAILY
            ):
                raise ValueError("crypto run asset must use consecutive daily observations")
            if self.horizon != CRYPTO_DECISION_HORIZON:
                raise ValueError("crypto run asset must use 20 calendar days")
            if self.calculation_id != market_return_calculation_id(
                "auto_adjusted",
                InstrumentKind.CRYPTO,
            ):
                raise ValueError("crypto run asset calculation is not registered")
            if self.strategy_rule_ids != expected_rules:
                raise ValueError("crypto run asset Strategy Rules are not registered")
            if self.observations_required != 21:
                raise ValueError("crypto run asset requires exactly 21 observations")
            if self.adjustment_basis != "auto_adjusted":
                raise ValueError("crypto run asset cannot inherit equity adjustment semantics")
        return self

    def signature_payload(self) -> dict[str, Any]:
        """Return only semantic fields committed by checkpoint/config signatures."""

        return {
            "asset_configuration_version": self.asset_configuration_version,
            "instrument_identity": self.instrument_identity.model_dump(mode="json"),
            "instrument_kind": self.instrument_kind.value,
            "registry_id": self.registry_id,
            "registry_digest": self.registry_digest,
            "reference_market": self.reference_market,
            "capability_profile": self.capability_profile.model_dump(mode="json"),
            "observation_calendar_kind": self.observation_calendar_kind.value,
            "calculation_id": self.calculation_id,
            "strategy_rule_ids": list(self.strategy_rule_ids),
            "horizon": self.horizon.model_dump(mode="json"),
            "observations_required": self.observations_required,
            "adjustment_basis": self.adjustment_basis,
        }

    @computed_field(return_type=str)
    @property
    def asset_configuration_signature(self) -> str:
        encoded = json.dumps(
            self.signature_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"asset-config:v1:{sha256(encoded).hexdigest()}"

    @computed_field(return_type=str)
    @property
    def asset_type(self) -> str:
        return "crypto" if self.instrument_kind is InstrumentKind.CRYPTO else "stock"

    def matches_symbol(self, symbol: str) -> bool:
        if self.instrument_kind is InstrumentKind.CRYPTO:
            from tradingagents.dataflows.crypto_universe import (
                canonical_supported_crypto_symbol,
            )

            requested = canonical_supported_crypto_symbol(symbol)
        else:
            requested = str(symbol).strip().upper()
        return requested == self.instrument_identity.symbol


class RunAssetConfigurationProjection(BaseModel):
    """Payload-safe checkpoint and audit representation of a run asset."""

    model_config = _CLOSED_MODEL_CONFIG

    asset_configuration_version: Literal["1.0"] = ASSET_CONFIGURATION_VERSION
    instrument_identity: InstrumentIdentityEvidence
    instrument_kind: InstrumentKind
    registry_id: str = Field(min_length=1)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_market: str = Field(min_length=1)
    capability_profile: CapabilityProfile
    observation_calendar_kind: ObservationCalendarKind
    calculation_id: str = Field(min_length=1)
    strategy_rule_ids: tuple[str, ...] = Field(min_length=1)
    horizon: DecisionHorizon
    observations_required: int = Field(ge=1)
    adjustment_basis: str = Field(min_length=1)
    asset_configuration_signature: str = Field(
        pattern=r"^asset-config:v1:[0-9a-f]{64}$"
    )
    asset_type: Literal["stock", "crypto"]

    @model_validator(mode="after")
    def _signature_and_kind_are_coherent(self) -> RunAssetConfigurationProjection:
        expected_asset_type = (
            "crypto" if self.instrument_kind is InstrumentKind.CRYPTO else "stock"
        )
        if self.asset_type != expected_asset_type:
            raise ValueError("run asset wire type contradicts instrument kind")
        payload = self.model_dump(
            mode="json",
            exclude={"asset_configuration_signature", "asset_type"},
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_signature = f"asset-config:v1:{sha256(encoded).hexdigest()}"
        if self.asset_configuration_signature != expected_signature:
            raise ValueError("run asset configuration signature mismatch")
        return self


def _registry_result(
    symbol: str,
    config: Mapping[str, Any],
) -> IdentityRegistryAvailable | IdentityRegistryUnavailable:
    if is_crypto_pair_syntax(symbol):
        registry_path = config.get("crypto_identity_registry_path")
        expected_sha256 = config.get("crypto_identity_registry_sha256")
        expected_registry_id = config.get("crypto_identity_registry_id")
        if not registry_path or not expected_sha256:
            diagnostic_code = (
                "crypto_registry_pin_partial_override"
                if bool(registry_path) != bool(expected_sha256)
                else "registry_path_or_digest_missing"
            )
            return IdentityRegistryUnavailable(
                reason=RegistryFailureReason.NOT_CONFIGURED,
                source_ref=str(
                    registry_path or "crypto-identity-registry:unconfigured"
                ),
                diagnostic_code=diagnostic_code,
            )
        if not expected_registry_id:
            return IdentityRegistryUnavailable(
                reason=RegistryFailureReason.NOT_CONFIGURED,
                source_ref=str(registry_path),
                diagnostic_code="crypto_registry_expected_id_missing",
            )
        return resolve_authoritative_crypto_identity(
            symbol,
            registry_path=registry_path,
            expected_sha256=expected_sha256,
            expected_registry_id=expected_registry_id,
        )
    return resolve_authoritative_instrument_identity(
        symbol,
        registry_path=config.get("instrument_identity_registry_path"),
        expected_sha256=config.get("instrument_identity_registry_sha256"),
    )


def resolve_run_asset_configuration(
    symbol: str,
    *,
    config: Mapping[str, Any],
) -> RunAssetConfiguration:
    """Resolve the immutable asset contract before any run objects are created."""

    result = _registry_result(symbol, config)
    if isinstance(result, IdentityRegistryUnavailable):
        raise RunAssetConfigurationError(
            reason=result.reason,
            diagnostic_code=result.diagnostic_code,
            source_ref=result.source_ref,
        )

    identity = result.identity
    try:
        instrument_kind = InstrumentKind(identity.instrument_kind)
        identity_evidence = InstrumentIdentityEvidence(
            symbol=identity.canonical_symbol,
            venue=identity.venue,
            instrument_kind=instrument_kind,
            currency=identity.currency,
            provenance=IdentityProvenance(
                provider=identity.provenance_provider,
                source_ref=identity.provenance_source_ref,
                retrieved_at=identity.provenance_retrieved_at,
                artifact_sha256=identity.artifact_sha256,
            ),
            display_name=identity.display_name,
        )
        is_crypto = instrument_kind is InstrumentKind.CRYPTO
        adjustment_basis = "auto_adjusted" if is_crypto else "qfq"
        rule_prefix = "market.return_20d.crypto" if is_crypto else "market.return_20d"
        return RunAssetConfiguration(
            instrument_identity=identity_evidence,
            instrument_kind=instrument_kind,
            registry_id=result.registry_id or "legacy-instrument-registry",
            registry_digest=result.registry_sha256,
            reference_market=identity.venue,
            capability_profile=capability_profile_for(instrument_kind),
            observation_calendar_kind=(
                ObservationCalendarKind.CONSECUTIVE_DAILY
                if is_crypto
                else ObservationCalendarKind.MARKET_SESSIONS
            ),
            calculation_id=market_return_calculation_id(
                adjustment_basis,
                instrument_kind,
            ),
            strategy_rule_ids=tuple(
                f"{rule_prefix}.{rating}" for rating in ("buy", "hold", "sell")
            ),
            horizon=CRYPTO_DECISION_HORIZON if is_crypto else DEFAULT_DECISION_HORIZON,
            observations_required=21,
            adjustment_basis=adjustment_basis,
            registry_source_ref=result.registry_source_ref,
            registry_artifact=result.raw_artifact,
        )
    except (TypeError, ValueError) as exc:
        raise RunAssetConfigurationError(
            reason=RegistryFailureReason.MALFORMED,
            diagnostic_code="asset_configuration_incoherent",
            source_ref=result.registry_source_ref,
        ) from exc


__all__ = [
    "ASSET_CONFIGURATION_VERSION",
    "ObservationCalendarKind",
    "RunAssetConfiguration",
    "RunAssetConfigurationError",
    "RunAssetConfigurationProjection",
    "resolve_run_asset_configuration",
]
