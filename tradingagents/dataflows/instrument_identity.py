"""Network-free authoritative instrument identity registry.

Ticker normalization is only routing: it may produce equivalent lookup keys,
but it never supplies venue or instrument kind.  Those fields are returned only
from a closed registry artifact whose exact bytes match a configured SHA-256.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.dataflows.crypto_universe import (
    CRYPTO_REGISTRY_ID,
    CryptoUniversePolicyError,
    canonical_supported_crypto_symbol,
    is_crypto_pair_syntax,
    validate_crypto_registry_rows,
)

_CLOSED = ConfigDict(frozen=True, extra="forbid")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAINLAND_ALIAS_PATTERN = re.compile(
    r"^(?P<code>\d{6})(?:\.(?P<suffix>SS|SH|SZ))?$",
    re.IGNORECASE,
)


class RegistryFailureReason(str, Enum):
    NOT_CONFIGURED = "registry_not_configured"
    NOT_FOUND = "identity_not_found"
    UNAVAILABLE = "registry_unavailable"
    MALFORMED = "malformed_registry"
    INTEGRITY_FAILURE = "registry_integrity_failure"


class _RegistryProvenance(BaseModel):
    model_config = _CLOSED

    provider: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)

    @field_validator("retrieved_at")
    @classmethod
    def _retrieval_time_is_iso8601(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("retrieved_at must be ISO-8601") from exc
        return value


class _RegistryRow(BaseModel):
    model_config = _CLOSED

    canonical_symbol: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    venue: str = Field(min_length=1)
    instrument_kind: Literal["equity", "fund", "index", "bond", "crypto"]
    currency: str = Field(min_length=1)
    display_name: str | None = None
    provenance: _RegistryProvenance

    @field_validator("canonical_symbol", mode="before")
    @classmethod
    def _canonical_symbol_is_normalized(cls, value: object) -> object:
        if not isinstance(value, str) or value != value.strip().upper():
            raise ValueError("canonical_symbol must be trimmed uppercase text")
        return value

    @field_validator("currency")
    @classmethod
    def _currency_is_iso_style_code(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z]{3}", value) is None:
            raise ValueError("currency must be a three-letter uppercase code")
        return value

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases_are_normalized(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("aliases must be a list")
        normalized = tuple(value)
        if any(
            not isinstance(alias, str) or alias != alias.strip().upper()
            for alias in normalized
        ):
            raise ValueError("aliases must contain trimmed uppercase text")
        if len(normalized) != len(set(normalized)):
            raise ValueError("aliases must be unique")
        return normalized


class _RegistryArtifact(BaseModel):
    model_config = _CLOSED

    schema_version: Literal["1.0"]
    registry_id: str = Field(min_length=1)
    rows: tuple[_RegistryRow, ...]


@dataclass(frozen=True)
class _RegistryArtifactValidationFailure:
    reason: RegistryFailureReason
    diagnostic_code: str


@dataclass(frozen=True)
class AuthoritativeInstrumentIdentity:
    canonical_symbol: str
    venue: str
    instrument_kind: str
    currency: str
    provenance_provider: str
    provenance_source_ref: str
    provenance_retrieved_at: str
    artifact_sha256: str
    display_name: str | None = None

    def as_mapping(self) -> dict[str, object]:
        identity: dict[str, object] = {
            "canonical_symbol": self.canonical_symbol,
            "venue": self.venue,
            "instrument_kind": self.instrument_kind,
            "currency": self.currency,
            "provenance": {
                "provider": self.provenance_provider,
                "source_ref": self.provenance_source_ref,
                "retrieved_at": self.provenance_retrieved_at,
                "artifact_sha256": self.artifact_sha256,
            },
        }
        if self.display_name:
            identity["company_name"] = self.display_name
        return identity


@dataclass(frozen=True)
class IdentityRegistryAvailable:
    identity: AuthoritativeInstrumentIdentity
    registry_sha256: str
    registry_source_ref: str
    raw_artifact: str
    registry_id: str | None = None


@dataclass(frozen=True)
class IdentityRegistryUnavailable:
    reason: RegistryFailureReason
    source_ref: str
    diagnostic_code: str


IdentityRegistryResult = IdentityRegistryAvailable | IdentityRegistryUnavailable


def _routing_lookup_keys(symbol: str) -> frozenset[str]:
    """Return lookup aliases without attaching any identity semantics."""
    normalized = symbol.strip().upper()
    if not normalized:
        return frozenset()
    keys = {normalized}
    match = _MAINLAND_ALIAS_PATTERN.fullmatch(normalized)
    if match is None:
        return frozenset(keys)
    code = match.group("code")
    suffix = match.group("suffix")
    keys.add(code)
    if suffix in {"SS", "SH"}:
        keys.update({f"{code}.SS", f"{code}.SH"})
    elif suffix == "SZ":
        keys.add(f"{code}.SZ")
    else:
        # These are candidates only.  A matching registry row is still required
        # and ambiguity across venues fails closed below.
        keys.update({f"{code}.SS", f"{code}.SH", f"{code}.SZ"})
    return frozenset(keys)


def _unavailable(
    reason: RegistryFailureReason,
    source_ref: str,
    diagnostic_code: str,
) -> IdentityRegistryUnavailable:
    return IdentityRegistryUnavailable(
        reason=reason,
        source_ref=source_ref or "identity-registry:unconfigured",
        diagnostic_code=diagnostic_code,
    )


def resolve_authoritative_instrument_identity(
    symbol: str,
    *,
    registry_path: str | Path | None = None,
    expected_sha256: str | None = None,
    _artifact_validator: Callable[
        [_RegistryArtifact], _RegistryArtifactValidationFailure | None
    ]
    | None = None,
) -> IdentityRegistryResult:
    """Resolve ``symbol`` only from a configured, digest-pinned JSON artifact."""
    if _artifact_validator is None and is_crypto_pair_syntax(symbol):
        return resolve_authoritative_crypto_identity(
            symbol,
            registry_path=registry_path,
            expected_sha256=expected_sha256,
        )

    if registry_path is None or expected_sha256 is None:
        from tradingagents.dataflows.config import get_config

        config = get_config()
        if registry_path is None:
            registry_path = config.get("instrument_identity_registry_path")
        if expected_sha256 is None:
            expected_sha256 = config.get("instrument_identity_registry_sha256")

    if not registry_path or not expected_sha256:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            "identity-registry:unconfigured",
            "registry_path_or_digest_missing",
        )

    path = Path(registry_path).expanduser()
    source_ref = str(path.resolve())
    normalized_digest = str(expected_sha256).strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized_digest) is None:
        return _unavailable(
            RegistryFailureReason.INTEGRITY_FAILURE,
            source_ref,
            "configured_digest_invalid",
        )

    try:
        artifact_bytes = path.read_bytes()
    except OSError:
        return _unavailable(
            RegistryFailureReason.UNAVAILABLE,
            source_ref,
            "registry_artifact_unreadable",
        )

    actual_digest = sha256(artifact_bytes).hexdigest()
    if not hmac.compare_digest(actual_digest, normalized_digest):
        return _unavailable(
            RegistryFailureReason.INTEGRITY_FAILURE,
            source_ref,
            "registry_digest_mismatch",
        )

    try:
        raw_artifact = artifact_bytes.decode("utf-8")
        artifact = _RegistryArtifact.model_validate_json(raw_artifact)
    except (UnicodeDecodeError, ValidationError, ValueError):
        return _unavailable(
            RegistryFailureReason.MALFORMED,
            source_ref,
            "registry_schema_invalid",
        )
    if _artifact_validator is not None:
        validation_failure = _artifact_validator(artifact)
        if validation_failure is not None:
            return _unavailable(
                validation_failure.reason,
                source_ref,
                validation_failure.diagnostic_code,
            )

    requested_keys = _routing_lookup_keys(symbol)
    matches: list[_RegistryRow] = []
    for row in artifact.rows:
        row_keys: set[str] = set()
        for alias in (row.canonical_symbol, *row.aliases):
            row_keys.update(_routing_lookup_keys(alias))
        if requested_keys.intersection(row_keys):
            matches.append(row)

    if not matches:
        return _unavailable(
            RegistryFailureReason.NOT_FOUND,
            source_ref,
            "identity_row_not_found",
        )
    if len(matches) != 1:
        return _unavailable(
            RegistryFailureReason.MALFORMED,
            source_ref,
            "identity_alias_ambiguous",
        )

    row = matches[0]
    return IdentityRegistryAvailable(
        identity=AuthoritativeInstrumentIdentity(
            canonical_symbol=row.canonical_symbol,
            venue=row.venue,
            instrument_kind=row.instrument_kind,
            currency=row.currency,
            provenance_provider=row.provenance.provider,
            provenance_source_ref=row.provenance.source_ref,
            provenance_retrieved_at=row.provenance.retrieved_at,
            artifact_sha256=actual_digest,
            display_name=row.display_name,
        ),
        registry_sha256=actual_digest,
        registry_source_ref=source_ref,
        raw_artifact=raw_artifact,
        registry_id=artifact.registry_id,
    )


def resolve_authoritative_crypto_identity(
    symbol: str,
    *,
    registry_path: str | Path | None = None,
    expected_sha256: str | None = None,
    expected_registry_id: str | None = None,
) -> IdentityRegistryResult:
    """Resolve a crypto symbol only from the separately pinned CCC registry."""
    if canonical_supported_crypto_symbol(symbol) is None:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            "crypto-identity-registry:unconfigured",
            "crypto_symbol_not_supported",
        )
    path_was_supplied = registry_path is not None
    digest_was_supplied = expected_sha256 is not None
    if path_was_supplied != digest_was_supplied:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            str(registry_path or "crypto-identity-registry:unconfigured"),
            "crypto_registry_pin_partial_override",
        )

    if not path_was_supplied:
        from tradingagents.dataflows.config import get_config

        config = get_config()
        registry_path = config.get("crypto_identity_registry_path")
        expected_sha256 = config.get("crypto_identity_registry_sha256")
        expected_registry_id = config.get("crypto_identity_registry_id")
    elif expected_registry_id is None:
        expected_registry_id = CRYPTO_REGISTRY_ID

    if not registry_path and not expected_sha256:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            "crypto-identity-registry:unconfigured",
            "registry_path_or_digest_missing",
        )
    if not registry_path:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            "crypto-identity-registry:unconfigured",
            "crypto_registry_path_missing",
        )
    if not expected_sha256:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            str(registry_path),
            "crypto_registry_expected_digest_missing",
        )
    if not expected_registry_id:
        return _unavailable(
            RegistryFailureReason.NOT_CONFIGURED,
            str(registry_path),
            "crypto_registry_expected_id_missing",
        )

    result = resolve_authoritative_instrument_identity(
        symbol,
        registry_path=registry_path,
        expected_sha256=expected_sha256,
        _artifact_validator=lambda artifact: _crypto_registry_diagnostic(
            artifact,
            expected_registry_id=expected_registry_id,
        ),
    )
    return result


def _crypto_registry_diagnostic(
    artifact: _RegistryArtifact,
    *,
    expected_registry_id: str = CRYPTO_REGISTRY_ID,
) -> _RegistryArtifactValidationFailure | None:
    if artifact.registry_id != expected_registry_id:
        return _RegistryArtifactValidationFailure(
            reason=RegistryFailureReason.INTEGRITY_FAILURE,
            diagnostic_code="crypto_registry_id_mismatch",
        )
    try:
        validate_crypto_registry_rows(artifact.rows)
    except CryptoUniversePolicyError as exc:
        return _RegistryArtifactValidationFailure(
            reason=RegistryFailureReason.MALFORMED,
            diagnostic_code=f"crypto_registry_{exc.diagnostic_code}",
        )
    return None
