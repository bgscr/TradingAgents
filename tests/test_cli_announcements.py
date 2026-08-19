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
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
