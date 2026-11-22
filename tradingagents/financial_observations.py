"""Source-row observations for the versioned CURRENT statement view.

This adapter never interprets model prose, creates canonical facts, or grants
PIT/decision eligibility. Its selector is one exact complete physical CSV row.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CURRENT_OBSERVATION_MARKER = "# financial_observations_contract=current-statement-observation-v1"
CURRENT_STATEMENT_COLUMNS = (
    "period_end",
    "ann_date",
    "f_ann_date",
    "retrieved_at",
    "observed_at",
    "provider_field",
    "original_value",
    "original_unit",
    "field",
    "value",
    "unit",
    "artifact_identity",
    "value_status",
)
STATEMENT_TITLES = {
    "get_balance_sheet": "Balance Sheet",
    "get_income_statement": "Income Statement",
    "get_cashflow": "Cash Flow",
}
FINANCIAL_TOOLS = frozenset((*STATEMENT_TITLES, "get_fundamentals"))
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_BASE_UNIT_ALIASES = frozenset({"cny_base_unit", "cny_yuan", "yuan", "元"})
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
FILING_CONTEXT_PREFIX = "# financial_filing_context="
# This view is explicitly cumulative report_type=1; it cannot certify other bases.
_REPORT_WORDS = {"1": "one"}
_FLAG_WORDS = {"0": "zero", "1": "one"}
_FILING_ARTIFACT = re.compile(r"financial-provider-artifact:v1:[0-9a-f]{64}\Z")


class FinancialFilingContext(BaseModel):
    """Optional provider metadata; never a period/decision eligibility grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    report_type: Literal["1"] | None = None
    consolidation_scope: Literal["consolidated", "parent", "unknown"] = "unknown"
    update_flag: Literal["0", "1"] | None = None

    @model_validator(mode="after")
    def consistent_scope(self):
        expected = "unknown" if self.report_type is None else "consolidated"
        if self.consolidation_scope != expected:
            raise ValueError("inconsistent filing context")
        return self


def filing_context_header(entries) -> str | None:
    """Closed symbol-only metadata; numeric codes must not enter numeric pools."""
    result = {}
    for identity, report_type, scope, update_flag in entries:
        if not isinstance(identity, str) or not _FILING_ARTIFACT.fullmatch(identity):
            return None
        report_type = report_type if report_type in _REPORT_WORDS else None
        scope = scope if report_type is not None else "unknown"
        update_flag = update_flag if update_flag in _FLAG_WORDS else None
        context = FinancialFilingContext(report_type=report_type, consolidation_scope=scope,
                                         update_flag=update_flag)
        encoded = {"report_type": _REPORT_WORDS.get(context.report_type),
                   "consolidation_scope": context.consolidation_scope,
                   "update_flag": _FLAG_WORDS.get(context.update_flag)}
        if identity in result and result[identity] != encoded:
            return None
        result[identity] = encoded
        if len(result) > 8:
            return None
    return FILING_CONTEXT_PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")) if result else None


def _bound_filing_context(lines, artifact_identity):
    headers = [line[len(FILING_CONTEXT_PREFIX):] for line in lines if line.startswith(FILING_CONTEXT_PREFIX)]
    if len(headers) != 1 or len(headers[0]) > 8192:
        return None

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate filing metadata key")
            obj[key] = value
        return obj

    try:
        values = json.loads(headers[0], object_pairs_hook=unique_object)
        if not isinstance(values, dict) or not 1 <= len(values) <= 8:
            return None
        contexts = {}
        report_codes = {word: code for code, word in _REPORT_WORDS.items()}
        flag_codes = {word: code for code, word in _FLAG_WORDS.items()}
        for identity, value in values.items():
            if not _FILING_ARTIFACT.fullmatch(identity) or not isinstance(value, dict):
                return None
            if set(value) != {"report_type", "consolidation_scope", "update_flag"}:
                return None
            if value["report_type"] is not None and value["report_type"] not in report_codes:
                return None
            if value["update_flag"] is not None and value["update_flag"] not in flag_codes:
                return None
            contexts[identity] = FinancialFilingContext(
                report_type=report_codes.get(value["report_type"]),
                consolidation_scope=value["consolidation_scope"],
                update_flag=flag_codes.get(value["update_flag"]))
        return contexts.get(artifact_identity)
    except (ValueError, TypeError, RecursionError):
        return None


