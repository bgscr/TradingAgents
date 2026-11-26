"""Bounded execution diagnostics, never financial facts or decision inputs."""
from __future__ import annotations

import re
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tradingagents.run_diagnostics import diagnostic_text

Scope = Literal["tool_excerpt", "inline_source", "indicator_observation", "snapshot_value"]
Method = Literal["exact", "absolute_tolerance", "percent_mismatch", "no_reference"]
Reason = Literal["input_limit", "sensitive_input", "numeric_limit", "check_limit",
                 "binding_unavailable", "not_executed", "invalid_text"]
Number = Annotated[str, Field(max_length=32, pattern=r"^-?\d{1,16}(?:\.\d{1,12})?%?$")]
Tolerance = Annotated[str, Field(max_length=32, pattern=r"^0\.\d{0,12}5$")]
_NUMBER = re.compile(r"-?\d{1,16}(?:\.\d{1,12})?%?\Z", re.ASCII)


class NumericComparisonCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    scope: Scope
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_token: Number
    reference_token: Number | None
    method: Method
    absolute_tolerance: Tolerance | None = None
    matched: bool

    @model_validator(mode="after")
    def consistent_shape(self):
        if (self.method == "absolute_tolerance") != (self.absolute_tolerance is not None):
            raise ValueError("comparison_tolerance_shape")
        if (self.method == "no_reference") != (self.reference_token is None):
            raise ValueError("comparison_reference_shape")
        if self.method in {"no_reference", "percent_mismatch"} and self.matched:
            raise ValueError("comparison_match_shape")
        if self.reference_token is not None:
            percent_equal = self.claim_token.endswith("%") == self.reference_token.endswith("%")
            left = Decimal(self.claim_token.removesuffix("%"))
            right = Decimal(self.reference_token.removesuffix("%"))
            if self.method == "percent_mismatch":
                if percent_equal:
                    raise ValueError("comparison_percent_shape")
            elif not percent_equal:
                raise ValueError("comparison_percent_shape")
            elif self.method == "exact":
                if self.matched != (left == right):
                    raise ValueError("comparison_exact_shape")
            elif self.method == "absolute_tolerance":
                decimals = len(self.claim_token.removesuffix("%").partition(".")[2])
                if (decimals == 0 or left == right
                        or Decimal(self.absolute_tolerance) != Decimal(5).scaleb(-(decimals + 1))
                        or self.matched != (abs(left - right) <= Decimal(self.absolute_tolerance))):
                    raise ValueError("comparison_tolerance_shape")
        return self


class NumericComparisonEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal["1"] = "1"
    coverage: Literal["numeric_checks_only"] = "numeric_checks_only"
    status: Literal["recorded", "unavailable"]
    financial_unit_status: Literal["unverified"] = "unverified"
    unavailable_reason: Reason | None = None
    # State reducers pass JSON-style mappings, not only JSON strings. Permit
    # list-to-tuple representation normalization; each check remains strict.
    checks: tuple[NumericComparisonCheck, ...] = Field(default=(), max_length=128, strict=False)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "recorded":
            if not self.checks or self.unavailable_reason is not None:
                raise ValueError("comparison_recorded_shape")
        elif self.checks or self.unavailable_reason is None:
            raise ValueError("comparison_unavailable_shape")
        return self


