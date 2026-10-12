from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import cli.main as cli_main
import tradingagents.market_history.bootstrap as bootstrap_module
from cli.main import app
from tradingagents.dataflows.baostock_calendar import (
    BaoStockSessionCalendarAcquisitionError,
    BaoStockSessionCalendarFailureReason,
    acquire_mainland_session_calendar as acquire_with_fake_transport,
)
from tradingagents.market_history.bootstrap import (
    MarketSessionCalendarBootstrapReceipt,
)


def _configure_disposable_history(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "history"
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_database_path",
        str(root / "market_history.sqlite3"),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_payload_root",
        str(root / "payloads"),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_backup_root",
        str(root / "backups"),
    )
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "market_history_mode", "shadow")
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "data_usage_mode",
        "personal_research",
    )


def _receipt() -> MarketSessionCalendarBootstrapReceipt:
    return MarketSessionCalendarBootstrapReceipt(
        provider="baostock",
        endpoint="query_trade_dates",
        upstream_service_id="upstream-service=sha256:"
        "2683441e7396f427de192df540ed017fd75dae048a365af1da3344f9aaf789f4",
        upstream_service_name="BaoStock TCP service",
        provider_dataset_id="provider-dataset=sha256:"
        "b9cbc86198c8e47afd07ce8ce8505718cca4739ff584298f056f794bd1d62d2d",
        dataset_name="mainland-session-calendar-v1",
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provenance="observed_point_in_time",
        requested_start_date=date(2026, 8, 10),
        requested_end_date=date(2026, 8, 12),
        evaluated_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
        calendar_revision_id="market-session-calendar=sha256:" + "3" * 64,
        calendar_refresh_ingestion_id="ingestion-run=sha256:" + "4" * 64,
        retrieved_at=None,
        observed_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
        published_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
        canonical_content_digest="5" * 64,
        normalizer_version="baostock-session-calendar-normalizer-v1",
        payload_contract="mainland-session-calendar-payload-v1",
        readiness_contract="mainland-session-calendar-readiness-v1",
        session_time_contract="mainland-cn-regular-session-times-v1",
        total_session_count=3,
        open_session_count=2,
        closed_session_count=1,
        latest_completed_session=date(2026, 8, 10),
        latest_completed_session_close=datetime(
            2026, 8, 10, 7, 0, tzinfo=timezone.utc
        ),
        next_open_session=date(2026, 8, 12),
        next_open_session_close=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        logical_request_id=None,
        workflow_sequence_id=None,
        physical_attempts=(),
        logical_request_count=0,
        physical_attempt_count=0,
        provider_io_performed=False,
        disposition="reused",
    )


class _ProviderResult:
    def __init__(self, rows: tuple[tuple[object, ...], ...] = ()) -> None:
        self.error_code = "0"
        self.error_msg = ""
        self.fields = ("calendar_date", "is_trading_day")
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[object]:
        return list(self._rows[self._index])


