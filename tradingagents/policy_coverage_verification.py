"""One authoritative coverage proof shared by admission, all stages and consumers."""

from contextvars import ContextVar
from functools import wraps

from tradingagents.policy_coverage_contract import (
    CURRENT_NAMESPACE,
    CoverageAssessment,
    HistoricalCoverageScope,
    artifact_ref,
    compare_current_inventories,
    compare_known_fields,
    validate_current_inventory,
)
from tradingagents.policy_coverage_models import (
    CoverageBinding,
    CoverageClaim,
    CoveragePolicy,
    CoverageRawComparison,
)

_operation = ContextVar("coverage_verification_operation", default=None)


def coverage_operation(function):
    """Bound one proof cache to one public operation, including failure exits.

    This is neither authority nor a process-global artifact cache. Context-local
    storage also prevents concurrent callers from sharing mutable proof objects.
    """

    @wraps(function)
    def checked(verifier, *args, **kwargs):
        token = _operation.set({"verifier": verifier, "entry": None, "current": {}})
        try:
            return function(verifier, *args, **kwargs)
        finally:
            _operation.reset(token)

    return checked


def reset_coverage_proof(verifier):
    context = _operation.get()
    if context is not None and context["verifier"] is verifier:
        context["entry"] = None
        context["current"].clear()


def _current_key(verifier, policy, binding, predicate, references):
    return (
        id(verifier.owner),
        verifier.owner.coverage_instance(),
        policy.to_bytes(),
        binding.to_bytes(),
        predicate,
        tuple(reference.to_bytes() for reference in references),
    )


def _remember_current_check(verifier, policy, binding, predicate, references):
    context = _operation.get()
    if context is not None and context["verifier"] is verifier and len(context["current"]) < 8:
        context["current"][_current_key(verifier, policy, binding, predicate, references)] = True


def _current_check(verifier, policy, binding, predicate, references, check):
    """At most eight successful pure checks, with no current payload retained.

    Scope/B0 proof and provenance are resolved by the caller on every invocation.
    Ordered references and a predicate tag prevent cross-predicate reuse.
    """
    context = _operation.get()
    if context is None or context["verifier"] is not verifier:
        return check()
    key = _current_key(verifier, policy, binding, predicate, references)
    entry = context["current"].pop(key, None)
    if entry is not None:
        for reference in references:
            verifier._bytes(reference)
        context["current"][key] = True
        return
    check()
    _remember_current_check(verifier, policy, binding, predicate, references)


def resolve_coverage(verifier, policy, binding, *, claim=None):
    if not isinstance(policy, CoveragePolicy):
        raise ValueError("coverage evidence requires a distinct versioned policy")
    binding = CoverageBinding.read(binding)
    if binding.scope != policy.coverage_scope or binding.measurement != artifact_ref(
        policy.coverage_measurement
    ):
        raise ValueError("coverage binding differs from exact policy/measurement")
    if claim is not None and isinstance(claim, CoverageClaim) and claim.coverage != binding:
        raise ValueError("coverage baseline differs from irreversible claim")
    context = _operation.get()
    if context is not None and context["verifier"] is not verifier:
        context = None
    instance = verifier.owner.coverage_instance()
    key = (verifier.owner, instance, policy.to_bytes(), binding.to_bytes())
    entry = context["entry"] if context is not None else None
    if entry is not None and entry[0] == key:
        # Always reread all complete immutable payloads and check their whole-file
        # pins. Cache only the deterministic proof, never provenance or authority.
        _, scope, baseline = entry
        context["entry"] = None
        for reference in (
            binding.scope,
            binding.measurement,
            binding.baseline,
            binding.assessment,
            policy.scope.before.artifact,
            policy.scope.after.artifact,
        ):
            verifier._bytes(reference)
        if (
            artifact_ref(scope) != binding.scope
            or artifact_ref(baseline) != binding.baseline
            or scope.before != policy.scope.before.artifact
            or scope.after != policy.scope.after.artifact
            or baseline.get("instance") != instance
        ):
            raise ValueError("cached coverage proof changed its complete bound contents")
        context["entry"] = entry
        return scope, baseline
    if context is not None:
        context["entry"] = None
    scope = verifier._model(binding.scope, HistoricalCoverageScope)
    if scope.before != policy.scope.before.artifact or scope.after != policy.scope.after.artifact:
        raise ValueError("coverage scope differs from exact original acquisitions")
    verifier._bytes(binding.measurement)
    baseline = verifier._saved(binding.baseline, CURRENT_NAMESPACE)
    if baseline.get("instance") != instance:
        raise ValueError("coverage baseline belongs to another owner instance")
    assessment = verifier._model(binding.assessment, CoverageAssessment)
    expected = compare_known_fields(
        scope,
        verifier._bytes(scope.before),
        verifier._bytes(scope.after),
        baseline,
        policy.coverage_measurement,
    )
    if assessment != expected or artifact_ref(baseline) != binding.baseline:
        raise ValueError(
            "coverage assessment lost complete known-field/unknown/expansion accounting"
        )
    if context is not None:
        context["entry"] = (key, scope, baseline)
    return scope, baseline