class ComparisonRecorder:
    """Local, bounded observer passed to actual comparator calls. No I/O."""

    def __init__(self, *texts):
        self.reason = None
        self.checks = []
        self.screen(*texts)

    def deny(self, reason):
        if self.reason is None:
            self.reason = reason
        self.checks.clear()

    def screen(self, *texts):
        if self.reason is not None:
            return
        for text in texts:
            if not isinstance(text, str) or len(text) > 16384:
                self.deny("input_limit")
                return
            # Only status is used; never retain the prose/hash projection.
            try:
                status = diagnostic_text(text, limit=16384)["status"]
            except UnicodeError:
                self.deny("invalid_text")
                return
            if status != "complete":
                self.deny("sensitive_input")
                return

    def observer(self, scope, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            self.deny("binding_unavailable")

        def record(claim, reference, method, tolerance, matched):
            if self.reason is not None:
                return
            values = (claim,) if reference is None else (claim, reference)
            if any(not isinstance(v, str) or len(v) > 32 or not _NUMBER.fullmatch(v) for v in values):
                self.deny("numeric_limit")
                return
            if len(self.checks) >= 128:
                self.deny("check_limit")
                return
            try:
                check = NumericComparisonCheck(
                    scope=scope, artifact_sha256=digest, claim_token=claim,
                    reference_token=reference, method=method,
                    absolute_tolerance=None if tolerance is None else format(tolerance, "f"),
                    matched=matched,
                )
            except ValidationError:
                self.deny("numeric_limit")
                return
            self.checks.append(check)

        return record

    def finish(self, facts):
        bound = {fact.artifact_sha256 for fact in facts}
        if any(check.artifact_sha256 not in bound for check in self.checks):
            self.deny("binding_unavailable")
        if self.reason is not None or not self.checks:
            return NumericComparisonEvidence(status="unavailable",
                                             unavailable_reason=self.reason or "not_executed")
        return NumericComparisonEvidence(status="recorded", checks=tuple(self.checks))


def bound_comparison(validation, facts, artifacts):
    """Check reference membership at publication; never recompute comparisons."""
    comparison = validation.comparison_evidence
    if comparison is None or comparison.status == "unavailable":
        return comparison
    selected = [fact for fact in facts if fact.fact_id in validation.fact_ids]
    bound = {fact.artifact_sha256 for fact in selected}
    available = {artifact.artifact_sha256 for artifact in artifacts}
    if (len(selected) != len(set(validation.fact_ids))
            or any(check.artifact_sha256 not in bound & available for check in comparison.checks)):
        return NumericComparisonEvidence(status="unavailable", unavailable_reason="binding_unavailable")
    screen = ComparisonRecorder()
    for artifact in artifacts:
        if artifact.artifact_sha256 in bound:
            screen.screen(artifact.raw_text)
    if screen.reason is not None:
        return NumericComparisonEvidence(status="unavailable", unavailable_reason=screen.reason)
    for fact in selected:
        matches = [artifact for artifact in artifacts if (
            artifact.artifact_sha256, artifact.source_ref, artifact.tool_call_id, artifact.tool_name
        ) == (fact.artifact_sha256, fact.source_ref, fact.tool_call_id, fact.tool_name)]
        if len(matches) != 1:
            return NumericComparisonEvidence(status="unavailable", unavailable_reason="binding_unavailable")
        artifact = matches[0]
        if (sha256(artifact.raw_text.encode("utf-8")).hexdigest() != fact.artifact_sha256
                or not (0 <= fact.source_span_start <= fact.source_span_end <= len(artifact.raw_text))
                or artifact.raw_text[fact.source_span_start:fact.source_span_end] != fact.raw_text):
            return NumericComparisonEvidence(status="unavailable", unavailable_reason="binding_unavailable")
    return comparison


def comparison_markdown(comparison, *, chinese):
    """Only closed, validated fields; no raw claims or free-form diagnostics."""
    comparison = NumericComparisonEvidence.model_validate(comparison.model_dump())
    if comparison.status == "unavailable":
        return [f"  - {'比较依据未保留' if chinese else 'Comparison unavailable'}: `{comparison.unavailable_reason}`."]
    lines = [
        "  - 数字校验执行记录；金融单位未核实，不是金融事实核验或事实错误证明。"
        if chinese else
        "  - Executed numeric checks; financial units unverified. Not verified financial facts or proof of error."
    ]
    scopes = {"tool_excerpt": "工具摘录", "inline_source": "内联全文",
              "indicator_observation": "指标观测值", "snapshot_value": "快照观测值"}
    methods = {"exact": "精确数值", "absolute_tolerance": "绝对容差",
               "percent_mismatch": "百分号类型不同", "no_reference": "无参照数字"}
    for check in comparison.checks[:8]:
        scope = scopes[check.scope] if chinese else check.scope
        method = methods[check.method] if chinese else check.method
        lines.append(
            f"  - {scope}: `{check.claim_token}` / `{check.reference_token or '—'}`; "
            f"{method}; {'容差' if chinese else 'tolerance'} `{check.absolute_tolerance or '—'}`; "
            f"{'匹配' if chinese else 'matched'} `{check.matched}`; artifact `{check.artifact_sha256}`."
        )
    if len(comparison.checks) > 8:
        lines.append(f"  - {'仅展示前 8 项，审计保留总项数' if chinese else 'First 8 shown; total audit checks'}: {len(comparison.checks)}.")
    return lines
