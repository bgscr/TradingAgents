"""Assess a query response without inventing provider completeness declarations.

The documented fields/items table is extensible. Optional pagination signals can
disprove a single-response acquisition, but their absence is not malformed data.
Neither an accepted response nor a matching total proves report-period coverage,
revision chronology, or historical/PIT availability.
"""

from __future__ import annotations

import math

RESPONSE_VALUES_CONTRACT = "financial-response-values-v1"
VALUE_COUNT_LIMIT = 65535
_VALUE_KINDS = ("null", "number", "text", "boolean", "nonfinite", "unsupported")
_TRACKED_VALUE_FIELDS = ("cost_fin_assets", "fair_value_fin_assets")
_VALUE_KEYS = frozenset({"contract", "status", "row_count", "column_count", "counts", "tracked_fields"})

RESPONSE_ASSESSMENT_CONTRACT = "financial-response-assessment-v2"
_PAGING = frozenset({"has_more", "total", "total_count", "offset", "limit", "truncated"})
_V2_KEYS = frozenset({
    "contract", "coverage", "has_more", "truncated", "offset", "limit",
    "unknown_top_keys", "unknown_data_keys", "row_count", "total", "status",
})


class FinancialResponseIncomplete(ValueError):
    def __init__(self, reason, summary):
        self.summary = summary
        super().__init__(reason)


class FinancialResponseValueInvalid(ValueError):
    """A fixed reason and bounded counts, never provider names or scalar values."""

    def __init__(self, summary):
        self.summary = summary
        super().__init__("financial_response_values_invalid")


def inspect_response_values(fields, items):
    """Validate decoded JSON cells before DataFrame/codec missing-value coercion.

    Counts saturate at VALUE_COUNT_LIMIT; they are diagnostics, not completeness
    or financial qualification. Text and boolean values retain their own types.
    """
    if (type(fields) is not list or not fields
            or any(type(field) is not str or not field for field in fields)
            or len(set(fields)) != len(fields) or type(items) is not list
            or any(type(row) is not list or len(row) != len(fields) for row in items)):
        raise ValueError("financial_response_values_invalid_table")
    counts = dict.fromkeys(_VALUE_KINDS, 0)
    tracked = {name: dict.fromkeys(_VALUE_KINDS, 0) if name in fields else None
               for name in _TRACKED_VALUE_FIELDS}
    tracked_indices = {fields.index(name): counter for name, counter in tracked.items()
                       if counter is not None}
    for row in items:
        for index, value in enumerate(row):
            if value is None:
                kind = "null"
            elif type(value) is bool:
                kind = "boolean"
            elif type(value) is int:
                kind = "number"
            elif type(value) is float:
                kind = "number" if math.isfinite(value) else "nonfinite"
            elif type(value) is str:
                kind = "text"
            else:
                kind = "unsupported"
            counts[kind] = min(VALUE_COUNT_LIMIT, counts[kind] + 1)
            if index in tracked_indices:
                counter = tracked_indices[index]
                counter[kind] = min(VALUE_COUNT_LIMIT, counter[kind] + 1)
    invalid = bool(counts["nonfinite"] or counts["unsupported"])
    summary = {
        "contract": RESPONSE_VALUES_CONTRACT,
        "status": "invalid" if invalid else "valid",
        "row_count": min(VALUE_COUNT_LIMIT, len(items)),
        "column_count": min(VALUE_COUNT_LIMIT, len(fields)),
        "counts": counts,
        "tracked_fields": tracked,
    }
    if invalid:
        raise FinancialResponseValueInvalid(summary)
    return summary


def validate_retained_values(summary, fields, items):
    """Check the closed diagnostic projection against retained scalar types.

    Call only when the acquisition retained pre-conversion evidence. Absence on
    an older artifact is unknown provenance and must never be filled in here.
    """
    def counter_valid(counter):
        return (type(counter) is dict and set(counter) == set(_VALUE_KINDS)
                and all(type(value) is int and 0 <= value <= VALUE_COUNT_LIMIT
                        for value in counter.values()))

    if (type(summary) is not dict or set(summary) != _VALUE_KEYS
            or summary["contract"] != RESPONSE_VALUES_CONTRACT or summary["status"] != "valid"
            or any(type(summary[key]) is not int or not 0 <= summary[key] <= VALUE_COUNT_LIMIT
                   for key in ("row_count", "column_count"))
            or not counter_valid(summary["counts"])
            or type(summary["tracked_fields"]) is not dict
            or set(summary["tracked_fields"]) != set(_TRACKED_VALUE_FIELDS)
            or any(value is not None and not counter_valid(value)
                   for value in summary["tracked_fields"].values())):
        raise ValueError("financial_response_values_invalid")
    expected = inspect_response_values(fields, items)
    if summary != expected:
        raise ValueError("financial_response_values_invalid")
    return expected


