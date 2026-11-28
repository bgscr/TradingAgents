"""Shared helpers for optional and fail-closed structured agent invocations.

Trader and Research Manager retain an optional free-text compatibility path.
Evidence-producing analysts and the Portfolio Manager use the required path in
enforcement mode so malformed or unsupported schemas cannot become evidence.

The caller chooses explicitly between ``invoke_required_structured`` and
``invoke_structured_or_freetext``; this module never silently changes an
enforced call into the compatibility path.

Centralising the patterns keeps failure classification consistent without
leaking provider payloads into reports or logs.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def submission_id_prefix(analyst: str) -> str:
    return f"{analyst}."


_MARKET_ID_PREFIX = submission_id_prefix("market")


_LOCAL_REPAIRS = {
    "blank_report": "Provide a nonblank report_markdown grounded in the original sources.",
    "duplicate_claim_id": "Give each distinct material claim a unique claim_id.",
    "claim_namespace": "Start every claim_id with the analyst namespace followed by a dot.",
    "duplicate_note_id": "Give each distinct evidence note a unique note_id.",
    "claim_note_collision": "Use distinct identifiers for material claims and evidence notes.",
    "unknown_evidence_id": "Use only evidence IDs shown in the original statement catalog.",
    "note_namespace": "Start every note_id with the analyst namespace followed by a dot."
}


class SubmissionConstraintError(ValueError):
    """A fixed local finalizer constraint, with no submitted values."""

    def __init__(self, kind: str):
        if kind not in _LOCAL_REPAIRS:
            raise ValueError("unknown submission constraint")
        super().__init__(_LOCAL_REPAIRS[kind])
        self.kind = kind


@dataclass(frozen=True)
class RequiredStructuredResult:
    """A fail-closed structured invocation result with a safe reason class."""

    value: Any | None
    reason: str | None = None
    attempts: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequiredStructuredBinding:
    """Bound structured runnable plus the safe schema name used for repair."""

    runnable: Any
    schema_name: str
    model_name: str
    method: str | None = None

    schema_json: str | None = None
    strict_json_model: type[BaseModel] | None = None

    def invoke(self, prompt: Any) -> Any:
        if self.method == "json_mode":
            prompt = _json_mode_prompt(prompt, self.schema_name, self.schema_json)
        return self.runnable.invoke(prompt)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def bind_required_structured(
    llm: Any,
    schema: type[T],
    agent_name: str,
    *,
    strict_json: bool = False,
) -> Any | None:
    """Bind required structured output without promising a free-text fallback."""
    try:
        required_binder = getattr(type(llm), "with_required_structured_output", None)
        if callable(required_binder):
            runnable = llm.with_required_structured_output(schema)
        else:
            runnable = llm.with_structured_output(schema, include_raw=True)
        raw_model_name = getattr(llm, "model_name", None)
        model_name = (
            raw_model_name
            if isinstance(raw_model_name, str) and raw_model_name
            else type(llm).__name__
        )
        method_resolver = getattr(type(llm), "get_required_structured_method", None)
        method = llm.get_required_structured_method() if callable(method_resolver) else None
        schema_json = None
        if strict_json:
            if method != "json_mode":
                raise ValueError("strict JSON validation requires JSON mode")
            schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        elif method == "json_mode" and agent_name == "Market Analyst":
            from tradingagents.evidence import AnalystEvidenceReport

            if schema is AnalystEvidenceReport:
                wire_schema = schema.model_json_schema()
                for definition, field_name in (
                    ("SubmittedEvidenceNote", "note_id"), ("SubmittedMaterialClaim", "claim_id"),
                ):
                    field = wire_schema["$defs"][definition]["properties"][field_name]
                    field["allOf"] = [{"pattern": "^" + re.escape(_MARKET_ID_PREFIX)}]
                schema_json = json.dumps(wire_schema, separators=(",", ":"))
        return RequiredStructuredBinding(
            runnable, schema.__name__, model_name, method, schema_json,
            strict_json_model=schema if strict_json else None,
        )
    except (NotImplementedError, AttributeError, ValueError, TypeError, RuntimeError):
        logger.warning(
            "%s: required structured output is unsupported by the provider",
            agent_name,
        )
        return None


def invoke_required_structured(
    structured_llm: Any | None,
    prompt: Any,
    agent_name: str,
    *,
    validator: Callable[[Any], Any] | None = None,
    max_attempts: int = 2,
    personal_validation_details: bool = False,
) -> RequiredStructuredResult:
    """Invoke a required schema, optionally allowing one fail-closed repair."""
    from tradingagents.run_diagnostics import record_output

    if type(max_attempts) is not int or max_attempts not in (1, 2):
        raise ValueError("structured max_attempts must be 1 or 2")
    if structured_llm is None:
        return RequiredStructuredResult(value=None, reason="unsupported", attempts=0)

    schema_name = getattr(structured_llm, "schema_name", "required schema")
    model_name = getattr(structured_llm, "model_name", "unknown")
    strict_json_model = (
        structured_llm.strict_json_model if isinstance(structured_llm, RequiredStructuredBinding) else None
    )
    current_prompt = prompt
    last_reason = "none_parsed"
    for attempt in range(1, max_attempts + 1):
        diagnostics = {}
        personal_observation = None
        validating_submission = False
        try:
            raw_result = structured_llm.invoke(current_prompt)
            value, reason, diagnostics, personal_observation = _unwrap_required_result(
                raw_result, strict_json_model=strict_json_model,
            )
            if value is not None and validator is not None:
                validating_submission = True
                value = validator(value)
                validating_submission = False
            if value is not None:
                record_output(agent_name.removesuffix(" Analyst").lower(), "finalizer", attempt,
                              diagnostics, outcome="success")
                logger.info(
                    "%s: required structured output succeeded "
                    "(schema=%s model=%s attempt=%d%s)",
                    agent_name,
                    schema_name,
                    model_name,
                    attempt,
                    _format_safe_diagnostics(diagnostics),
                )
                return RequiredStructuredResult(value=value, attempts=attempt)
            last_reason = reason or "none_parsed"
        except Exception as exc:  # noqa: BLE001 - classified without provider payloads
            last_reason = _structured_failure_reason(exc)
            response_diagnostics = diagnostics
            if agent_name != "Market Analyst":
                diagnostics = {}
            exception_diagnostics = _safe_exception_diagnostics(exc)
            diagnostics.update(exception_diagnostics)
            if last_reason == "output_limit":
                personal_observation = ("sdk_length_finish_reason_error", exception_diagnostics)
            elif validating_submission and agent_name != "Market Analyst":
                if personal_validation_details and isinstance(exc, SubmissionConstraintError):
                    response_diagnostics["validation_kind"] = exc.kind
                personal_observation = (last_reason, response_diagnostics)
            if last_reason == "validation_error":
                if validating_submission and agent_name == "Market Analyst" and isinstance(exc, SubmissionConstraintError):
                    diagnostics["validation_kind"] = exc.kind
                else:
                    diagnostics.update(_validation_metadata(exc))

        record_output(agent_name.removesuffix(" Analyst").lower(), "finalizer", attempt,
                      diagnostics, outcome=last_reason, personal_observation=personal_observation,
                      personal_validation_details=personal_validation_details)
        logger.warning(
            "%s: required structured output failed "
            "(schema=%s model=%s attempt=%d reason=%s%s)",
            agent_name,
            schema_name,
            model_name,
            attempt,
            last_reason,
            _format_safe_diagnostics(diagnostics),
        )
        if attempt == max_attempts or last_reason not in {"none_parsed", "validation_error"}:
            return RequiredStructuredResult(
                value=None,
                reason=last_reason,
                attempts=attempt,
                diagnostics=safe_output_diagnostics(diagnostics),
            )
        fields = ()
        if isinstance(structured_llm, RequiredStructuredBinding) and structured_llm.schema_json:
            fields = safe_output_diagnostics(diagnostics).get("validation_fields", ())
        local_kind = diagnostics.get("validation_kind") if agent_name == "Market Analyst" else None
        current_prompt = _repair_prompt(prompt, schema_name, fields, local_kind)

    raise AssertionError("unreachable structured retry state")


def _structured_failure_reason(exc: Exception) -> str:
    if _is_output_limit(exc):
        return "output_limit"
    return (
        "validation_error"
        if isinstance(exc, (ValidationError, ValueError, TypeError))
        else "transport_error"
    )


def _is_output_limit(exc: Exception) -> bool:
    # Lazy: pure classification users need not initialize an additional SDK.
    # An actual SDK length exception already implies that OpenAI was imported.
    import sys

    sdk = sys.modules.get("openai")
    length_error = getattr(sdk, "LengthFinishReasonError", None)
    return isinstance(length_error, type) and isinstance(exc, length_error)


def _unwrap_required_result(
    result: Any,
    *,
    strict_json_model: type[BaseModel] | None = None,
) -> tuple[Any | None, str | None, dict[str, Any], tuple[str, dict[str, Any]] | None]:
    """Remove LangChain's include_raw envelope without retaining its payload."""
    if not isinstance(result, Mapping) or not {
        "raw",
        "parsed",
        "parsing_error",
    }.issubset(result):
        if strict_json_model is not None:
            return None, "validation_error", {"validation_kind": "validation_unspecified"}, None
        return result, None if result is not None else "none_parsed", {}, None

    raw = result.get("raw")
    parsed = result.get("parsed")
    parsing_error = result.get("parsing_error")
    metadata = getattr(raw, "response_metadata", {}) or {}
    tool_calls = getattr(raw, "tool_calls", ()) or ()
    raw_diagnostics = response_output_diagnostics(raw)
    sdk_diagnostics = _safe_exception_diagnostics(parsing_error) if parsing_error is not None else {}
    diagnostics = {**raw_diagnostics, "tool_call_count": len(tool_calls), **sdk_diagnostics}
    raw_length = metadata.get("finish_reason") == "length"
    sdk_length = sdk_diagnostics.get("finish_reason") == "length"
    if raw_length or sdk_length:
        # Both signals can describe a single truncated response. Keep one
        # observation with its own counters, preferring raw when equally complete.
        use_sdk = sdk_length and (
            not raw_length
            or len(bounded_analyst_output_usage(sdk_diagnostics))
            > len(bounded_analyst_output_usage(raw_diagnostics))
        )
        observation = (
            ("sdk_length_finish_reason_error", sdk_diagnostics)
            if use_sdk else ("raw_finish_reason_length", raw_diagnostics)
        )
        return None, "output_limit", diagnostics, observation
    if strict_json_model is not None:
        # LangChain may repair partial JSON before building its parsed model.
        # Validate the complete raw text locally, after checking SDK/raw length.
        try:
            text = getattr(raw, "content", None)
            if not isinstance(text, str):
                raise TypeError("JSON response must be text")
            parsed = strict_json_model.model_validate(json.loads(
                text, object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            ))
        except (ValueError, TypeError) as exc:
            diagnostics.update(_validation_metadata(exc))
            return None, "validation_error", diagnostics, None
    if parsed is not None:
        return parsed, None, diagnostics, None
    if parsing_error is not None:
        diagnostics.update(_validation_metadata(parsing_error))
    return (
        None,
        "validation_error" if parsing_error is not None else "none_parsed",
        diagnostics,
        None,
    )


