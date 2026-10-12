"""Proof reuse never caches mutable authority, file bytes or a public decision."""

import pytest

from tests.fixtures.coverage_case import coverage_case
from tradingagents.policy_coverage_verification import coverage_operation, resolve_coverage


@pytest.mark.windows
def test_successful_proof_is_reused_only_inside_one_public_operation(tmp_path, monkeypatch):
    import tradingagents.policy_coverage_verification as module

    case = coverage_case(tmp_path)
    calls = []
    original = module.compare_known_fields

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(module, "compare_known_fields", counted)

    @coverage_operation
    def check(verifier):
        verifier.eligibility(case.policy, case.approval)
        resolve_coverage(verifier, case.policy, case.coverage)
        resolve_coverage(verifier, case.policy, case.coverage)

    check(case.verifier)
    assert len(calls) == 1
    check(case.verifier)
    assert len(calls) == 2
    resolve_coverage(case.verifier, case.policy, case.coverage)
    resolve_coverage(case.verifier, case.policy, case.coverage)
    assert len(calls) == 4  # no cache outside the public operation


@pytest.mark.windows
def test_cache_rechecks_mutable_parsed_baseline_and_discards_failed_entry(tmp_path):
    case = coverage_case(tmp_path)

    @coverage_operation
    def check(verifier):
        _, baseline = resolve_coverage(verifier, case.policy, case.coverage)
        baseline["roots"][0]["entries"][0]["attributes"] += 1
        with pytest.raises(ValueError, match="cached coverage proof"):
            resolve_coverage(verifier, case.policy, case.coverage)
        # The failed entry was discarded: actual immutable payload is reparsed.
        _, restored = resolve_coverage(verifier, case.policy, case.coverage)
        assert restored != baseline

    check(case.verifier)


@pytest.mark.windows
@pytest.mark.parametrize("member", ["scope", "measurement", "baseline", "assessment"])
def test_cache_reads_complete_artifacts_again_and_rejects_changed_bytes(tmp_path, member):
    case = coverage_case(tmp_path)

    @coverage_operation
    def check(verifier):
        resolve_coverage(verifier, case.policy, case.coverage)
        reference = getattr(case.coverage, member)
        case.owner.artifacts[(reference.identity, reference.pin.sha256)] = b"{}"
        with pytest.raises(ValueError):
            resolve_coverage(verifier, case.policy, case.coverage)

    check(case.verifier)


@pytest.mark.windows
def test_current_proofs_are_ordered_bounded_freshly_pinned_and_operation_local(
    tmp_path, monkeypatch
):
    import tradingagents.policy_coverage_verification as module
    from tradingagents.policy_coverage_models import CoverageRawComparison

    case = coverage_case(tmp_path)
    case.reserve()
    comparison = case.raw_comparison("ISO03")
    calls = []
    original = module.compare_current_inventories

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(module, "compare_current_inventories", counted)

    @coverage_operation
    def check(verifier):
        module.verify_coverage_comparison(
            verifier, comparison, policy=case.policy, claim=case.claim, bridge=False
        )
        module.verify_coverage_comparison(
            verifier, comparison, policy=case.policy, claim=case.claim, bridge=False
        )
        module.require_current_baseline(verifier, case.policy, case.claim, comparison.before)
        assert len(module._operation.get()["current"]) <= 8
        swapped = comparison.model_dump(mode="json", exclude={"identity"})
        swapped.update(before=comparison.after, after=comparison.before)
        with pytest.raises(ValueError, match="phases"):
            module.verify_coverage_comparison(
                verifier,
                CoverageRawComparison.seal(**swapped),
                policy=case.policy,
                claim=case.claim,
                bridge=False,
            )
        ref = comparison.after
        key = (ref.identity, ref.pin.sha256)
        saved = case.owner.artifacts[key]
        case.owner.artifacts[key] = b"changed"
        with pytest.raises(ValueError):
            module.verify_coverage_comparison(
                verifier, comparison, policy=case.policy, claim=case.claim, bridge=False
            )
        case.owner.artifacts[key] = saved

    check(case.verifier)
    assert len(calls) == 1
    check(case.verifier)
    assert len(calls) == 2
    assert module._operation.get() is None
