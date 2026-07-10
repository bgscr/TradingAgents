# CLI Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CLI analysis progress visible throughout a run in both interactive PowerShell and captured output while keeping finalized reports authoritative.

**Architecture:** Keep LangGraph `stream_mode="values"`, derive typed progress events from changes in cumulative state, and feed the same events to the audit log and a dual-mode display boundary. Rich terminals retain the live layout; non-TTY hosts receive durable line-oriented progress, and rendering failures fall back to plain output without failing graph execution.

**Tech Stack:** Python 3.10+, LangGraph values streaming, Rich, Typer, pytest

## Global Constraints

- Work only in `D:\prj\TradingAgents\TradingAgents\.worktrees\agent\cli-observability-design` on branch `codex/cli-observability-design`.
- Prefix every shell command with `rtk`.
- Use the worktree's verified CodeGraph index before structural code searches.
- Keep `stream_mode="values"`; do not add token-by-token LLM streaming.
- Finalized state fields remain the only source for `current_report` and saved reports.
- Do not expose partial structured-output JSON, tool calls, or unfinished LLM text as report content.
- Rendering failures are fail-open; graph, run-status, and report-writing failures retain existing failure semantics.
- Do not modify prompts, recommendation schemas, report contents, vendor behavior, or market-data wording.
- Do not add a user-facing display flag or a new runtime dependency.
- Use test-driven development for each behavior change and commit after each task passes its focused tests.

## File Structure

- Create `cli/run_progress.py`: immutable progress events, cumulative-state change detection, stable fingerprints, final-decision summaries, and ID-less message keys.
- Create `cli/run_display.py`: layout rendering, Rich and plain display implementations, renderer selection, and Rich-to-plain fallback.
- Modify `cli/main.py`: import the new boundaries, make report updates idempotent, integrate progress/display into `run_analysis()`, and retain only the latest values state.
- Create `tests/test_cli_progress.py`: realistic cumulative-state and message-key unit tests.
- Create `tests/test_cli_run_display.py`: Rich, non-TTY, and fallback display tests.
- Create `tests/test_cli_observability.py`: run-level cumulative-stream regression tests.
- Modify `tests/test_china_a_run_logging.py`: remove the old synthetic message-free progress tests after equivalent realistic coverage exists in `tests/test_cli_progress.py`.

---

### Task 1: Cumulative-State Progress Tracking

**Files:**
- Create: `cli/run_progress.py`
- Create: `tests/test_cli_progress.py`
- Modify: `tests/test_china_a_run_logging.py:125-215`

**Interfaces:**
- Consumes: cumulative LangGraph values chunks shaped as `dict[str, Any]`.
- Produces: `ProgressEvent(message_type, content, source_key, source_fingerprint)`.
- Produces: `StateProgressTracker.events_for(chunk) -> list[ProgressEvent]`.
- Produces: `message_key(message) -> str`, using an ID first and a stable fingerprint otherwise.

- [ ] **Step 1: Write failing tests for realistic cumulative values chunks**

Create `tests/test_cli_progress.py`:

