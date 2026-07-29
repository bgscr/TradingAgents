import datetime
import json
import os
import re
import time
from collections import deque
from contextlib import ExitStack, nullcontext, suppress
from functools import wraps
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import typer
from rich.align import Align
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from cli.announcements import display_announcements, fetch_announcements
from cli.console_encoding import configure_utf8_stdio
from cli.run_display import create_run_display
from cli.run_progress import StateProgressTracker, message_key
from cli.runtime_artifacts import (
    ActiveRuntimeArtifactRunError,
    RuntimeArtifactWriter,
    collect_runtime_artifacts,
)
from cli.stats_handler import StatsCallbackHandler
from cli.utils import (
    ask_anthropic_effort,
    ask_gemini_thinking_config,
    ask_glm_region,
    ask_minimax_region,
    ask_openai_reasoning_effort,
    ask_output_language,
    ask_qwen_region,
    confirm_ollama_endpoint,
    detect_asset_type,
    ensure_api_key,
    get_ticker,
    prompt_openai_compatible_url,
    resolve_backend_url,
    select_analysts,
    select_deep_thinking_agent,
    select_llm_provider,
    select_research_depth,
    select_shallow_thinking_agent,
)
from tradingagents.asset_configuration import (
    RunAssetConfigurationError,
    resolve_run_asset_configuration,
)
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import asset_configuration_failure_evidence
from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    get_initial_analyst_node,
)
from tradingagents.graph.evidence_gate import create_preflight_gate_node
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import CheckpointSession, TradingAgentsGraph
from tradingagents.recorded_replay import (
    DEFAULT_RECORDED_FIXTURE_MANIFEST,
    load_recorded_run_fixtures,
    replay_recorded_run,
)
from tradingagents.reporting import write_report_tree
from tradingagents.strategy_registry import create_production_decision_policy
from tradingagents.terminal_contract import configuration_digest

configure_utf8_stdio()
console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
    invoke_without_command=True,
    no_args_is_help=False,
)


CHINA_A_ENHANCEMENT_PRESETS = {
    "basic": "Basic - current behavior, no extra China A-share enhancement",
    "flow_sentiment": "Flow and sentiment - fund flow, Dragon-Tiger, margin, heat",
    "announcements": "Announcements - disclosures, dividends, buybacks, major events",
    "industry_policy": "Industry and policy - sector, concept, policy context",
    "all": "All enhancements - flow, announcements, industry, and policy context",
}

CHINA_A_ENHANCEMENT_ALIASES = {
    "1": "basic",
    "2": "flow_sentiment",
    "3": "announcements",
    "4": "industry_policy",
    "5": "all",
}

CHINA_A_B_SHARE_PREFIXES = ("900", "200")

_ERROR_DIAGNOSTICS_CONTRACT_VERSION = "1.0"
_MAX_ERROR_CHAIN_ENTRIES = 8
_MAX_ERROR_MESSAGE_CHARS = 500
_ERROR_URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+",
    re.IGNORECASE,
)
_ERROR_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTHORIZATION|AUTH|"
    r"COOKIE|CREDENTIALS?|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)
_ERROR_SECRET_HEADER_RE = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie)\s*([:=])\s*[^\r\n]+",
    re.IGNORECASE,
)
_ERROR_BEARER_TOKEN_RE = re.compile(
    r"\b(bearer|basic)\s+[a-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_ERROR_SECRET_FIELD_RE = re.compile(
    r"(?<![\w-])"
    r"(?P<prefix>(?P<key_quote>[\"']?)(?:"
    r"(?i:(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|password|passwd|client[_-]?secret|secret|credentials?|authorization|"
    r"cookie|set-cookie|key))|"
    r"[A-Za-z0-9]*(?:ApiKey|APIKey|AccessToken|RefreshToken|Token|Password|Passwd|"
    r"ClientSecret|Secret|Credentials?|Authorization|Cookie|Key)"
    r")(?P=key_quote)\s*[:=]\s*)",
)


def is_mainland_ticker(ticker: str) -> bool:
    instrument = resolve_mainland_instrument(ticker)
    if instrument is None:
        return False
    return not instrument.akshare_code.startswith(CHINA_A_B_SHARE_PREFIXES)


def is_china_a_ticker(ticker: str) -> bool:
    """Return whether company-only China equity enhancements apply."""
    instrument = resolve_mainland_instrument(ticker)
    return (
        instrument is not None
        and instrument.instrument_kind == "equity"
        and is_mainland_ticker(ticker)
    )


def select_china_a_enhancement_preset() -> str:
    console.print("[bold]China A-share enhancement preset[/bold]")
    for idx, (value, label) in enumerate(CHINA_A_ENHANCEMENT_PRESETS.items(), start=1):
        console.print(f"  {idx}. {value} - {label}")

    while True:
        raw = typer.prompt(
            "Select preset",
            default="1",
        ).strip().lower()
        choice = CHINA_A_ENHANCEMENT_ALIASES.get(raw, raw)
        if choice in CHINA_A_ENHANCEMENT_PRESETS:
            return choice
        console.print(
            "[red]Invalid preset. Choose 1-5 or one of: "
            + ", ".join(CHINA_A_ENHANCEMENT_PRESETS)
            + "[/red]"
        )


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    # Fixed teams that always run (not user-selectable)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Analyst name mapping
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Sentiment Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
    }

    # Report section mapping: section -> (analyst_key for filtering, finalizing_agent)
    # analyst_key: which analyst selection controls this section (None = always included)
    # finalizing_agent: which agent must be "completed" for this report to count as done
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Sentiment Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
        "analysis_outcome": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.analyst_submissions = {}
        self.selected_analysts = []
        self._processed_message_ids = set()

    def init_for_analysis(self, selected_analysts):
        """Initialize agent status and report sections based on selected analysts.

        Args:
            selected_analysts: List of analyst type strings (e.g., ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # Build agent_status dynamically
        self.agent_status = {}

        # Add selected analysts
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # Add fixed teams
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # Build report_sections dynamically
        self.report_sections = {}
        self.analyst_submissions = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # Reset other state
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._processed_message_ids.clear()

    def get_completed_reports_count(self):
        """Count reports that are finalized (their finalizing agent is completed).

        A report is considered complete when:
        1. The report section has content (not None), AND
        2. The agent responsible for finalizing that report has status "completed"

        This prevents interim updates (like debate rounds) from counting as completed.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # Report is complete if it has content AND its finalizing agent is done
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content) -> bool:
        if section_name not in self.report_sections:
            return False
        if self.report_sections[section_name] == content:
            return False
        self.report_sections[section_name] = content
        self._update_current_report()
        return True

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content

        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "investment_plan": "Research Advisory Commentary",
                "trader_investment_plan": "Trader Advisory Commentary",
                "final_trade_decision": "Portfolio Management Decision",
                "analysis_outcome": "Analysis Outcome",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports - use .get() to handle missing sections
        analyst_sections = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections.get("fundamentals_report"):
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )

        # Research Team Reports
        if self.report_sections.get("investment_plan"):
            report_parts.append("## Research Advisory Commentary")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## Trader Advisory Commentary")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Analysis outcome or Portfolio Management Decision
        if self.report_sections.get("analysis_outcome"):
            report_parts.append("## Analysis Outcome")
            report_parts.append(f"{self.report_sections['analysis_outcome']}")
        elif self.report_sections.get("final_trade_decision"):
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()




