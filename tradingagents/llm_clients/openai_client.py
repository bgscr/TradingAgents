import os
import re
import time
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai import DEFAULT_MAX_RETRIES
from pydantic import PrivateAttr

from tradingagents.run_telemetry import ModelTransportAttempt, RunTelemetryLedger

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model

_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_RESPONSE_METADATA_EXTENSION = "tradingagents.model_transport_response"
_OBSERVED_CLIENT_ATTRIBUTE = "_tradingagents_model_transport_observed"


@dataclass(frozen=True)
class _ModelTransportCallContext:
    ledger: RunTelemetryLedger
    logical_call_id: str
    provider_family: str
    model_id: str
    configured_max_retries: int


_ACTIVE_MODEL_TRANSPORT_CALL: ContextVar[_ModelTransportCallContext | None] = (
    ContextVar("active_model_transport_call", default=None)
)


def _safe_transport_value(
    value: Any,
    *,
    pattern: re.Pattern[str],
    maximum_chars: int,
) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= maximum_chars and pattern.fullmatch(normalized):
        return normalized
    return "unavailable"


def _safe_provider_request_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if (
        normalized
        and len(normalized) <= 128
        and _SAFE_REQUEST_ID_RE.fullmatch(normalized)
    ):
        return normalized
    return None


def _retry_index(request: httpx.Request) -> int | None:
    raw_value = request.headers.get("x-stainless-retry-count")
    try:
        value = int(raw_value) if raw_value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def _transport_exception_family(exc: Exception) -> str:
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection"
    if isinstance(exc, httpx.TransportError):
        return "transport"
    return "client"


def _capture_response_metadata(response: httpx.Response) -> None:
    """Attach only status/request-id allowlist fields for send-error handling."""

    with suppress(Exception):
        response.request.extensions[_RESPONSE_METADATA_EXTENSION] = (
            int(response.status_code),
            _safe_provider_request_id(response.headers.get("x-request-id")),
        )


async def _capture_async_response_metadata(response: httpx.Response) -> None:
    _capture_response_metadata(response)


def _model_transport_attempt(
    *,
    context: _ModelTransportCallContext,
    request: httpx.Request,
    attempt_ordinal: int,
    elapsed_seconds: float,
    disposition: str,
    response: httpx.Response | None = None,
    exception: Exception | None = None,
) -> ModelTransportAttempt:
    response_metadata = request.extensions.get(_RESPONSE_METADATA_EXTENSION)
    http_status: int | None = None
    provider_request_id: str | None = None
    if response is not None:
        http_status = int(response.status_code)
        provider_request_id = _safe_provider_request_id(
            response.headers.get("x-request-id")
        )
    elif (
        isinstance(response_metadata, tuple)
        and len(response_metadata) == 2
        and isinstance(response_metadata[0], int)
    ):
        http_status = response_metadata[0]
        provider_request_id = _safe_provider_request_id(response_metadata[1])
    exception_class = None
    exception_family = None
    if exception is not None:
        exception_class = _safe_transport_value(
            type(exception).__name__,
            pattern=re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$"),
            maximum_chars=128,
        )
        exception_family = _transport_exception_family(exception)
    return ModelTransportAttempt(
        logical_call_id=context.logical_call_id,
        attempt_ordinal=attempt_ordinal,
        configured_max_retries=context.configured_max_retries,
        retry_index=_retry_index(request),
        provider_family=context.provider_family,
        model_id=context.model_id,
        endpoint_host=_safe_transport_value(
            request.url.host,
            pattern=_SAFE_HOST_RE,
            maximum_chars=253,
        ),
        elapsed_seconds=max(0.0, round(elapsed_seconds, 6)),
        disposition=disposition,
        exception_class=exception_class,
        exception_family=exception_family,
        http_status=http_status,
        provider_request_id=provider_request_id,
    )


def _record_sync_transport_send(
    original_send: Any,
    request: httpx.Request,
    *args: Any,
    **kwargs: Any,
) -> httpx.Response:
    context = _ACTIVE_MODEL_TRANSPORT_CALL.get()
    if context is None:
        return original_send(request, *args, **kwargs)
    attempt_ordinal = _begin_transport_attempt(context)
    if attempt_ordinal is None:
        return original_send(request, *args, **kwargs)
    started_at = time.monotonic()
    try:
        response = original_send(request, *args, **kwargs)
    except Exception as exc:
        _settle_transport_failure(
            context,
            request,
            attempt_ordinal,
            started_at,
            exc,
        )
        raise
    _settle_transport_response(
        context,
        request,
        attempt_ordinal,
        started_at,
        response,
    )
    return response