def verify_coverage_comparison(verifier, comparison, *, policy, claim, bridge):
    if not isinstance(comparison, CoverageRawComparison):
        raise ValueError("new coverage policy cannot reinterpret old raw comparison")
    scope, baseline = resolve_coverage(verifier, policy, comparison.coverage, claim=claim)
    if bridge:
        if (
            comparison.relation != "KNOWN_RECORDED_FIELDS"
            or comparison.after != comparison.coverage.baseline
        ):
            raise ValueError("pre-claim bridge does not bind exact assessed B0")
        return
    if comparison.relation != "CURRENT_WINDOW":
        raise ValueError("current window requires full current equality")

    def check():
        a = verifier._saved(comparison.before, CURRENT_NAMESPACE)
        b = verifier._saved(comparison.after, CURRENT_NAMESPACE)
        if a.get("phase") != "before" or b.get("phase") != "after":
            raise ValueError("current acquisition phases disagree")
        if a["instance"] != baseline["instance"] or b["instance"] != baseline["instance"]:
            raise ValueError("current measurement instance mismatch")
        result = compare_current_inventories(a, b, scope, policy.coverage_measurement)
        if (
            not result["equal"]
            or a["roots"] != baseline["roots"]
            or b["roots"] != baseline["roots"]
        ):
            raise ValueError("current window changed from its complete irreversible baseline")
        # Only a wholly successful pair also proves each complete input equals B0.
        for reference in (comparison.before, comparison.after):
            _remember_current_check(
                verifier, policy, comparison.coverage, "COMPLETE_CURRENT_EQUALS_B0", (reference,)
            )

    _current_check(
        verifier,
        policy,
        comparison.coverage,
        "ORDERED_COMPLETE_CURRENT_PAIR",
        (comparison.before, comparison.after),
        check,
    )


def require_current_baseline(verifier, policy, claim, reference):
    scope, baseline = resolve_coverage(verifier, policy, claim.coverage, claim=claim)

    def check():
        current = verifier._saved(reference, CURRENT_NAMESPACE)
        validate_current_inventory(current, scope, policy.coverage_measurement)
        if current["instance"] != baseline["instance"] or current["roots"] != baseline["roots"]:
            raise ValueError("current stage/closure baseline differs from bound B0")

    _current_check(
        verifier, policy, claim.coverage, "COMPLETE_CURRENT_EQUALS_B0", (reference,), check
    )


def require_record_coverage(policy, claim, record):
    if isinstance(policy, CoveragePolicy):
        if (
            not isinstance(claim, CoverageClaim)
            or getattr(record, "coverage", None) != claim.coverage
        ):
            raise ValueError("stage/review/closure lost its irrevocable coverage caveat")
    elif getattr(record, "coverage", None) is not None:
        raise ValueError("old policy cannot reinterpret a new coverage record")
