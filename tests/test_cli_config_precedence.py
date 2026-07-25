"""CLI config precedence (#976, #977).

An explicit environment override for the debate/risk round counts, or the
checkpoint flag, must win over the interactive research-depth selection — the CLI
must not clobber an env-configured value back to a prompt/flag default.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import cli.main as m

# Minimal selections dict shaped like get_user_selections()'s return value.
SELECTIONS = {
    "research_depth": 5,
    "shallow_thinker": "gpt-5.4-mini",
    "deep_thinker": "gpt-5.5",
    "backend_url": None,
    "llm_provider": "openai",
    "google_thinking_level": None,
    "openai_reasoning_effort": None,
    "anthropic_effort": None,
    "output_language": "English",
}


def test_research_depth_sets_both_rounds_without_env(monkeypatch):
    for var in ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "TRADINGAGENTS_MAX_RISK_ROUNDS"):
        monkeypatch.delenv(var, raising=False)
    cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 5
    assert cfg["max_risk_discuss_rounds"] == 5


def test_env_round_counts_win_over_selection(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    # DEFAULT_CONFIG already reflects the env (applied at import); emulate that.
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2, max_risk_discuss_rounds=4)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # env value, not research_depth=5
    assert cfg["max_risk_discuss_rounds"] == 4


def test_partial_env_only_overrides_that_count(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # env wins
    assert cfg["max_risk_discuss_rounds"] == 5  # falls through to research_depth


def test_checkpoint_none_preserves_env_default():
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=True)  # e.g. env-enabled
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["checkpoint_enabled"] is True  # not clobbered back to False


@pytest.mark.parametrize("flag", [True, False])
def test_checkpoint_flag_overrides_env(flag):
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=not flag)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=flag)
    assert cfg["checkpoint_enabled"] is flag


@pytest.mark.parametrize(
    ("environment_value", "cli_args", "expected_enabled"),
    [
        ("true", [], True),
        ("true", ["--no-checkpoint"], False),
        ("false", ["--checkpoint"], True),
        (None, [], False),
    ],
    ids=[
        "environment-enables-when-flag-omitted",
        "no-checkpoint-flag-overrides-enabled-environment",
        "checkpoint-flag-overrides-disabled-environment",
        "disabled-when-neither-source-enables",
    ],
)
def test_checkpoint_precedence_reaches_cli_graph_execution(
    tmp_path,
    environment_value,
    cli_args,
    expected_enabled,
):
    output_path = tmp_path / "checkpoint-probe.json"
    environment = os.environ.copy()
    environment["CHECKPOINT_PROBE_OUTPUT"] = str(output_path)
    if environment_value is None:
        environment.pop("TRADINGAGENTS_CHECKPOINT_ENABLED", None)
    else:
        environment["TRADINGAGENTS_CHECKPOINT_ENABLED"] = environment_value

    completed = subprocess.run(
        [sys.executable, "-m", "tests.cli_checkpoint_probe", *cli_args],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(output_path.read_text(encoding="utf-8"))
    assert observed["config_checkpoint_enabled"] is expected_enabled
    assert observed["stream_thread_id_present"] is expected_enabled