def inspect_response_completeness(response):
    summary = {
        "contract": RESPONSE_ASSESSMENT_CONTRACT,
        "coverage": "query_response_only",
        "has_more": "missing",
        "truncated": "missing",
        "offset": None,
        "limit": None,
        "unknown_top_keys": 0,
        "unknown_data_keys": 0,
        "row_count": None,
        "total": None,
        "status": "unresolved",
    }
    if (
        type(response) is not dict
        or type(response.get("code")) is not int
        or not -(2 ** 31) <= response["code"] < 2 ** 31
    ):
        raise FinancialResponseIncomplete("financial_response_invalid_envelope", summary)
    if response["code"] != 0:
        raise FinancialResponseIncomplete("financial_response_business_rejection", summary)
    data = response.get("data")
    if type(data) is not dict:
        raise FinancialResponseIncomplete("financial_completeness_invalid_data", summary)
    fields, items = data.get("fields"), data.get("items")
    if (
        type(fields) is not list
        or not fields
        or any(type(field) is not str or not field for field in fields)
        or len(set(fields)) != len(fields)
        or type(items) is not list
        or any(type(row) is not list or len(row) != len(fields) for row in items)
    ):
        raise FinancialResponseIncomplete("financial_response_invalid_table", summary)
    summary["unknown_top_keys"] = min(
        32, len(set(response) - {"code", "msg", "request_id", "data"})
    )
    summary["unknown_data_keys"] = min(32, len(set(data) - {"fields", "items"} - _PAGING))
    count = len(items)
    summary["row_count"] = count
    # Interpret only the existing data-object paging extension contract.
    # Same-named envelope keys have no supported pagination meaning.
    # No arbitrary extension names or values enter retained evidence or errors.
    for key in ("has_more", "truncated"):
        if key not in data:
            continue
        if type(data[key]) is not bool:
            summary[key] = "invalid"
        else:
            summary[key] = "true" if data[key] else "false"
        if summary[key] != "false":
            raise FinancialResponseIncomplete(
                "financial_completeness_" + key + "_" + summary[key], summary
            )
    for key, aliases in (("total", ("total", "total_count")),
                         ("offset", ("offset",)), ("limit", ("limit",))):
        values = [data[name] for name in aliases if name in data]
        if not values:
            continue
        if any(type(value) is not int or value < 0 for value in values):
            raise FinancialResponseIncomplete("financial_completeness_invalid_" + key, summary)
        if len(set(values)) != 1:
            raise FinancialResponseIncomplete("financial_completeness_conflicting_" + key, summary)
        summary[key] = values[0]
        if ((key == "total" and values[0] != count)
                or (key == "offset" and values[0] != 0)
                or (key == "limit" and (values[0] == 0 or values[0] < count))):
            raise FinancialResponseIncomplete("financial_completeness_inconsistent_" + key, summary)
    summary["status"] = "usable_response"
    return summary


def validate_retained_completeness(summary, row_count):
    """Validate the recorded version as-is; never upgrade old observations."""
    if type(row_count) is not int or row_count < 0 or type(summary) is not dict:
        raise ValueError("financial_completeness_evidence_missing_or_invalid")
    if summary.get("contract") == "financial-response-completeness-v1":
        return _validate_v1(summary, row_count)
    if (
        set(summary) != _V2_KEYS
        or summary["contract"] != RESPONSE_ASSESSMENT_CONTRACT
        or summary["coverage"] != "query_response_only"
        or summary["status"] != "usable_response"
        or type(summary["has_more"]) is not str
        or summary["has_more"] not in {"missing", "false"}
        or type(summary["truncated"]) is not str
        or summary["truncated"] not in {"missing", "false"}
        or type(summary["row_count"]) is not int
        or summary["row_count"] != row_count
        or any(type(summary[key]) is not int or not 0 <= summary[key] <= 32
               for key in ("unknown_top_keys", "unknown_data_keys"))
        or (summary["total"] is not None
            and (type(summary["total"]) is not int or summary["total"] != row_count))
        or (summary["offset"] is not None
            and (type(summary["offset"]) is not int or summary["offset"] != 0))
        or (summary["limit"] is not None
            and (type(summary["limit"]) is not int
                 or summary["limit"] <= 0 or summary["limit"] < row_count))
    ):
        raise ValueError("financial_completeness_evidence_missing_or_invalid")
    return summary


def _validate_v1(summary, row_count):
    """Keep the original strict v1 evidence meaning, including zero extensions."""
    if (
        type(summary) is not dict
        or set(summary)
        != {
            "contract",
            "has_more",
            "unknown_top_keys",
            "unknown_data_keys",
            "row_count",
            "total",
            "status",
        }
        or summary["contract"] != "financial-response-completeness-v1"
        or summary["has_more"] != "false"
        or summary["status"] != "provider_declared_complete"
        or type(summary["row_count"]) is not int
        or summary["row_count"] != row_count
        or type(summary["total"]) is not int
        or summary["total"] != row_count
        or any(
            type(summary[key]) is not int or summary[key] != 0
            for key in ("unknown_top_keys", "unknown_data_keys")
        )
    ):
        raise ValueError("financial_completeness_evidence_missing_or_invalid")
    return summary
