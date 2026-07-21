"""Publish signals only from fully authorized terminal state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tradingagents.terminal_contract import authorized_trading_decision_from_state


class SignalProcessor:
    """Read the 5-tier rating from an audited terminal publication."""

    def __init__(self, quick_thinking_llm: Any = None):
        # The LLM argument is accepted for backwards compatibility but no
        # longer used: the PM's structured output guarantees the rating is
        # parseable from the rendered markdown without a second LLM call.
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, final_state: Mapping[str, Any]) -> str:
        """Return the rating only from an audited terminal publication."""

        return authorized_trading_decision_from_state(final_state).rating.value
