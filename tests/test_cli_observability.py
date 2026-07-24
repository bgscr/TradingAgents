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
from tradingagents.evidence import (
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    render_analysis_outcome,
)
from tradingagents.reporting import write_report_tree

_INSUFFICIENT_OUTCOME_MODEL = AnalysisOutcome(
    readiness=EvidenceReadiness.INSUFFICIENT,
    reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
)
_INSUFFICIENT_OUTCOME = render_analysis_outcome(_INSUFFICIENT_OUTCOME_MODEL)
_INSUFFICIENT_OUTCOME_CONTRACT = _INSUFFICIENT_OUTCOME_MODEL.model_dump(mode="json")


@pytest.mark.unit
def test_message_buffer_reports_whether_section_content_changed():
    buffer = cli_main.MessageBuffer()
    buffer.init_for_analysis(["market"])

    assert buffer.update_report_section("market_report", "first") is True
    assert buffer.update_report_section("market_report", "first") is False
    assert buffer.update_report_section("market_report", "second") is True


@pytest.mark.unit
def test_message_buffer_labels_model_authored_plans_as_advisory_commentary():
    buffer = cli_main.MessageBuffer()
    buffer.init_for_analysis(["market"])

    buffer.update_report_section("investment_plan", "Research prose")
    buffer.update_report_section(
        "trader_investment_plan",
        "FINAL TRANSACTION PROPOSAL: **SELL**",
    )

    assert buffer.current_report == (
        "### Trader Advisory Commentary\n"
        "FINAL TRANSACTION PROPOSAL: **SELL**"
    )
    assert "## Research Advisory Commentary\n\nResearch prose" in buffer.final_report
    assert (
        "## Trader Advisory Commentary\n\n"
        "FINAL TRANSACTION PROPOSAL: **SELL**"
    ) in buffer.final_report


@pytest.mark.unit
@pytest.mark.parametrize(
    "runtime_graph_phase",
    [
        "research_debate",
        "trading",
        "risk_debate",
        "portfolio_synthesis",
    ],
)
def test_operator_message_projection_labels_model_prose_as_advisory_commentary(
    runtime_graph_phase,
):
    message_type, content = cli_main.classify_message_type(
        AIMessage(content="Model-authored prose"),
        runtime_graph_phase=runtime_graph_phase,
    )

    assert message_type == "Advisory Commentary"
    assert content == "Model-authored prose"


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


def _run_with_chunks(
    tmp_path,
    monkeypatch,
    chunks,
    *,
    invoke_cli=False,
    evidence_gate_mode="enforce",
    initial_state=None,
    callback_script=None,
):
    class FakeStream:
        def __init__(self):
            self.finished = False
            self.callbacks = ()

        def stream(self, *args, **kwargs):
            if callback_script is not None:
                callback_script(self.callbacks)
            yield from chunks
            self.finished = True

    fake_stream = FakeStream()

    class FakePropagator:
        def create_initial_state(self, *args, **kwargs):
            if initial_state is not None:
                return dict(initial_state)
            return {
                "company_of_interest": args[0],
                "trade_date": args[1],
                "asset_type": kwargs["asset_type"],
                "evidence_state": EvidenceState().model_dump(mode="json"),
                "messages": [],
            }

        def get_graph_args(self, *args, **kwargs):
            return {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = fake_stream
            fake_stream.callbacks = tuple(kwargs.get("callbacks", ()))

        def create_initial_state(self, *args, **kwargs):
            return self.propagator.create_initial_state(*args, **kwargs)

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
        dict(
            cli_main.DEFAULT_CONFIG,
            results_dir=str(tmp_path),
            evidence_gate_mode=evidence_gate_mode,
        ),
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
def test_cli_terminal_audit_matches_equivalent_programmatic_publication(
    tmp_path,
    monkeypatch,
):
    from tradingagents.terminal_contract import apply_terminal_contract

    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
    )
    rendered_outcome = render_analysis_outcome(outcome)
    initial_state = {
        "company_of_interest": "601658.SS",
        "ticker": "601658.SS",
        "trade_date": "2026-07-09",
        "asset_type": "stock",
        "evidence_state": EvidenceState().model_dump(mode="json"),
        "decision_audit_created_at": "2026-07-09T00:00:00Z",
    }
    terminal_chunk = {
        "messages": [],
        "analysis_outcome": rendered_outcome,
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
    }

    _run_with_chunks(
        tmp_path / "cli",
        monkeypatch,
        [terminal_chunk],
        initial_state=initial_state,
    )
    cli_audit_path = next((tmp_path / "cli").rglob("decision-audit.json"))
    cli_audit = json.loads(cli_audit_path.read_text(encoding="utf-8"))

    programmatic_state = {**initial_state, **terminal_chunk}
    programmatic_state.update(
        {
            "evidence_gate_mode": "enforce",
            "configuration_digest": cli_audit["run"]["configuration_digest"],
        }
    )
    apply_terminal_contract(programmatic_state)
    write_report_tree(
        programmatic_state,
        "601658.SS",
        tmp_path / "programmatic",
    )
    programmatic_audit_path = next(
        (tmp_path / "programmatic").rglob("decision-audit.json")
    )
    programmatic_audit = json.loads(
        programmatic_audit_path.read_text(encoding="utf-8")
    )

    assert cli_audit["terminal"] == programmatic_audit["terminal"]
    assert cli_audit["run"] == programmatic_audit["run"]