class FinancialObservation(BaseModel):
    """Typed meaning of a source row, not the meaning of accompanying prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract: Literal["current-statement-observation-v1"] = "current-statement-observation-v1"
    observation_id: str = Field(pattern=r"^finobs:[0-9a-f]{64}$")
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SS|SZ)$")
    tool_name: Literal["get_balance_sheet", "get_income_statement", "get_cashflow"]
    frequency: Literal["annual", "quarterly"]
    field: str
    provider_field: str
    value: str
    original_value: str
    original_unit: str
    source_unit: Literal["CNY_BASE_UNIT"] = "CNY_BASE_UNIT"
    unit: Literal["CNY"] = "CNY"
    scale: Literal["1"] = "1"
    period_end: str
    ann_date: str
    f_ann_date: str
    period_basis: Literal["period_end_balance", "cumulative_report_type_1"]
    provider_artifact_identity: str
    filing_context: FinancialFilingContext | None = Field(default=None, exclude_if=lambda value: value is None)
    semantic_scope: Literal["source_row_only"] = "source_row_only"
    statement_verified: Literal[False] = False
    decision_qualified: Literal[False] = False

    def render(self) -> str:
        context = self.filing_context
        scope = {"consolidated": "合并", "parent": "母公司", "unknown": "未知"}[
            context.consolidation_scope if context else "unknown"]
        return (
            f"{self.symbol} | {self.field} | {self.value} {self.unit} | scale={self.scale}"
            f" | period_end={self.period_end} | ann_date={self.ann_date}"
            f" | f_ann_date={self.f_ann_date or 'unknown'} | source_frequency={self.frequency}"
            f" | basis={self.period_basis} | observation_id={self.observation_id}"
            f" | 报表范围={scope} | 供应商报表类型={context.report_type if context and context.report_type else '未知'}"
            f" | 更新标记={context.update_flag if context and context.update_flag is not None else '未知'}"
        )


def _valid_row(row: dict[str, str], frequency: str) -> bool:
    try:
        period, announcement = (
            date.fromisoformat(row["period_end"]),
            date.fromisoformat(row["ann_date"]),
        )
        if period.isoformat() != row["period_end"] or announcement.isoformat() != row["ann_date"]:
            return False
        if announcement < period or (period.month, period.day) not in {
            (3, 31),
            (6, 30),
            (9, 30),
            (12, 31),
        }:
            return False
        if frequency == "annual" and (period.month, period.day) != (12, 31):
            return False
        if row["f_ann_date"]:
            final = date.fromisoformat(row["f_ann_date"])
            if final.isoformat() != row["f_ann_date"] or final < period:
                return False
        for key in ("retrieved_at", "observed_at"):
            stamp = datetime.fromisoformat(row[key])
            if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                return False
        if not _FIELD.fullmatch(row["field"]) or not _ARTIFACT.fullmatch(row["artifact_identity"]):
            return False
        if row["value_status"] == "missing":
            return all(
                row[key] == ""
                for key in ("provider_field", "original_value", "original_unit", "value", "unit")
            )
        if row["value_status"] != "reported" or not _FIELD.fullmatch(row["provider_field"]):
            return False
        if not all(_DECIMAL.fullmatch(row[key]) for key in ("value", "original_value")):
            return False
        value, original = Decimal(row["value"]), Decimal(row["original_value"])
        return (
            row["unit"] == "CNY_BASE_UNIT"
            and row["original_unit"].casefold() in _BASE_UNIT_ALIASES
            and value.is_finite()
            and original.is_finite()
            and value == original
        )
    except (ValueError, InvalidOperation):
        return False


def bind_current_statement_observation(
    *,
    raw_text: str,
    quote: str,
    source_ref: str,
    tool_call_id: str,
    tool_name: str,
    artifact_sha256: str,
    symbol: str,
) -> FinancialObservation | None:
    """Recompute a bound observation; unknown/malformed views fail closed."""
    if (
        tool_name not in STATEMENT_TITLES
        or not source_ref
        or not tool_call_id
        or re.fullmatch(r"[0-9]{6}\.(SS|SZ)", symbol) is None
        or sha256(raw_text.encode("utf-8")).hexdigest() != artifact_sha256
    ):
        return None
    physical = raw_text.splitlines(keepends=True)
    lines = [line.removesuffix("\n").removesuffix("\r") for line in physical]
    header = ",".join(CURRENT_STATEMENT_COLUMNS)
    if (
        lines.count(CURRENT_OBSERVATION_MARKER) != 1
        or lines.count(header) != 1
        or lines.count("# CURRENT-only; source=tushare; not strict PIT evidence.") != 1
    ):
        return None
    first = re.fullmatch(
        re.escape(f"# {STATEMENT_TITLES[tool_name]} data for {symbol} ")
        + r"\((annual|quarterly)\)",
        lines[0] if lines else "",
    )
    if first is None:
        return None
    frequency = first.group(1)
    header_index = lines.index(header)
    if not all(line.startswith("# ") for line in lines[:header_index]):
        return None
    if not lines[header_index + 1 :] or any(not line for line in lines[header_index + 1 :]):
        return None
    if lines[header_index + 1 :].count(quote) != 1 or not quote:
        return None
    selected = None
    seen = set()
    offset = sum(len(line) for line in physical[: header_index + 1])
    for line, exact in zip(lines[header_index + 1 :], physical[header_index + 1 :], strict=True):
        try:
            cells = next(csv.reader([line], strict=True))
        except (csv.Error, StopIteration):
            return None
        if len(cells) != len(CURRENT_STATEMENT_COLUMNS) or line in seen:
            return None
        seen.add(line)
        row = dict(zip(CURRENT_STATEMENT_COLUMNS, cells, strict=True))
        if not _valid_row(row, frequency):
            return None
        if line == quote:
            selected = (row, offset)
        offset += len(exact)
    if selected is None or selected[0]["value_status"] != "reported":
        return None
    row, start = selected
    binding = {
        "contract": "current-statement-observation-v1",
        "source_ref": source_ref,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "artifact_sha256": artifact_sha256,
        "symbol": symbol,
        "frequency": frequency,
        "span_start": start,
        "span_end": start + len(quote),
        "row": row,
    }
    identity = sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FinancialObservation(
        observation_id="finobs:" + identity,
        symbol=symbol,
        tool_name=tool_name,
        frequency=frequency,
        field=row["field"],
        provider_field=row["provider_field"],
        value=row["value"],
        original_value=row["original_value"],
        original_unit=row["original_unit"],
        period_end=row["period_end"],
        ann_date=row["ann_date"],
        f_ann_date=row["f_ann_date"],
        period_basis="period_end_balance"
        if tool_name == "get_balance_sheet"
        else "cumulative_report_type_1",
        provider_artifact_identity=row["artifact_identity"],
        filing_context=_bound_filing_context(lines[:header_index], row["artifact_identity"]),
    )
