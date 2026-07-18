from __future__ import annotations

import gzip
import hashlib
import json
import time

import pytest


@pytest.mark.unit
def test_tool_payload_is_content_addressed_deduplicated_and_previewed(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    payload = {"symbol": "000725.SZ", "rows": ["x" * 200]}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()

    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        preview_chars=40,
    )
    writer.record_tool_call("12:00:00", "get_stock_data", payload)
    writer.record_tool_call("12:00:01", "get_stock_data", payload)
    writer.close()

    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.json.gz"
    )
    assert gzip.decompress(artifact_path.read_bytes()) == canonical
    assert list((tmp_path / "runtime_artifacts").rglob("*.json.gz")) == [
        artifact_path
    ]

    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.count(f"artifact=sha256:{digest}") == 2
    assert f"bytes={len(canonical)}" in log_text
    assert "x" * 100 not in log_text
    assert writer.get_metrics()["artifacts"] == {
        "compression_operations": 1,
        "created": 1,
        "deduplicated": 1,
    }


@pytest.mark.unit
def test_flush_makes_queued_runtime_events_durable(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        batch_size=8,
        flush_interval_seconds=60,
    )

    writer.record_message("12:00:00", "Analysis", "Market report ready")
    writer.record_tool_call("12:00:01", "get_stock_data", {"symbol": "NVDA"})
    writer.flush()

    log_text = log_path.read_text(encoding="utf-8")
    assert "12:00:00 [Analysis] Market report ready" in log_text
    assert "12:00:01 [Tool Call] get_stock_data" in log_text
    assert list((tmp_path / "runtime_artifacts").rglob("*.json.gz"))
    writer.close()


@pytest.mark.unit
def test_flush_surfaces_background_failure_without_waiting_for_timeout(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[tmp_path],
        flush_interval_seconds=0,
    )
    writer.record_message("12:00:00", "Analysis", "cannot write to a directory")

    started_at = time.monotonic()
    with pytest.raises(RuntimeError, match="background writer failed"):
        writer.flush(timeout=2.0)

    assert time.monotonic() - started_at < 0.5


@pytest.mark.unit
def test_queue_saturation_is_bounded_and_reported(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        max_queue_size=1,
        batch_size=256,
        flush_interval_seconds=60,
    )

    for index in range(10_000):
        writer.record_message("12:00:00", "Analysis", f"update {index}")
    writer.flush()

    metrics = writer.get_metrics()
    assert metrics["queue_capacity"] == 1
    assert metrics["max_queue_depth"] <= 1
    assert metrics["dropped_events"] + metrics["coalesced_events"] > 0
    assert metrics["dropped_by_kind"] or metrics["coalesced_by_kind"]
    assert "[RuntimeArtifacts] queue_saturation" in log_path.read_text(
        encoding="utf-8"
    )
    writer.close()


@pytest.mark.unit
def test_final_metrics_cover_runtime_costs_and_queue_health(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    metrics_path = tmp_path / "runtime_metrics.json"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        metrics_path=metrics_path,
    )
    writer.record_duration("graph_phase", "graph_stream", 1.25)
    writer.record_duration("tool", "get_stock_data", 0.4)
    writer.record_duration("model", "gpt-5-mini", 0.8)
    writer.record_duration("report", "market_report", 0.05)
    writer.record_tool_call("12:00:00", "get_stock_data", {"rows": [1, 2, 3]})
    writer.flush()
    writer.close()

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["durations"]["graph_phase"]["graph_stream"][
        "total_seconds"
    ] == pytest.approx(1.25)
    assert metrics["durations"]["tool"]["get_stock_data"][
        "total_seconds"
    ] == pytest.approx(0.4)
    assert metrics["durations"]["model"]["gpt-5-mini"][
        "total_seconds"
    ] == pytest.approx(0.8)
    assert metrics["durations"]["report"]["market_report"][
        "total_seconds"
    ] == pytest.approx(0.05)
    assert metrics["queue"]["max_depth"] >= 1
    assert metrics["queue"]["current_depth"] == 0
    assert metrics["bytes"]["payload_uncompressed"] > 0
    assert metrics["bytes"]["payload_compressed"] > 0
    assert metrics["bytes"]["primary_logs"] > 0
    assert metrics["flush"]["count"] >= 2
    assert metrics["flush"]["max_seconds"] >= 0
    assert metrics["saturation"] == {
        "coalesced_by_kind": {},
        "coalesced_events": 0,
        "dropped_by_kind": {},
        "dropped_events": 0,
    }


@pytest.mark.unit
def test_critical_event_is_synchronous_after_pending_detail(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        flush_interval_seconds=60,
    )
    writer.record_message("12:00:00", "Analysis", "queued detail")

    writer.record_critical("12:00:01", "System", "fatal graph failure")

    assert log_path.read_text(encoding="utf-8").endswith(
        "12:00:00 [Analysis] queued detail\n"
        "12:00:01 [System] fatal graph failure\n"
    )
    writer.close()


@pytest.mark.unit
def test_phase_transition_flushes_and_records_each_graph_phase(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
        flush_interval_seconds=60,
    )
    writer.transition_phase("graph_initializing", at=10.0)
    writer.record_message("12:00:00", "System", "graph initialized")

    writer.transition_phase("graph_stream", at=12.5)

    assert "graph initialized" in log_path.read_text(encoding="utf-8")
    writer.finish_phases(at=17.0)
    metrics = writer.get_metrics()
    assert metrics["durations"]["graph_phase"]["graph_initializing"][
        "total_seconds"
    ] == pytest.approx(2.5)
    assert metrics["durations"]["graph_phase"]["graph_stream"][
        "total_seconds"
    ] == pytest.approx(4.5)
    writer.close()


