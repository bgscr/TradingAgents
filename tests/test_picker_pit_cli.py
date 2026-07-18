import json
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tradingagents.picker import cli
from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITError
from tradingagents.picker.pit_models import Dataset, PartitionKey

runner = CliRunner()


def test_verify_command_runs_explicit_integrity_audit(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache = PITCache(tmp_path)
    cache.mark_pending(key)
    cache.close()

    result = runner.invoke(cli.app, ["verify", "--cache-dir", str(tmp_path)])

    assert result.exit_code == 1
    summary = json.loads(result.stdout)
    assert summary["partitions"] == 1
    assert summary["failed"] == 1
    assert "status is pending" in summary["failures"][key.storage_key][0]


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


def test_backfill_forwards_all_cli_options(monkeypatch, tmp_path):
    captured = {}

    def run_backfill(**kwargs):
        captured.update(kwargs)
        return 1, 2, 0, "run-options"

    monkeypatch.setattr(cli, "run_backfill", run_backfill)
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
            "--calls-per-minute",
            "17",
            "--refresh",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "start_date": "20260709",
        "end_date": "20260710",
        "cache_dir": tmp_path,
        "calls_per_minute": 17,
        "refresh": True,
    }


def test_run_backfill_composes_provider_limiters_probe_and_ingestion(
    monkeypatch, tmp_path
):
    events = []
    rate_requests = []
    created_rates = []
    provider = object()
    cache = object()
    retry_policy = object()

    class Config:
        cache_dir = tmp_path

        @staticmethod
        def rate_for(endpoint):
            rate_requests.append(endpoint)
            return {"daily": 17, "daily_basic": 9}[endpoint]

    config = Config()

    def from_env(cache_dir, calls_per_minute):
        assert cache_dir == tmp_path
        assert calls_per_minute == 23
        return config

    def create_provider(actual_config):
        assert actual_config is config
        return provider

    def create_cache(root):
        assert root == tmp_path
        return cache

    class FakeLimiter:
        def __init__(self, calls_per_minute):
            self.calls_per_minute = calls_per_minute
            created_rates.append(calls_per_minute)

    class FakeIngestor:
        def __init__(self, actual_provider, actual_cache, limiter_for, retry):
            assert actual_provider is provider
            assert actual_cache is cache
            assert retry is retry_policy
            daily = limiter_for("daily")
            assert limiter_for("daily") is daily
            daily_basic = limiter_for("daily_basic")
            assert daily is not daily_basic
            assert daily.calls_per_minute == 17
            assert daily_basic.calls_per_minute == 9

        def probe(self):
            events.append("probe")

        def ingest(self, start_date, end_date, refresh):
            events.append(("ingest", start_date, end_date, refresh))
            return SimpleNamespace(completed=4, skipped=5, failed=0, run_id="run-wiring")

    monkeypatch.setattr(cli.PITConfig, "from_env", from_env)
    monkeypatch.setattr(cli.TushareProvider, "create", create_provider)
    monkeypatch.setattr(cli, "PITCache", create_cache)
    monkeypatch.setattr(cli, "TokenBucketLimiter", FakeLimiter)
    monkeypatch.setattr(cli, "RetryPolicy", lambda: retry_policy)
    monkeypatch.setattr(cli, "PITIngestor", FakeIngestor)

    result = cli.run_backfill(
        start_date="20260709",
        end_date="20260710",
        cache_dir=tmp_path,
        calls_per_minute=23,
        refresh=True,
    )

    assert result == (4, 5, 0, "run-wiring")
    assert rate_requests == ["daily", "daily_basic"]
    assert created_rates == [17, 9]
    assert events == [
        "probe",
        ("ingest", "20260709", "20260710", True),
    ]


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("2026710", "20260710"),
        ("20260229", "20260710"),
        ("20260711", "20260710"),
    ],
)
def test_invalid_backfill_dates_never_create_or_probe_provider(
    monkeypatch, tmp_path, start_date, end_date
):
    events = []
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")

    class UnexpectedProvider:
        def probe(self):
            events.append("probe")

    def create_provider(config):
        events.append("create")
        return UnexpectedProvider()

    monkeypatch.setattr(cli.TushareProvider, "create", create_provider)
    result = runner.invoke(
        cli.app,
        [
            "backfill",
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--cache-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "PIT backfill failed:" in result.stderr
    assert events == []
    assert "secret-token" not in result.output


def test_snapshot_prints_machine_readable_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "snapshot_summary",
        lambda **kwargs: {
            "as_of": "20260710",
            "active": 5000,
            "eligible": 1200,
            "coverage": 0.99,
            "warnings": ["partial suspension data"],
        },
    )
    result = runner.invoke(
        cli.app, ["snapshot", "--date", "20260710", "--cache-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert result.stdout == (
        '{"active": 5000, "as_of": "20260710", "coverage": 0.99, '
        '"eligible": 1200, "warnings": ["partial suspension data"]}\n'
    )


@pytest.mark.parametrize("value", ["2026710", "20260229", "2026-07-10"])
def test_snapshot_command_reports_invalid_date_concisely(tmp_path, value):
    result = runner.invoke(
        cli.app, ["snapshot", "--date", value, "--cache-dir", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "PIT snapshot failed:" in result.stderr
    assert "YYYYMMDD" in result.stderr


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


@pytest.mark.parametrize(
    ("command", "target"),
    [
        (
            ["backfill", "--start-date", "20260709", "--end-date", "20260710"],
            "run_backfill",
        ),
        (["snapshot", "--date", "20260710"], "snapshot_summary"),
    ],
)
def test_pit_error_traceback_does_not_retain_token(monkeypatch, command, target):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")

    def fail(**kwargs):
        raise PITError("provider rejected secret-token")

    monkeypatch.setattr(cli, target, fail)
    result = runner.invoke(cli.app, command)

    assert result.exit_code == 1
    assert "provider rejected <redacted>" in result.stderr
    formatted = "".join(traceback.format_exception(result.exception))
    assert "secret-token" not in formatted


def test_operator_guide_names_all_deferred_phase_categories():
    guide = (
        Path(__file__).parents[1] / "docs" / "pit-data-foundation.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(guide.split())

    for category in (
        "industry history",
        "money flow",
        "price limits",
        "dividends and broader corporate actions",
        "ranking",
        "execution",
        "walk-forward evaluation",
    ):
        assert category in normalized
