import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


class StatsCallbackHandler(BaseCallbackHandler):
    """Callback handler that tracks LLM calls, tool calls, and token usage."""

    def __init__(
        self,
        *,
        metrics_recorder: Any | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._metrics_recorder = metrics_recorder
        self._clock = clock
        self._model_started: dict[UUID, tuple[float, str]] = {}
        self._tool_started: dict[UUID, tuple[float, str]] = {}
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when an LLM starts."""
        with self._lock:
            self.llm_calls += 1
        self._start_timing("model", serialized, kwargs.get("run_id"))

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when a chat model starts."""
        with self._lock:
            self.llm_calls += 1
        self._start_timing("model", serialized, kwargs.get("run_id"))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage from LLM response."""
        self._finish_timing("model", kwargs.get("run_id"))
        try:
            generation = response.generations[0][0]
        except (AttributeError, IndexError, TypeError):
            return

        usage_metadata = None
        if hasattr(generation, "message"):
            message = generation.message
            if isinstance(message, AIMessage) and hasattr(message, "usage_metadata"):
                usage_metadata = message.usage_metadata

        if usage_metadata:
            with self._lock:
                self.tokens_in += usage_metadata.get("input_tokens", 0)
                self.tokens_out += usage_metadata.get("output_tokens", 0)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Increment tool call counter when a tool starts."""
        with self._lock:
            self.tool_calls += 1
        self._start_timing("tool", serialized, kwargs.get("run_id"))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self._finish_timing("tool", kwargs.get("run_id"))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._finish_timing("model", kwargs.get("run_id"))

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self._finish_timing("tool", kwargs.get("run_id"))

    @staticmethod
    def _operation_name(serialized: dict[str, Any]) -> str:
        name = serialized.get("name")
        if name:
            return str(name)
        identifier = serialized.get("id")
        if isinstance(identifier, (list, tuple)) and identifier:
            return str(identifier[-1])
        if identifier:
            return str(identifier)
        return "unknown"

    def _start_timing(
        self,
        category: str,
        serialized: dict[str, Any],
        run_id: UUID | None,
    ) -> None:
        if self._metrics_recorder is None or run_id is None:
            return
        started = (self._clock(), self._operation_name(serialized))
        with self._lock:
            target = self._model_started if category == "model" else self._tool_started
            target[run_id] = started

    def _finish_timing(self, category: str, run_id: UUID | None) -> None:
        if self._metrics_recorder is None or run_id is None:
            return
        with self._lock:
            target = self._model_started if category == "model" else self._tool_started
            started = target.pop(run_id, None)
        if started is None:
            return
        started_at, name = started
        self._metrics_recorder.record_duration(
            category,
            name,
            max(0.0, self._clock() - started_at),
        )

    def get_stats(self) -> dict[str, Any]:
        """Return current statistics."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            }