async def _record_async_transport_send(
    original_send: Any,
    request: httpx.Request,
    *args: Any,
    **kwargs: Any,
) -> httpx.Response:
    context = _ACTIVE_MODEL_TRANSPORT_CALL.get()
    if context is None:
        return await original_send(request, *args, **kwargs)
    attempt_ordinal = _begin_transport_attempt(context)
    if attempt_ordinal is None:
        return await original_send(request, *args, **kwargs)
    started_at = time.monotonic()
    try:
        response = await original_send(request, *args, **kwargs)
    except Exception as exc:
        _settle_transport_failure(
            context,
            request,
            attempt_ordinal,
            started_at,
            exc,
        )
        raise
    _settle_transport_response(
        context,
        request,
        attempt_ordinal,
        started_at,
        response,
    )
    return response


def _begin_transport_attempt(context: _ModelTransportCallContext) -> int | None:
    try:
        return context.ledger.begin_model_transport_attempt(context.logical_call_id)
    except Exception:
        return None


def _settle_transport_failure(
    context: _ModelTransportCallContext,
    request: httpx.Request,
    attempt_ordinal: int,
    started_at: float,
    exc: Exception,
) -> None:
    with suppress(Exception):
        context.ledger.record_model_transport_failure(
            _model_transport_attempt(
                context=context,
                request=request,
                attempt_ordinal=attempt_ordinal,
                elapsed_seconds=time.monotonic() - started_at,
                disposition="terminal_transport_failure",
                exception=exc,
            )
        )


def _settle_transport_response(
    context: _ModelTransportCallContext,
    request: httpx.Request,
    attempt_ordinal: int,
    started_at: float,
    response: httpx.Response,
) -> None:
    disposition = "status_failure" if response.status_code >= 400 else "success"
    with suppress(Exception):
        context.ledger.record_model_transport_response(
            _model_transport_attempt(
                context=context,
                request=request,
                attempt_ordinal=attempt_ordinal,
                elapsed_seconds=time.monotonic() - started_at,
                disposition=disposition,
                response=response,
            )
        )