@pytest.mark.unit
@pytest.mark.parametrize("evidence_gate_mode", ("enforce", "shadow"))
def test_cli_audit_preserves_configured_evidence_gate_mode(
    tmp_path,
    monkeypatch,
    evidence_gate_mode,
):
    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [{
            "messages": [],
            "analysis_outcome": _INSUFFICIENT_OUTCOME,
            "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
        }],
        evidence_gate_mode=evidence_gate_mode,
    )

    audit_path = next(tmp_path.rglob("decision-audit.json"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["run"]["evidence_gate_mode"] == evidence_gate_mode


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
            "analysis_outcome": _INSUFFICIENT_OUTCOME,
            "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
        }
    ]

    class FakeStream:
        def stream(self, *args, **kwargs):
            yield from chunks

    class FakePropagator:
        def create_initial_state(self, company_name, trade_date, **kwargs):
            return {
                "messages": [("human", company_name)],
                "company_of_interest": company_name,
                "trade_date": str(trade_date),
                "asset_type": kwargs.get("asset_type", "stock"),
                "evidence_state": EvidenceState().model_dump(mode="json"),
            }

        def get_graph_args(self, *args, **kwargs):
            return {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        def create_initial_state(self, *args, **kwargs):
            return self.propagator.create_initial_state(*args, **kwargs)

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

    display = _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            debate_state,
            {
                "messages": [],
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            },
        ],
    )

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
def test_trader_proposal_is_logged_as_advisory_commentary_without_rewriting_raw_text(
    tmp_path,
    monkeypatch,
):
    advisory = "FINAL TRANSACTION PROPOSAL: **SELL**"
    display = _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {
                "messages": [AIMessage(content=advisory, id="trader-advisory")],
                "trader_investment_plan": advisory,
            },
            {
                "messages": [],
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            },
        ],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    raw_projections = [
        content
        for section_name, content, _path, _finished in display.report_calls
        if section_name == "trader_investment_plan"
    ]

    assert f"[Advisory Commentary] {advisory}" in log_text
    assert f"[Agent] {advisory}" not in log_text
    assert raw_projections == [advisory]
    artifact_match = re.search(
        rf"\[Advisory Commentary\] {re.escape(advisory)} "
        r"artifact=sha256:([0-9a-f]{64})",
        log_text,
    )
    assert artifact_match is not None
    digest = artifact_match.group(1)
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json.gz"
    )
    assert json.loads(gzip.decompress(artifact_path.read_bytes())) == advisory


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
        "analysis_outcome": _INSUFFICIENT_OUTCOME,
        "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
    }

    display = _run_with_chunks(tmp_path, monkeypatch, [finalized_state])

    finalized_calls = [
        call
        for call in display.report_calls
        if call[0] in {"investment_plan", "analysis_outcome"}
    ]
    assert [(call[0], call[1], call[3]) for call in finalized_calls] == [
        ("investment_plan", "**Recommendation**: Underweight", False),
        ("analysis_outcome", _INSUFFICIENT_OUTCOME, False),
    ]