@pytest.mark.unit
def test_offline_gc_dry_run_keeps_referenced_and_unreferenced_artifacts(tmp_path):
    from cli.runtime_artifacts import collect_runtime_artifacts

    referenced_digest = "a" * 64
    unreferenced_digest = "b" * 64
    artifact_dir = tmp_path / "runtime_artifacts" / "sha256"
    referenced_path = artifact_dir / "aa" / f"{referenced_digest}.json.gz"
    unreferenced_path = artifact_dir / "bb" / f"{unreferenced_digest}.json.gz"
    referenced_path.parent.mkdir(parents=True)
    unreferenced_path.parent.mkdir(parents=True)
    referenced_path.write_bytes(b"referenced")
    unreferenced_path.write_bytes(b"unreferenced")
    run_dir = tmp_path / "NVDA" / "2026-07-17" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run_status.json").write_text(
        '{"status":"completed"}',
        encoding="utf-8",
    )
    (run_dir / "message_tool.log").write_text(
        f"12:00:00 [Tool Call] x artifact=sha256:{referenced_digest}\n",
        encoding="utf-8",
    )

    report = collect_runtime_artifacts(tmp_path, dry_run=True)

    assert report.scanned_artifacts == 2
    assert report.referenced_artifacts == 1
    assert report.removable_artifacts == 1
    assert report.removed_artifacts == 0
    assert report.candidates == (unreferenced_path,)
    assert referenced_path.exists()
    assert unreferenced_path.exists()


@pytest.mark.unit
def test_offline_gc_deletes_only_unreferenced_artifacts(tmp_path):
    from cli.runtime_artifacts import collect_runtime_artifacts

    referenced_digest = "c" * 64
    unreferenced_digest = "d" * 64
    artifact_dir = tmp_path / "runtime_artifacts" / "sha256"
    referenced_path = artifact_dir / "cc" / f"{referenced_digest}.json.gz"
    unreferenced_path = artifact_dir / "dd" / f"{unreferenced_digest}.json.gz"
    referenced_path.parent.mkdir(parents=True)
    unreferenced_path.parent.mkdir(parents=True)
    referenced_path.write_bytes(b"keep")
    unreferenced_path.write_bytes(b"delete")
    run_dir = tmp_path / "NVDA" / "2026-07-17" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "message_tool.log").write_text(
        f"12:00:00 [Tool Call] x artifact=sha256:{referenced_digest}\n",
        encoding="utf-8",
    )

    report = collect_runtime_artifacts(tmp_path, dry_run=False)

    assert report.removed_artifacts == 1
    assert referenced_path.exists()
    assert not unreferenced_path.exists()


@pytest.mark.unit
def test_offline_gc_refuses_to_run_while_analysis_is_active(tmp_path):
    from cli.runtime_artifacts import (
        ActiveRuntimeArtifactRunError,
        collect_runtime_artifacts,
    )

    digest = "e" * 64
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / "ee"
        / f"{digest}.json.gz"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"candidate")
    run_dir = tmp_path / "NVDA" / "2026-07-17" / "runs" / "active-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_status.json").write_text(
        '{"status":"running"}',
        encoding="utf-8",
    )

    with pytest.raises(ActiveRuntimeArtifactRunError, match="active-run"):
        collect_runtime_artifacts(tmp_path, dry_run=False)

    assert artifact_path.exists()


@pytest.mark.unit
def test_runtime_artifact_gc_cli_defaults_to_dry_run(tmp_path):
    from typer.testing import CliRunner

    from cli.main import app

    digest = "f" * 64
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / "ff"
        / f"{digest}.json.gz"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"candidate")

    result = CliRunner().invoke(app, ["runtime-artifacts-gc", str(tmp_path)])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "scanned=1 referenced=0 removable=1 removed=0" in result.output
    assert artifact_path.exists()


@pytest.mark.unit
def test_offline_gc_honors_latest_log_references(tmp_path):
    from cli.runtime_artifacts import collect_runtime_artifacts

    digest = "1" * 64
    artifact_path = (
        tmp_path
        / "runtime_artifacts"
        / "sha256"
        / "11"
        / f"{digest}.json.gz"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"latest-reference")
    latest_log = tmp_path / "NVDA" / "2026-07-17" / "latest_message_tool.log"
    latest_log.parent.mkdir(parents=True)
    latest_log.write_text(
        f"12:00:00 [Tool Result] artifact=sha256:{digest}\n",
        encoding="utf-8",
    )

    report = collect_runtime_artifacts(tmp_path, dry_run=False)

    assert report.referenced_artifacts == 1
    assert report.removed_artifacts == 0
    assert artifact_path.exists()


@pytest.mark.unit
def test_payload_serialization_runs_off_the_caller_thread(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    class SlowSerialization:
        def __str__(self):
            time.sleep(0.25)
            return "serialized"

    log_path = tmp_path / "message_tool.log"
    log_path.write_text("run metadata\n", encoding="utf-8")
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[log_path],
    )

    started_at = time.monotonic()
    writer.record_tool_call("12:00:00", "slow_tool", {"value": SlowSerialization()})
    caller_seconds = time.monotonic() - started_at

    assert caller_seconds < 0.1
    writer.close()
