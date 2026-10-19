import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

import cli.main as cli_main
from cli.announcements import fetch_announcements
from cli.config import announcements_enabled


def _forbid_announcement_network_construction(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    def unexpected_network_construction(*_args, **_kwargs):
        pytest.fail(message)

    monkeypatch.setattr(requests, "get", unexpected_network_construction)
    monkeypatch.setattr(requests.sessions, "Session", unexpected_network_construction)
    monkeypatch.setattr(socket, "socket", unexpected_network_construction)
    monkeypatch.setattr(socket, "create_connection", unexpected_network_construction)


@pytest.mark.unit
@pytest.mark.parametrize("value", (None, "", "  "))
def test_announcements_are_enabled_when_startup_setting_is_unset_or_empty(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", value)

    assert announcements_enabled() is True


@pytest.mark.unit
def test_disabled_announcements_return_empty_without_network_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    _forbid_announcement_network_construction(
        monkeypatch,
        "disabled CLI announcements attempted network construction",
    )

    assert fetch_announcements() == {
        "announcements": [],
        "require_attention": False,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (" true ", True),
        ("1", True),
        ("YeS", True),
        (" ON ", True),
        (" false ", False),
        ("0", False),
        ("No", False),
        (" OFF ", False),
    ),
)
def test_announcement_startup_setting_accepts_strict_boolean_spellings(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", value)

    assert announcements_enabled() is expected


@pytest.mark.unit
def test_malformed_announcement_setting_fails_before_network_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "sometimes")
    _forbid_announcement_network_construction(
        monkeypatch,
        "malformed CLI announcement setting reached network construction",
    )

    with pytest.raises(ValueError) as caught:
        fetch_announcements()

    assert str(caught.value) == (
        "Invalid value for TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED: expected one of "
        "true/1/yes/on/false/0/no/off"
    )


@pytest.mark.unit
def test_analyze_startup_reaches_questionnaire_with_announcements_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuestionnaireReached(RuntimeError):
        pass

    def stop_at_ticker() -> None:
        raise QuestionnaireReached

    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    monkeypatch.setattr(cli_main, "get_ticker", stop_at_ticker)
    _forbid_announcement_network_construction(
        monkeypatch,
        "disabled CLI startup attempted announcement network construction",
    )

    result = CliRunner().invoke(cli_main.app, ["analyze"])

    assert isinstance(result.exception, QuestionnaireReached)


@pytest.mark.unit
def test_qualified_validation_reaches_questionnaire_without_announcement_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuestionnaireReached(RuntimeError):
        pass

    def stop_at_ticker() -> None:
        raise QuestionnaireReached

    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    monkeypatch.setattr(cli_main, "get_ticker", stop_at_ticker)
    _forbid_announcement_network_construction(
        monkeypatch,
        "qualified validation constructed announcement network I/O",
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["analyze", "--qualified-routing-validation"],
    )

    assert isinstance(result.exception, QuestionnaireReached)


@pytest.mark.unit
@pytest.mark.parametrize("value", (None, "true"))
def test_qualified_validation_requires_announcements_disabled_before_live_work(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", value)
    questionnaire_calls = 0
    run_allocations = 0
    graph_constructions = 0

    def forbidden_questionnaire():
        nonlocal questionnaire_calls
        questionnaire_calls += 1
        pytest.fail("announcement preflight reached the CLI questionnaire")

    def forbidden_run_allocation(*_args, **_kwargs):
        nonlocal run_allocations
        run_allocations += 1
        pytest.fail("announcement preflight reached run allocation")

    def forbidden_graph_construction(*_args, **_kwargs):
        nonlocal graph_constructions
        graph_constructions += 1
        pytest.fail("announcement preflight reached provider/model construction")

    monkeypatch.setattr(cli_main, "get_user_selections", forbidden_questionnaire)
    monkeypatch.setattr(cli_main, "_prepare_run_artifacts", forbidden_run_allocation)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", forbidden_graph_construction)
    _forbid_announcement_network_construction(
        monkeypatch,
        "qualified validation constructed announcement network I/O",
    )

    with pytest.raises(
        RuntimeError,
        match="qualified_routing_validation_requires_announcements_disabled",
    ):
        cli_main.run_analysis(qualified_routing_validation=True)

    assert questionnaire_calls == 0
    assert run_allocations == 0
    assert graph_constructions == 0


@pytest.mark.unit
def test_qualified_validation_preflight_evidence_retains_only_safe_disable_state(
    tmp_path: Path,
) -> None:
    config = dict(cli_main.DEFAULT_CONFIG)
    config["results_dir"] = str(tmp_path)
    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-08-12",
        "asset_type": "stock",
        "analysts": [],
        "llm_provider": "openai",
        "china_a_enhancement_preset": "basic",
    }
    calendar_receipt = cli_main.ProductionQualifiedCalendarPreflightReceipt(
        contract_version="production-qualified-calendar-preflight-v1",
        calendar_revision_id="market-session-calendar=sha256:" + "a" * 64,
        requested_session="2026-08-12",
        evaluated_at="2026-08-12T08:00:00Z",
        latest_completed_session="2026-08-12",
        next_open_session="2026-08-13",
        reader_uses_primary_database=True,
        exact_revision_verified=True,
    )
    preflight_evidence = {
        "contract_version": "qualified-routing-validation-preflight-v1",
        "cli_announcements_enabled": False,
        "calendar_reader": calendar_receipt.model_dump(mode="json"),
    }

    artifacts = cli_main._prepare_run_artifacts(
        config,
        selections,
        cli_announcements_enabled=False,
        qualified_validation_calendar_receipt=calendar_receipt,
    )
    retained = json.loads(
        Path(artifacts["status_file"]).read_text(encoding="utf-8")
    )["qualified_validation_preflight"]

    assert retained == preflight_evidence
    assert retained["cli_announcements_enabled"] is False
    assert "environment" not in retained
    assert "credentials" not in retained


@pytest.mark.unit
def test_unset_setting_preserves_announcement_request_and_response_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_arguments = None

    class AnnouncementResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "announcements": ["Current announcement"],
                "require_attention": True,
            }

    def record_request(url: str, *, timeout: float):
        nonlocal request_arguments
        request_arguments = (url, timeout)
        return AnnouncementResponse()

    monkeypatch.delenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", raising=False)
    monkeypatch.setattr(requests, "get", record_request)

    result = fetch_announcements()

    assert request_arguments == ("https://api.tauric.ai/v1/announcements", 1.0)
    assert result == {
        "announcements": ["Current announcement"],
        "require_attention": True,
    }


