"""Exact source/proof closure tests, with synthetic files and no native execution."""

from hashlib import sha256

import pytest

from scripts.policy_owner.preflight_evidence import (
    _COVERAGE_PROOF_REQUIRED,
    verify_coverage_source_proof,
)
from tests.fixtures.coverage_case import coverage_case
from tradingagents.policy_coverage_models import CoveragePolicy
from tradingagents.policy_qualified_contract import BytePin, LineagePlan, canonical_bytes


def fixture(tmp_path):
    (tmp_path / "case").mkdir()
    case = coverage_case(tmp_path / "case")
    package = tmp_path / "package"
    package.mkdir()
    depot = tmp_path / "depot"
    depot.mkdir()
    files = []
    pins = {}
    for name in sorted(_COVERAGE_PROOF_REQUIRED):
        raw = ("SYNTHETIC SOURCE PIN VECTOR " + name).encode()
        pin = BytePin.of(raw).model_dump(mode="json")
        pins[name] = pin
        files.append({"relative_path": name, **pin})
        for root in (package, depot):
            path = root / "source" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    value = {
        "contract_version": "policy-qualified-exact-source-bundle-v2",
        "candidate": case.candidate.model_dump(mode="json"),
        "files": files,
        "execution_authorized": False,
    }
    raw = canonical_bytes(value)
    path = package / "CONTRACT-SOURCES.json"
    path.write_bytes(raw)
    plan = case.policy.lineage_plan.model_dump(mode="json", exclude={"lineage_plan_identity"})
    plan["consumer_contract_pin"] = BytePin.of(raw)
    policy = case.policy.model_dump(mode="json", exclude={"acceptance_policy_identity"})
    policy["lineage_plan"] = LineagePlan.seal(**plan)
    return CoveragePolicy.seal(**policy), {"source_pins": pins}, path, depot


def digest(path):
    raw = path.read_bytes()
    return len(raw), sha256(raw).hexdigest()


def test_preflight_requires_complete_proof_and_exact_proposed_candidate(tmp_path):
    policy, witness, path, depot = fixture(tmp_path)
    assert verify_coverage_source_proof(
        policy=policy, witness=witness, contract_path=path, depot=depot, digest=digest
    )["candidate"] == policy.scope.candidate.model_dump(mode="json")
    from tradingagents.policy_qualified_contract import strict_json

    changed = strict_json(path.read_bytes())
    changed["candidate"]["commit"] = "0" * 40
    raw = canonical_bytes(changed)
    path.write_bytes(raw)
    plan = policy.lineage_plan.model_dump(mode="json", exclude={"lineage_plan_identity"})
    plan["consumer_contract_pin"] = BytePin.of(raw)
    body = policy.model_dump(mode="json", exclude={"acceptance_policy_identity"})
    body["lineage_plan"] = LineagePlan.seal(**plan)
    resealed = CoveragePolicy.seal(**body)
    with pytest.raises(ValueError, match="candidate differs"):
        verify_coverage_source_proof(
            policy=resealed, witness=witness, contract_path=path, depot=depot, digest=digest
        )


@pytest.mark.parametrize(
    "change", ["missing_pin", "unknown_member", "changed_delivered", "changed_fixture"]
)
def test_preflight_never_silently_skips_source_or_fixture_members(tmp_path, change):
    policy, witness, path, depot = fixture(tmp_path)
    if change == "missing_pin":
        witness["source_pins"].pop("tradingagents/policy_coverage_verification.py")
    elif change == "unknown_member":
        witness["source_pins"]["unrelated/ignored.py"] = BytePin.of(b"fake").model_dump(mode="json")
    elif change == "changed_delivered":
        (depot / "source/tradingagents/policy_coverage_contract.py").write_bytes(b"changed")
    else:
        (path.parent / "source/tests/fixtures/coverage_case.py").write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_coverage_source_proof(
            policy=policy, witness=witness, contract_path=path, depot=depot, digest=digest
        )
