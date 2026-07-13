from typer.testing import CliRunner

from tradingagents.picker import cli
from tradingagents.picker.errors import PITError

runner = CliRunner()


def test_backfill_reports_summary_without_printing_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")
    monkeypatch.setattr(cli, "run_backfill", lambda **kwargs: (2, 3, 0, "run-1"))
    result = runner.invoke(
        cli.app,
        [
            "backfill",
            "--start-date",
            "20260709",
            "--end-date",
            "20260710",
            "--cache-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "completed=2 skipped=3 failed=0 run_id=run-1" in result.stdout
    assert "secret-token" not in result.stdout


def test_snapshot_prints_machine_readable_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "snapshot_summary",
        lambda **kwargs: {
            "as_of": "20260710",
            "active": 5000,
            "eligible": 1200,
            "coverage": 0.99,
        },
    )
    result = runner.invoke(
        cli.app, ["snapshot", "--date", "20260710", "--cache-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert '"coverage": 0.99' in result.stdout


def test_backfill_reports_pit_error_without_printing_token(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")

    def fail(**kwargs):
        raise PITError("partition failed with token secret-token")

    monkeypatch.setattr(cli, "run_backfill", fail)
    result = runner.invoke(
        cli.app,
        ["backfill", "--start-date", "20260709", "--end-date", "20260710"],
    )
    assert result.exit_code == 1
    assert "PIT backfill failed: partition failed with token <redacted>" in result.stderr
    assert "secret-token" not in result.output


def test_snapshot_reports_pit_error_concisely(monkeypatch):
    def fail(**kwargs):
        raise PITError("required cached partition daily/20260710 is unavailable")

    monkeypatch.setattr(cli, "snapshot_summary", fail)
    result = runner.invoke(cli.app, ["snapshot", "--date", "20260710"])
    assert result.exit_code == 1
    assert (
        "PIT snapshot failed: required cached partition daily/20260710 is unavailable"
        in result.stderr
    )
