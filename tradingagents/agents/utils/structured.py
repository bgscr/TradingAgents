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

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


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

    def invoke(self, prompt: Any) -> Any:
        if self.method == "json_mode":
            prompt = _json_mode_prompt(prompt, self.schema_name)
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
        return RequiredStructuredBinding(runnable, schema.__name__, model_name, method)
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
) -> RequiredStructuredResult:
    """Invoke a required schema, optionally allowing one fail-closed repair."""
    if type(max_attempts) is not int or max_attempts not in (1, 2):
        raise ValueError("structured max_attempts must be 1 or 2")
    if structured_llm is None:
        return RequiredStructuredResult(value=None, reason="unsupported", attempts=0)

    schema_name = getattr(structured_llm, "schema_name", "required schema")
    model_name = getattr(structured_llm, "model_name", "unknown")
    current_prompt = prompt
    last_reason = "none_parsed"
    for attempt in range(1, max_attempts + 1):
        try:
            raw_result = structured_llm.invoke(current_prompt)
            value, reason, diagnostics = _unwrap_required_result(raw_result)
            if value is not None and validator is not None:
                value = validator(value)
            if value is not None:
                from tradingagents.run_diagnostics import record_output

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
            diagnostics = _safe_exception_diagnostics(exc)

        from tradingagents.run_diagnostics import record_output

        record_output(agent_name.removesuffix(" Analyst").lower(), "finalizer", attempt,
                      diagnostics, outcome=last_reason)
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
        current_prompt = _repair_prompt(prompt, schema_name)

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


def _unwrap_required_result(result: Any) -> tuple[Any | None, str | None, dict[str, Any]]:
    """Remove LangChain's include_raw envelope without retaining its payload."""
    if not isinstance(result, Mapping) or not {
        "raw",
        "parsed",
        "parsing_error",
    }.issubset(result):
        return result, None if result is not None else "none_parsed", {}

    raw = result.get("raw")
    parsed = result.get("parsed")
    parsing_error = result.get("parsing_error")
    metadata = getattr(raw, "response_metadata", {}) or {}
    tool_calls = getattr(raw, "tool_calls", ()) or ()
    diagnostics = response_output_diagnostics(raw)
    diagnostics["tool_call_count"] = len(tool_calls)
    if parsing_error is not None:
        diagnostics.update(_safe_exception_diagnostics(parsing_error))
    if metadata.get("finish_reason") == "length" or (
        parsing_error is not None and _is_output_limit(parsing_error)
    ):
        # A partially parsed object is not complete evidence. Do not spend an
        # automatic repair attempt or silently raise the configured output cap.
        return None, "output_limit", diagnostics
    if parsed is not None:
        return parsed, None, diagnostics
    return (
        None,
        "validation_error" if parsing_error is not None else "none_parsed",
        diagnostics,
    )


def _safe_exception_diagnostics(exc: Exception) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"error_type": type(exc).__name__}
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
    if isinstance(exc, ValidationError):
        fields = []
        for error in exc.errors(include_url=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            fields.append(f"{location}:{error.get('type', 'validation_error')}")
        diagnostics["validation_fields"] = tuple(fields)
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


def _format_safe_diagnostics(diagnostics: Mapping[str, Any]) -> str:
    safe = tuple(
        f"{key}={value}"
        for key, value in diagnostics.items()
        if value not in (None, "", (), [])
    )
    return " " + " ".join(safe) if safe else ""


def _repair_prompt(original_prompt: Any, schema_name: str) -> Any:
    correction = (
        "Previous structured attempt failed validation. "
        f"Produce {schema_name} exactly once with every required field and no prose. "
        "Use only evidence and source references from the original request."
    )
    if isinstance(original_prompt, (list, tuple)):
        return [*original_prompt, HumanMessage(content=correction)]
    return f"{original_prompt}\n\n{correction}"


def _json_mode_prompt(original_prompt: Any, schema_name: str) -> Any:
    instruction = (
        f"Return exactly one valid JSON object matching {schema_name}. "
        "Return JSON only, with no markdown fence or surrounding prose."
    )
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
