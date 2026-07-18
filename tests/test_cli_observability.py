import gzip
import json
import re
from io import StringIO
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console
from typer.testing import CliRunner

from cli import main as cli_main
from cli.run_display import PlainRunDisplay


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


def _run_with_chunks(tmp_path, monkeypatch, chunks, *, invoke_cli=False):
    class FakeStream:
        def __init__(self):
            self.finished = False

        def stream(self, *args, **kwargs):
            yield from chunks
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

        def resolve_evidence_state(self, *args, **kwargs):
            return None

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

    if invoke_cli:
        result = CliRunner().invoke(cli_main.app, [])
        assert result.exit_code == 0, result.output
    else:
        cli_main.run_analysis()
    return display


@pytest.mark.unit
def test_run_analysis_attributes_tool_before_same_chunk_status_advance(
    tmp_path,
    monkeypatch,
):
    tool_message = AIMessage(
        content="",
        id="market-tool-message",
        tool_calls=[
            {
                "name": "get_stock_data",
                "args": {"symbol": "601658.SS"},
                "id": "market-tool-call",
                "type": "tool_call",
            }
        ],
    )
    chunks = [
        {
            "messages": [tool_message],
            "market_report": "Market report body",
        }
    ]

    class FakeStream:
        def stream(self, *args, **kwargs):
            yield from chunks

    class FakePropagator:
        def create_initial_state(self, *args, **kwargs):
            return {}

        def get_graph_args(self, *args, **kwargs):
            return {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        def resolve_instrument_context(self, *args, **kwargs):
            return "resolved identity"

        def resolve_evidence_state(self, *args, **kwargs):
            return None

    stream = StringIO()
    plain_console = Console(file=stream, force_terminal=False, color_system=None)
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda _console, buffer, _stats, _start: PlainRunDisplay(
            plain_console,
            buffer,
        ),
    )
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *args, **kwargs: "N")

    cli_main.run_analysis()

    output = stream.getvalue()
    assert "[Tool] Bull Researcher requested get_stock_data" not in output
    assert output.count("[Tool] Market Analyst requested get_stock_data") == 1


@pytest.mark.unit
def test_debate_only_chunks_emit_progress_without_finalized_report_artifacts(
    tmp_path,
    monkeypatch,
):
    debate_state = {
        "messages": [AIMessage(content="Debate activity", id=None)],
        "investment_debate_state": {
            "current_response": "Bull Analyst: upside case",
            "bull_history": "Bull Analyst: upside case",
            "bear_history": "",
            "judge_decision": "",
        },
        "risk_debate_state": {
            "latest_speaker": "Aggressive",
            "current_aggressive_response": "Aggressive Analyst: reduce risk",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "aggressive_history": "Aggressive Analyst: reduce risk",
            "conservative_history": "",
            "neutral_history": "",
            "history": "Aggressive Analyst: reduce risk",
            "judge_decision": "",
            "count": 1,
        },
    }

    display = _run_with_chunks(tmp_path, monkeypatch, [debate_state])

    assert ("Research", "Bull Researcher updated investment debate") in display.events
    assert ("Risk", "Aggressive Analyst updated risk debate") in display.events
    assert not [
        call
        for call in display.report_calls
        if call[0] in {"investment_plan", "final_trade_decision"}
    ]

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    assert not (run_dir / "reports" / "investment_plan.md").exists()
    assert not (run_dir / "reports" / "final_trade_decision.md").exists()


