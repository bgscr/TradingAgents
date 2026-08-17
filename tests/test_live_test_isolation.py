import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.live_test_guard import live_test_enabled

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_guarded_live_tests(
    tmp_path: Path,
    *,
    exported_credentials: bool,
    dotenv_credentials: bool,
) -> subprocess.CompletedProcess[str]:
    canary_path = tmp_path / "live-constructor-called"
    probe_path = tmp_path / "constructor_probe.py"
    probe_path.write_text(
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_call(item):\n"
        "    if item.get_closest_marker('live') is not None:\n"
        f"        Path({str(canary_path)!r}).write_text('called', encoding='utf-8')\n"
        "        raise AssertionError('live test entered call phase without opt-in')\n",
        encoding="utf-8",
    )

    deepseek_secret = "deepseek-regression-canary-never-print"
    tushare_secret = "tushare-regression-canary-never-print"
    if dotenv_credentials:
        (tmp_path / ".env").write_text(
            f"DEEPSEEK_API_KEY={deepseek_secret}\nTUSHARE_TOKEN={tushare_secret}\n",
            encoding="utf-8",
        )

    environment = os.environ.copy()
    environment.pop("DEEPSEEK_API_KEY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("TUSHARE_TOKEN", None)
    environment["RUN_LIVE_DEEPSEEK_TESTS"] = "0"
    environment["RUN_LIVE_TUSHARE_TESTS"] = "0"
    if dotenv_credentials:
        environment["PYTHON_DOTENV_DISABLED"] = "0"
    environment["PYTEST_PLUGINS"] = "constructor_probe"
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(tmp_path),
                str(_REPOSITORY_ROOT),
                environment.get("PYTHONPATH", ""),
            ),
        )
    )
    if exported_credentials:
        environment["DEEPSEEK_API_KEY"] = deepseek_secret
        environment["TUSHARE_TOKEN"] = tushare_secret

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(tmp_path / "pytest-temp"),
            "-m",
            "live",
            str(_REPOSITORY_ROOT / "tests"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert not canary_path.exists(), result.stdout + result.stderr
    combined_output = result.stdout + result.stderr
    assert deepseek_secret not in combined_output
    assert tushare_secret not in combined_output
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != ".env" and path.suffix != ".pyc":
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert deepseek_secret not in content
            assert tushare_secret not in content
    return result


def test_exported_credentials_without_positive_opt_in_cannot_run_live_tests(tmp_path):
    result = _run_guarded_live_tests(
        tmp_path,
        exported_credentials=True,
        dotenv_credentials=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout


def test_dotenv_credentials_without_positive_opt_in_cannot_run_live_tests(tmp_path):
    result = _run_guarded_live_tests(
        tmp_path,
        exported_credentials=False,
        dotenv_credentials=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({"PROVIDER_KEY": "real-looking-key"}, False),
        ({"RUN_LIVE_PROVIDER_TESTS": "1"}, False),
        (
            {
                "RUN_LIVE_PROVIDER_TESTS": "1",
                "PROVIDER_KEY": "real-looking-key",
            },
            True,
        ),
        (
            {
                "RUN_LIVE_PROVIDER_TESTS": "true",
                "PROVIDER_KEY": "real-looking-key",
            },
            False,
        ),
        (
            {
                "RUN_LIVE_PROVIDER_TESTS": "1",
                "PROVIDER_KEY": "placeholder",
            },
            False,
        ),
    ),
)
def test_explicit_positive_opt_in_is_independent_of_credentials(environment, expected):
    assert live_test_enabled(
        opt_in="RUN_LIVE_PROVIDER_TESTS",
        credentials=("PROVIDER_KEY",),
        environment=environment,
    ) is expected


def test_fake_deterministic_client_remains_usable_without_live_opt_in():
    calls = []

    class FakeClient:
        def __init__(self):
            calls.append("constructed")

    FakeClient()

    assert calls == ["constructed"]


def test_live_test_declarations_use_the_central_opt_in_contract():
    from tests.test_deepseek_reasoning import TestDeepSeekLiveStructuredOutput
    from tests.test_tushare_pit_live import test_tushare_trade_calendar_probe

    expected = (
        (
            TestDeepSeekLiveStructuredOutput,
            "RUN_LIVE_DEEPSEEK_TESTS",
            ("DEEPSEEK_API_KEY",),
        ),
        (
            test_tushare_trade_calendar_probe,
            "RUN_LIVE_TUSHARE_TESTS",
            ("TUSHARE_TOKEN",),
        ),
    )
    for test_object, opt_in, credentials in expected:
        marker = next(
            marker
            for marker in test_object.pytestmark
            if marker.name == "live"
        )
        assert marker.kwargs == {
            "opt_in": opt_in,
            "credentials": credentials,
        }
