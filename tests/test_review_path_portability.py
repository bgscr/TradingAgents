"""Portable inputs for offline source review; no owner or OS state is opened."""
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.policy_owner import run_registered_test as driver


def test_legacy_dependencies_follow_source_tree():
    assert driver.DEPENDENCIES == Path(driver.__file__).resolve().parents[2] / ".venv/Lib/site-packages"


def test_local_binding_requires_every_pinned_value(monkeypatch):
    fields = {"ledger_identity": "example-ledger", "storage_binding_identity": "example-storage"}
    pins = {name: sha256(value.encode()).hexdigest() for name, value in fields.items()}
    monkeypatch.setattr(driver, "LEGACY_BINDING_PINS", pins)
    assert driver.verified_legacy_bindings(fields) == fields
    for name in fields:
        with pytest.raises(ValueError):
            driver.verified_legacy_bindings({**fields, name: "other"})
        with pytest.raises(ValueError):
            driver.verified_legacy_bindings({k: v for k, v in fields.items() if k != name})


def test_operator_comes_from_the_verified_role_and_stays_exact(monkeypatch):
    monkeypatch.setattr(driver, "DEPLOYMENT_PATH", None)
    monkeypatch.setattr(driver, "LEGACY_OPERATOR_PIN", sha256(b"test-operator").hexdigest())
    assert driver.checked_operator_sid("test-operator") == "test-operator"
    with pytest.raises(ValueError):
        driver.checked_operator_sid("other")
    monkeypatch.setattr(driver, "DEPLOYMENT_PATH", Path("reviewed-deployment"))
    monkeypatch.setattr(driver, "EXPECTED", {"operator_sid": "explicit-operator"})
    assert driver.checked_operator_sid("explicit-operator") == "explicit-operator"
    with pytest.raises(ValueError):
        driver.checked_operator_sid("test-operator")