def _unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise json.JSONDecodeError("duplicate JSON key", "", 0)
    return result


def _reject_json_constant(_):
    raise json.JSONDecodeError("nonfinite JSON constant", "", 0)


_REPORT_FIELDS = frozenset({
    "contract_version", "report_markdown", "material_claims", "evidence_notes",
    "material_claims[].claim_id", "material_claims[].statement",
    "material_claims[].source_ref", "material_claims[].source_quote",
    "evidence_notes[].note_id", "evidence_notes[].kind", "evidence_notes[].statement",
    "evidence_notes[].source_ref", "evidence_notes[].source_quote",
    "material_claims[].evidence_id", "evidence_notes[].evidence_id",
})
_VALIDATION_KINDS = frozenset({"json_syntax", "schema_validation", "validation_unspecified", *_LOCAL_REPAIRS})


def _validation_metadata(exc):
    # Inspect typed causes only. SDK parser messages may contain the entire output.
    current = exc
    for _ in range(5):
        if not isinstance(current, BaseException):
            break
        if isinstance(current, json.JSONDecodeError):
            return {"validation_kind": "json_syntax"}
        if isinstance(current, ValidationError):
            errors = current.errors(include_url=False, include_input=False, include_context=False)
            if any(error["type"] == "json_invalid" for error in errors):
                return {"validation_kind": "json_syntax"}
            fields = set()
            for error in errors[:8]:
                loc = error["loc"]
                if len(loc) == 1 and type(loc[0]) is str and loc[0] in (
                        "contract_version", "report_markdown", "material_claims", "evidence_notes"):
                    fields.add(loc[0])
                elif (len(loc) == 3 and loc[0] in ("material_claims", "evidence_notes")
                      and type(loc[1]) is int and type(loc[2]) is str):
                    path = f"{loc[0]}[].{loc[2]}"
                    if path in _REPORT_FIELDS:
                        fields.add(path)
            return {"validation_kind": "schema_validation", "validation_fields": sorted(fields)}
        current = current.__cause__
        if current is None:
            break
    return {"validation_kind": "validation_unspecified"}