```python
from types import SimpleNamespace

import pytest

from cli.run_progress import StateProgressTracker, message_key


@pytest.mark.unit
def test_progress_tracker_reads_state_changes_when_messages_are_cumulative():
    tracker = StateProgressTracker()
    messages = [SimpleNamespace(id="msg-1", content="analyst output")]

    chunks = [
        {
            "messages": messages,
            "market_report": "Market report body",
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_debate_state": {
                "current_response": "Bull Analyst: upside case",
            },
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_debate_state": {
                "judge_decision": "**Recommendation**: Underweight",
            },
            "investment_plan": "**Recommendation**: Underweight",
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_plan": "**Recommendation**: Underweight",
            "trader_investment_plan": "**Action**: Sell",
            "risk_debate_state": {
                "latest_speaker": "Aggressive",
                "current_aggressive_response": "Aggressive Analyst: size up",
            },
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_plan": "**Recommendation**: Underweight",
            "trader_investment_plan": "**Action**: Sell",
            "risk_debate_state": {
                "judge_decision": "**Rating**: Underweight",
            },
            "final_trade_decision": "**Rating**: Underweight\n\nReduce exposure.",
        },
    ]

    events = [event for chunk in chunks for event in tracker.events_for(chunk)]

    assert [(event.message_type, event.content) for event in events] == [
        ("Analysis", "Market Analyst produced market report"),
        ("Research", "Bull Researcher updated investment debate"),
        ("Research", "Research Manager produced investment plan"),
        ("Trading", "Trader produced transaction plan"),
        ("Risk", "Aggressive Analyst updated risk debate"),
        ("Portfolio", "Final decision ready: Underweight"),
    ]


@pytest.mark.unit
def test_progress_tracker_deduplicates_repeated_full_state():
    tracker = StateProgressTracker()
    chunk = {
        "messages": [SimpleNamespace(id="msg-1", content="done")],
        "risk_debate_state": {
            "latest_speaker": "Neutral",
            "current_neutral_response": "Neutral Analyst: wait",
        },
    }

    assert len(tracker.events_for(chunk)) == 1
    assert tracker.events_for(chunk) == []

    updated = {
        **chunk,
        "risk_debate_state": {
            "latest_speaker": "Neutral",
            "current_neutral_response": "Neutral Analyst: reduce size",
        },
    }
    assert len(tracker.events_for(updated)) == 1


@pytest.mark.unit
def test_progress_tracker_ignores_malformed_or_empty_optional_state():
    tracker = StateProgressTracker()

    assert tracker.events_for(None) == []
    assert tracker.events_for({"investment_debate_state": None}) == []
    assert tracker.events_for({"risk_debate_state": "bad-state"}) == []
    assert tracker.events_for({"final_trade_decision": ""}) == []


@pytest.mark.unit
def test_message_key_prefers_id_and_fingerprints_idless_messages():
    with_id = SimpleNamespace(id="abc", content="same", tool_calls=[])
    first = SimpleNamespace(id=None, content="same", tool_calls=[])
    second = SimpleNamespace(id=None, content="same", tool_calls=[])
    changed = SimpleNamespace(id=None, content="changed", tool_calls=[])

    assert message_key(with_id) == "id:abc"
    assert message_key(first) == message_key(second)
    assert message_key(first).startswith("fingerprint:")
    assert message_key(first) != message_key(changed)
```

Delete the three old `_state_progress_events` tests from
`tests/test_china_a_run_logging.py:125-215`; their message-free chunks encode the
wrong runtime contract and are replaced by the tests above.

- [ ] **Step 2: Run the new tests and verify the module is missing**

Run:

```powershell
rtk pytest -q tests/test_cli_progress.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'cli.run_progress'`.

- [ ] **Step 3: Implement the progress event model and tracker**

Create `cli/run_progress.py`:

```python
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
```

- [ ] **Step 4: Run focused tests and verify progress behavior passes**

Run:

```powershell
rtk pytest -q tests/test_cli_progress.py tests/test_china_a_run_logging.py
```

Expected: all tests in both files pass.

- [ ] **Step 5: Commit cumulative-state progress tracking**

```powershell
rtk git add cli/run_progress.py tests/test_cli_progress.py tests/test_china_a_run_logging.py
rtk git commit -m "feat: track cumulative CLI progress"
```

---

### Task 2: Dual-Mode Run Display

**Files:**
- Create: `cli/run_display.py`
- Create: `tests/test_cli_run_display.py`
- Modify: `cli/main.py:1-530`
- Modify: `cli/main.py:1028-1033`
- Modify: `cli/main.py:1430-1610`

**Interfaces:**
- Consumes: `ProgressEvent` from Task 1.
- Consumes: the existing `MessageBuffer` shape, `StatsCallbackHandler`, `Console`, start time, and report directory.
- Produces: `RunDisplay` lifecycle methods `start()`, `refresh(spinner_text=None)`, `publish_event(event)`, `report_ready(section_name, content, path)`, and `close()`.
- Produces: `create_run_display(...) -> ResilientRunDisplay`.

- [ ] **Step 1: Write failing display tests**

Create `tests/test_cli_run_display.py`:

```python
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from cli.run_display import (
    PlainRunDisplay,
    ResilientRunDisplay,
    RichRunDisplay,
)
from cli.run_progress import ProgressEvent


class FakeBuffer:
    def __init__(self) -> None:
        self.agent_status = {"Market Analyst": "in_progress"}
        self.messages = []
        self.tool_calls = []
        self.current_report = None
        self.report_sections = {"market_report": None}

    def get_completed_reports_count(self) -> int:
        return 0


@pytest.mark.unit
def test_plain_display_emits_progress_and_report_before_close(tmp_path):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    buffer = FakeBuffer()
    display = PlainRunDisplay(console, buffer)

    display.start()
    display.refresh("retrieving and validating market data")
    display.publish_event(
        ProgressEvent("Research", "Bull Researcher updated investment debate", "x", "1")
    )
    display.report_ready(
        "market_report",
        "A finalized market report with actionable evidence.",
        tmp_path / "market_report.md",
    )

    output = stream.getvalue()
    assert "Market Analyst" in output
    assert "retrieving and validating market data" in output
    assert "Bull Researcher updated investment debate" in output
    assert "market_report ready" in output
    assert str(tmp_path / "market_report.md") in output


@pytest.mark.unit
def test_rich_display_replaces_waiting_copy_with_final_report():
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system=None,
        width=120,
        height=40,
    )
    buffer = FakeBuffer()
    stats = SimpleNamespace(
        get_stats=lambda: {
            "llm_calls": 0,
            "tool_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
    )
    display = RichRunDisplay(console, buffer, stats, start_time=0.0)

    display.start()
    display.refresh("retrieving and validating market data")
    buffer.current_report = "### Market Analysis\nREPORT BODY"
    display.refresh()
    display.close()

    output = stream.getvalue()
    assert "The report will appear when this analyst's tool/LLM round completes." in output
    assert "REPORT BODY" in output


@pytest.mark.unit
def test_resilient_display_switches_to_plain_when_rich_refresh_fails():
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    buffer = FakeBuffer()

    class BrokenDisplay:
        def start(self):
            return None

        def refresh(self, spinner_text=None):
            raise RuntimeError("render failed")

        def publish_event(self, event):
            raise RuntimeError("render failed")

        def report_ready(self, section_name, content, path):
            raise RuntimeError("render failed")

        def close(self):
            return None

    plain = PlainRunDisplay(console, buffer)
    display = ResilientRunDisplay(console, BrokenDisplay(), plain)

    display.start()
    display.refresh("working")
    display.publish_event(ProgressEvent("Risk", "risk updated", "risk", "1"))

    output = stream.getvalue()
    assert "Rich display failed: RuntimeError: render failed" in output
    assert "risk updated" in output
```

- [ ] **Step 2: Run display tests and verify the module is missing**

Run:

```powershell
rtk pytest -q tests/test_cli_run_display.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'cli.run_display'`.

- [ ] **Step 3: Extract layout rendering and implement display lifecycle**

Create `cli/run_display.py` and move the existing implementations at
`cli/main.py:303-530` into it:

Start the file with these imports:

```python
from __future__ import annotations

import datetime
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from cli.run_progress import ProgressEvent
```

- Rename `update_display()` to `render_layout()`.
- Add `message_buffer` as the second positional parameter.
- Replace every access to the global `message_buffer` with that parameter.
- Keep the progress table, recent-message table, finalized-report Markdown, and
  statistics footer behavior unchanged.
- Keep `create_layout()`, `format_tokens()`, and `format_tool_args()` as module
  helpers.
- In the no-report branch, replace the static waiting panel with the active
  agent and the explanatory copy tested above. Derive the active agent from the
  first `agent_status` entry whose value is `in_progress`.

Use this exact no-report branch in `render_layout()`:

```python
else:
    active = _active_agent(message_buffer)
    activity = spinner_text or "working"
    layout["analysis"].update(
        Panel(
            Group(
                Spinner("dots", text=f"{active} - {activity}"),
                Text(
                    "The report will appear when this analyst's tool/LLM "
                    "round completes.",
                    style="dim",
                ),
            ),
            title="Current Report",
            border_style="green",
            padding=(1, 2),
        )
    )
```

Add these lifecycle classes below the relocated rendering helpers:

```python
class RunDisplay(Protocol):
    def start(self) -> None: ...
    def refresh(self, spinner_text: str | None = None) -> None: ...
    def publish_event(self, event: ProgressEvent) -> None: ...
    def report_ready(
        self,
        section_name: str,
        content: str,
        path: Path,
    ) -> None: ...
    def close(self) -> None: ...


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _active_agent(message_buffer: Any) -> str:
    for agent, status in message_buffer.agent_status.items():
        if status == "in_progress":
            return agent
    return "Analysis pipeline"


class RichRunDisplay:
    def __init__(self, console, message_buffer, stats_handler, start_time) -> None:
        self.console = console
        self.message_buffer = message_buffer
        self.stats_handler = stats_handler
        self.start_time = start_time
        self.layout = create_layout()
        self._live: Live | None = None

    def start(self) -> None:
        render_layout(
            self.layout,
            self.message_buffer,
            stats_handler=self.stats_handler,
            start_time=self.start_time,
        )
        self._live = Live(
            self.layout,
            console=self.console,
            refresh_per_second=4,
        )
        self._live.start()

    def refresh(self, spinner_text: str | None = None) -> None:
        render_layout(
            self.layout,
            self.message_buffer,
            spinner_text=spinner_text,
            stats_handler=self.stats_handler,
            start_time=self.start_time,
        )
        if self._live is not None:
            self._live.refresh()

    def publish_event(self, event: ProgressEvent) -> None:
        self.refresh()

    def report_ready(self, section_name: str, content: str, path: Path) -> None:
        self.refresh()

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None


class PlainRunDisplay:
    def __init__(self, console: Console, message_buffer: Any) -> None:
        self.console = console
        self.message_buffer = message_buffer
        self._last_status: tuple[str, str] | None = None

    def start(self) -> None:
        return None

    def refresh(self, spinner_text: str | None = None) -> None:
        active = _active_agent(self.message_buffer)
        activity = spinner_text or "working"
        status = (active, activity)
        if status == self._last_status:
            return
        self._last_status = status
        self.console.print(f"{_timestamp()} [Progress] {active} - {activity}")

    def publish_event(self, event: ProgressEvent) -> None:
        self.console.print(f"{_timestamp()} [{event.message_type}] {event.content}")

    def report_ready(self, section_name: str, content: str, path: Path) -> None:
        normalized = " ".join(str(content).split())
        preview = normalized[:240]
        if len(normalized) > 240:
            preview += "..."
        self.console.print(f"{_timestamp()} [Analysis] {section_name} ready: {path}")
        if preview:
            self.console.print(f"  {preview}")

    def close(self) -> None:
        return None


class ResilientRunDisplay:
    def __init__(
        self,
        console: Console,
        primary: RunDisplay,
        plain: PlainRunDisplay,
    ) -> None:
        self.console = console
        self._active: RunDisplay = primary
        self._plain = plain
        self._using_plain = primary is plain
        self._reported_failure = False

    def _call(self, method_name: str, *args) -> None:
        try:
            getattr(self._active, method_name)(*args)
            return
        except Exception as exc:
            if self._using_plain:
                return
            with suppress(Exception):
                self._active.close()
            self._active = self._plain
            self._using_plain = True
            if not self._reported_failure:
                self._reported_failure = True
                with suppress(Exception):
                    self.console.print(
                        f"{_timestamp()} [Display] Rich display failed: "
                        f"{type(exc).__name__}: {exc}; switching to plain output"
                    )
            with suppress(Exception):
                if method_name != "start":
                    self._active.start()
                getattr(self._active, method_name)(*args)

    def start(self) -> None:
        self._call("start")

    def refresh(self, spinner_text: str | None = None) -> None:
        self._call("refresh", spinner_text)

    def publish_event(self, event: ProgressEvent) -> None:
        self._call("publish_event", event)

    def report_ready(self, section_name: str, content: str, path: Path) -> None:
        self._call("report_ready", section_name, content, path)

    def close(self) -> None:
        self._call("close")


def create_run_display(
    console: Console,
    message_buffer: Any,
    stats_handler: Any,
    start_time: float,
) -> ResilientRunDisplay:
    plain = PlainRunDisplay(console, message_buffer)
    primary: RunDisplay
    if console.is_terminal:
        primary = RichRunDisplay(console, message_buffer, stats_handler, start_time)
    else:
        primary = plain
    return ResilientRunDisplay(console, primary, plain)
```

The protocol methods use `...` only as Python's required Protocol stub body;
they are executable type declarations, not missing implementation work.

- [ ] **Step 4: Switch the current CLI to the relocated rendering helpers**

In `cli/main.py`:

- Remove the Rich imports used only by the relocated rendering body: `box`,
  `Layout`, `Spinner`, `Table`, and `Text`.
- Keep `Align`, `Console`, `Live`, `Markdown`, `Panel`, and `Rule`; the current
  run loop and non-layout CLI screens still use them at this task boundary.
- Delete `create_layout()`, `format_tokens()`, `update_display()`, and
  `format_tool_args()` from `cli/main.py`.
- Add this temporary import, which Task 3 will replace with
  `create_run_display`:

```python
from cli.run_display import create_layout, render_layout
```

Replace each current `update_display(...)` call with the corresponding injected
buffer call:

```python
render_layout(
    layout,
    message_buffer,
    stats_handler=stats_handler,
    start_time=start_time,
)
```

For the call that currently passes `spinner_text`, use:

```python
render_layout(
    layout,
    message_buffer,
    spinner_text=spinner_text,
    stats_handler=stats_handler,
    start_time=start_time,
)
```

Do not change the `Live` lifecycle or graph loop in this task. This commit only
relocates one implementation, so no display logic is duplicated between files.

- [ ] **Step 5: Run display tests and current CLI regression tests**

Run:

```powershell
rtk pytest -q tests/test_cli_run_display.py tests/test_cli_progress.py tests/test_cli_run_status.py tests/test_china_a_run_logging.py
```

Expected: all tests pass, including Rich-to-plain fallback and the existing
`run_analysis()` failure paths using the relocated renderer.

- [ ] **Step 6: Commit the display boundary and mechanical extraction**

```powershell
rtk git add cli/run_display.py cli/main.py tests/test_cli_run_display.py
rtk git commit -m "feat: add dual-mode CLI run display"
```