class _FakeBaoStockTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def login(self) -> object:
        self.calls.append(("login",))
        return _ProviderResult()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
        self.calls.append(("query_trade_dates", start_date, end_date))
        return _ProviderResult(
            (
                ("2026-08-10", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            )
        )

    def logout(self) -> object:
        self.calls.append(("logout",))
        return _ProviderResult()


@pytest.mark.unit
def test_market_session_calendar_bootstrap_cli_calls_shared_service_and_emits_one_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_disposable_history(tmp_path, monkeypatch)
    calls: list[tuple[object, object, object]] = []

    def bootstrap(request, *, history_config, clock):
        calls.append((request, history_config, clock))
        return _receipt()

    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap_mainland_session_calendar",
        bootstrap,
    )

    result = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
            "--authorize-provider-io",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    request, history_config, clock = calls[0]
    assert request.contract_version == "market-session-calendar-bootstrap-request-v1"
    assert request.start_date == date(2026, 8, 10)
    assert request.end_date == date(2026, 8, 12)
    assert request.evaluated_at == datetime(
        2026, 8, 11, 16, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert request.authorize_provider_io is True
    assert request.force_refresh is False
    assert history_config.database_path.name == "market_history.sqlite3"
    assert clock.now().tzinfo is not None
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0] == json.dumps(
        _receipt().model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.unit
def test_market_session_calendar_bootstrap_cli_real_service_created_then_reused(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_disposable_history(tmp_path, monkeypatch)
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    base_args = [
        "market-session-calendar-bootstrap",
        "--reference-market",
        "mainland-cn",
        "--provider",
        "baostock",
        "--start-date",
        "2026-08-10",
        "--end-date",
        "2026-08-12",
        "--evaluated-at",
        "2026-08-11T16:00:00+08:00",
    ]

    created_result = CliRunner().invoke(app, [*base_args, "--authorize-provider-io"])
    reused_result = CliRunner().invoke(app, base_args)

    assert created_result.exit_code == 0, created_result.output
    assert reused_result.exit_code == 0, reused_result.output
    assert len(created_result.stdout.splitlines()) == 1
    assert len(reused_result.stdout.splitlines()) == 1
    created = json.loads(created_result.stdout)
    reused = json.loads(reused_result.stdout)
    assert created["disposition"] == "created"
    assert created["provider_io_performed"] is True
    assert reused["disposition"] == "reused"
    assert reused["provider_io_performed"] is False
    assert reused["calendar_revision_id"] == created["calendar_revision_id"]
    assert reused["physical_attempt_count"] == 0
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]


@pytest.mark.unit
def test_market_session_calendar_bootstrap_cli_emits_safe_typed_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_disposable_history(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap_mainland_session_calendar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BaoStockSessionCalendarAcquisitionError(
                BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED
            )
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "contract_version": "market-session-calendar-bootstrap-error-v1",
        "diagnostic_code": "operator_authorization_required",
        "failure_category": "request",
        "physical_attempt_count": 0,
        "retryable": False,
    }


@pytest.mark.unit
def test_market_session_calendar_bootstrap_cli_sanitizes_unexpected_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_disposable_history(tmp_path, monkeypatch)
    forbidden = "C:\\Users\\operator\\secret-token raw provider body"
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap_mainland_session_calendar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(forbidden)),
    )

    result = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
            "--authorize-provider-io",
        ],
    )

    assert result.exit_code == 5
    assert json.loads(result.stdout) == {
        "contract_version": "market-session-calendar-bootstrap-error-v1",
        "diagnostic_code": "calendar_bootstrap_failed",
        "failure_category": "internal",
    }
    assert forbidden not in result.output
    assert "Users" not in result.output
    assert "provider body" not in result.output


@pytest.mark.unit
def test_market_session_calendar_bootstrap_is_separate_from_analysis_options() -> None:
    analyze_help = CliRunner().invoke(app, ["analyze", "--help"])
    bootstrap_help = CliRunner().invoke(
        app,
        ["market-session-calendar-bootstrap", "--help"],
    )

    assert analyze_help.exit_code == 0, analyze_help.output
    assert bootstrap_help.exit_code == 0, bootstrap_help.output
    from rich.text import Text

    analyze_text = Text.from_ansi(analyze_help.output).plain
    bootstrap_text = Text.from_ansi(bootstrap_help.output).plain
    assert "--authorize-provider-io" not in analyze_text
    assert "--force-refresh" not in analyze_text
    assert "--bootstrap-if-missing" not in analyze_text
    assert "--authorize-provider-io" in bootstrap_text
    assert "--force-refresh" in bootstrap_text
    assert "symbol" not in bootstrap_text.casefold()
    assert "checkpoint" not in bootstrap_text.casefold()


@pytest.mark.unit
def test_market_session_calendar_bootstrap_cli_constructs_no_analysis_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_disposable_history(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_main,
        "run_analysis",
        lambda *_args, **_kwargs: pytest.fail("maintenance must not run analysis"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap_mainland_session_calendar",
        lambda *_args, **_kwargs: _receipt(),
    )

    result = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
