"""Offline public contracts for permanently distinct protection attempts."""

from __future__ import annotations

import copy

import pytest

from tradingagents.runtime_protection import (
    AttemptInputs,
    bind_production_preparation,
    derive_attempt,
    evidence_identity,
    make_content_manifest,
    seal_materialization_plan,
)


def attempt_inputs(**changes):
    values = {
        "namespace_root": r"D:\disposable-protection-fixtures",
        "namespace_identity": "runtime-protection-namespace:v1:sha256:" + "f" * 64,
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "qualified_source_identity": "qualified-source-ready-for-ticket07-preparation:v1:sha256:"
        + "3" * 64,
        "production_preparation_identity": "production-qualified-routing-preparation:v1:"
        + "4" * 64,
        "preparation_lineage_identity": "fixture-preparation:v1:sha256:" + "5" * 64,
        "completed_session": "2026-01-02",
        "prospective_thread": "1234567890abcdef",
        "prospective_run": "run:" + "6" * 64,
        "runtime_content_identity": "runtime-content:v1:sha256:" + "7" * 64,
        "app_content_identity": "app-content:v1:sha256:" + "8" * 64,
        "runtime_ancestry_identity": "runtime-ancestry:v1:sha256:" + "9" * 64,
        "app_ancestry_identity": "app-ancestry:v1:sha256:" + "a" * 64,
        "deployment_configuration_identity": "deployment-config:v1:sha256:" + "b" * 64,
        "environment_identity": "sealed-environment:v1:sha256:" + "c" * 64,
        "protection_source_identity": "runtime-protection-source:v1:sha256:" + "d" * 64,
        "attempt_ordinal": 1,
    }
    values.update(changes)
    return values


def test_attempt_derivation_is_deterministic_and_successor_paths_are_disjoint():
    first = derive_attempt(AttemptInputs(**attempt_inputs()))
    same = derive_attempt(AttemptInputs(**attempt_inputs()))
    successor = derive_attempt(AttemptInputs(**attempt_inputs(attempt_ordinal=2)))
    assert first == same
    assert first.attempt_identity != successor.attempt_identity
    assert not set(first.paths.model_dump().values()) & set(successor.paths.model_dump().values())
    assert first.contract_version == "runtime-protection-attempt-v1"
    assert first.execution_authorized is False
    assert first.paths.private_app_root.startswith(first.paths.private_runtime_root + "\\")
    assert first.paths.history_anchor.startswith(first.paths.payload_root + "\\")


@pytest.mark.parametrize("field", tuple(attempt_inputs()))
def test_every_semantic_input_is_bound_into_attempt_identity(field):
    original = attempt_inputs()
    changed = copy.deepcopy(original)
    if field == "attempt_ordinal":
        changed[field] = 2
    elif field == "completed_session":
        changed[field] = "2026-01-05"
    elif field == "namespace_root":
        changed[field] += "-other"
    else:
        changed[field] = changed[field][:-1] + ("e" if changed[field][-1] != "e" else "f")
    assert (
        derive_attempt(AttemptInputs(**original)).attempt_identity
        != derive_attempt(AttemptInputs(**changed)).attempt_identity
    )


@pytest.mark.parametrize("ordinal", (0, -1, True, "1", 1.5))
def test_ordinal_is_explicit_positive_integer_without_random_fallback(ordinal):
    with pytest.raises(ValueError):
        AttemptInputs(**attempt_inputs(attempt_ordinal=ordinal))


@pytest.mark.parametrize(
    "root", (r"D:relative", r"D:\a\..\b", r"\\server\share", r"D:\a:stream", "D:/a/")
)
def test_ambiguous_namespace_paths_are_rejected(root):
    with pytest.raises(ValueError):
        AttemptInputs(**attempt_inputs(namespace_root=root))


def test_materialization_seals_component_ancestry_and_path_derived_configuration():
    runtime = make_content_manifest(
        role="runtime",
        source_commit="1" * 40,
        source_tree="2" * 40,
        ancestry_identity=attempt_inputs()["runtime_ancestry_identity"],
        files=[{"relative_path": "base/Lib/ssl.py", "byte_length": 3, "sha256": "0" * 64}],
        native_images=[],
        python_version="3.13.7",
    )
    app = make_content_manifest(
        role="app",
        source_commit="1" * 40,
        source_tree="2" * 40,
        ancestry_identity=attempt_inputs()["app_ancestry_identity"],
        files=[{"relative_path": "tradingagents/main.py", "byte_length": 3, "sha256": "1" * 64}],
        native_images=[],
    )
    config = {
        "project_dir": r"D:\old\tradingagents",
        "output_language": "English",
        "crypto_identity_registry_path": r"D:\old\registry.json",
        "crypto_identity_registry_checksum_path": r"D:\old\registry.sha256",
        "cninfo_operator_minimum_interval_seconds": 7,
    }
    environment = {
        "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
        "PYTHON_DOTENV_DISABLED": "1",
        "SystemRoot": r"C:\Windows",
        "PATH": r"{private_runtime_root}\base;{private_runtime_root}\venv\Scripts;C:\Windows\System32",
    }
    attempt = derive_attempt(
        AttemptInputs(
            **attempt_inputs(
                runtime_content_identity=runtime["identity"],
                app_content_identity=app["identity"],
                deployment_configuration_identity=evidence_identity(
                    "runtime-protection-configuration:v1", config
                ),
                environment_identity=evidence_identity(
                    "runtime-protection-environment:v1",
                    {
                        "variables": environment,
                        "working_directory": r"D:\qualified\cwd",
                        "credential_bindings": [],
                    },
                ),
            )
        )
    )
    plan = seal_materialization_plan(
        attempt=attempt,
        runtime_content=runtime,
        app_content=app,
        source_locations={
            "runtime/base/Lib/ssl.py": r"D:\source\ssl.py",
            "app/tradingagents/main.py": r"D:\source\main.py",
        },
        deployment_configuration=config,
        environment=environment,
        working_directory=r"D:\qualified\cwd",
        python_version="3.13.7",
    )
    assert plan["mapped_configuration"]["cninfo_operator_minimum_interval_seconds"] == 7
    assert (
        plan["mapped_configuration"]["project_dir"]
        == attempt.paths.private_app_root + r"\tradingagents"
    )
    assert plan["attempt"]["attempt_identity"] == attempt.attempt_identity
    assert plan["execution_authorized"] is False
    assert plan["working_directory"] == r"D:\qualified\cwd"
    assert config["project_dir"] == r"D:\old\tradingagents"


def test_adapter_preserves_existing_plan15_preparation_without_provider_io(tmp_path, monkeypatch):
    import importlib.util
    import socket

    import tradingagents.qualified_routing_composition as composition
    from tests.test_plan15_production_binding import _blueprint, _config, _source_binding

    def forbidden(*args, **kwargs):
        raise AssertionError("offline preparation attempted I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-only")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    source = _source_binding()
    production = composition.seal_production_qualified_routing_preparation(
        blueprint=_blueprint(_config(tmp_path)),
        qualified_source=source,
        current_source=source,
    )
    values = attempt_inputs()
    for key in (
        "source_commit",
        "source_tree",
        "qualified_source_identity",
        "production_preparation_identity",
    ):
        values.pop(key)
    attempt = bind_production_preparation(production, **values)
    assert attempt.inputs.production_preparation_identity == production.preparation_identity
    assert production.plan_version == "1.5"
    assert production.execution_authorized is False
    assert attempt.execution_authorized is False