def _safe_exception_diagnostics(exc: Exception) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    if _is_output_limit(exc):
        diagnostics["finish_reason"] = "length"
        usage = getattr(getattr(exc, "completion", None), "usage", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if type(completion_tokens) is int and completion_tokens >= 0:
            diagnostics["completion_tokens"] = completion_tokens
            details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            if type(reasoning_tokens) is int and 0 <= reasoning_tokens <= completion_tokens:
                diagnostics["reasoning_tokens"] = reasoning_tokens
    return diagnostics


def safe_output_diagnostics(value: Any) -> dict[str, Any]:
    """Whitelist observed output metadata; never infer usage from the configured cap."""
    if not isinstance(value, Mapping):
        return {}
    safe = {}
    finish = value.get("finish_reason")
    if isinstance(finish, str) and finish in {
        "stop", "length", "tool_calls", "function_call", "content_filter",
    }:
        safe["finish_reason"] = finish
    completion = value.get("completion_tokens")
    if type(completion) is int and completion >= 0:
        safe["completion_tokens"] = completion
        reasoning = value.get("reasoning_tokens")
        if type(reasoning) is int and 0 <= reasoning <= completion:
            safe["reasoning_tokens"] = reasoning
    kind = value.get("validation_kind")
    if type(kind) is str and kind in _VALIDATION_KINDS:
        safe["validation_kind"] = kind
        fields = value.get("validation_fields")
        if kind == "schema_validation" and type(fields) in (list, tuple):
            safe["validation_fields"] = sorted({f for f in fields[:8] if type(f) is str and f in _REPORT_FIELDS})
    return safe


def response_output_diagnostics(response: Any) -> dict[str, Any]:
    """Read only SDK/LangChain usage counters, not response or reasoning content."""
    metadata = getattr(response, "response_metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    usage = getattr(response, "usage_metadata", None)
    usage = usage if isinstance(usage, Mapping) else {}
    provider_usage = metadata.get("token_usage")
    provider_usage = provider_usage if isinstance(provider_usage, Mapping) else {}
    completion = usage.get("output_tokens", provider_usage.get("completion_tokens"))
    details = usage.get("output_token_details")
    details = details if isinstance(details, Mapping) else {}
    provider_details = provider_usage.get("completion_tokens_details")
    provider_details = provider_details if isinstance(provider_details, Mapping) else {}
    reasoning = details.get("reasoning", provider_details.get("reasoning_tokens"))
    return safe_output_diagnostics({
        "finish_reason": metadata.get("finish_reason"),
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
    })


def bounded_analyst_output_usage(diagnostics: Mapping[str, Any]) -> dict[str, int]:
    """Project only usable counters for the personal receipt's bounded event."""
    completion = diagnostics.get("completion_tokens")
    if type(completion) is not int or not 0 <= completion < (1 << 31):
        return {}
    usage = {"completion_tokens": completion}
    reasoning = diagnostics.get("reasoning_tokens")
    if type(reasoning) is int and 0 <= reasoning <= completion:
        usage["reasoning_tokens"] = reasoning
    return usage


def _format_safe_diagnostics(diagnostics: Mapping[str, Any]) -> str:
    safe = tuple(
        f"{key}={value}"
        for key, value in diagnostics.items()
        if value not in (None, "", (), [])
    )
    return " " + " ".join(safe) if safe else ""


def _repair_prompt(original_prompt: Any, schema_name: str, fields: tuple[str, ...] | list[str] = (), kind: str | None = None) -> Any:
    correction = (
        "Previous structured attempt failed validation. "
        f"Produce {schema_name} exactly once with every required field and no prose. "
        "Use only evidence and source references from the original request."
    )
    if kind in _LOCAL_REPAIRS:
        correction += f" Local constraint [{kind}]: {_LOCAL_REPAIRS[kind]}"
        if kind in {"claim_namespace", "note_namespace"}:
            correction += f" Use the literal prefix {_MARKET_ID_PREFIX!r}."
    if fields:
        correction += " Failed schema paths: " + ", ".join(fields) + "."
    if isinstance(original_prompt, (list, tuple)):
        return [*original_prompt, HumanMessage(content=correction)]
    return f"{original_prompt}\n\n{correction}"


def _json_mode_prompt(original_prompt: Any, schema_name: str, schema_json: str | None = None) -> Any:
    instruction = (
        f"Return exactly one valid JSON object matching {schema_name}. "
        "Return JSON only, with no markdown fence or surrounding prose."
    )
    if schema_json is not None:
        instruction += "\nJSON Schema:\n" + schema_json
    if isinstance(original_prompt, (list, tuple)):
        return [*original_prompt, HumanMessage(content=instruction)]
    return f"{original_prompt}\n\n{instruction}"


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # A thinking model can answer in plain text instead of calling
                # the tool, leaving the parser with nothing to return. Treat it
                # as a structured miss and fall back, with a clear reason.
                raise ValueError("structured output returned no parsed result")
            return render(result)
        except Exception as exc:
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content