@pytest.mark.unit
def test_blocked_analysis_outcome_is_published_and_saved_without_decision(
    tmp_path,
    monkeypatch,
):
    outcome = _INSUFFICIENT_OUTCOME

    display = _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {
                "messages": [],
                "market_report": "Market report body",
                "analysis_outcome": outcome,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
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
    saved_outcome = (
        run_dir / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")
    assert outcome in saved_outcome
    assert "Source Availability Coverage" in saved_outcome
    assert "Validated Fact Coverage" in saved_outcome
    assert "Decision Assertion Coverage" in saved_outcome
    assert not (run_dir / "reports" / "5_portfolio" / "decision.md").exists()


@pytest.mark.unit
def test_blocked_cli_report_enforces_boundary_across_streamed_state_deltas(
    tmp_path,
    monkeypatch,
):
    outcome = _INSUFFICIENT_OUTCOME

    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [
            {"messages": [], "market_report": "Market report body"},
            {
                "messages": [],
                "analysis_outcome": outcome,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            },
        ],
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    complete = (run_dir / "reports" / "complete_report.md").read_text(
        encoding="utf-8"
    )
    assert "Market report body" not in complete
    assert outcome in complete
    assert not (run_dir / "reports" / "1_analysts").exists()
    assert not (run_dir / "reports" / "market_report.md").exists()


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
        "analysis_outcome": _INSUFFICIENT_OUTCOME,
        "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
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
    assert report_calls.count(("analysis_outcome", _INSUFFICIENT_OUTCOME)) == 1
    assert len(report_calls) == 4

    assert ("Research", "Research Manager produced investment plan") in display.events
    assert (
        "Portfolio",
        "Analysis completed without a Trading Decision",
    ) in display.events

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    log_text = (run_dir / "message_tool.log").read_text(encoding="utf-8")
    assert log_text.count("Market report body") == 1
    assert "[Research] Research Manager produced investment plan" in log_text
    assert "[Portfolio] Analysis completed without a Trading Decision" in log_text
    assert not (run_dir / "reports" / "market_report.md").exists()


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
        [
            {
                "messages": [tool_message],
                "market_report": "Market report body",
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            }
        ],
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
def test_run_analysis_reconciles_circuit_rejection_between_audit_and_metrics(
    tmp_path,
    monkeypatch,
):
    from tradingagents.dataflows.acquisition import (
        AcquisitionController,
        AcquisitionFailure,
        AcquisitionRequest,
    )
    from tradingagents.dataflows.market_snapshot import (
        get_active_acquisition_controller,
    )
    from tradingagents.evidence import AcquisitionUnavailableReason

    def unavailable_news(_request):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
        )

    def run_chunks():
        controller = get_active_acquisition_controller()
        assert isinstance(controller, AcquisitionController)
        providers = (("news-primary", unavailable_news),)
        for tool_call_id in ("news-call-1", "news-call-2"):
            controller.acquire(
                AcquisitionRequest(
                    capability="instrument_news",
                    source_ref="news:601658.SS",
                    tool_call_id=tool_call_id,
                    tool_name="get_news",
                ),
                validator=lambda value: value,
                serializer=str,
                providers=providers,
            )
        yield {
            "messages": [],
            "analysis_outcome": _INSUFFICIENT_OUTCOME,
            "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
        }

    _run_with_chunks(tmp_path, monkeypatch, run_chunks())

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    audit = json.loads(
        (run_dir / "reports" / "decision-audit.json").read_text(encoding="utf-8")
    )
    metrics = json.loads(
        (run_dir / "runtime_metrics.json").read_text(encoding="utf-8")
    )
    expected_summary = {
        "attempts": 2,
        "available": 0,
        "unavailable": 2,
        "retryable_unavailable": 1,
        "retry_events": 0,
        "circuit_breaker_events": 1,
        "unavailable_reasons": {
            "circuit_open": 1,
            "provider_error": 1,
        },
    }

    assert audit["schema_version"] == "4.0"
    assert audit["telemetry"]["acquisition"]["summary"] == expected_summary
    assert [
        event["outcome"]["reason"]
        for event in audit["telemetry"]["acquisition"]["events"]
    ] == ["provider_error", "circuit_open"]
    assert metrics["terminal"]["acquisition"] == expected_summary


@pytest.mark.unit
def test_run_analysis_reconciles_stage_usage_between_audit_and_metrics(
    tmp_path,
    monkeypatch,
):
    from uuid import UUID

    from langchain_core.outputs import ChatGeneration, LLMResult

    def emit_model_and_tool_usage(callbacks):
        handler = callbacks[0]
        model_run_id = UUID("00000000-0000-0000-0000-000000000001")
        tool_run_id = UUID("00000000-0000-0000-0000-000000000002")
        handler.on_chat_model_start(
            {"name": "test-model"},
            [[]],
            run_id=model_run_id,
            metadata={"langgraph_node": "market_analyst"},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[[
                    ChatGeneration(
                        message=AIMessage(
                            content="analysis",
                            usage_metadata={
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "total_tokens": 120,
                            },
                        )
                    )
                ]]
            ),
            run_id=model_run_id,
        )
        handler.on_tool_start(
            {"name": "get_news"},
            "{}",
            run_id=tool_run_id,
            metadata={"langgraph_node": "tools_news"},
        )
        handler.on_tool_end("news", run_id=tool_run_id)

    _run_with_chunks(
        tmp_path,
        monkeypatch,
        [{
            "messages": [],
            "analysis_outcome": _INSUFFICIENT_OUTCOME,
            "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
        }],
        callback_script=emit_model_and_tool_usage,
    )

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    audit = json.loads(
        (run_dir / "reports" / "decision-audit.json").read_text(encoding="utf-8")
    )
    metrics = json.loads(
        (run_dir / "runtime_metrics.json").read_text(encoding="utf-8")
    )
    stage = audit["telemetry"]["stages"]["analysis"]

    assert stage["model_calls"] == 1
    assert stage["model_tokens_in"] == 100
    assert stage["model_tokens_out"] == 20
    assert stage["model_seconds"] >= 0
    assert stage["tool_calls"] == 1
    assert stage["tool_seconds"] >= 0
    assert stage["cost"] == {"available": False, "amount_usd": None}
    assert metrics["stage_activity"]["analysis"] == stage
    assert metrics["terminal"]["terminal_route"] == audit["telemetry"]["terminal_route"]


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
        [
            {
                "messages": [result_message],
                "market_report": "Market report body",
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            }
        ],
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
        [
            {
                "messages": [],
                "market_report": "Market report body",
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            }
        ],
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
            {
                "messages": [],
                "analysis_outcome": _INSUFFICIENT_OUTCOME,
                "analysis_outcome_contract": _INSUFFICIENT_OUTCOME_CONTRACT,
            },
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
