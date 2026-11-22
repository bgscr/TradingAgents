"""Closed advisory selections for the opt-in finite disclosure workflow.

All prose is produced by the trusted runtime. Models can select registered IDs
and one relationship; they cannot author observations, amounts or conclusions.
This module does not supply directional assertions or alter decision policy.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from tradingagents.finite_disclosure_runtime import (
    is_finite_state as is_finite_state,
    render_selection,
    require_context,
)

FiniteRole = Literal[
    "bull",
    "bear",
    "research_manager",
    "trader",
    "aggressive",
    "conservative",
    "neutral",
    "portfolio_manager",
]

_LABELS = {
    "bull": "Bull Analyst",
    "bear": "Bear Analyst",
    "research_manager": "Research Manager",
    "trader": "Trader",
    "aggressive": "Aggressive Analyst",
    "conservative": "Conservative Analyst",
    "neutral": "Neutral Analyst",
    "portfolio_manager": "Portfolio Manager",
}
_MAX_TRACE = 128


class FiniteAdvisorySelection(BaseModel):
    """The entire model-authored vocabulary; no arbitrary text or numbers."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    evidence_ids: list[StrictStr] = Field(min_length=1, max_length=9)
    statement_ids: list[StrictStr] = Field(min_length=1, max_length=60)
    relationship: Literal["cites", "scope_limited"]

    @field_validator("evidence_ids", "statement_ids")
    @classmethod
    def unique_nonempty_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item or len(item) > 256 or item.strip() != item for item in value
        ):
            raise ValueError("finite selection identifiers are invalid")
        return value


class FiniteAdvisoryEntry(BaseModel):
    """Checkpoint-safe closed record for one downstream node invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    role: FiniteRole
    selection: FiniteAdvisorySelection


def _render_entry(entry: FiniteAdvisoryEntry, state: Mapping[str, Any]) -> str:
    rendered = render_selection(
        entry.role,
        entry.selection.model_dump(mode="json"),
        state,
    )
    if not isinstance(rendered, str) or not rendered:
        raise ValueError("finite runtime returned an invalid rendering")
    return f"{_LABELS[entry.role]}: {rendered}"


def _project_trace(
    entries: list[FiniteAdvisoryEntry],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild legacy-shaped state exclusively from revalidated selections."""
    investment = {
        "history": "",
        "bull_history": "",
        "bear_history": "",
        "current_response": "",
        "judge_decision": "",
        "count": 0,
    }
    risk = {
        "history": "",
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "latest_speaker": "",
        "judge_decision": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "count": 0,
    }
    projected: dict[str, Any] = {
        "investment_debate_state": investment,
        "risk_debate_state": risk,
        "investment_plan": "",
        "trader_investment_plan": "",
        "portfolio_advisory": "",
    }
    for entry in entries:
        role = entry.role
        argument = _render_entry(entry, state)
        if role in {"bull", "bear"}:
            investment["history"] += "\n" + argument
            investment[f"{role}_history"] += "\n" + argument
            investment["current_response"] = argument
            investment["count"] += 1
        elif role == "research_manager":
            investment["judge_decision"] = argument
            investment["current_response"] = argument
            projected["investment_plan"] = argument
        elif role == "trader":
            projected["trader_investment_plan"] = argument
        elif role in {"aggressive", "conservative", "neutral"}:
            risk["history"] += "\n" + argument
            risk[f"{role}_history"] += "\n" + argument
            risk[f"current_{role}_response"] = argument
            risk["latest_speaker"] = role.title()
            risk["count"] += 1
        else:
            risk["judge_decision"] = argument
            risk["latest_speaker"] = "Judge"
            projected["portfolio_advisory"] = argument
    return projected


