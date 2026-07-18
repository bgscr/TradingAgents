from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.unit
def test_callback_lifecycles_record_model_and_tool_durations(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter
    from cli.stats_handler import StatsCallbackHandler

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
    )
    clock_values = iter((10.0, 10.8, 20.0, 20.4))
    handler = StatsCallbackHandler(
        metrics_recorder=writer,
        clock=lambda: next(clock_values),
    )
    model_run_id = UUID("00000000-0000-0000-0000-000000000001")
    tool_run_id = UUID("00000000-0000-0000-0000-000000000002")

    handler.on_chat_model_start(
        {"name": "gpt-5-mini"},
        [],
        run_id=model_run_id,
    )
    handler.on_llm_end(None, run_id=model_run_id)
    handler.on_tool_start(
        {"name": "get_stock_data"},
        "NVDA",
        run_id=tool_run_id,
    )
    handler.on_tool_end("rows", run_id=tool_run_id)

    metrics = writer.get_metrics()
    assert metrics["durations"]["model"]["gpt-5-mini"][
        "total_seconds"
    ] == pytest.approx(0.8)
    assert metrics["durations"]["tool"]["get_stock_data"][
        "total_seconds"
    ] == pytest.approx(0.4)
    writer.close()