def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open(Path(__file__).parent / "static" / "welcome.txt", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingAgents",
        subtitle="Multi-Agents LLM Financial Trading Framework",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # Add vertical space before announcements

    # Fetch and display announcements (silent on failure)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    def thinking_value_or_prompt(env_var, config_key, label, box_title, box_body, prompt_fn):
        """Return the env-configured reasoning/thinking value, or prompt for it.

        When ``env_var`` is set the interactive choice is skipped and the value
        the env overlay placed on DEFAULT_CONFIG is used — mirroring the
        env-precedence rule applied to the other selection steps.
        """
        if os.environ.get(env_var):
            value = DEFAULT_CONFIG[config_key]
            console.print(f"[green]✓ {label} from environment:[/green] {value}")
            return value
        console.print(create_question_box(box_title, box_body))
        return prompt_fn()

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol",
            "Enter the ticker, with exchange suffix when needed (e.g. SPY, 0700.HK, BTC-USD)",
            "SPY",
        )
    )
    selected_ticker = get_ticker()
    asset_type = detect_asset_type(selected_ticker)
    china_a_enhancement_preset = "basic"
    if is_china_a_ticker(selected_ticker):
        console.print(
            create_question_box(
                "Step 1b: China A-share Enhancements",
                "Select additional mainland China data sources for this run",
                "basic",
            )
        )
        china_a_enhancement_preset = select_china_a_enhancement_preset()
    # Only announce when it's not the default stock path, to avoid printing
    # "stock" on every run.
    if asset_type.value != "stock":
        console.print(
            f"[green]Detected asset type:[/green] {asset_type.value}"
        )

    # Step 2: Analysis date
    default_date = _analysis_date_limit(selected_ticker)[0].strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date(selected_ticker)

    # Step 3: Output language (skipped when set via TRADINGAGENTS_OUTPUT_LANGUAGE)
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        output_language = DEFAULT_CONFIG["output_language"]
        console.print(
            f"[green]✓ Output language from environment:[/green] {output_language}"
        )
    else:
        console.print(
            create_question_box(
                "Step 3: Output Language",
                "Select the language for analyst reports and final decision"
            )
        )
        output_language = ask_output_language()

    # Step 4: Select analysts
    console.print(
        create_question_box(
            "Step 4: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts(asset_type, selected_ticker)
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 5: Research depth (skipped when both round counts are set via env).
    # Research depth maps to the debate + risk round counts; when both are
    # supplied through TRADINGAGENTS_MAX_DEBATE_ROUNDS / _MAX_RISK_ROUNDS we keep
    # the run non-interactive and honor the env values (#977).
    depth_from_env = bool(os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS")) and bool(
        os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS")
    )
    if depth_from_env:
        selected_research_depth = DEFAULT_CONFIG["max_debate_rounds"]
        console.print(
            f"[green]✓ Research depth from environment:[/green] "
            f"{DEFAULT_CONFIG['max_debate_rounds']} debate / "
            f"{DEFAULT_CONFIG['max_risk_discuss_rounds']} risk rounds"
        )
    else:
        console.print(
            create_question_box(
                "Step 5: Research Depth", "Select your research depth level"
            )
        )
        selected_research_depth = select_research_depth()

    # Step 6: LLM Provider (skipped when set via TRADINGAGENTS_LLM_PROVIDER).
    # The backend URL comes from TRADINGAGENTS_LLM_BACKEND_URL when set,
    # otherwise the provider's default endpoint — the same value the menu
    # would have picked.
    provider_from_env = bool(os.environ.get("TRADINGAGENTS_LLM_PROVIDER"))
    if provider_from_env:
        selected_llm_provider = DEFAULT_CONFIG["llm_provider"].lower()
        backend_url = resolve_backend_url(
            selected_llm_provider, env_url=DEFAULT_CONFIG["backend_url"]
        )
        console.print(f"[green]✓ LLM provider from environment:[/green] {selected_llm_provider}")
        console.print(f"[green]✓ Backend URL:[/green] {backend_url}")
        # Still confirm/persist the API key so the run doesn't fail later.
        ensure_api_key(selected_llm_provider)
    else:
        console.print(
            create_question_box(
                "Step 6: LLM Provider", "Select your LLM provider"
            )
        )
        selected_llm_provider, backend_url = select_llm_provider()

        # Providers with regional endpoints prompt for the region as a secondary
        # step so the main dropdown stays clean (mainland China and international
        # accounts cannot share API keys).
        if selected_llm_provider == "qwen":
            selected_llm_provider, backend_url = ask_qwen_region()
        elif selected_llm_provider == "minimax":
            selected_llm_provider, backend_url = ask_minimax_region()
        elif selected_llm_provider == "glm":
            selected_llm_provider, backend_url = ask_glm_region()

        # Honor an explicit env backend URL even when the provider was chosen
        # interactively, so it isn't overwritten by the menu default (#978).
        backend_url = resolve_backend_url(
            selected_llm_provider, backend_url, env_url=DEFAULT_CONFIG["backend_url"]
        )

        # The generic OpenAI-compatible endpoint has no default; ask for it if
        # neither the menu nor the environment supplied one.
        if selected_llm_provider == "openai_compatible" and not backend_url:
            backend_url = prompt_openai_compatible_url()

        # For Ollama, surface the resolved endpoint (OLLAMA_BASE_URL vs default)
        # before model selection so it's obvious where we're connecting.
        if selected_llm_provider == "ollama":
            confirm_ollama_endpoint(backend_url)

        # Confirm the provider's API key is present; prompt the user to paste
        # one and persist it to .env if it's missing, so the analysis run
        # doesn't fail later at the first API call.
        ensure_api_key(selected_llm_provider)

    # Step 7: Thinking agents (skipped when either model is set via environment)
    if os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM") or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"):
        selected_shallow_thinker = DEFAULT_CONFIG["quick_think_llm"]
        selected_deep_thinker = DEFAULT_CONFIG["deep_think_llm"]
        console.print(
            f"[green]✓ Thinking agents from environment:[/green] "
            f"quick={selected_shallow_thinker}, deep={selected_deep_thinker}"
        )
    else:
        console.print(
            create_question_box(
                "Step 7: Thinking Agents", "Select your thinking agents for analysis"
            )
        )
        selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
        selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 8: Provider-specific reasoning/thinking configuration. Each knob is
    # settable via its TRADINGAGENTS_* env var; when that var is set (or the
    # provider itself came from env) the prompt is skipped and the configured
    # value is used — same env-precedence rule as the steps above. None = each
    # provider's own default.
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_from_env:
        thinking_level = DEFAULT_CONFIG["google_thinking_level"]
        reasoning_effort = DEFAULT_CONFIG["openai_reasoning_effort"]
        anthropic_effort = DEFAULT_CONFIG["anthropic_effort"]
    elif provider_lower == "google":
        thinking_level = thinking_value_or_prompt(
            "TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "google_thinking_level",
            "Gemini thinking mode", "Step 8: Thinking Mode",
            "Configure Gemini thinking mode", ask_gemini_thinking_config,
        )
    elif provider_lower == "openai":
        reasoning_effort = thinking_value_or_prompt(
            "TRADINGAGENTS_OPENAI_REASONING_EFFORT", "openai_reasoning_effort",
            "Reasoning effort", "Step 8: Reasoning Effort",
            "Configure OpenAI reasoning effort level", ask_openai_reasoning_effort,
        )
    elif provider_lower == "anthropic":
        anthropic_effort = thinking_value_or_prompt(
            "TRADINGAGENTS_ANTHROPIC_EFFORT", "anthropic_effort",
            "Claude effort", "Step 8: Effort Level",
            "Configure Claude effort level", ask_anthropic_effort,
        )

    return {
        "ticker": selected_ticker,
        "asset_type": asset_type.value,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
        "china_a_enhancement_preset": china_a_enhancement_preset,
    }


def _analysis_date_limit(ticker: str | None = None) -> tuple[datetime.date, str]:
    if ticker and is_mainland_ticker(ticker):
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date(), "Beijing"
    return datetime.datetime.now().date(), "local"


def get_analysis_date(ticker: str | None = None):
    """Get the analysis date from user input."""
    while True:
        limit_date, limit_label = _analysis_date_limit(ticker)
        date_str = typer.prompt("", default=limit_date.strftime("%Y-%m-%d"))
        try:
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > limit_date:
                console.print(
                    f"[red]Error: Analysis date cannot be in the future "
                    f"(max {limit_label} date: {limit_date:%Y-%m-%d})[/red]"
                )
                continue
            if ticker and is_mainland_ticker(ticker) and analysis_date.date() == limit_date:
                console.print(
                        "[yellow]Mainland-market same-day data may be incomplete until "
                    "mainland markets close and vendors finish publishing.[/yellow]"
                )
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """Save the complete analysis report to disk (shared CLI/API writer)."""
    return write_report_tree(final_state, ticker, save_path)


def display_complete_report(final_state):
    """Display the complete analysis report sequentially (avoids truncation)."""
    console.print()
    console.print(Rule("Complete Analysis Report", style="bold green"))

    # I. Analyst Team Reports
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        console.print(Panel("[bold]I. Analyst Team Reports[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. Research Team Decision[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. Trading Team
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. Trading Team Plan[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. Risk Management Team
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. Risk Management Team Decision[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. Portfolio Manager Decision
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. Portfolio Manager Decision[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """Update status for research team members (not Trader)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# Ordered list of analysts for status transitions
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}

_ANALYST_SUBMISSION_PREFIX = "analyst."
_ANALYST_SUBMISSION_NAMES = {"social": "sentiment"}
_SAFE_SUBMISSION_REASON_CLASSES = frozenset(
    {"unsupported", "none_parsed", "validation_error", "transport_error"}
)
_SAFE_SUBMISSION_MODES = frozenset(
    {"direct_tool", "direct_structured", "finalized_structured"}
)
_STRUCTURED_OUTPUT_POLICY_VERSION = "analyst_submission_evidence_v1"


def _value_from_mapping_or_object(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _analyst_submission_source(evidence_state, analyst_key: str):
    """Return the required submission source for an analyst, when recorded.

    Evidence sources are the authoritative success signal for structured analyst
    submissions. Missing sources deliberately retain the legacy report-content
    behavior for old checkpoints and graph states.
    """
    if evidence_state is None:
        return None
    sources = _value_from_mapping_or_object(evidence_state, "sources", ()) or ()
    submission_name = _ANALYST_SUBMISSION_NAMES.get(analyst_key, analyst_key)
    expected_id = f"{_ANALYST_SUBMISSION_PREFIX}{submission_name}.submission"
    for source in sources:
        if _value_from_mapping_or_object(source, "source_id") != expected_id:
            continue
        if not _value_from_mapping_or_object(source, "required", False):
            continue
        status = _value_from_mapping_or_object(source, "status", "unavailable")
        status = getattr(status, "value", status)
        return {
            "status": str(status).lower(),
            "reason_class": _safe_submission_reason_class(
                _value_from_mapping_or_object(source, "detail", "")
            ),
            "mode": _safe_submission_mode(
                _value_from_mapping_or_object(source, "detail", "")
            ),
        }
    return None


def _safe_submission_reason_class(detail) -> str | None:
    """Keep only a classified fallback reason, never provider error text."""
    candidate = str(detail or "").strip().lower().split(":", maxsplit=1)[0]
    return candidate if candidate in _SAFE_SUBMISSION_REASON_CLASSES else None


def _safe_submission_mode(detail) -> str | None:
    candidate = str(detail or "").strip().lower()
    return candidate if candidate in _SAFE_SUBMISSION_MODES else None


def _submission_agent_status(submission: dict | None) -> str | None:
    if submission is None:
        return None
    if submission["status"] == "available":
        return "completed"
    reason_class = submission.get("reason_class")
    if reason_class == "transport_error":
        return "degraded"
    if reason_class == "validation_error":
        return "failed"
    return "unavailable"


def _sync_analyst_wall_time_from_chunk(
    tracker: AnalystWallTimeTracker,
    chunk: dict,
    known_submissions: dict[str, dict] | None = None,
) -> None:
    """Record analyst wall time only after a validated submission is available."""
    current_time = time.monotonic()
    active_found = False
    evidence_state = chunk.get("evidence_state")
    for spec in tracker.plan.specs:
        submission = (known_submissions or {}).get(spec.key)
        if submission is None:
            submission = _analyst_submission_source(evidence_state, spec.key)
        if submission is not None:
            if submission["status"] != "available":
                # A terminal unavailable submission must not accrue completed
                # wall time or prevent the next analyst from becoming active.
                continue
            tracker.mark_started(spec.key, started_at=current_time)
            tracker.mark_completed(spec.key, completed_at=current_time)
        elif chunk.get(spec.report_key):
            # Backward compatibility for checkpoints created before submission
            # evidence was persisted.
            tracker.mark_started(spec.key, started_at=current_time)
            tracker.mark_completed(spec.key, completed_at=current_time)
        elif not active_found:
            tracker.mark_started(spec.key, started_at=current_time)
            active_found = True


def _complete_non_analyst_agents(
    buffer: MessageBuffer,
    *,
    admission_blocked: bool = False,
) -> None:
    """Finish or skip downstream statuses without hiding analyst failures."""
    analyst_names = set(ANALYST_AGENT_NAMES.values())
    for agent in buffer.agent_status:
        if agent not in analyst_names:
            buffer.update_agent_status(
                agent,
                "skipped" if admission_blocked else "completed",
            )


def _admission_was_blocked(final_state: dict) -> bool:
    admission = final_state.get("admission_gate") or {}
    return _value_from_mapping_or_object(admission, "admitted") is False


def update_analyst_statuses(message_buffer, chunk, wall_time_tracker=None):
    """Update analyst statuses based on accumulated report state.

    Logic:
    - Store new report content from the current chunk if present
    - Check accumulated report_sections (not just current chunk) for status
    - Analysts with reports = completed
    - First analyst without report = in_progress
    - Remaining analysts without reports = pending
    - When all analysts done, set Bull Researcher to in_progress
    """
    selected = message_buffer.selected_analysts
    found_active = False

    evidence_state = chunk.get("evidence_state")
    for analyst_key in selected:
        submission = _analyst_submission_source(evidence_state, analyst_key)
        if submission is not None:
            message_buffer.analyst_submissions[analyst_key] = submission
    if wall_time_tracker is not None:
        _sync_analyst_wall_time_from_chunk(
            wall_time_tracker,
            chunk,
            message_buffer.analyst_submissions,
        )

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # Capture new report content from current chunk
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # Required submission evidence supersedes report text. In particular, a
        # non-directional ANALYSIS_UNAVAILABLE report is useful for the user but
        # must not look like a completed analyst result.
        has_report = bool(message_buffer.report_sections.get(report_key))
        submission = message_buffer.analyst_submissions.get(analyst_key)
        status_from_submission = _submission_agent_status(submission)

        if status_from_submission is not None:
            message_buffer.update_agent_status(agent_name, status_from_submission)
        elif has_report:
            # Legacy checkpoints and graph states did not expose submission
            # sources, so preserve their original report-content behavior.
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # When all analysts complete, transition research team to in_progress
    if (
        not found_active
        and selected
        and message_buffer.agent_status.get("Bull Researcher") == "pending"
    ):
        message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """Extract string content from various message formats.
    Returns None if no meaningful text content is found.
    """
    import ast

    def is_empty(val):
        """Check if value is empty using Python's truthiness."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # Can't parse = real text
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


_ADVISORY_COMMENTARY_PHASES = frozenset(
    {
        "research_debate",
        "trading",
        "risk_debate",
        "portfolio_synthesis",
    }
)


def classify_message_type(
    message,
    *,
    runtime_graph_phase: str | None = None,
) -> tuple[str, str | None]:
    """Classify LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Advisory Commentary,
                          Data, Control
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        if runtime_graph_phase in _ADVISORY_COMMENTARY_PHASES:
            return ("Advisory Commentary", content)
        return ("Agent", content)

    # Fallback for unknown types
    return ("System", content)


_RUNTIME_GRAPH_PHASES = (
    "analysis",
    "research_debate",
    "trading",
    "risk_debate",
    "portfolio_synthesis",
)


def _runtime_graph_phase_for_chunk(chunk: dict) -> str:
    if chunk.get("final_trade_decision") or chunk.get("analysis_outcome"):
        return "portfolio_synthesis"

    risk_state = chunk.get("risk_debate_state")
    if isinstance(risk_state, dict) and any(
        risk_state.get(key)
        for key in (
            "current_aggressive_response",
            "current_conservative_response",
            "current_neutral_response",
            "aggressive_history",
            "conservative_history",
            "neutral_history",
            "judge_decision",
        )
    ):
        return "risk_debate"

    if chunk.get("trader_investment_plan"):
        return "trading"

    debate_state = chunk.get("investment_debate_state")
    if chunk.get("investment_plan") or (
        isinstance(debate_state, dict)
        and any(
            debate_state.get(key)
            for key in (
                "current_response",
                "bull_history",
                "bear_history",
                "judge_decision",
            )
        )
    ):
        return "research_debate"

    return "analysis"


def _build_run_config(selections: dict, checkpoint: bool | None) -> dict:
    """Assemble the run config from interactive selections, honoring env precedence.

    Round counts and checkpoint follow "explicit env/flag wins": an env-applied
    value on DEFAULT_CONFIG is preserved unless the user overrode it on the CLI.
    """
    config = DEFAULT_CONFIG.copy()
    # Research depth sets both round counts, but an explicit env override
    # (TRADINGAGENTS_MAX_DEBATE_ROUNDS / _MAX_RISK_ROUNDS) wins over the
    # interactive selection — leave the env-applied value in place (#977).
    if not os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        config["max_debate_rounds"] = selections["research_depth"]
    if not os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS"):
        config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")
    config["china_a_enhancement_preset"] = selections.get(
        "china_a_enhancement_preset",
        DEFAULT_CONFIG.get("china_a_enhancement_preset", "basic"),
    )
    # --checkpoint/--no-checkpoint overrides only when explicitly given; omitting
    # the flag preserves TRADINGAGENTS_CHECKPOINT_ENABLED / the default (#976).
    if checkpoint is not None:
        config["checkpoint_enabled"] = checkpoint
    return config


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _analyst_values(analysts=None) -> list[str]:
    if analysts is None:
        return []
    return [analyst.value if hasattr(analyst, "value") else str(analyst) for analyst in analysts]


def _safe_provider_name(provider) -> str | None:
    """Return a compact provider identifier suitable for a durable artifact."""
    if provider is None:
        return None
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(provider).strip().lower())
    return normalized[:80] or None


def _safe_backend_host(backend_url) -> str | None:
    """Persist only a backend host, never credentials, paths, or query data."""
    if not backend_url:
        return None
    parsed = urlsplit(str(backend_url))
    if not parsed.hostname and "://" not in str(backend_url):
        parsed = urlsplit(f"//{backend_url}")
    return parsed.hostname.lower() if parsed.hostname else None


def _record_analyst_submission_observability(artifacts: dict, evidence_state) -> None:
    """Append safe structured-output outcome classes to the run status artifact."""
    if evidence_state is None:
        return
    status_file = artifacts["status_file"]
    payload = _read_run_status(status_file)
    submissions = dict(payload.get("analyst_submissions") or {})
    structured_output = dict(payload.get("structured_output") or {})
    fallback_reason_classes = dict(
        structured_output.get("fallback_reason_classes") or {}
    )

    for analyst_key in ANALYST_ORDER:
        submission = _analyst_submission_source(evidence_state, analyst_key)
        if submission is None:
            continue
        entry = {"status": submission["status"]}
        reason_class = submission.get("reason_class")
        mode = submission.get("mode")
        if submission["status"] == "available" and mode is not None:
            entry["mode"] = mode
        if submission["status"] != "available" and reason_class is not None:
            entry["reason_class"] = reason_class
            fallback_reason_classes[analyst_key] = reason_class
        submissions[analyst_key] = entry

    payload["analyst_submissions"] = submissions
    structured_output["fallback_reason_classes"] = fallback_reason_classes
    payload["structured_output"] = structured_output
    payload["updated_at"] = _now_iso()
    _write_run_status(status_file, payload)


def _write_run_status(status_file: Path, payload: dict) -> None:
    status_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _read_run_status(status_file: Path) -> dict:
    return json.loads(status_file.read_text(encoding="utf-8"))


def _prepare_run_artifacts(config: dict, selections: dict) -> dict[str, Path | str]:
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    config_digest = configuration_digest(config)
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    run_dir = results_dir / "runs" / run_id
    report_dir = run_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    log_file = run_dir / "message_tool.log"
    latest_log_file = results_dir / "latest_message_tool.log"
    metadata = (
        f"run_id={run_id} "
        f"ticker={selections['ticker']} "
        f"analysis_date={selections['analysis_date']} "
        f"asset_type={selections['asset_type']} "
        f"china_a_enhancement_preset={selections.get('china_a_enhancement_preset', 'basic')}\n"
    )
    log_file.write_text(metadata, encoding="utf-8")
    latest_log_file.write_text(metadata, encoding="utf-8")

    status_file = run_dir / "run_status.json"
    now = _now_iso()
    _write_run_status(
        status_file,
        {
            "run_id": run_id,
            "artifact_run_id": run_id,
            "canonical_run_id": None,
            "configuration_digest": config_digest,
            "ticker": selections["ticker"],
            "llm_provider": _safe_provider_name(
                config.get("llm_provider", selections.get("llm_provider"))
            ),
            "quick_think_model": config.get("quick_think_llm"),
            "deep_think_model": config.get("deep_think_llm"),
            "backend_url_host": _safe_backend_host(config.get("backend_url")),
            "structured_output_policy_version": _STRUCTURED_OUTPUT_POLICY_VERSION,
            "analyst_submissions": {},
            "structured_output": {"fallback_reason_classes": {}},
            "analysis_date": selections["analysis_date"],
            "asset_type": selections["asset_type"],
            "selected_analysts": _analyst_values(selections.get("analysts")),
            "china_a_enhancement_preset": selections.get(
                "china_a_enhancement_preset", "basic"
            ),
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "failed_at": None,
            "terminal_at": None,
            "status": "running",
            "lifecycle_status": "running",
            "terminal_outcome_kind": None,
            "evidence_integrity_status": None,
            "current_phase": "artifacts_prepared",
            "active_phase": "artifacts_prepared",
            "failed_phase": None,
            "operational_error_category": None,
            "audit_digest": None,
            "error_summary": None,
            "error_diagnostics": None,
            "reports_written": [],
        },
    )
    return {
        "run_id": run_id,
        "results_dir": results_dir,
        "run_dir": run_dir,
        "report_dir": report_dir,
        "log_file": log_file,
        "latest_log_file": latest_log_file,
        "status_file": status_file,
        "artifact_root": Path(config["results_dir"]) / "runtime_artifacts",
        "metrics_file": run_dir / "runtime_metrics.json",
        "configuration_digest": config_digest,
    }


def _update_run_status(artifacts: dict, **updates) -> None:
    runtime_writer = artifacts.get("runtime_writer")
    current_phase = updates.get("current_phase")
    terminal = updates.get("status") in {"completed", "failed"}
    if runtime_writer is not None and current_phase and not terminal:
        runtime_writer.transition_phase(current_phase)
    status_file = artifacts["status_file"]
    payload = _read_run_status(status_file)
    if terminal:
        updates["current_phase"] = None
        updates["active_phase"] = None
    elif current_phase:
        updates["active_phase"] = current_phase
    if updates.get("status") is not None:
        updates.setdefault("lifecycle_status", updates["status"])
    payload.update(updates)
    payload["updated_at"] = _now_iso()
    if updates.get("status") == "completed":
        payload["completed_at"] = payload["updated_at"]
    if updates.get("status") == "failed":
        payload["failed_at"] = payload["updated_at"]
    if terminal:
        payload["terminal_at"] = payload["updated_at"]
    _write_run_status(status_file, payload)
    if runtime_writer is not None and terminal:
        stats_handler = artifacts.get("stats_handler")
        stats = stats_handler.get_stats() if stats_handler is not None else {}
        runtime_writer.record_terminal_summary(
            terminal_route=str(
                updates.get("terminal_outcome_kind") or "operational_failure"
            ),
            stats=stats,
            acquisition_outcomes=artifacts.get(
                "terminal_acquisition_outcomes",
                (),
            ),
            run_telemetry=artifacts.get("terminal_run_telemetry"),
        )
        runtime_writer.finish_phases()


def _redact_error_url(match: re.Match) -> str:
    value = match.group(0)
    trailing = ""
    while value and value[-1] in ".,;)]}":
        trailing = value[-1] + trailing
        value = value[:-1]
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        hostname = None
    return f"{hostname or '<url>'}{trailing}"


def _known_secret_environment_values() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value
                for name, value in os.environ.items()
                if value and _ERROR_SECRET_ENV_RE.search(name)
            },
            key=len,
            reverse=True,
        )
    )


def _secret_value_end(message: str, start: int) -> int:
    if start >= len(message):
        return start

    opener = message[start]
    if opener in {'"', "'"}:
        escaped = False
        for index in range(start + 1, len(message)):
            character = message[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == opener:
                return index + 1
        return len(message)

    if opener in "[{(":
        stack = [opener]
        pairs = {"]": "[", "}": "{", ")": "("}
        quote = None
        escaped = False
        for index in range(start + 1, len(message)):
            character = message[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character in {'"', "'"}:
                quote = character
            elif character in "[{(":
                stack.append(character)
            elif character in "]})":
                if pairs[character] != stack[-1]:
                    return len(message)
                stack.pop()
                if not stack:
                    return index + 1
        return len(message)

    index = start
    while index < len(message) and message[index] not in ",;})]":
        index += 1
    return index


def _redact_structured_secret_values(message: str) -> str:
    parts: list[str] = []
    cursor = 0
    search_from = 0
    while match := _ERROR_SECRET_FIELD_RE.search(message, search_from):
        parts.append(message[cursor : match.end()])
        value_start = match.end()
        value_end = _secret_value_end(message, value_start)
        value = message[value_start:value_end]
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            parts.append(f"{value[0]}<redacted>{value[0]}")
        else:
            parts.append("<redacted>")
        cursor = value_end
        search_from = value_end
    parts.append(message[cursor:])
    return "".join(parts)


def _sanitize_exception_message(message: str) -> tuple[str, bool]:
    sanitized = _ERROR_URL_RE.sub(_redact_error_url, message)
    sanitized = _ERROR_BEARER_TOKEN_RE.sub(
        lambda match: f"{match.group(1)} <redacted>",
        sanitized,
    )
    sanitized = _ERROR_SECRET_HEADER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)} <redacted>",
        sanitized,
    )
    sanitized = _redact_structured_secret_values(sanitized)
    for secret in _known_secret_environment_values():
        sanitized = sanitized.replace(secret, "<redacted>")

    home_variants = {
        str(Path.home()),
        str(Path.home()).replace("\\", "/"),
    }
    for home in sorted(home_variants, key=len, reverse=True):
        if home:
            sanitized = re.sub(re.escape(home), "<home>", sanitized, flags=re.IGNORECASE)

    sanitized = " ".join(sanitized.split())
    truncated = len(sanitized) > _MAX_ERROR_MESSAGE_CHARS
    if truncated:
        sanitized = sanitized[: _MAX_ERROR_MESSAGE_CHARS - 1] + "…"
    return sanitized, truncated


def _exception_chain_diagnostics(exc: BaseException) -> dict:
    entries: list[dict] = []
    seen: set[int] = set()
    relationship = "outermost"
    current: BaseException | None = exc
    cycle_detected = False

    while current is not None:
        identity = id(current)
        if identity in seen:
            cycle_detected = True
            break
        seen.add(identity)
        message, message_truncated = _sanitize_exception_message(str(current))
        entries.append(
            {
                "exception_type": type(current).__name__,
                "message": message,
                "message_truncated": message_truncated,
                "relationship": relationship,
            }
        )

        if current.__cause__ is not None:
            current = current.__cause__
            relationship = "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            relationship = "context"
        else:
            current = None

    chain_truncated = len(entries) > _MAX_ERROR_CHAIN_ENTRIES
    if chain_truncated:
        entries = [
            *entries[: _MAX_ERROR_CHAIN_ENTRIES - 1],
            entries[-1],
        ]
    root_cause = dict(entries[-1])
    return {
        "contract_version": _ERROR_DIAGNOSTICS_CONTRACT_VERSION,
        "chain": entries,
        "root_cause": root_cause,
        "chain_truncated": chain_truncated,
        "cycle_detected": cycle_detected,
    }


def _exception_summary(exc: BaseException) -> str:
    message, _ = _sanitize_exception_message(str(exc))
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _exception_log_summary(summary: str, diagnostics: dict) -> str:
    chain = diagnostics["chain"]
    if len(chain) <= 1:
        return summary
    root_cause = diagnostics["root_cause"]
    root_summary = root_cause["exception_type"]
    if root_cause["message"]:
        root_summary = f"{root_summary}: {root_cause['message']}"
    return f"{summary}; root cause: {root_summary}"


def _mark_run_failed(artifacts: dict | None, exc: BaseException, current_phase: str) -> None:
    if artifacts is None:
        return
    summary = _exception_summary(exc)
    diagnostics = _exception_chain_diagnostics(exc)
    log_summary = _exception_log_summary(summary, diagnostics)
    with suppress(Exception):
        telemetry_ledger = artifacts.get("run_telemetry_ledger")
        if telemetry_ledger is not None:
            artifacts["terminal_run_telemetry"] = telemetry_ledger.finalize(
                terminal_route="operational_failure",
            ).model_dump(mode="json")
    with suppress(Exception):
        _update_run_status(
            artifacts,
            status="failed",
            current_phase=None,
            failed_phase=current_phase,
            terminal_outcome_kind="operational_failure",
            operational_error_category=(
                "report_publication"
                if current_phase in {"report_writing", "checkpoint_cleanup"}
                else (
                    "configuration"
                    if current_phase in {"setup", "graph_initializing"}
                    else "graph_execution"
                )
            ),
            error_summary=summary,
            error_diagnostics=diagnostics,
        )
    with suppress(Exception):
        runtime_writer = artifacts.get("runtime_writer")
        if runtime_writer is not None:
            runtime_writer.record_critical(
                datetime.datetime.now().strftime("%H:%M:%S"),
                "System",
                f"Run failed during {current_phase}: {log_summary}",
            )
        else:
            _append_line_to_run_logs(
                [artifacts["log_file"], artifacts["latest_log_file"]],
                f"{datetime.datetime.now().strftime('%H:%M:%S')} "
                f"[System] Run failed during {current_phase}: {log_summary}\n",
            )


def _write_run_reports(final_state: dict, ticker: str, artifacts: dict) -> Path:
    started_at = time.monotonic()
    final_state["configuration_digest"] = artifacts["configuration_digest"]
    telemetry_ledger = artifacts.get("run_telemetry_ledger")
    if telemetry_ledger is not None:
        terminal_route = final_state.get("terminal_outcome_kind")
        if terminal_route is None:
            terminal_route = (
                "trading_decision"
                if final_state.get("trading_decision") is not None
                else "analysis_outcome"
            )
        telemetry = telemetry_ledger.finalize(
            terminal_route=str(terminal_route),
        ).model_dump(mode="json")
        final_state["run_telemetry"] = telemetry
        artifacts["terminal_run_telemetry"] = telemetry
    report_file = save_report_to_disk(final_state, ticker, artifacts["report_dir"])
    runtime_writer = artifacts.get("runtime_writer")
    if runtime_writer is not None:
        runtime_writer.record_duration(
            "report",
            "complete_report",
            time.monotonic() - started_at,
        )
    _update_run_status(artifacts, reports_written=[str(report_file)])
    return report_file


def _mark_run_completed(final_state: dict, artifacts: dict) -> None:
    _update_run_status(
        artifacts,
        status="completed",
        current_phase=None,
        lifecycle_status=final_state["lifecycle_status"],
        terminal_outcome_kind=final_state["terminal_outcome_kind"],
        evidence_integrity_status=final_state["evidence_integrity_status"],
        canonical_run_id=final_state["run_id"],
        configuration_digest=final_state["configuration_digest"],
        audit_digest=final_state["decision_audit_sha256"],
    )


def _append_line_to_run_logs(paths: list[Path], line: str) -> None:
    for path in paths:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def _complete_asset_configuration_failure(
    *,
    selections: dict,
    config: dict,
    artifacts: dict,
    error: RunAssetConfigurationError,
) -> dict:
    """Publish a typed non-directional result without constructing a graph."""

    evidence = asset_configuration_failure_evidence(error)
    final_state = Propagator().create_initial_state(
        selections["ticker"],
        selections["analysis_date"],
        asset_type=selections["asset_type"],
        evidence_state=evidence,
    )
    final_state.update(
        create_preflight_gate_node(
            create_production_decision_policy(),
            None,
        )(final_state)
    )
    final_state["graph_signature"] = "asset_configuration=unavailable:v1"
    final_state["evidence_gate_mode"] = config.get("evidence_gate_mode", "enforce")
    final_state["asset_configuration_failure"] = {
        "contract_version": "1.0",
        "reason": error.reason.value,
        "diagnostic_code": error.diagnostic_code,
    }
    _write_run_reports(final_state, selections["ticker"], artifacts)
    _mark_run_completed(final_state, artifacts)
    return final_state


def run_analysis(checkpoint: bool | None = None):
    # First get all user selections
    selections = get_user_selections()

    config = _build_run_config(selections, checkpoint)
    asset_configuration = None
    asset_configuration_error = None
    should_resolve_asset = selections["asset_type"] == "crypto" or any(
        analyst.value == "fundamentals" for analyst in selections["analysts"]
    )
    if should_resolve_asset:
        try:
            asset_configuration = resolve_run_asset_configuration(
                selections["ticker"],
                config=config,
            )
        except RunAssetConfigurationError as exc:
            asset_configuration_error = exc
        else:
            config = dict(config)
            config["asset_configuration_signature"] = (
                asset_configuration.asset_configuration_signature
            )

    artifacts = None
    current_phase = "setup"

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]
    analyst_execution_plan = build_analyst_execution_plan(selected_analyst_keys)
    analyst_wall_time_tracker = AnalystWallTimeTracker(analyst_execution_plan)

    artifacts = _prepare_run_artifacts(config, selections)
    runtime_writer = RuntimeArtifactWriter(
        artifact_root=artifacts["artifact_root"],
        log_paths=[artifacts["log_file"], artifacts["latest_log_file"]],
        metrics_path=artifacts["metrics_file"],
    )
    artifacts["runtime_writer"] = runtime_writer
    stats_handler = StatsCallbackHandler(metrics_recorder=runtime_writer)
    artifacts["stats_handler"] = stats_handler
    if asset_configuration_error is not None:
        try:
            return _complete_asset_configuration_failure(
                selections=selections,
                config=config,
                artifacts=artifacts,
                error=asset_configuration_error,
            )
        finally:
            with suppress(Exception):
                runtime_writer.close()
    current_phase = "graph_initializing"
    _update_run_status(artifacts, current_phase="graph_initializing")
    report_dir = artifacts["report_dir"]

    try:
        # Initialize the graph with callbacks bound to LLMs
        graph = TradingAgentsGraph(
            selected_analyst_keys,
            config=config,
            debug=True,
            callbacks=[stats_handler],
            asset_type=selections["asset_type"],
            asset_configuration=asset_configuration,
        )

        # Initialize message buffer with selected analysts
        message_buffer.init_for_analysis(selected_analyst_keys)
    except BaseException as exc:
        _mark_run_failed(artifacts, exc, current_phase=current_phase)
        with suppress(Exception):
            runtime_writer.close()
        raise

    # Track start time for elapsed display
    start_time = time.time()

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            if message_type == "Data":
                runtime_writer.record_tool_result(timestamp, content)
            else:
                runtime_writer.record_message(timestamp, message_type, content)

        return wrapper

    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            runtime_writer.record_tool_call(timestamp, tool_name, args)

        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(section_name, content):
            changed = func(section_name, content)
            if not changed:
                return False

            stored = obj.report_sections[section_name]
            if stored:
                started_at = time.monotonic()
                file_name = f"{section_name}.md"
                text = (
                    "\n".join(str(item) for item in stored) if isinstance(stored, list) else stored
                )
                path = report_dir / file_name
                with open(path, "w", encoding="utf-8") as report_file:
                    report_file.write(text)
                display.report_ready(section_name, text, path)
                runtime_writer.record_duration(
                    "report",
                    section_name,
                    time.monotonic() - started_at,
                )
            return True

        return wrapper

    try:
        display = create_run_display(
            console,
            message_buffer,
            stats_handler,
            start_time,
        )
    except BaseException as exc:
        _mark_run_failed(artifacts, exc, current_phase=current_phase)
        with suppress(Exception):
            runtime_writer.close()
        raise

    base_add_message = MessageBuffer.add_message.__get__(message_buffer, MessageBuffer)
    base_add_tool_call = MessageBuffer.add_tool_call.__get__(
        message_buffer,
        MessageBuffer,
    )
    base_update_report_section = MessageBuffer.update_report_section.__get__(
        message_buffer,
        MessageBuffer,
    )
    message_buffer.add_message = base_add_message
    message_buffer.add_tool_call = base_add_tool_call
    message_buffer.update_report_section = base_update_report_section
    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(
        message_buffer,
        "update_report_section",
    )

    spinner_text = f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
    snapshot_scope = ExitStack()
    try:
        run_asset = getattr(graph, "asset_configuration", None)
        snapshot_context = (
            authoritative_snapshot_run()
            if run_asset is None
            else authoritative_snapshot_run(asset_configuration=run_asset)
        )
        snapshot_run = snapshot_scope.enter_context(snapshot_context)
        artifacts["run_telemetry_ledger"] = snapshot_run.telemetry_ledger
        stats_handler.set_telemetry_recorder(snapshot_run.telemetry_ledger)
        checkpoint_scope_factory = getattr(graph, "checkpoint_scope", None)
        if callable(checkpoint_scope_factory):
            checkpoint_context = checkpoint_scope_factory(
                selections["ticker"],
                selections["analysis_date"],
                selections["asset_type"],
            )
        elif config.get("checkpoint_enabled"):
            raise RuntimeError(
                "checkpointing is enabled but the graph has no checkpoint scope"
            )
        else:
            checkpoint_context = nullcontext(CheckpointSession({}, False))
        checkpoint_session = snapshot_scope.enter_context(checkpoint_context)
        checkpoint_graph_config = checkpoint_session.graph_config

        display.start()
        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        if selections["asset_type"] != "stock":
            message_buffer.add_message(
                "System",
                f"Detected asset type: {selections['asset_type']}",
            )
        message_buffer.add_message("System", f"Analysis date: {selections['analysis_date']}")
        message_buffer.add_message(
            "System",
            "Selected analysts: " + ", ".join(analyst.value for analyst in selections["analysts"]),
        )

        # Update agent status to in_progress for the first analyst
        first_analyst = get_initial_analyst_node(analyst_execution_plan)
        message_buffer.update_agent_status(first_analyst, "in_progress")
        analyst_wall_time_tracker.mark_started(selected_analyst_keys[0])
        display.refresh(spinner_text)

        # Initialize state and get graph args with callbacks. Acquire trusted
        # evidence first so a missing registry identity can short-circuit without
        # optional provider enrichment or model-mediated work.
        evidence_state = graph.resolve_evidence_state(
            selections["ticker"], selections["analysis_date"]
        )
        init_agent_state = graph.create_initial_state(
            selections["ticker"],
            selections["analysis_date"],
            asset_type=selections["asset_type"],
            evidence_state=evidence_state,
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(
            callbacks=[stats_handler],
            run_id=init_agent_state.get("run_id"),
        )
        if checkpoint_graph_config:
            args.setdefault("config", {}).setdefault("configurable", {}).update(
                checkpoint_graph_config.get("configurable", {})
            )

        # Stream the analysis
        current_phase = "graph_stream"
        _update_run_status(artifacts, current_phase=current_phase)
        runtime_graph_phase = "analysis"
        runtime_writer.transition_phase(runtime_graph_phase)
        snapshot_run.telemetry_ledger.transition_stage(runtime_graph_phase)
        tracker = StateProgressTracker()
        latest_state = dict(init_agent_state)
        graph_input = (
            None if checkpoint_session.resume_from_checkpoint else init_agent_state
        )
        for chunk in graph.graph.stream(graph_input, **args):
            observed_phase = _runtime_graph_phase_for_chunk(chunk)
            if _RUNTIME_GRAPH_PHASES.index(observed_phase) > _RUNTIME_GRAPH_PHASES.index(
                runtime_graph_phase
            ):
                runtime_graph_phase = observed_phase
                runtime_writer.transition_phase(runtime_graph_phase)
                snapshot_run.telemetry_ledger.transition_stage(runtime_graph_phase)
            for message in chunk.get("messages", []):
                key = message_key(message)
                if key in message_buffer._processed_message_ids:
                    continue
                message_buffer._processed_message_ids.add(key)

                msg_type, content = classify_message_type(
                    message,
                    runtime_graph_phase=runtime_graph_phase,
                )
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

            display.refresh(spinner_text)

            for event in tracker.events_for(chunk):
                message_buffer.add_message(event.message_type, event.content)
                display.publish_event(event)

            # Update analyst statuses based on report state (runs on every chunk)
            update_analyst_statuses(
                message_buffer,
                chunk,
                wall_time_tracker=analyst_wall_time_tracker,
            )
            _record_analyst_submission_observability(
                artifacts,
                chunk.get("evidence_state"),
            )

            # Research Team - Handle Investment Debate State
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # Only update status when there's actual content
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if judge:
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            if chunk.get("investment_plan"):
                message_buffer.update_report_section(
                    "investment_plan", chunk["investment_plan"]
                )

            # Trading Team
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # Risk Management Team - Handle Risk Debate State
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if (
                    agg_hist
                    and message_buffer.agent_status.get("Aggressive Analyst")
                    != "completed"
                ):
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                if (
                    con_hist
                    and message_buffer.agent_status.get("Conservative Analyst")
                    != "completed"
                ):
                    message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                if (
                    neu_hist
                    and message_buffer.agent_status.get("Neutral Analyst") != "completed"
                ):
                    message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                if judge and message_buffer.agent_status.get("Portfolio Manager") != "completed":
                    message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                    message_buffer.update_agent_status("Aggressive Analyst", "completed")
                    message_buffer.update_agent_status("Conservative Analyst", "completed")
                    message_buffer.update_agent_status("Neutral Analyst", "completed")
                    message_buffer.update_agent_status("Portfolio Manager", "completed")

            if chunk.get("final_trade_decision"):
                message_buffer.update_report_section(
                    "final_trade_decision", chunk["final_trade_decision"]
                )
            if chunk.get("analysis_outcome"):
                message_buffer.update_report_section(
                    "analysis_outcome", chunk["analysis_outcome"]
                )

            latest_state.update(chunk)
            display.refresh(spinner_text)

        final_state = latest_state
        final_state["evidence_gate_mode"] = config.get(
            "evidence_gate_mode",
            "enforce",
        )
        signature_builder = getattr(graph, "_run_signature", None)
        if callable(signature_builder):
            final_state["graph_signature"] = signature_builder(
                selections["asset_type"]
            )

        # Keep analyst statuses evidence-derived. A non-directional fallback
        # report must remain visibly unavailable/degraded/failed at completion.
        _complete_non_analyst_agents(
            message_buffer,
            admission_blocked=_admission_was_blocked(final_state),
        )

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )
        message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

        # Update final report sections
        for section in message_buffer.report_sections:
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        display.refresh()
        current_phase = "report_writing"
        _update_run_status(artifacts, current_phase=current_phase)
        _write_run_reports(final_state, selections["ticker"], artifacts)
        checkpoint_clearer = getattr(graph, "clear_run_checkpoint", None)
        if config.get("checkpoint_enabled"):
            current_phase = "checkpoint_cleanup"
            _update_run_status(artifacts, current_phase=current_phase)
        if callable(checkpoint_clearer):
            checkpoint_clearer(
                selections["ticker"],
                selections["analysis_date"],
                selections["asset_type"],
            )
        elif config.get("checkpoint_enabled"):
            raise RuntimeError(
                "checkpointing is enabled but the graph cannot clear completed runs"
            )
        _mark_run_completed(final_state, artifacts)

    except BaseException as exc:
        _mark_run_failed(artifacts, exc, current_phase=current_phase)
        raise
    finally:
        snapshot_scope.close()
        display.close()
        message_buffer.add_message = base_add_message
        message_buffer.add_tool_call = base_add_tool_call
        message_buffer.update_report_section = base_update_report_section
        with suppress(Exception):
            runtime_writer.close()

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")
    console.print(f"[dim]{analyst_wall_time_tracker.format_summary()}[/dim]")

    # Prompt to save report
    save_choice = typer.prompt("Save report?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "Save path (press Enter for default)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")

    # Prompt to display full report
    display_choice = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.callback()
def default_analysis_command(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume. Omit to honor configuration.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running.",
    ),
) -> None:
    """Run the legacy default analysis when no subcommand is supplied."""
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints

        count = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {count} checkpoint(s).[/yellow]")
    run_analysis(checkpoint=checkpoint)


@app.command("runtime-artifacts-gc")
def runtime_artifacts_gc(
    results_root: Annotated[
        Path,
        typer.Argument(help="Results root containing runtime_artifacts and run logs."),
    ],
    delete: bool = typer.Option(
        False,
        "--delete",
        help="Delete unreferenced artifacts; the default is a dry run.",
    ),
) -> None:
    """Collect unreferenced runtime payloads outside active analyses."""
    try:
        report = collect_runtime_artifacts(results_root, dry_run=not delete)
    except ActiveRuntimeArtifactRunError as exc:
        raise typer.BadParameter(str(exc)) from exc

    mode = "DELETE" if delete else "DRY RUN"
    typer.echo(
        f"{mode} scanned={report.scanned_artifacts} "
        f"referenced={report.referenced_artifacts} "
        f"removable={report.removable_artifacts} "
        f"removed={report.removed_artifacts} "
        f"bytes={report.removable_bytes}"
    )
    for candidate in report.candidates:
        typer.echo(str(candidate))


@app.command("crypto-identity-registry-refresh")
def crypto_identity_registry_refresh(
    candidate: Annotated[
        Path,
        typer.Argument(
            help="Offline crypto registry candidate JSON to validate and publish."
        ),
    ],
    registry_path: Annotated[
        Path | None,
        typer.Option(help="Registry JSON path; defaults to the production registry."),
    ] = None,
    checksum_path: Annotated[
        Path | None,
        typer.Option(help="SHA-256 manifest path; defaults beside the registry."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(help="Environment file updated with the crypto registry pin."),
    ] = None,
    full_tests: Annotated[
        bool,
        typer.Option("--full-tests", help="Run the complete test suite after refresh."),
    ] = False,
) -> None:
    """Validate an offline crypto candidate, publish all pins, and verify."""
    from tradingagents.dataflows.identity_registry_refresh import (
        DEFAULT_CRYPTO_REGISTRY_PATH,
        DEFAULT_ENV_PATH,
        RegistryRefreshError,
        refresh_crypto_identity_registry,
    )

    resolved_registry_path = registry_path or DEFAULT_CRYPTO_REGISTRY_PATH
    try:
        result = refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=resolved_registry_path,
            checksum_path=checksum_path
            or resolved_registry_path.with_suffix(".sha256"),
            env_path=env_file or DEFAULT_ENV_PATH,
            full_tests=full_tests,
        )
    except RegistryRefreshError as exc:
        typer.echo(f"Crypto registry refresh failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Crypto registry refreshed rows={len(result.added)}")
    typer.echo(f"Registry: {result.registry_path}")
    typer.echo(f"SHA-256: {result.digest}")
    typer.echo(f"Environment: {result.env_path}")
    typer.echo("Tests passed")


@app.command("identity-registry-refresh")
def identity_registry_refresh(
    symbols: Annotated[
        list[str],
        typer.Argument(
            help=(
                "Explicit Mainland Instrument symbols such as "
                "600895.SS, 510500.SS, or 000001.SZ."
            )
        ),
    ],
    registry_path: Annotated[
        Path | None,
        typer.Option(help="Registry JSON path; defaults to the production registry."),
    ] = None,
    checksum_path: Annotated[
        Path | None,
        typer.Option(help="SHA-256 manifest path; defaults beside the registry."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(help="Environment file updated with the registry path and digest."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        typer.Option(min=1.0, help="Per-exchange request timeout."),
    ] = 20.0,
    full_tests: Annotated[
        bool,
        typer.Option("--full-tests", help="Run the complete test suite after refresh."),
    ] = False,
) -> None:
    """From a source checkout, fetch identities, rebuild, pin, and test."""
    from tradingagents.dataflows.identity_registry_refresh import (
        DEFAULT_ENV_PATH,
        DEFAULT_REGISTRY_PATH,
        REGISTRY_PATH_ENV,
        REGISTRY_SHA256_ENV,
        RegistryRefreshError,
        refresh_identity_registry,
    )

    resolved_registry_path = registry_path or DEFAULT_REGISTRY_PATH
    try:
        result = refresh_identity_registry(
            symbols,
            registry_path=resolved_registry_path,
            checksum_path=checksum_path or resolved_registry_path.with_suffix(".sha256"),
            env_path=env_file or DEFAULT_ENV_PATH,
            timeout_seconds=timeout_seconds,
            full_tests=full_tests,
        )
    except RegistryRefreshError as exc:
        typer.echo(f"Registry refresh failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        "Registry refreshed "
        f"added={len(result.added)} updated={len(result.updated)} "
        f"unchanged={len(result.unchanged)}"
    )
    typer.echo(f"Registry: {result.registry_path}")
    typer.echo(f"SHA-256: {result.digest}")
    typer.echo(f"Environment: {result.env_path}")
    typer.echo("Tests passed")

    stale_overrides: list[str] = []
    configured_path = os.environ.get(REGISTRY_PATH_ENV)
    if configured_path:
        try:
            path_is_current = Path(configured_path).resolve() == result.registry_path
        except (OSError, ValueError):
            path_is_current = False
        if not path_is_current:
            stale_overrides.append(REGISTRY_PATH_ENV)
    configured_digest = os.environ.get(REGISTRY_SHA256_ENV)
    if configured_digest and configured_digest.strip().lower() != result.digest:
        stale_overrides.append(REGISTRY_SHA256_ENV)
    if stale_overrides:
        typer.echo(
            "Warning: stale process environment overrides detected: "
            f"{', '.join(stale_overrides)}. If these variables are exported by "
            "your shell or service, unset them before the next analysis so "
            f"{result.env_path.name} can take effect.",
            err=True,
        )


@app.command()
def analyze(
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
):
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    run_analysis(checkpoint=checkpoint)


@app.command("replay-recorded")
def replay_recorded(
    fixture: Annotated[
        str,
        typer.Argument(help="Recorded fixture ticker or SHA-256 content address."),
    ],
    output_directory: Annotated[
        Path,
        typer.Option(
            "--output-directory",
            help="Directory for the deterministic audit and report.",
        ),
    ],
    manifest: Annotated[
        Path,
        typer.Option(
            "--manifest",
            help="Typed recorded-fixture expectation manifest.",
        ),
    ] = DEFAULT_RECORDED_FIXTURE_MANIFEST,
) -> None:
    """Replay a recorded run offline without providers or models."""

    fixture_key = fixture.strip()
    matches = tuple(
        candidate
        for candidate in load_recorded_run_fixtures(manifest)
        if candidate.content_sha256 == fixture_key
        or candidate.recorded.run.ticker.casefold() == fixture_key.casefold()
    )
    if len(matches) != 1:
        raise typer.BadParameter(
            "fixture must identify exactly one recorded ticker or content address"
        )
    result = replay_recorded_run(matches[0], output_directory)
    typer.echo(
        json.dumps(
            {
                "terminal_contract": result.terminal.model_dump(mode="json"),
                "blocked_stage": result.blocked_stage,
                "counters": result.counters.model_dump(mode="json"),
                "report_path": str(result.report_path),
                "audit_path": str(result.audit_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    app()