@pytest.mark.unit
def test_unset_setting_preserves_announcement_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(_url: str, *, timeout: float):
        raise requests.RequestException(f"unavailable after {timeout} seconds")

    monkeypatch.delenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", raising=False)
    monkeypatch.setattr(requests, "get", fail_request)

    assert fetch_announcements() == {
        "announcements": [
            "[cyan]For more information, please visit[/cyan] "
            "[link=https://github.com/TauricResearch]"
            "https://github.com/TauricResearch[/link]"
        ],
        "require_attention": False,
    }


@pytest.mark.unit
def test_source_locked_module_help_succeeds_with_announcements_disabled() -> None:
    sensitive_markers = (
        "API_KEY",
        "TOKEN",
        "PASSWORD",
        "SECRET",
        "CREDENTIAL",
        "COOKIE",
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in sensitive_markers)
    }
    environment.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED": "false",
            "RUN_LIVE_DEEPSEEK_TESTS": "0",
            "RUN_LIVE_PROVIDER_TESTS": "0",
            "RUN_LIVE_TUSHARE_TESTS": "0",
        }
    )
    environment.pop("PYTHONPATH", None)

    probe = subprocess.run(
        (sys.executable, "-m", "cli.main", "--help"),
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.unit
@pytest.mark.parametrize("returncode", (0, 1))
def test_source_locked_help_preserves_non_text_output(monkeypatch, returncode):
    """Exercise the existing probe contract without launching a child process."""
    output = b"stdout\x90"
    errors = b"stderr\xff"

    def fake_run(command, **kwargs):
        assert kwargs["text"] is False
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, returncode, output, errors)

    monkeypatch.setattr(subprocess, "run", fake_run)
    if returncode:
        with pytest.raises(AssertionError) as failure:
            test_source_locked_module_help_succeeds_with_announcements_disabled()
        assert str(failure.value).splitlines()[0] == repr(output + errors)
    else:
        test_source_locked_module_help_succeeds_with_announcements_disabled()
