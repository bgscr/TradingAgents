"""Immutable artifact codec for qualified AKShare-Sina statements."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pandas as pd

from tradingagents.dataflows.financial_contracts import (
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestKey

AKSHARE_SINA_STATEMENT_ADAPTER_VERSION = "akshare-sina-statement-adapter-v1"
AKSHARE_SINA_QUALIFIED_PACKAGE_VERSION = "1.18.73"
AKSHARE_SINA_MONETARY_UNIT_CONTRACT = (
    "akshare-1.18.73-sina-item-value-cny-base-unit-v1"
)


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AKShare-Sina observation time must be timezone-aware")
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
        if value.tzinfo is None or value.utcoffset() is None:
            return value.isoformat()
        return require_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError("unsupported AKShare-Sina provider value")


def encode_provider_artifact(
    *,
    statement_id: str,
    frame: pd.DataFrame,
    retrieved_at: datetime,
    observed_at: datetime,
) -> bytes:
    """Encode every provider field and value before normalization."""

    columns = [str(column) for column in frame.columns]
    if not columns or len(columns) != len(set(columns)):
        raise ValueError("AKShare-Sina response columns must be nonempty and unique")
    rows = [
        {
            column: _json_value(value)
            for column, value in zip(columns, values, strict=True)
        }
        for values in frame.itertuples(index=False, name=None)
    ]
    payload = {
        "artifact_contract_version": AKSHARE_SINA_STATEMENT_ADAPTER_VERSION,
        "provider_id": "akshare_sina",
        "endpoint_id": "stock_financial_report_sina",
        "dataset_id": f"sina_company_finance_report_2022.{statement_id}",
        "statement_id": statement_id,
        "schema_identity": (
            "akshare_1_18_73_stock_financial_report_sina_v1"
        ),
        "qualified_package_version": AKSHARE_SINA_QUALIFIED_PACKAGE_VERSION,
        "monetary_unit_contract": AKSHARE_SINA_MONETARY_UNIT_CONTRACT,
        "retrieved_at": require_utc(retrieved_at).isoformat().replace("+00:00", "Z"),
        "observed_at": require_utc(observed_at).isoformat().replace("+00:00", "Z"),
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


def decode_provider_artifact(
    value: bytes,
    *,
    statement_id: str,
) -> dict[str, object]:
    """Validate a cached immutable artifact before candidate projection."""

    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("AKShare-Sina statement artifact is malformed") from exc
    expected_keys = {
        "artifact_contract_version",
        "provider_id",
        "endpoint_id",
        "dataset_id",
        "statement_id",
        "schema_identity",
        "qualified_package_version",
        "monetary_unit_contract",
        "retrieved_at",
        "observed_at",
        "columns",
        "rows",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload["artifact_contract_version"]
        != AKSHARE_SINA_STATEMENT_ADAPTER_VERSION
        or payload["provider_id"] != "akshare_sina"
        or payload["endpoint_id"] != "stock_financial_report_sina"
        or payload["statement_id"] != statement_id
        or payload["dataset_id"]
        != f"sina_company_finance_report_2022.{statement_id}"
        or payload["qualified_package_version"]
        != AKSHARE_SINA_QUALIFIED_PACKAGE_VERSION
        or payload["monetary_unit_contract"]
        != AKSHARE_SINA_MONETARY_UNIT_CONTRACT
        or not isinstance(payload["columns"], list)
        or not isinstance(payload["rows"], list)
    ):
        raise ValueError("AKShare-Sina statement artifact is malformed")
    columns = payload["columns"]
    if (
        any(not isinstance(column, str) for column in columns)
        or len(columns) != len(set(columns))
        or any(
            not isinstance(row, dict) or set(row) != set(columns)
            for row in payload["rows"]
        )
    ):
        raise ValueError("AKShare-Sina statement artifact rows are malformed")
    return payload


def financial_artifact_identity(
    *,
    key: ProviderSubrequestKey,
    payload: Mapping[str, object],
) -> FinancialProviderArtifactIdentity:
    dataset = _dataset_identity(payload)
    rows = payload["rows"]
    assert isinstance(rows, list)
    raw_payload_sha256 = _unordered_rows_sha256(rows)
    return FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request=key.model_dump(mode="json"),
        provider_metadata={
            "provider_id": "akshare_sina",
            "endpoint_id": "stock_financial_report_sina",
            "statement_id": str(payload["statement_id"]),
            "schema_identity": str(payload["schema_identity"]),
            "qualified_package_version": str(payload["qualified_package_version"]),
            "monetary_unit_contract": str(payload["monetary_unit_contract"]),
            "raw_payload_sha256": raw_payload_sha256,
        },
        retrieved_at=_payload_time(payload, "retrieved_at"),
        observed_at=_payload_time(payload, "observed_at"),
        payload={"raw_payload_sha256": raw_payload_sha256},
    )


def financial_row_artifact_identities(
    *,
    key: ProviderSubrequestKey,
    response_artifact: FinancialProviderArtifactIdentity,
    payload: Mapping[str, object],
) -> tuple[FinancialProviderArtifactIdentity, ...]:
    """Bind every original period row to a distinct local revision identity."""

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
        row_payload_sha256 = sha256(canonical_row.encode("utf-8")).hexdigest()
        duplicate_occurrence = duplicate_counts.get(canonical_row, 0)
        duplicate_counts[canonical_row] = duplicate_occurrence + 1
        identities.append(
            FinancialProviderArtifactIdentity.create(
                dataset=dataset,
                canonical_request=key.model_dump(mode="json"),
                provider_metadata={
                    "provider_id": "akshare_sina",
                    "endpoint_id": "stock_financial_report_sina",
                    "statement_id": str(payload["statement_id"]),
                    "schema_identity": str(payload["schema_identity"]),
                    "monetary_unit_contract": str(
                        payload["monetary_unit_contract"]
                    ),
                    "response_payload_sha256": response_artifact.payload_sha256,
                    "row_payload_sha256": row_payload_sha256,
                    "duplicate_occurrence": duplicate_occurrence,
                },
                retrieved_at=_payload_time(payload, "retrieved_at"),
                observed_at=_payload_time(payload, "observed_at"),
                payload={"row_payload_sha256": row_payload_sha256},
            )
        )
    return tuple(identities)


def _dataset_identity(
    payload: Mapping[str, object],
) -> FinancialProviderDatasetIdentity:
    return FinancialProviderDatasetIdentity.create(
        provider_id="akshare_sina",
        endpoint_id="stock_financial_report_sina",
        dataset_id=str(payload["dataset_id"]),
        schema_identity=str(payload["schema_identity"]),
    )


def _unordered_rows_sha256(rows: Sequence[object]) -> str:
    canonical_rows = sorted(
        json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for row in rows
    )
    return sha256(
        json.dumps(
            canonical_rows,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _payload_time(payload: Mapping[str, object], field: str) -> datetime:
    return datetime.fromisoformat(str(payload[field]).replace("Z", "+00:00"))


__all__ = [
    "AKSHARE_SINA_QUALIFIED_PACKAGE_VERSION",
    "AKSHARE_SINA_STATEMENT_ADAPTER_VERSION",
    "decode_provider_artifact",
    "encode_provider_artifact",
    "financial_artifact_identity",
    "financial_row_artifact_identities",
    "require_utc",
]