def validate_finite_inputs(state: Mapping[str, Any]) -> list[FiniteAdvisoryEntry]:
    """Validate trusted binding, every prior selection and retained prose.

    Empty initial fields are allowed. Once populated, advisory fields must equal
    their deterministic projection. Arbitrary report and memory prose is never
    included in the finite model prompt; the runtime validates source bindings.
    """
    require_context(state)
    raw_trace = state.get("finite_advisory_trace", [])
    if not isinstance(raw_trace, list) or len(raw_trace) > _MAX_TRACE:
        raise ValueError("finite advisory trace is invalid")
    entries = [FiniteAdvisoryEntry.model_validate(item) for item in raw_trace]
    projected = _project_trace(entries, state)
    for key, expected in projected.items():
        actual = state.get(key)
        if isinstance(expected, dict):
            if actual is None and not any(expected.values()):
                continue
            if not isinstance(actual, Mapping) or set(actual) - set(expected):
                raise ValueError("finite advisory debate state is invalid")
            for field, value in expected.items():
                observed = actual.get(field, 0 if field == "count" else "")
                if type(observed) is not type(value) or observed != value:
                    raise ValueError("finite advisory debate differs from selection trace")
        elif actual is None and expected == "":
            continue
        elif actual != expected or not isinstance(actual, str):
            raise ValueError("finite advisory rendering differs from selection trace")
    return entries


def _invoke_once(llm: Any, prompt: str) -> FiniteAdvisorySelection:
    """Exactly one closed-schema invocation, with no repair or free-text path."""
    try:
        required_binder = getattr(type(llm), "with_required_structured_output", None)
        if callable(required_binder):
            runnable = llm.with_required_structured_output(FiniteAdvisorySelection)
        else:
            runnable = llm.with_structured_output(
                FiniteAdvisorySelection,
                include_raw=True,
            )
        result = runnable.invoke(prompt)
        if isinstance(result, Mapping) and {"raw", "parsed", "parsing_error"} <= set(result):
            if result["parsing_error"] is not None or result["parsed"] is None:
                raise ValueError("finite structured response is invalid")
            result = result["parsed"]
        return FiniteAdvisorySelection.model_validate(result)
    except Exception:
        # Do not disclose provider text in node errors or retain raw content.
        raise ValueError("finite advisory structured selection failed") from None


def finite_advisory_update(
    llm: Any,
    role: FiniteRole,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one preserved downstream role against the trusted finite catalog."""
    if role not in _LABELS:
        raise ValueError("finite advisory role is invalid")
    entries = validate_finite_inputs(state)
    if len(entries) >= _MAX_TRACE:
        raise ValueError("finite advisory trace limit reached")
    context = require_context(state)
    catalog = context.selection_catalog(role)
    prompt = (
        f"You are the {_LABELS[role]} in finite disclosure advisory mode. "
        "Select registered evidence and statement IDs relevant to your role. "
        "Return only FiniteAdvisorySelection. Use cites for selected observations "
        "or scope_limited to emphasize their evidence limits. Do not author text, "
        "amounts, ratios, trading directions or additional relationships. "
        "Financial observations are limited to the registered disclosure scope.\n"
        "Trusted identifier catalog:\n"
        + json.dumps(catalog, ensure_ascii=True, sort_keys=True)
        + "\nPrior validated advisory selections:\n"
        + "\n".join(_render_entry(entry, state) for entry in entries)
    )
    selection = _invoke_once(llm, prompt)
    entry = FiniteAdvisoryEntry(role=role, selection=selection)
    _render_entry(entry, state)  # Cross-record membership checked before any update.
    trace = [*entries, entry]
    projection = _project_trace(trace, state)
    update: dict[str, Any] = {
        "finite_advisory_trace": [item.model_dump(mode="json") for item in trace],
    }
    if role in {"bull", "bear", "research_manager"}:
        update["investment_debate_state"] = projection["investment_debate_state"]
        if role == "research_manager":
            update["investment_plan"] = projection["investment_plan"]
    elif role == "trader":
        update["trader_investment_plan"] = projection["trader_investment_plan"]
        update["sender"] = "Trader"
    else:
        update["risk_debate_state"] = projection["risk_debate_state"]
        if role == "portfolio_manager":
            update["portfolio_advisory"] = projection["portfolio_advisory"]
    validate_finite_inputs({**state, **update})
    return update
