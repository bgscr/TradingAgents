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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class RequiredStructuredResult:
    """A fail-closed structured invocation result with a safe reason class."""

    value: BaseModel | None
    reason: str | None = None


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
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError, ValueError, TypeError):
        logger.warning(
            "%s: required structured output is unsupported by the provider",
            agent_name,
        )
        return None


def invoke_required_structured(
    structured_llm: Any | None,
    prompt: Any,
    agent_name: str,
) -> RequiredStructuredResult:
    """Invoke a required schema once and classify failure without leaking payloads."""
    if structured_llm is None:
        return RequiredStructuredResult(value=None, reason="unsupported")
    try:
        result = structured_llm.invoke(prompt)
        if result is None:
            return RequiredStructuredResult(value=None, reason="none_parsed")
        return RequiredStructuredResult(value=result)
    except Exception as exc:  # noqa: BLE001 - provider errors are safely classified
        reason = (
            "validation_error"
            if isinstance(exc, (ValidationError, ValueError, TypeError))
            else "transport_error"
        )
        logger.warning(
            "%s: required structured output failed (%s)",
            agent_name,
            reason,
        )
        return RequiredStructuredResult(value=None, reason=reason)


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
