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


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def render_layout(
    layout,
    message_buffer,
    spinner_text=None,
    stats_handler=None,
    start_time=None,
):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingAgents CLI[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to TradingAgents",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team - filter to only include agents in agent_status
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Sentiment Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": [
            "Aggressive Analyst",
            "Neutral Analyst",
            "Conservative Analyst",
        ],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Filter teams to only include agents that are in agent_status
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp descending (newest first)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # Calculate how many messages we can show based on available space
    max_messages = 12

    # Get the first N messages (newest ones)
    recent_messages = all_messages[:max_messages]

    # Add messages to table (already in newest-first order)
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
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

    # Footer with statistics
    # Agent progress - derived from agent_status dict
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # Report progress - based on agent completion (not just content existence)
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # Build stats parts
    stats_parts = [f"Agents: {agents_completed}/{agents_total}"]

    # LLM and tool stats from callback handler
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM: {stats['llm_calls']}")
        stats_parts.append(f"Tools: {stats['tool_calls']}")

        # Token display with graceful fallback
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = (
                f"Tokens: {format_tokens(stats['tokens_in'])}↑ "
                f"{format_tokens(stats['tokens_out'])}↓"
            )
        else:
            tokens_str = "Tokens: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"Reports: {reports_completed}/{reports_total}")

    # Elapsed time
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"⏱ {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def format_tool_args(args, max_length=80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[: max_length - 3] + "..."
    return result


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
        self.console.print(
            f"{_timestamp()} [Analysis] {section_name} ready: {path}",
            soft_wrap=True,
        )
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