---

### Task 3: Integrate Progress, Display, and Idempotent Reports

**Files:**
- Modify: `cli/main.py:1-530`
- Modify: `cli/main.py:1342-1616`
- Create: `tests/test_cli_observability.py`
- Modify: `tests/test_cli_run_status.py:108-176`

**Interfaces:**
- Consumes: `StateProgressTracker`, `message_key`, and `ProgressEvent` from Task 1.
- Consumes: `create_run_display()` and `ResilientRunDisplay` from Task 2.
- Produces: unchanged public `run_analysis(checkpoint=None)` behavior and report artifacts.
- Changes internal `MessageBuffer.update_report_section(section_name, content) -> bool` to report whether content changed.

- [ ] **Step 1: Write failing MessageBuffer and run-level regression tests**

Create `tests/test_cli_observability.py`:

```python
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cli import main as cli_main


@pytest.mark.unit
def test_message_buffer_reports_whether_section_content_changed():
    buffer = cli_main.MessageBuffer()
    buffer.init_for_analysis(["market"])

    assert buffer.update_report_section("market_report", "first") is True
    assert buffer.update_report_section("market_report", "first") is False
    assert buffer.update_report_section("market_report", "second") is True


def _run_selections():
    return {
        "ticker": "601658.SS",
        "analysis_date": "2026-07-09",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "gpt-5-mini",
        "deep_thinker": "gpt-5",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }


@pytest.mark.unit
def test_run_analysis_publishes_reports_and_state_progress_before_stream_end(
    tmp_path,
    monkeypatch,
):
    market_message = AIMessage(content="Market report body", id=None)
    market_state = {
        "messages": [market_message],
        "market_report": "Market report body",
    }
    research_state = {
        **market_state,
        "investment_debate_state": {
            "current_response": "Bull Analyst: upside case",
            "bull_history": "Bull Analyst: upside case",
            "bear_history": "",
            "judge_decision": "",
        },
    }
    final_state = {
        **research_state,
        "investment_plan": "**Recommendation**: Underweight",
        "trader_investment_plan": "**Action**: Sell",
        "risk_debate_state": {
            "latest_speaker": "Judge",
            "current_aggressive_response": "Aggressive Analyst: reduce risk",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "aggressive_history": "Aggressive Analyst: reduce risk",
            "conservative_history": "",
            "neutral_history": "",
            "history": "Aggressive Analyst: reduce risk",
            "judge_decision": "**Rating**: Underweight",
            "count": 1,
        },
        "final_trade_decision": "**Rating**: Underweight\n\nReduce exposure.",
    }

    class FakeStream:
        def __init__(self):
            self.finished = False

        def stream(self, *args, **kwargs):
            yield market_state
            yield market_state
            yield research_state
            yield final_state
            self.finished = True

    fake_stream = FakeStream()

    class FakePropagator:
        def create_initial_state(self, *args, **kwargs):
            return {}

        def get_graph_args(self, *args, **kwargs):
            return {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = fake_stream

        def resolve_instrument_context(self, *args, **kwargs):
            return "resolved identity"

    class RecordingDisplay:
        def __init__(self):
            self.events = []
            self.report_calls = []

        def start(self):
            return None

        def refresh(self, spinner_text=None):
            return None

        def publish_event(self, event):
            self.events.append((event.message_type, event.content))

        def report_ready(self, section_name, content, path):
            self.report_calls.append(
                (section_name, content, path, fake_stream.finished)
            )

        def close(self):
            return None

    display = RecordingDisplay()
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    monkeypatch.setattr(cli_main, "create_run_display", lambda *args, **kwargs: display)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *args, **kwargs: "N")

    cli_main.run_analysis()

    market_calls = [call for call in display.report_calls if call[0] == "market_report"]
    assert len(market_calls) == 1
    assert market_calls[0][3] is False
    assert ("Research", "Bull Researcher updated investment debate") in display.events
    assert ("Portfolio", "Final decision ready: Underweight") in display.events

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    assert log_text.count("Market report body") == 1
    assert "[Research] Bull Researcher updated investment debate" in log_text
    assert "[Portfolio] Final decision ready: Underweight" in log_text
    assert (run_dir / "reports" / "market_report.md").read_text(
        encoding="utf-8"
    ) == "Market report body"
```

Extend the existing graph-stream interruption test in
`tests/test_cli_run_status.py` by monkeypatching `create_run_display` to a no-op
recording display. This keeps the test independent of terminal detection and
asserts `close()` is called when streaming raises. Leave the graph-initialization
failure test unchanged because display construction happens only after the graph
is initialized.

- [ ] **Step 2: Run the regression tests and verify current behavior fails**

Run:

```powershell
rtk pytest -q tests/test_cli_observability.py tests/test_cli_run_status.py
```

Expected failures:

- `MessageBuffer.update_report_section()` returns `None` instead of `True` or `False`.
- `cli.main` has no `create_run_display` integration.
- cumulative state still suppresses Research and Portfolio progress.

- [ ] **Step 3: Make report updates idempotent**

Change `MessageBuffer.update_report_section()` in `cli/main.py` to:

```python
def update_report_section(self, section_name, content) -> bool:
    if section_name not in self.report_sections:
        return False
    if self.report_sections[section_name] == content:
        return False
    self.report_sections[section_name] = content
    self._update_current_report()
    return True
```

Change the nested report decorator in `run_analysis()` so unchanged content is
not written or announced:

```python
def save_report_section_decorator(obj, func_name):
    func = getattr(obj, func_name)

    @wraps(func)
    def wrapper(section_name, content):
        changed = func(section_name, content)
        if not changed:
            return False

        stored = obj.report_sections[section_name]
        if stored:
            file_name = f"{section_name}.md"
            text = (
                "\n".join(str(item) for item in stored)
                if isinstance(stored, list)
                else stored
            )
            path = report_dir / file_name
            with open(path, "w", encoding="utf-8") as report_file:
                report_file.write(text)
            display.report_ready(section_name, text, path)
        return True

    return wrapper
```

The closure may reference `display` before its assignment because it is invoked
only after `display` is constructed; keep construction before the first report
update.

- [ ] **Step 4: Replace global display helpers and progress helpers with imports**

In `cli/main.py`:

- Remove `hashlib` and `Live`. Task 2 already removed the Rich imports and
  helper bodies used only by layout rendering.
- Keep `Align`; the welcome screen still uses it outside the live layout.
- Keep `Console`, `Markdown`, `Panel`, and `Rule` because post-run and complete
  report output still use them.
- Delete `_nonempty_text()`, `_state_event_fingerprint()`, `_add_state_event()`,
  `_investment_debate_speaker()`, `_risk_debate_speaker()`,
  `_state_progress_events()`, and `_emit_state_progress_events()`.
- Replace Task 2's temporary `create_layout, render_layout` import with:

```python
from cli.run_display import create_run_display
from cli.run_progress import StateProgressTracker, message_key
```

- [ ] **Step 5: Integrate the display lifecycle and cumulative-state tracker**

Replace the manual `Layout` and `Live` setup in `run_analysis()` with this
control flow while retaining the existing analyst/research/trader/risk status
blocks:

```python
start_time = time.time()
display = create_run_display(
    console,
    message_buffer,
    stats_handler,
    start_time,
)

message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
message_buffer.update_report_section = save_report_section_decorator(
    message_buffer,
    "update_report_section",
)

spinner_text = f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
display.start()
try:
    message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
    if selections["asset_type"] != "stock":
        message_buffer.add_message(
            "System",
            f"Detected asset type: {selections['asset_type']}",
        )
    message_buffer.add_message(
        "System",
        f"Analysis date: {selections['analysis_date']}",
    )
    message_buffer.add_message(
        "System",
        "Selected analysts: "
        + ", ".join(analyst.value for analyst in selections["analysts"]),
    )

    first_analyst = get_initial_analyst_node(analyst_execution_plan)
    message_buffer.update_agent_status(first_analyst, "in_progress")
    analyst_wall_time_tracker.mark_started(selected_analyst_keys[0])
    display.refresh(spinner_text)

    instrument_context = graph.resolve_instrument_context(
        selections["ticker"],
        selections["asset_type"],
    )
    init_agent_state = graph.propagator.create_initial_state(
        selections["ticker"],
        selections["analysis_date"],
        asset_type=selections["asset_type"],
        instrument_context=instrument_context,
    )
    args = graph.propagator.get_graph_args(callbacks=[stats_handler])

    current_phase = "graph_stream"
    _update_run_status(artifacts, current_phase=current_phase)
    tracker = StateProgressTracker()
    latest_state = {}

    for chunk in graph.graph.stream(init_agent_state, **args):
        for message in chunk.get("messages", []):
            key = message_key(message)
            if key in message_buffer._processed_message_ids:
                continue
            message_buffer._processed_message_ids.add(key)

            msg_type, content = classify_message_type(message)
            if content and content.strip():
                message_buffer.add_message(msg_type, content)

            if hasattr(message, "tool_calls") and message.tool_calls:
                for tool_call in message.tool_calls:
                    if isinstance(tool_call, dict):
                        message_buffer.add_tool_call(
                            tool_call["name"],
                            tool_call["args"],
                        )
                    else:
                        message_buffer.add_tool_call(
                            tool_call.name,
                            tool_call.args,
                        )

        for event in tracker.events_for(chunk):
            message_buffer.add_message(event.message_type, event.content)
            display.publish_event(event)

        update_analyst_statuses(
            message_buffer,
            chunk,
            wall_time_tracker=analyst_wall_time_tracker,
        )

        if chunk.get("investment_debate_state"):
            debate_state = chunk["investment_debate_state"]
            bull_hist = debate_state.get("bull_history", "").strip()
            bear_hist = debate_state.get("bear_history", "").strip()
            judge = debate_state.get("judge_decision", "").strip()

            if bull_hist or bear_hist:
                update_research_team_status("in_progress")
            if bull_hist:
                message_buffer.update_report_section(
                    "investment_plan",
                    f"### Bull Researcher Analysis\n{bull_hist}",
                )
            if bear_hist:
                message_buffer.update_report_section(
                    "investment_plan",
                    f"### Bear Researcher Analysis\n{bear_hist}",
                )
            if judge:
                message_buffer.update_report_section(
                    "investment_plan",
                    f"### Research Manager Decision\n{judge}",
                )
                update_research_team_status("completed")
                message_buffer.update_agent_status("Trader", "in_progress")

        if chunk.get("trader_investment_plan"):
            message_buffer.update_report_section(
                "trader_investment_plan",
                chunk["trader_investment_plan"],
            )
            if message_buffer.agent_status.get("Trader") != "completed":
                message_buffer.update_agent_status("Trader", "completed")
                message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

        if chunk.get("risk_debate_state"):
            risk_state = chunk["risk_debate_state"]
            agg_hist = risk_state.get("aggressive_history", "").strip()
            con_hist = risk_state.get("conservative_history", "").strip()
            neu_hist = risk_state.get("neutral_history", "").strip()
            judge = risk_state.get("judge_decision", "").strip()

            if agg_hist:
                if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                message_buffer.update_report_section(
                    "final_trade_decision",
                    f"### Aggressive Analyst Analysis\n{agg_hist}",
                )
            if con_hist:
                if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                    message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                message_buffer.update_report_section(
                    "final_trade_decision",
                    f"### Conservative Analyst Analysis\n{con_hist}",
                )
            if neu_hist:
                if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                    message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                message_buffer.update_report_section(
                    "final_trade_decision",
                    f"### Neutral Analyst Analysis\n{neu_hist}",
                )
            if judge and message_buffer.agent_status.get("Portfolio Manager") != "completed":
                message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                message_buffer.update_report_section(
                    "final_trade_decision",
                    f"### Portfolio Manager Decision\n{judge}",
                )
                message_buffer.update_agent_status("Aggressive Analyst", "completed")
                message_buffer.update_agent_status("Conservative Analyst", "completed")
                message_buffer.update_agent_status("Neutral Analyst", "completed")
                message_buffer.update_agent_status("Portfolio Manager", "completed")

        latest_state = chunk
        display.refresh(spinner_text)

    final_state = latest_state

    for agent in message_buffer.agent_status:
        message_buffer.update_agent_status(agent, "completed")
    message_buffer.add_message(
        "System",
        f"Completed analysis for {selections['analysis_date']}",
    )
    message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

    for section in message_buffer.report_sections:
        if section in final_state:
            message_buffer.update_report_section(section, final_state[section])

    display.refresh()
    current_phase = "report_writing"
    _update_run_status(artifacts, current_phase=current_phase)
    _write_run_reports(final_state, selections["ticker"], artifacts)
except BaseException as exc:
    _mark_run_failed(artifacts, exc, current_phase=current_phase)
    raise
finally:
    display.close()
```

- [ ] **Step 6: Keep failure tests isolated from terminal behavior**

Add this reusable no-op display to `tests/test_cli_run_status.py` and monkeypatch
`cli_main.create_run_display` in
`test_run_analysis_marks_failed_when_graph_stream_is_interrupted`:

```python
class NoOpDisplay:
    def __init__(self):
        self.closed = False

    def start(self):
        return None

    def refresh(self, spinner_text=None):
        return None

    def publish_event(self, event):
        return None

    def report_ready(self, section_name, content, path):
        return None

    def close(self):
        self.closed = True
```

In the graph-stream interruption test:

```python
display = NoOpDisplay()
monkeypatch.setattr(cli_main, "create_run_display", lambda *args, **kwargs: display)
```

After the existing exception and `run_status.json` assertions, add:

```python
assert display.closed is True
```

- [ ] **Step 7: Run focused integration tests**

Run:

```powershell
rtk pytest -q tests/test_cli_observability.py tests/test_cli_run_status.py tests/test_cli_progress.py tests/test_cli_run_display.py tests/test_china_a_run_logging.py
```

