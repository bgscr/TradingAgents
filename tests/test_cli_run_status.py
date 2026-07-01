import json

import pytest

from cli import main as cli_main


def _selections():
    return {
        "ticker": "688519.SS",
        "analysis_date": "2026-07-01",
        "asset_type": "stock",
        "analysts": ["market", "social"],
        "china_a_enhancement_preset": "all",
    }


@pytest.mark.unit
def test_prepare_run_artifacts_writes_running_status(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    status_path = artifacts["status_file"]
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["ticker"] == "688519.SS"
    assert payload["analysis_date"] == "2026-07-01"
    assert payload["selected_analysts"] == ["market", "social"]
    assert payload["status"] == "running"
    assert payload["current_phase"] == "artifacts_prepared"
    assert payload["completed_at"] is None
    assert payload["error_summary"] is None
    assert payload["reports_written"] == []


@pytest.mark.unit
def test_update_run_status_marks_completed(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._update_run_status(
        artifacts,
        status="completed",
        current_phase="report_writing",
        reports_written=["reports/complete_report.md"],
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["current_phase"] == "report_writing"
    assert payload["completed_at"] is not None
    assert payload["reports_written"] == ["reports/complete_report.md"]


@pytest.mark.unit
def test_mark_run_failed_records_error_summary(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._mark_run_failed(
        artifacts,
        RuntimeError("stream stopped"),
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_phase"] == "graph_stream"
    assert payload["completed_at"] is None
    assert payload["error_summary"] == "RuntimeError: stream stopped"
