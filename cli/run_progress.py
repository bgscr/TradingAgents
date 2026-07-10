from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProgressEvent:
    message_type: str
    content: str
    source_key: str
    source_fingerprint: str


_ANALYST_REPORT_EVENTS = {
    "market_report": "Market Analyst produced market report",
    "sentiment_report": "Sentiment Analyst produced sentiment report",
    "news_report": "News Analyst produced news report",
    "fundamentals_report": "Fundamentals Analyst produced fundamentals report",
}

_RISK_SPEAKERS = {
    "Aggressive": ("Aggressive Analyst", "current_aggressive_response"),
    "Conservative": ("Conservative Analyst", "current_conservative_response"),
    "Neutral": ("Neutral Analyst", "current_neutral_response"),
}

_RATING_RE = re.compile(
    r"^\*\*Rating\*\*\s*:\s*(Buy|Overweight|Hold|Underweight|Sell)\b",
    re.IGNORECASE,
)


def stable_fingerprint(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(value)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def message_key(message: Any) -> str:
    message_id = getattr(message, "id", None)
    if message_id is not None:
        return f"id:{message_id}"

    payload = {
        "class": f"{type(message).__module__}.{type(message).__qualname__}",
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
    }
    return f"fingerprint:{stable_fingerprint(payload)}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _investment_speaker(text: str) -> str | None:
    if text.startswith("Bull Analyst:"):
        return "Bull Researcher"
    if text.startswith("Bear Analyst:"):
        return "Bear Researcher"
    return None


def _decision_content(value: Any) -> str:
    for line in _text(value).splitlines():
        match = _RATING_RE.match(line.strip())
        if match:
            return f"Final decision ready: {match.group(1).title()}"
    return "Final decision ready"


class StateProgressTracker:
    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def _add(
        self,
        events: list[ProgressEvent],
        message_type: str,
        content: str,
        source_key: str,
        source_value: Any,
    ) -> None:
        fingerprint = stable_fingerprint(source_value)
        event_key = (source_key, fingerprint)
        if event_key in self._seen:
            return
        self._seen.add(event_key)
        events.append(
            ProgressEvent(
                message_type=message_type,
                content=content,
                source_key=source_key,
                source_fingerprint=fingerprint,
            )
        )

    def events_for(self, chunk: Any) -> list[ProgressEvent]:
        if not isinstance(chunk, dict):
            return []

        events: list[ProgressEvent] = []

        for source_key, content in _ANALYST_REPORT_EVENTS.items():
            report = _text(chunk.get(source_key))
            if report:
                self._add(events, "Analysis", content, source_key, report)

        investment_plan = _text(chunk.get("investment_plan"))
        if investment_plan:
            self._add(
                events,
                "Research",
                "Research Manager produced investment plan",
                "investment_plan",
                investment_plan,
            )
        else:
            debate = chunk.get("investment_debate_state")
            if isinstance(debate, dict):
                response = _text(debate.get("current_response"))
                speaker = _investment_speaker(response)
                if speaker:
                    self._add(
                        events,
                        "Research",
                        f"{speaker} updated investment debate",
                        "investment_debate_state.current_response",
                        response,
                    )

        trader_plan = _text(chunk.get("trader_investment_plan"))
        if trader_plan:
            self._add(
                events,
                "Trading",
                "Trader produced transaction plan",
                "trader_investment_plan",
                trader_plan,
            )

        final_decision = _text(chunk.get("final_trade_decision"))
        if final_decision:
            self._add(
                events,
                "Portfolio",
                _decision_content(final_decision),
                "final_trade_decision",
                final_decision,
            )
        else:
            risk = chunk.get("risk_debate_state")
            if isinstance(risk, dict):
                latest = _text(risk.get("latest_speaker"))
                speaker = _RISK_SPEAKERS.get(latest)
                if speaker:
                    label, response_key = speaker
                    response = _text(risk.get(response_key))
                    if response:
                        self._add(
                            events,
                            "Risk",
                            f"{label} updated risk debate",
                            f"risk_debate_state.{response_key}",
                            response,
                        )

        return events