Expected: all focused tests pass. The market report is announced once despite a
repeated full-state chunk, and failure tests confirm display cleanup.

- [ ] **Step 8: Run the complete suite**

Run:

```powershell
rtk pytest -q
```

Expected: `684` existing baseline tests plus the newly added tests pass with zero
failures.

- [ ] **Step 9: Run the two-stage review gate**

Spec compliance review:

- Confirm TTY and non-TTY renderers are selected from `console.is_terminal`.
- Confirm every `ProgressEvent` reaches both `message_buffer.add_message()` and
  the display.
- Confirm finalized reports remain the only `current_report` input.
- Confirm report, graph, and status failures still use the existing failure path.
- Confirm report-reliability and market-data behavior are untouched.

Code quality review:

- Confirm `cli/run_progress.py` has no Rich or Typer imports.
- Confirm `cli/run_display.py` does not mutate graph state or write report files.
- Confirm `cli/main.py` owns orchestration and artifact writes.
- Confirm tests cover success, malformed state, deduplication, fallback, and
  interrupted streams.
- Confirm no unrelated formatting or refactoring appears in the diff.

Do not proceed to commit if the spec compliance review finds a mismatch.

- [ ] **Step 10: Commit run-loop integration**

```powershell
rtk git add cli/main.py tests/test_cli_observability.py tests/test_cli_run_status.py
rtk git commit -m "feat: surface CLI analysis progress"
```

---

### Task 4: Automated Gate and User-Assisted PowerShell Smoke Test

**Files:**
- Verify only; no planned file changes.

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: automated non-TTY evidence plus user-observed interactive PowerShell
  evidence and an agent audit of the resulting run artifacts.

- [ ] **Step 1: Verify repository and CodeGraph state**

Run:

```powershell
rtk git status --short --branch
rtk codegraph status
```

Expected: branch `codex/cli-observability-design`, no uncommitted changes, and an
up-to-date CodeGraph index.

- [ ] **Step 2: Run the full suite from a clean committed tree**

Run:

```powershell
rtk pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Hand the interactive smoke test to the user and stop**

Send the user these exact instructions, then end the turn without continuing to
final review or branch completion:

From Windows Terminal PowerShell, run:

```powershell
.\start_tradingagents.ps1
```

Verify before allowing the run to finish:

- the active analyst appears immediately
- the waiting copy explains the current tool/LLM round
- the first finalized report replaces the waiting copy
- Research, Risk, and Portfolio progress appears during later phases

After the run completes, ask the user to report:

- ticker, analysis date, and run ID if visible
- whether each checkpoint above passed
- any stale, duplicated, missing, or malformed display content
- any screenshot or copied console excerpt that helps reproduce a mismatch

Do not perform additional implementation work until the user explicitly says
the smoke test is finished.

- [ ] **Step 4: Resume from user feedback and audit the generated artifacts**

After the user reports completion, incorporate their observations and locate the
newest run status:

```powershell
rtk proxy powershell -NoProfile -Command { Get-ChildItem -LiteralPath (Join-Path $HOME '.tradingagents\logs') -Recurse -Filter run_status.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1 FullName,LastWriteTime }
```

Use the returned run directory to inspect completion status and progress events:

```powershell
rtk rg -n '"(status|current_phase|error_summary|started_at|completed_at)"' '<RUN_DIR>\run_status.json'
rtk rg -n "\[(Analysis|Research|Trading|Risk|Portfolio)\]" '<RUN_DIR>\message_tool.log'
rtk proxy powershell -NoProfile -Command { Get-ChildItem -LiteralPath '<RUN_DIR>\reports' -Recurse -File | Sort-Object CreationTime | Select-Object FullName,CreationTime,LastWriteTime }
```

Replace `<RUN_DIR>` with the absolute directory returned by the first command.
Verify that report creation and progress events occurred before `completed_at`,
that the final portfolio event is present, and that the user's observations
match the persisted evidence.

If the smoke test reveals a defect, dispatch one fix subagent with the complete
feedback and log evidence, re-run the focused and full automated tests, review
the fix, and request another user smoke test only when the interactive behavior
changed materially.

- [ ] **Step 5: Confirm final diff scope after smoke verification**

Run:

```powershell
rtk git diff main...HEAD --stat
rtk git status --short
```

Expected changed implementation files:

- `cli/run_progress.py`
- `cli/run_display.py`
- `cli/main.py`
- `tests/test_cli_progress.py`
- `tests/test_cli_run_display.py`
- `tests/test_cli_observability.py`
- `tests/test_cli_run_status.py`
- `tests/test_china_a_run_logging.py`
- the approved design and implementation-plan documents

Expected status: clean.
