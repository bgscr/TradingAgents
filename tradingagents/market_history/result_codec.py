from __future__ import annotations

import base64
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_NESTING = 64


def encode_coordinated_result(value: object) -> bytes:
    """Encode a Yahoo result without an executable object-deserialization format."""

    payload = json.dumps(
        _encode(value, depth=0),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_RESULT_BYTES:
        raise ValueError("coordinated result exceeds the persisted handoff limit")
    return payload


def decode_coordinated_result(payload: bytes) -> object:
    """Decode only the closed result types emitted by encode_coordinated_result."""

    if len(payload) > _MAX_RESULT_BYTES:
        raise ValueError("persisted coordinated result exceeds the handoff limit")
    value = json.loads(payload.decode("utf-8"))
    return _decode(value, depth=0)


def _encode(value: object, *, depth: int) -> object:
    if depth > _MAX_NESTING:
        raise ValueError("coordinated result nesting exceeds the safe limit")
    next_depth = depth + 1
    if isinstance(value, pd.DataFrame):
        return {
            "type": "dataframe",
            "columns": _encode_index(value.columns, depth=next_depth),
            "index": _encode_index(value.index, depth=next_depth),
            "data": [
                [_encode(cell, depth=next_depth) for cell in row]
                for row in value.itertuples(index=False, name=None)
            ],
        }
    if isinstance(value, pd.Series):
        return {
            "type": "series",
            "name": _encode(value.name, depth=next_depth),
            "index": _encode_index(value.index, depth=next_depth),
            "data": [_encode(cell, depth=next_depth) for cell in value.tolist()],
        }
    if value is pd.NA or value is pd.NaT:
        return {"type": "missing"}
    if isinstance(value, np.generic):
        return _encode(value.item(), depth=next_depth)
    if isinstance(value, pd.Timestamp):
        return {"type": "timestamp", "value": value.isoformat()}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, dict):
        return {
            "type": "dict",
            "items": [
                [
                    _encode(key, depth=next_depth),
                    _encode(item, depth=next_depth),
                ]
                for key, item in value.items()
            ],
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "items": [_encode(item, depth=next_depth) for item in value],
        }
    if isinstance(value, tuple):
        return {
            "type": "tuple",
            "items": [_encode(item, depth=next_depth) for item in value],
        }
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": repr(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return {"type": "scalar", "value": value}
    raise TypeError(
        f"unsupported coordinated result type: {type(value).__module__}."
        f"{type(value).__qualname__}"
    )


def _encode_index(index: pd.Index, *, depth: int) -> object:
    return {
        "multi": isinstance(index, pd.MultiIndex),
        "names": [_encode(name, depth=depth) for name in index.names],
        "values": [_encode(value, depth=depth) for value in index.tolist()],
    }


def _decode(value: object, *, depth: int) -> object:
    if depth > _MAX_NESTING or not isinstance(value, dict):
        raise ValueError("persisted coordinated result is malformed")
    kind = value.get("type")
    next_depth = depth + 1
    if kind == "dataframe":
        columns = _decode_index(value.get("columns"), depth=next_depth)
        index = _decode_index(value.get("index"), depth=next_depth)
        raw_data = value.get("data")
        if not isinstance(raw_data, list):
            raise ValueError("persisted DataFrame result is malformed")
        data = [
            [_decode(cell, depth=next_depth) for cell in row]
            for row in raw_data
            if isinstance(row, list)
        ]
        if len(data) != len(raw_data):
            raise ValueError("persisted DataFrame rows are malformed")
        return pd.DataFrame(data, columns=columns, index=index)
    if kind == "series":
        raw_data = value.get("data")
        if not isinstance(raw_data, list):
            raise ValueError("persisted Series result is malformed")
        return pd.Series(
            [_decode(item, depth=next_depth) for item in raw_data],
            index=_decode_index(value.get("index"), depth=next_depth),
            name=_decode(value.get("name"), depth=next_depth),
        )
    if kind == "missing":
        return None
    if kind in {"timestamp", "datetime", "date", "decimal", "bytes", "float"}:
        raw = value.get("value")
        if not isinstance(raw, str):
            raise ValueError("persisted scalar result is malformed")
        if kind == "timestamp":
            return pd.Timestamp(raw)
        if kind == "datetime":
            return datetime.fromisoformat(raw)
        if kind == "date":
            return date.fromisoformat(raw)
        if kind == "decimal":
            return Decimal(raw)
        if kind == "bytes":
            return base64.b64decode(raw, validate=True)
        return float(raw)
    if kind in {"dict", "list", "tuple"}:
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("persisted collection result is malformed")
        if kind == "dict":
            decoded: dict[Any, object] = {}
            for pair in raw_items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("persisted mapping result is malformed")
                decoded[_decode(pair[0], depth=next_depth)] = _decode(
                    pair[1], depth=next_depth
                )
            return decoded
        items = [_decode(item, depth=next_depth) for item in raw_items]
        return items if kind == "list" else tuple(items)
    if kind == "scalar" and set(value) == {"type", "value"}:
        scalar = value["value"]
        if scalar is None or isinstance(scalar, (str, int, float, bool)):
            return scalar
    raise ValueError("persisted coordinated result has an unsupported type tag")


def _decode_index(value: object, *, depth: int) -> pd.Index:
    if not isinstance(value, dict) or set(value) != {"multi", "names", "values"}:
        raise ValueError("persisted DataFrame index is malformed")
    raw_names = value["names"]
    raw_values = value["values"]
    if (
        not isinstance(value["multi"], bool)
        or not isinstance(raw_names, list)
        or not isinstance(raw_values, list)
    ):
        raise ValueError("persisted DataFrame index is malformed")
    names = [_decode(name, depth=depth) for name in raw_names]
    values = [_decode(item, depth=depth) for item in raw_values]
    if value["multi"]:
        if not all(isinstance(item, tuple) for item in values):
            raise ValueError("persisted MultiIndex values are malformed")
        return pd.MultiIndex.from_tuples(values, names=names)
    if len(names) != 1:
        raise ValueError("persisted Index name is malformed")
    return pd.Index(values, name=names[0])
