from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import Enum
from hashlib import sha256
from typing import TypeAlias


class RevisionKind(str, Enum):
    EQUIVALENCE_RESULT = "equivalence-result"
    INGESTION_RUN = "ingestion-run"
    HISTORY_BUNDLE = "history-bundle"
    PROVIDER_FRAME = "provider-frame"
    RAW_MARKET_OBSERVATION = "raw-market-observation"
    TRADING_STATUS = "trading-status"
    ADJUSTMENT_FACTOR = "adjustment-factor"
    MARKET_SESSION_CALENDAR = "market-session-calendar"
    PUBLICATION_WATERMARK = "publication-watermark"
    SNAPSHOT = "snapshot"


CanonicalScalar: TypeAlias = str | int | bool | None
CanonicalValue: TypeAlias = (
    CanonicalScalar | Mapping[str, "CanonicalValue"] | Sequence["CanonicalValue"]
)


def revision_identity(
    kind: RevisionKind,
    fields: Mapping[str, CanonicalValue],
) -> str:
    """Return a stable identity for an immutable, normalized revision."""
    if not isinstance(kind, RevisionKind):
        raise TypeError("kind must be a RevisionKind")
    if not fields:
        raise ValueError("revision fields must not be empty")
    canonical_fields = _canonical_value(fields, path="fields")
    encoded = json.dumps(
        {"fields": canonical_fields, "kind": kind.value},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{kind.value}=sha256:{sha256(encoded).hexdigest()}"


def _canonical_value(value: CanonicalValue, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError(
            f"{path} contains a float; normalize exact market values to strings first"
        )
    if isinstance(value, Mapping):
        canonical: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"{path} keys must be non-empty strings")
            canonical[key] = _canonical_value(item, path=f"{path}.{key}")
        return canonical
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonical_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")