@pytest.mark.unit
def test_finalized_reports_are_published_exactly_before_stream_exhaustion(
    tmp_path,
    monkeypatch,
):
    finalized_state = {
        "messages": [AIMessage(content="Final decisions", id=None)],
        "investment_debate_state": {
            "current_response": "Bull Analyst: upside case",
            "bull_history": "Bull Analyst: upside case",
            "bear_history": "",
            "judge_decision": "Provisional research judge text",
        },
        "investment_plan": "**Recommendation**: Underweight",
        "risk_debate_state": {
            "latest_speaker": "Judge",
            "current_aggressive_response": "Aggressive Analyst: reduce risk",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "aggressive_history": "Aggressive Analyst: reduce risk",
            "conservative_history": "",
            "neutral_history": "",
            "history": "Aggressive Analyst: reduce risk",
            "judge_decision": "Provisional risk judge text",
            "count": 1,
        },
        "final_trade_decision": "**Rating**: Underweight\n\nReduce exposure.",
    }

    display = _run_with_chunks(tmp_path, monkeypatch, [finalized_state])

    finalized_calls = [
        call
        for call in display.report_calls
        if call[0] in {"investment_plan", "final_trade_decision"}
    ]
    assert [(call[0], call[1], call[3]) for call in finalized_calls] == [
        ("investment_plan", "**Recommendation**: Underweight", False),
        (
            "final_trade_decision",
            "**Rating**: Underweight\n\nReduce exposure.",
            False,
        ),
    ]


@pytest.mark.unit
def test_blocked_analysis_outcome_is_published_and_saved_without_decision(
    tmp_path,
    monkeypatch,
):
    outcome = (
        "**Analysis Outcome:** Insufficient Evidence\n\n"
        "No Trading Decision was issued."
    )

    display = _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {
                "messages": [],
                "market_report": "Market report body",
                "analysis_outcome": outcome,
            }
        ],
    )

    outcome_calls = [
        call for call in display.report_calls if call[0] == "analysis_outcome"
    ]
    assert [(call[1], call[3]) for call in outcome_calls] == [(outcome, False)]
    assert not [
        call for call in display.report_calls if call[0] == "final_trade_decision"
    ]
    run_dir = next(tmp_path.rglob("run_status.json")).parent
    assert (
        run_dir / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8") == outcome
    assert not (run_dir / "reports" / "5_portfolio" / "decision.md").exists()


@pytest.mark.unit
def test_blocked_cli_report_merges_streamed_state_deltas(tmp_path, monkeypatch):
    outcome = (
        "**Analysis Outcome:** Insufficient Evidence\n\n"
        "No Trading Decision was issued."
    )

    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {"messages": [], "market_report": "Market report body"},
            {"messages": [], "analysis_outcome": outcome},
        ],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    complete = (run_dir / "reports" / "complete_report.md").read_text(
        encoding="utf-8"
    )
    assert "Market report body" in complete
    assert outcome in complete


@pytest.mark.unit
def test_repeated_full_state_writes_each_finalized_report_once(tmp_path, monkeypatch):
    full_state = {
        "messages": [AIMessage(content="Market report body", id=None)],
        "market_report": "Market report body",
        "investment_debate_state": {
            "current_response": "Bull Analyst: upside case",
            "bull_history": "Bull Analyst: upside case",
            "bear_history": "Bear Analyst: downside case",
            "judge_decision": "Provisional research judge text",
        },
        "investment_plan": "**Recommendation**: Underweight",
        "trader_investment_plan": "**Action**: Sell",
        "risk_debate_state": {
            "latest_speaker": "Judge",
            "current_aggressive_response": "Aggressive Analyst: reduce risk",
            "current_conservative_response": "Conservative Analyst: exit",
            "current_neutral_response": "Neutral Analyst: wait",
            "aggressive_history": "Aggressive Analyst: reduce risk",
            "conservative_history": "Conservative Analyst: exit",
            "neutral_history": "Neutral Analyst: wait",
            "history": "Risk debate history",
            "judge_decision": "Provisional risk judge text",
            "count": 3,
        },
        "final_trade_decision": "**Rating**: Underweight\n\nReduce exposure.",
    }

    display = _run_with_chunks(tmp_path, monkeypatch, [full_state, full_state])

    report_calls = [
        (section_name, content)
        for section_name, content, _path, _finished in display.report_calls
    ]
    assert report_calls.count(("market_report", "Market report body")) == 1
    assert report_calls.count(
        ("investment_plan", "**Recommendation**: Underweight")
    ) == 1
    assert report_calls.count(("trader_investment_plan", "**Action**: Sell")) == 1
    assert report_calls.count(
        ("final_trade_decision", "**Rating**: Underweight\n\nReduce exposure.")
    ) == 1
    assert len(report_calls) == 4

    assert ("Research", "Research Manager produced investment plan") in display.events
    assert ("Portfolio", "Final decision ready: Underweight") in display.events

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    assert log_text.count("Market report body") == 1
    assert "[Research] Research Manager produced investment plan" in log_text
    assert "[Portfolio] Final decision ready: Underweight" in log_text
    assert (run_dir / "reports" / "market_report.md").read_text(
        encoding="utf-8"
    ) == "Market report body"


