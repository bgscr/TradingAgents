from pathlib import Path

import pytest


def _selections(**overrides):
    selections = {
        "ticker": "600895.SS",
        "analysis_date": "2026-06-30",
        "asset_type": "stock",
        "china_a_enhancement_preset": "flow_sentiment",
    }
    selections.update(overrides)
    return selections


@pytest.mark.unit
def test_prepare_run_artifacts_creates_run_scoped_message_log(tmp_path, monkeypatch):
    import cli.main as m

    class FixedDateTime(m.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 30, 6, 8, 16)

    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)

    artifacts = m._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    log_file = artifacts["log_file"]
    latest_log_file = artifacts["latest_log_file"]
    report_dir = artifacts["report_dir"]

    assert log_file == tmp_path / "600895.SS" / "2026-06-30" / "runs" / "20260630_060816" / "message_tool.log"
    assert latest_log_file == tmp_path / "600895.SS" / "2026-06-30" / "latest_message_tool.log"
    assert report_dir == tmp_path / "600895.SS" / "2026-06-30" / "runs" / "20260630_060816" / "reports"
    assert log_file.exists()
    assert latest_log_file.exists()
    assert "china_a_enhancement_preset=flow_sentiment" in log_file.read_text(encoding="utf-8")
    assert latest_log_file.read_text(encoding="utf-8") == log_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_prepare_run_artifacts_resets_latest_log_for_new_run(tmp_path, monkeypatch):
    import cli.main as m

    class FirstRunDateTime(m.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 30, 6, 8, 16)

    class SecondRunDateTime(m.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 30, 6, 9, 17)

    monkeypatch.setattr(m.datetime, "datetime", FirstRunDateTime)
    first = m._prepare_run_artifacts({"results_dir": str(tmp_path)}, _selections())
    m._append_line_to_run_logs(
        [first["log_file"], first["latest_log_file"]],
        "06:08:17 [System] First run extra line\n",
    )

    monkeypatch.setattr(m.datetime, "datetime", SecondRunDateTime)
    second = m._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(china_a_enhancement_preset="announcements"),
    )
    m._append_line_to_run_logs(
        [second["log_file"], second["latest_log_file"]],
        "06:09:18 [System] Second run only\n",
    )

    first_run_text = first["log_file"].read_text(encoding="utf-8")
    second_run_text = second["log_file"].read_text(encoding="utf-8")
    latest_text = second["latest_log_file"].read_text(encoding="utf-8")

    assert "run_id=20260630_060816" in first_run_text
    assert "First run extra line" in first_run_text
    assert "run_id=20260630_060917" in second_run_text
    assert "Second run only" in second_run_text
    assert "run_id=20260630_060917" in latest_text
    assert "china_a_enhancement_preset=announcements" in latest_text
    assert "Second run only" in latest_text
    assert "First run extra line" not in latest_text
    assert latest_text == second_run_text


@pytest.mark.unit
def test_prepare_run_artifacts_defaults_preset_to_basic(tmp_path, monkeypatch):
    import cli.main as m

    class FixedDateTime(m.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 30, 6, 8, 16)

    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)

    selections = _selections()
    selections.pop("china_a_enhancement_preset")

    artifacts = m._prepare_run_artifacts({"results_dir": str(tmp_path)}, selections)

    assert "china_a_enhancement_preset=basic" in artifacts["log_file"].read_text(encoding="utf-8")
    assert "china_a_enhancement_preset=basic" in artifacts["latest_log_file"].read_text(encoding="utf-8")


@pytest.mark.unit
def test_append_line_writes_run_log_and_latest_log(tmp_path):
    import cli.main as m

    run_log = tmp_path / "runs" / "20260630_060816" / "message_tool.log"
    latest_log = tmp_path / "latest_message_tool.log"
    run_log.parent.mkdir(parents=True)
    run_log.write_text("header\n", encoding="utf-8")
    latest_log.write_text("header\n", encoding="utf-8")

    m._append_line_to_run_logs([run_log, latest_log], "06:08:17 [System] Completed\n")

    assert run_log.read_text(encoding="utf-8").endswith("06:08:17 [System] Completed\n")
    assert latest_log.read_text(encoding="utf-8") == run_log.read_text(encoding="utf-8")
