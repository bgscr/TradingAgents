from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from cli.run_display import (
    PlainRunDisplay,
    ResilientRunDisplay,
    RichRunDisplay,
    create_run_display,
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
def test_plain_refresh_emits_new_messages_and_tools_without_arguments_once():
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    buffer = FakeBuffer()
    display = PlainRunDisplay(console, buffer)

    buffer.messages.append(("12:00:00", "System", "Analysis initialized"))
    buffer.tool_calls.append(
        ("12:00:01", "get_stock_data", {"symbol": "SECRET", "date": "2026-07-10"})
    )
    display.refresh("retrieving data")
    first_output = stream.getvalue()
    display.refresh("retrieving data")

    assert "12:00:00 [System] Analysis initialized" in first_output
    assert "12:00:01 [Tool] Market Analyst requested get_stock_data" in first_output
    assert "SECRET" not in first_output
    assert "2026-07-10" not in first_output
    assert stream.getvalue() == first_output


@pytest.mark.unit
def test_plain_display_prints_buffered_and_published_progress_event_once():
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    buffer = FakeBuffer()
    display = PlainRunDisplay(console, buffer)
    event = ProgressEvent(
        "Research",
        "Bull Researcher updated investment debate",
        "investment_debate_state.current_response",
        "fingerprint",
    )

    buffer.messages.append(("12:00:00", event.message_type, event.content))
    display.refresh("working")
    assert stream.getvalue().count(event.content) == 1

    display.publish_event(event)
    display.refresh("working")

    assert stream.getvalue().count(event.content) == 1


@pytest.mark.unit
def test_plain_display_does_not_suppress_later_distinct_progress_event():
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    buffer = FakeBuffer()
    display = PlainRunDisplay(console, buffer)
    events = [
        ProgressEvent("Risk", "Risk debate updated", "risk.first", "fingerprint-1"),
        ProgressEvent("Risk", "Risk debate updated", "risk.second", "fingerprint-2"),
    ]

    for index, event in enumerate(events):
        buffer.messages.append((f"12:00:0{index}", event.message_type, event.content))
        display.publish_event(event)
        display.refresh("working")

    assert stream.getvalue().count("Risk debate updated") == 2


@pytest.mark.unit
def test_create_run_display_wraps_rich_for_terminal_console():
    console = Console(
        file=StringIO(),
        force_terminal=True,
        color_system=None,
    )

    display = create_run_display(console, FakeBuffer(), None, 0.0)

    assert isinstance(display, ResilientRunDisplay)
    assert isinstance(display._active, RichRunDisplay)
    assert isinstance(display._plain, PlainRunDisplay)


@pytest.mark.unit
def test_create_run_display_wraps_plain_for_non_terminal_console():
    console = Console(
        file=StringIO(),
        force_terminal=False,
        color_system=None,
    )

    display = create_run_display(console, FakeBuffer(), None, 0.0)

    assert isinstance(display, ResilientRunDisplay)
    assert isinstance(display._active, PlainRunDisplay)
    assert display._active is display._plain


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
