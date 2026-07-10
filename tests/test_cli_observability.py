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
