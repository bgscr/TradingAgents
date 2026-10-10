"""Disposable Python child, never imports or invokes TradingAgents analysis."""

import base64
import hashlib
import json
import os


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


context = json.loads(base64.b64decode(os.environ["TRADINGAGENTS_PROTECTION_CONTEXT"]))
assert os.environ["TRADINGAGENTS_OUTPUT_LANGUAGE"] == "English"
assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
assert all(os.environ[key] == "0" for key in os.environ if key.startswith("RUN_LIVE_"))
assert os.environ.get("PYTHONPATH") is None
gate = context["handoff"]
assert gate["symbol"] == "DISPOSABLE_FIXTURE"
if context["phase"] == "guard":
    message = {
        "contract_version": "runtime-protection-guard-v1",
        "phase": "guard",
        "attempt_identity": context["attempt_identity"],
        "packet_identity": context["packet_identity"],
        "prepared_identity": context["prepared_identity"],
        "source_guard_identity": "fixture-guard:v1:sha256:" + "a" * 64,
        "checkpoint_identity": gate["checkpoint_identity"],
        "history_identity": gate["history_identity"],
        "prospective_thread": context["prospective_thread"],
        "prospective_run": context["prospective_run"],
        "status": "PASS",
        "live_io": {"provider": 0, "model": 0, "network": 0, "CNINFO": 0},
        "checkpoint_original_exact_equal": True,
        "retained_provenance_changed": False,
        "run_or_thread_allocated": False,
        "execution_authorized": False,
        **{
            key: context[key]
            for key in (
                "runtime_manifest_identity",
                "app_manifest_identity",
                "environment_identity",
                "runtime_config_identity",
            )
        },
        **{
            key: gate[key]
            for key in (
                "plan_signature",
                "cninfo_profile_identity",
                "cninfo_source_policy_identity",
                "cninfo_operator_minimum_interval_seconds",
                "legacy_authoritative",
                "qualified_shadow_authoritative",
            )
        },
    }
    message["identity"] = (
        "runtime-protection-guard:v1:sha256:" + hashlib.sha256(canonical(message)).hexdigest()
    )
else:
    assert context["phase"] == "step4"
    message = {
        "phase": "step4",
        "attempt_identity": context["attempt_identity"],
        "packet_identity": context["packet_identity"],
        "questionnaire": {
            key: gate[key]
            for key in ("symbol", "analysis_session", "selected_analysts", "output_language")
        },
        "literal_confirmation": "YES",  # Synthetic test confirmation, no real gate or analysis.
        "runtime_config_identity": context["runtime_config_identity"],
    }
pipe = r"\\.\pipe\TradingAgentsProtection-" + context["attempt_identity"].rsplit(":", 1)[1]
with open(pipe, "r+b", buffering=0) as stream:
    stream.write(canonical(message) + b"\n")
    response = json.loads(stream.readline(65537))
assert response["accepted"] is True
assert response["attempt_identity"] == context["attempt_identity"]
