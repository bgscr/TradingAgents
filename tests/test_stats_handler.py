from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.unit
def test_callback_lifecycles_record_model_and_tool_durations(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.run_telemetry import RunTelemetryLedger

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
    )
    clock_values = iter((10.0, 10.8, 20.0, 20.4))
    telemetry = RunTelemetryLedger()
    telemetry.transition_stage("analysis")
    handler = StatsCallbackHandler(
        metrics_recorder=writer,
        telemetry_recorder=telemetry,
        clock=lambda: next(clock_values),
    )
    model_run_id = UUID("00000000-0000-0000-0000-000000000001")
    tool_run_id = UUID("00000000-0000-0000-0000-000000000002")

    handler.on_chat_model_start(
        {"name": "gpt-5-mini"},
        [],
        run_id=model_run_id,
        metadata={"langgraph_node": "market_analyst"},
    )
    handler.on_llm_end(None, run_id=model_run_id)
    handler.on_tool_start(
        {"name": "get_stock_data"},
        "NVDA",
        run_id=tool_run_id,
        metadata={"langgraph_node": "tools_market"},
    )
    handler.on_tool_end("rows", run_id=tool_run_id)

    metrics = writer.get_metrics()
    assert metrics["durations"]["model"]["gpt-5-mini"][
        "total_seconds"
    ] == pytest.approx(0.8)
    assert metrics["durations"]["tool"]["get_stock_data"][
        "total_seconds"
    ] == pytest.approx(0.4)
    stage = telemetry.finalize().stages["analysis"]
    assert stage.model_calls == 1
    assert stage.model_tokens_in == 0
    assert stage.model_tokens_out == 0
    assert stage.tool_calls == 1
    writer.close()


@pytest.mark.unit
def test_run_telemetry_records_callback_activity_against_its_captured_stage():
    from tradingagents.run_telemetry import RunTelemetryLedger

    ledger = RunTelemetryLedger()
    ledger.transition_stage("analysis")

    ledger.record_model_activity(
        stage="trading",
        seconds=0.5,
        tokens_in=10,
        tokens_out=2,
    )
    ledger.record_tool_activity(seconds=0.25)

    projection = ledger.finalize()
    assert projection.stages["trading"].model_calls == 1
    assert projection.stages["analysis"].tool_calls == 1


@pytest.mark.unit
def test_cli_callback_captures_each_graph_stage_at_operation_start():
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.run_telemetry import RunTelemetryLedger

    clock_values = iter((10.0, 10.5, 20.0, 20.75))
    ledger = RunTelemetryLedger()
    handler = StatsCallbackHandler(
        telemetry_recorder=ledger,
        clock=lambda: next(clock_values),
    )
    analysis_run_id = UUID("00000000-0000-0000-0000-000000000003")
    research_run_id = UUID("00000000-0000-0000-0000-000000000004")

    handler.on_chat_model_start(
        {"name": "analysis-model"},
        [],
        run_id=analysis_run_id,
        metadata={"langgraph_node": "market_analyst"},
    )
    ledger.transition_stage("research_debate")
    handler.on_llm_end(None, run_id=analysis_run_id)
    handler.on_chat_model_start(
        {"name": "research-model"},
        [],
        run_id=research_run_id,
        metadata={"langgraph_node": "bull_researcher"},
    )
    ledger.transition_stage("trading")
    handler.on_llm_end(None, run_id=research_run_id)

    stages = ledger.finalize().stages
    assert stages["analysis"].model_calls == 1
    assert stages["research_debate"].model_calls == 1
    assert "trading" not in stages
