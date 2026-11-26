"""Standard-library internal receipt checks, not financial requalification.

Both source-bound workers regenerate these summaries through the typed financial
assessment. Self-consistent dictionaries alone do not authenticate provider data.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime

CONTRACT = "current-statement-diagnostics-v1"
_COLLECTOR = ContextVar("current_statement_diagnostics", default=None)
_ALLOWED_REASONS = {"insufficient_completeness", "pit_cutoff_not_satisfied"}
_PERIOD_KEYS = {
    "candidate_identity",
    "period_end",
    "ann_date",
    "f_ann_date",
    "disposition",
    "rejection_reasons",
    "critical_coverage",
    "core_coverage",
    "missing_critical_fields",
    "missing_core_fields",
    "company_type",
    "declared_core_count",
}

_TOP_KEYS = {
    "contract",
    "ticker",
    "tool_name",
    "endpoint",
    "frequency",
    "as_of",
    "cache_scope",
    "row_count",
    "filtered_rows",
    "row_rejections",
    "unsupported_rows",
    "cutoff_rejections",
    "revision_conflict",
    "selected_periods",
    "cache_complete",
    "cache_observations",
    "periods",
}
_OBSERVATION_KEYS = {
    "payload_sha256",
    "announcement_start",
    "announcement_end",
    "checked_at",
    "retrieved_at",
    "observed_at",
}
_ENDPOINTS = {
    "get_balance_sheet": "balancesheet",
    "get_income_statement": "income",
    "get_cashflow": "cashflow",
}


def scopes_from_binding(binding):
    values = [
        binding["account"],
        binding["instrument_identity_revision"],
        None,
        hashlib.sha256(binding["destination"].encode()).hexdigest(),
        binding["normalizer_version"],
    ]
    result = {}
    for tool, endpoint in _ENDPOINTS.items():
        values[2] = endpoint
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value)
            for value in values
        ):
            raise ValueError("cache scope requires bounded non-secret opaque identifiers")
        result[tool] = hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode()
        ).hexdigest()
    return result


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@contextmanager
def capture_statement_diagnostics():
    records = []
    token = _COLLECTOR.set(records)
    try:
        yield records
    finally:
        _COLLECTOR.reset(token)


def record_statement_diagnostic(value):
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.append(copy.deepcopy(value))


def gap_eligible(value):
    """A gap is allowed only after all non-completeness checks succeeded."""
    from decimal import Decimal, InvalidOperation

    try:
        if set(value) not in (_TOP_KEYS, _TOP_KEYS | {"gap_eligible"}):
            return False
        if (
            "gap_eligible" in value
            and type(value["gap_eligible"]) is not bool
            or value["endpoint"] != _ENDPOINTS.get(value["tool_name"])
            or value["frequency"] not in {"annual", "quarterly"}
            or not re.fullmatch(r"\d{6}\.(SS|SZ)", value["ticker"])
            or not re.fullmatch(r"[0-9a-f]{64}", value["cache_scope"])
        ):
            return False
        cutoff = date.fromisoformat(value["as_of"])
        for name in (
            "row_count",
            "filtered_rows",
            "row_rejections",
            "unsupported_rows",
            "cutoff_rejections",
            "selected_periods",
        ):
            if type(value[name]) is not int or value[name] < 0:
                return False
        if (
            type(value["cache_complete"]) is not bool
            or type(value["revision_conflict"]) is not bool
        ):
            return False
        for observation in value["cache_observations"]:
            if set(observation) != _OBSERVATION_KEYS or not re.fullmatch(
                r"[0-9a-f]{64}", observation["payload_sha256"]
            ):
                return False
            left, right = map(
                date.fromisoformat,
                (observation["announcement_start"], observation["announcement_end"]),
            )
            if not left <= right <= cutoff:
                return False
            for key in ("checked_at", "retrieved_at", "observed_at"):
                if datetime.fromisoformat(observation[key]).utcoffset() is None:
                    return False
        periods = value["periods"]
        if (
            value["contract"] != CONTRACT
            or value["row_count"] <= 0
            or value["filtered_rows"]
            or value["row_rejections"]
            or value["unsupported_rows"]
            or value["cutoff_rejections"]
            or value["revision_conflict"]
            or not value["cache_complete"]
            or value["selected_periods"] != 0
            or not value["cache_observations"]
            or not periods
        ):
            return False
        for period in periods:
            if set(period) != _PERIOD_KEYS:
                return False
            if not re.fullmatch(r"[a-z0-9:-]+:[0-9a-f]{64}", period["candidate_identity"]):
                return False
            if not all(
                date.fromisoformat(period[key]) <= cutoff
                for key in ("ann_date", "f_ann_date", "period_end")
            ):
                return False
            missing = period["missing_core_fields"]
            count = period["declared_core_count"]
            if (
                type(count) is not int
                or not 0 < count <= 1000
                or period["company_type"] not in {"bank", "industrial_non_bank"}
                or not isinstance(missing, list)
                or len(missing) != len(set(missing))
                or not all(
                    isinstance(field, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", field)
                    for field in missing
                )
                or Decimal(period["core_coverage"])
                != Decimal(count - len(missing)) / Decimal(count)
            ):
                return False
            reasons = set(period["rejection_reasons"])
            if (
                period["disposition"] != "rejected"
                or "insufficient_completeness" not in reasons
                or not reasons <= _ALLOWED_REASONS
                or period["missing_critical_fields"] != []
                or Decimal(period["critical_coverage"]) != 1
                or not 0 <= Decimal(period["core_coverage"]) < Decimal("0.9")
                or not period["missing_core_fields"]
                or not period["ann_date"]
                or not period["f_ann_date"]
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def unavailable_view(diagnostic, *, ticker, tool, frequency, as_of, expected_scope=None):
    if (
        diagnostic.get("ticker") != ticker
        or diagnostic.get("tool_name") != tool
        or diagnostic.get("frequency") != frequency
        or diagnostic.get("as_of") != as_of
        or diagnostic.get("gap_eligible") is not True
        or not gap_eligible(diagnostic)
        or (expected_scope is not None and diagnostic.get("cache_scope") != expected_scope)
    ):
        raise ValueError("unverified_optional_financial_gap")
    observations = diagnostic["cache_observations"]
    return {
        "status": "unavailable",
        "reason": "insufficient_completeness",
        "financial_fact_created": False,
        "diagnostics": copy.deepcopy(diagnostic),
        "sha256": canonical_digest(diagnostic),
        "retrieved_at": sorted({x["retrieved_at"] for x in observations}),
        "observed_at": sorted({x["observed_at"] for x in observations}),
        "periods": sorted({x["period_end"] for x in diagnostic["periods"]}),
    }


def validate_prepared_views(views, *, ticker, as_of, allow_optional_gaps, expected_scopes=None):
    expected = {
        f"{tool}:{frequency}"
        for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow")
        for frequency in ("quarterly", "annual")
    }
    if not isinstance(views, dict) or set(views) != expected:
        raise ValueError("financial_preparation_requires_six_outcomes")
    gaps = 0
    for name, view in views.items():
        tool, frequency = name.split(":")
        if view.get("status", "available") == "available":
            if (
                set(view)
                not in (
                    {"sha256", "retrieved_at", "observed_at", "periods"},
                    {"status", "sha256", "retrieved_at", "observed_at", "periods"},
                )
                or not view.get("sha256")
                or not view.get("observed_at")
                or not view.get("retrieved_at")
                or not view.get("periods")
            ):
                raise ValueError("incomplete_available_financial_view")
        elif view.get("status") == "unavailable" and allow_optional_gaps:
            expected_view = unavailable_view(
                view.get("diagnostics", {}),
                ticker=ticker,
                tool=tool,
                frequency=frequency,
                as_of=as_of,
                expected_scope=(expected_scopes or {}).get(tool),
            )
            if view != expected_view:
                raise ValueError("financial_gap_receipt_mismatch")
            gaps += 1
        else:
            raise ValueError("financial_gap_not_authorized")
    return gaps
