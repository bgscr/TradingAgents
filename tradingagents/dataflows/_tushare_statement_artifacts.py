"""Immutable artifact codec for qualified Tushare statement responses."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pandas as pd

from tradingagents.dataflows.financial_contracts import (
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestKey

TUSHARE_STATEMENT_ADAPTER_VERSION = "tushare-statement-adapter-v1"


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Tushare statement observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_value(value: object) -> object:
    if value is None:
        return None
    try:
        missing = bool(pd.isna(value))
    except (TypeError, ValueError):
        missing = False
    if missing:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        value = value.item()
    if isinstance(value, datetime):
        return require_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool):
        return value
    raise TypeError("unsupported Tushare provider value")


def encode_provider_artifact(
    *,
    endpoint_id: str,
    frame: pd.DataFrame,
    retrieved_at: datetime,
    observed_at: datetime,
    artifact_contract_version: str = TUSHARE_STATEMENT_ADAPTER_VERSION,
    adapter_metadata: Mapping[str, object] | None = None,
) -> bytes:
    """Encode every provider column and value before normalization."""

    columns = [str(column) for column in frame.columns]
    if not columns or len(columns) != len(set(columns)):
        raise ValueError("Tushare response columns must be nonempty and unique")
    rows = [
        {
            column: _json_value(value)
            for column, value in zip(columns, values, strict=True)
        }
        for values in frame.itertuples(index=False, name=None)
    ]
    payload = {
        "artifact_contract_version": artifact_contract_version,
        "provider_id": "tushare",
        "endpoint_id": endpoint_id,
        "dataset_id": "single_stock_history",
        "schema_identity": f"tushare_{endpoint_id}_single_stock_v1",
        "retrieved_at": require_utc(retrieved_at).isoformat().replace("+00:00", "Z"),
        "observed_at": require_utc(observed_at).isoformat().replace("+00:00", "Z"),
        "columns": columns,
        "rows": rows,
    }
    if adapter_metadata is not None:
        payload["adapter_metadata"] = dict(adapter_metadata)
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_provider_artifact(
    value: bytes,
    *,
    endpoint_id: str,
    artifact_contract_version: str = TUSHARE_STATEMENT_ADAPTER_VERSION,
    adapter_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate a cached artifact before deriving provider-neutral candidates."""

    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Tushare statement artifact is malformed") from exc
    expected_keys = {
        "artifact_contract_version",
        "provider_id",
        "endpoint_id",
        "dataset_id",
        "schema_identity",
        "retrieved_at",
        "observed_at",
        "columns",
        "rows",
    }
    if adapter_metadata is not None:
        expected_keys.add("adapter_metadata")
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload["artifact_contract_version"] != artifact_contract_version
        or payload["provider_id"] != "tushare"
        or payload["endpoint_id"] != endpoint_id
        or (
            adapter_metadata is not None
            and payload.get("adapter_metadata") != dict(adapter_metadata)
        )
        or not isinstance(payload["columns"], list)
        or not isinstance(payload["rows"], list)
    ):
        raise ValueError("Tushare statement artifact is malformed")
    columns = payload["columns"]
    if any(
        not isinstance(row, dict) or set(row) != set(columns)
        for row in payload["rows"]
    ):
        raise ValueError("Tushare statement artifact rows are malformed")
    return payload


def financial_artifact_identity(
    *,
    key: ProviderSubrequestKey,
    payload: Mapping[str, object],
) -> FinancialProviderArtifactIdentity:
    """Bind the bulk response to its canonical request and canonical row payload."""

    dataset = _dataset_identity(payload)
    provider_metadata = {
        "provider_id": "tushare",
        "endpoint_id": str(payload["endpoint_id"]),
        "schema_identity": str(payload["schema_identity"]),
    }
    if "adapter_metadata" in payload:
        provider_metadata["adapter_metadata"] = payload["adapter_metadata"]
    return FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request=key.model_dump(mode="json"),
        provider_metadata=provider_metadata,
        retrieved_at=_payload_time(payload, "retrieved_at"),
        observed_at=_payload_time(payload, "observed_at"),
        payload=payload["rows"],
    )


def financial_row_artifact_identities(
    *,
    key: ProviderSubrequestKey,
    response_artifact: FinancialProviderArtifactIdentity,
    payload: Mapping[str, object],
) -> tuple[FinancialProviderArtifactIdentity, ...]:
    """Create a distinct Ticket 02 local revision binding for every raw row."""

    dataset = _dataset_identity(payload)
    rows = payload["rows"]
    assert isinstance(rows, list)
    duplicate_counts: dict[str, int] = {}
    identities: list[FinancialProviderArtifactIdentity] = []
    for row in rows:
        canonical_row = json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        duplicate_occurrence = duplicate_counts.get(canonical_row, 0)
        duplicate_counts[canonical_row] = duplicate_occurrence + 1
        identities.append(
            FinancialProviderArtifactIdentity.create(
                dataset=dataset,
                canonical_request=key.model_dump(mode="json"),
                provider_metadata={
                    "provider_id": "tushare",
                    "endpoint_id": str(payload["endpoint_id"]),
                    "schema_identity": str(payload["schema_identity"]),
                    "response_payload_sha256": response_artifact.payload_sha256,
                    "row_payload_sha256": sha256(
                        canonical_row.encode("utf-8")
                    ).hexdigest(),
                    "duplicate_occurrence": duplicate_occurrence,
                },
                retrieved_at=_payload_time(payload, "retrieved_at"),
                observed_at=_payload_time(payload, "observed_at"),
                payload=row,
            )
        )
    return tuple(identities)


def _dataset_identity(
    payload: Mapping[str, object],
) -> FinancialProviderDatasetIdentity:
    return FinancialProviderDatasetIdentity.create(
        provider_id="tushare",
        endpoint_id=str(payload["endpoint_id"]),
        dataset_id=str(payload["dataset_id"]),
        schema_identity=str(payload["schema_identity"]),
    )


def _payload_time(payload: Mapping[str, object], field: str) -> datetime:
    return datetime.fromisoformat(str(payload[field]).replace("Z", "+00:00"))


__all__ = [
    "TUSHARE_STATEMENT_ADAPTER_VERSION",
    "decode_provider_artifact",
    "encode_provider_artifact",
    "financial_artifact_identity",
    "financial_row_artifact_identities",
    "require_utc",
]