@pytest.mark.unit
def test_run_analysis_emits_bounded_logs_artifacts_and_runtime_metrics(
    tmp_path,
    monkeypatch,
):
    payload = {"symbol": "601658.SS", "raw_rows": ["x" * 2_000]}
    tool_message = AIMessage(
        content="",
        id="large-tool-message",
        tool_calls=[
            {
                "name": "get_stock_data",
                "args": payload,
                "id": "large-tool-call",
                "type": "tool_call",
            }
        ],
    )

    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [{"messages": [tool_message], "market_report": "Market report body"}],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    digest_match = re.search(r"artifact=sha256:([0-9a-f]{64})", log_text)
    assert digest_match is not None
    assert "x" * 1_000 not in log_text
    digest = digest_match.group(1)
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json.gz"
    )
    assert json.loads(gzip.decompress(artifact_path.read_bytes())) == payload

    metrics = json.loads(
        (run_dir / "runtime_metrics.json").read_text(encoding="utf-8")
    )
    assert "graph_stream" in metrics["durations"]["graph_phase"]
    assert "market_report" in metrics["durations"]["report"]


@pytest.mark.unit
def test_run_analysis_artifactizes_complete_tool_results(tmp_path, monkeypatch):
    tool_result = "provider rows: " + "y" * 2_000
    result_message = ToolMessage(
        content=tool_result,
        tool_call_id="large-tool-call",
        name="get_stock_data",
    )

    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [{"messages": [result_message], "market_report": "Market report body"}],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    digest_match = re.search(
        r"\[Tool Result\].*artifact=sha256:([0-9a-f]{64})",
        log_text,
    )
    assert digest_match is not None
    assert "y" * 1_000 not in log_text
    digest = digest_match.group(1)
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json.gz"
    )
    assert json.loads(gzip.decompress(artifact_path.read_bytes())) == tool_result


@pytest.mark.unit
def test_cli_without_subcommand_preserves_default_analysis(tmp_path, monkeypatch):
    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [{"messages": [], "market_report": "Market report body"}],
        invoke_cli=True,
    )

    status_path = next(tmp_path.rglob("run_status.json"))
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "completed"


@pytest.mark.unit
def test_runtime_metrics_attribute_each_post_analyst_graph_phase(
    tmp_path,
    monkeypatch,
):
    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {"messages": [], "market_report": "Market report body"},
            {
                "messages": [],
                "investment_debate_state": {
                    "current_response": "Bull Analyst: upside",
                    "bull_history": "Bull Analyst: upside",
                },
                "investment_plan": "Research plan",
            },
            {"messages": [], "trader_investment_plan": "Trading plan"},
            {
                "messages": [],
                "risk_debate_state": {
                    "latest_speaker": "Aggressive",
                    "current_aggressive_response": "Aggressive Analyst: reduce risk",
                    "aggressive_history": "Aggressive Analyst: reduce risk",
                },
            },
            {"messages": [], "final_trade_decision": "**Rating**: Hold"},
        ],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    metrics = json.loads(
        (run_dir / "runtime_metrics.json").read_text(encoding="utf-8")
    )
    assert {
        "analysis",
        "research_debate",
        "trading",
        "risk_debate",
        "portfolio_synthesis",
        "report_writing",
    } <= set(metrics["durations"]["graph_phase"])