def _instrument_http_client(client: httpx.Client | httpx.AsyncClient) -> None:
    """Wrap the existing client send boundary without replacing its pool."""

    if getattr(client, _OBSERVED_CLIENT_ATTRIBUTE, False):
        return
    if isinstance(client, httpx.AsyncClient):
        client.event_hooks["response"].append(_capture_async_response_metadata)
        original_send = client.send

        async def observed_async_send(
            request: httpx.Request,
            *args: Any,
            **kwargs: Any,
        ) -> httpx.Response:
            return await _record_async_transport_send(
                original_send,
                request,
                *args,
                **kwargs,
            )

        client.send = observed_async_send  # type: ignore[method-assign]
    else:
        client.event_hooks["response"].append(_capture_response_metadata)
        original_send = client.send

        def observed_sync_send(
            request: httpx.Request,
            *args: Any,
            **kwargs: Any,
        ) -> httpx.Response:
            return _record_sync_transport_send(
                original_send,
                request,
                *args,
                **kwargs,
            )

        client.send = observed_sync_send  # type: ignore[method-assign]
    setattr(client, _OBSERVED_CLIENT_ATTRIBUTE, True)


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and capability-aware binding.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling.

    ``with_structured_output`` consults the per-model capability table
    (``capabilities.get_capabilities``) to pick the method and to decide
    whether ``tool_choice`` may be sent. Models that reject ``tool_choice``
    (e.g. DeepSeek V4 and reasoner — per their official tool-calling
    guide) still bind the schema as a tool, but no ``tool_choice``
    parameter is sent.

    Provider-specific quirks beyond structured-output (e.g. DeepSeek's
    reasoning_content roundtrip) live in subclasses so this base class
    stays small.
    """

    _model_transport_provider_family: str = PrivateAttr(default="unavailable")
    _model_transport_configured_max_retries: int = PrivateAttr(
        default=DEFAULT_MAX_RETRIES
    )

    def _activate_model_transport_call(
        self,
    ) -> tuple[_ModelTransportCallContext, Token] | None:
        try:
            from tradingagents.dataflows.market_snapshot import (
                get_active_run_telemetry,
            )

            ledger = get_active_run_telemetry()
            if ledger is None:
                return None
            context = _ModelTransportCallContext(
                ledger=ledger,
                logical_call_id=ledger.begin_model_transport_call(),
                provider_family=_safe_transport_value(
                    self._model_transport_provider_family,
                    pattern=_SAFE_PROVIDER_RE,
                    maximum_chars=64,
                ),
                model_id=_safe_transport_value(
                    self.model_name,
                    pattern=_SAFE_MODEL_RE,
                    maximum_chars=200,
                ),
                configured_max_retries=self._model_transport_configured_max_retries,
            )
            return context, _ACTIVE_MODEL_TRANSPORT_CALL.set(context)
        except Exception:
            return None

    @staticmethod
    def _deactivate_model_transport_call(
        activation: tuple[_ModelTransportCallContext, Token] | None,
    ) -> None:
        if activation is None:
            return
        context, token = activation
        with suppress(Exception):
            context.ledger.finish_model_transport_call(context.logical_call_id)
        _ACTIVE_MODEL_TRANSPORT_CALL.reset(token)

    def _generate(self, *args: Any, **kwargs: Any):
        activation = self._activate_model_transport_call()
        try:
            return super()._generate(*args, **kwargs)
        finally:
            self._deactivate_model_transport_call(activation)

    async def _agenerate(self, *args: Any, **kwargs: Any):
        activation = self._activate_model_transport_call()
        try:
            return await super()._agenerate(*args, **kwargs)
        finally:
            self._deactivate_model_transport_call(activation)

    def _instrument_model_transport_clients(self) -> None:
        """Attach observation to the SDK-owned clients without replacing pools."""

        _instrument_http_client(self.root_client._client)
        _instrument_http_client(self.root_async_client._client)

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # When the model rejects tool_choice, suppress langchain's hardcoded
        # value. The schema is still bound as a tool — exactly what
        # DeepSeek's official tool-calling examples do.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


class LocalCompatibleChatOpenAI(NormalizedChatOpenAI):
    """OpenAI-compatible client for arbitrary local servers (LM Studio, vLLM,
    llama.cpp via the generic ``openai_compatible`` provider).

    Their tool-calling support varies, and many reject the object-form
    ``tool_choice`` langchain sends for function-calling structured output. Bind
    the schema as a tool but don't force tool_choice, so structured output works
    across local servers regardless of the model ID's capabilities (#1057).
    """

    def with_structured_output(self, schema, *, method=None, **kwargs):
        resolved = method or get_capabilities(self.model_name).preferred_structured_method
        if resolved == "function_calling":
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Thinking-mode round-trip is the only DeepSeek-specific behavior that
    stays here. When DeepSeek's thinking models return a response with
    ``reasoning_content``, that field must be echoed back as part of the
    assistant message on the next turn or the API fails with HTTP 400.
    ``_create_chat_result`` captures it on receive and
    ``_get_request_payload`` re-attaches it on send.

    Ordinary thinking-model tool binding keeps capability-driven ``tool_choice``
    suppression. Required Flash/V4 schema finalizers use JSON mode through
    ``with_required_structured_output`` below.
    """

    def get_required_structured_method(self) -> str:
        """Return the transport used by fail-closed required schemas."""
        if self.model_name in {
            "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
            "deepseek-v4-pro",
        }:
            return "json_mode"
        return get_capabilities(self.model_name).preferred_structured_method

    def with_required_structured_output(self, schema, **kwargs):
        """Bind a fail-closed schema finalizer using V4's supported JSON mode.

        Ordinary analyst data-tool selection continues to use ``bind_tools``
        and remains unchanged. V4 thinking mode rejects every ``tool_choice``
        form, including a named function, but does support JSON response mode.
        Reasoner and unverified future variants retain the capability table's
        existing function-calling behavior.
        """
        kwargs.setdefault("include_raw", True)
        if self.get_required_structured_method() == "json_mode":
            kwargs.pop("tool_choice", None)
            return self.with_structured_output(schema, method="json_mode", **kwargs)
        return self.with_structured_output(schema, **kwargs)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_), strict=False):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", []), strict=False
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax-specific overrides on top of the OpenAI-compatible client.

    M2.x reasoning models embed ``<think>...</think>`` blocks directly in
    ``message.content`` by default, which would pollute saved reports.
    Per platform.minimax.io/docs/api-reference/text-openai-api,
    ``reasoning_split=True`` redirects the thinking block into
    ``reasoning_details`` so ``content`` stays clean. It is sent via
    ``extra_body`` (not a top-level kwarg) because the openai SDK validates
    top-level params and rejects unknown ones like reasoning_split (#826).

    The flag is gated by ``ModelCapabilities.requires_reasoning_split`` so
    only M2.x reasoning models receive it; non-reasoning MiniMax endpoints
    (Coding Plan, MiniMax-Text-01) never see it.

    Tool-choice handling for M2.x — those models accept only the string
    enum ``{"none", "auto"}`` and reject langchain's function-spec dict —
    is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if get_capabilities(self.model_name).requires_reasoning_split:
            # Pass via extra_body, not as a top-level kwarg: the openai SDK
            # (>=1.56) validates top-level params against Completions.create
            # and rejects unknown ones like reasoning_split (#826). extra_body
            # is forwarded into the request body untouched.
            extra_body = payload.setdefault("extra_body", {})
            extra_body.setdefault("reasoning_split", True)
        return payload


# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "temperature",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# OpenAI's ``reasoning_effort`` is only accepted by reasoning models — the GPT-5
# family and the o-series. Non-reasoning models (gpt-4.1, gpt-4o, ...) 400 with
# "Unsupported parameter: 'reasoning.effort' is not supported with this model".
# Drop the kwarg for those rather than crash the run.
_OPENAI_REASONING_MODEL = re.compile(r"^(gpt-5|o[1-9])")


def _supports_reasoning_effort(model: str) -> bool:
    """Whether the (native OpenAI) model accepts ``reasoning_effort``."""
    return bool(_OPENAI_REASONING_MODEL.match(model.lower().strip()))


@dataclass(frozen=True)
class ProviderSpec:
    """Declarative config for one OpenAI-compatible provider.

    The OpenAI-compatible family (OpenAI, xAI, DeepSeek, Qwen, GLM, MiniMax,
    OpenRouter, Ollama, and any user endpoint) all speak the same Chat
    Completions API and differ only by these fields — so one row here replaces
    the former per-provider base-URL dict, auth handling, and client-class
    branches. Native Anthropic / Google use their own clients (genuinely
    different APIs) and are intentionally NOT in this registry.

    The API-key env var stays in ``api_key_env.PROVIDER_API_KEY_ENV`` (the single
    source consulted by both this client and the CLI prompt); only behavior that
    is provider-specific (base URL, key optionality, wire-format quirks via
    ``chat_class``) lives here.
    """

    chat_class: type = NormalizedChatOpenAI   # provider quirks live in the subclass
    base_url: str | None = None            # default endpoint (None -> SDK default)
    base_url_env: str | None = None        # env var that overrides base_url (e.g. OLLAMA_BASE_URL)
    key_optional: bool = False                # don't require/prompt; send a placeholder if unset
    placeholder_key: str = "EMPTY"            # sent when no key is available (keyless local servers)
    require_base_url: bool = False            # error if no base_url is resolved (generic endpoint)
    use_responses_api: bool = False           # native OpenAI Responses API


# Single source of truth for the OpenAI-compatible provider family. Dual-region
# providers (qwen/glm/minimax) keep separate endpoints because international and
# China accounts cannot share credentials (#758).
OPENAI_COMPATIBLE_PROVIDERS: dict[str, ProviderSpec] = {
    "openai":     ProviderSpec(use_responses_api=True),
    "xai":        ProviderSpec(base_url="https://api.x.ai/v1"),
    "deepseek":   ProviderSpec(base_url="https://api.deepseek.com", chat_class=DeepSeekChatOpenAI),
    "qwen":       ProviderSpec(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "qwen-cn":    ProviderSpec(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":        ProviderSpec(base_url="https://api.z.ai/api/paas/v4/"),
    "glm-cn":     ProviderSpec(base_url="https://open.bigmodel.cn/api/paas/v4/"),
    "minimax":    ProviderSpec(base_url="https://api.minimax.io/v1", chat_class=MinimaxChatOpenAI),
    "minimax-cn": ProviderSpec(base_url="https://api.minimaxi.com/v1", chat_class=MinimaxChatOpenAI),
    "openrouter": ProviderSpec(base_url="https://openrouter.ai/api/v1"),
    "mistral":    ProviderSpec(base_url="https://api.mistral.ai/v1"),
    "kimi":       ProviderSpec(base_url="https://api.moonshot.ai/v1"),
    "groq":       ProviderSpec(base_url="https://api.groq.com/openai/v1"),
    "nvidia":     ProviderSpec(base_url="https://integrate.api.nvidia.com/v1"),
    "ollama":     ProviderSpec(base_url="http://localhost:11434/v1", base_url_env="OLLAMA_BASE_URL",
                               key_optional=True, placeholder_key="ollama"),
    # Generic endpoint: user supplies base_url; key optional (keyless local).
    "openai_compatible": ProviderSpec(
        require_base_url=True, key_optional=True, chat_class=LocalCompatibleChatOpenAI
    ),
}


def is_openai_compatible(provider: str) -> bool:
    """Whether ``provider`` is served by the OpenAI-compatible registry."""
    return provider.lower() in OPENAI_COMPATIBLE_PROVIDERS


def _is_native_openai_base_url(base_url: str | None) -> bool:
    """True when ``base_url`` is unset or points at api.openai.com.

    The Responses API (/v1/responses) only exists on native OpenAI. A custom
    base_url on the ``openai`` provider (a proxy, gateway, or local server)
    speaks only Chat Completions, so the Responses API must stay off there even
    though the provider spec enables it (#1024).
    """
    if not base_url:
        return True
    if "://" not in base_url:
        base_url = "https://" + base_url
    host = urlparse(base_url).hostname or ""
    return host == "api.openai.com" or host.endswith(".openai.com")


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return a configured ChatOpenAI instance, driven by the provider registry."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(self.provider)
        chat_cls = NormalizedChatOpenAI

        if spec is not None:
            chat_cls = spec.chat_class

            # base_url precedence: explicit client base_url (carries the config /
            # TRADINGAGENTS_LLM_BACKEND_URL value) > provider env override (e.g.
            # OLLAMA_BASE_URL) > provider default. None means use the SDK default.
            env_base_url = os.environ.get(spec.base_url_env) if spec.base_url_env else None
            base_url = self.base_url or env_base_url or spec.base_url
            if spec.require_base_url and not base_url:
                raise ValueError(
                    f"Provider '{self.provider}' requires a base_url. Set it via "
                    "backend_url / TRADINGAGENTS_LLM_BACKEND_URL to your endpoint, "
                    "e.g. http://localhost:8000/v1 (vLLM) or http://localhost:1234/v1 "
                    "(LM Studio)."
                )
            if base_url:
                llm_kwargs["base_url"] = base_url

            # API key: required unless key_optional; keyless local servers get a
            # placeholder. The env-var name is the single source in api_key_env.
            api_key_env = get_api_key_env(self.provider)
            api_key = os.environ.get(api_key_env) if api_key_env else None
            if api_key:
                llm_kwargs["api_key"] = api_key
            elif spec.key_optional:
                llm_kwargs["api_key"] = spec.placeholder_key
            elif api_key_env:
                raise ValueError(
                    f"API key for provider '{self.provider}' is not set. "
                    f"Please set the {api_key_env} environment variable "
                    f"(e.g. add {api_key_env}=your_key to your .env file)."
                )

            # The Responses API only exists on native OpenAI; if the user points
            # the openai provider at a custom base_url (proxy/gateway/local), it
            # only speaks Chat Completions, so keep Responses off there (#1024).
            if spec.use_responses_api and _is_native_openai_base_url(base_url):
                llm_kwargs["use_responses_api"] = True
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "reasoning_effort" and not _supports_reasoning_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        # The subclass (provider quirks) comes from the registry spec.
        llm = chat_cls(**llm_kwargs)
        llm._instrument_model_transport_clients()
        llm._model_transport_provider_family = self.provider
        configured_max_retries = llm_kwargs.get("max_retries")
        llm._model_transport_configured_max_retries = (
            DEFAULT_MAX_RETRIES
            if configured_max_retries is None
            else int(configured_max_retries)
        )
        return llm

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
