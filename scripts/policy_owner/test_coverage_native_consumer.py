"""Cross-language coverage envelope vectors; no owner/child/authentication calls."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--native-fixture", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.absolute()
    source = Path(__file__).resolve().parents[2]
    if (
        out.exists()
        or out.is_relative_to(source)
        or out.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh disposable fixture output required")
    out.mkdir()
    case_root = out / "case"
    case_root.mkdir()
    sys.path[:0] = [str(source), str(args.dependencies)]
    from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
    from tests.fixtures.coverage_case import coverage_case
    from tradingagents.policy_coverage_models import CoverageEnvelope
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import canonical_bytes

    bound_evidence_process(seconds=300)
    started = time.monotonic()
    case = coverage_case(case_root)
    case.reserve()
    execution = synthetic_stage_vectors(case)
    result = case.verifier.finalize(
        policy=case.policy, approval=case.approval, claim=case.claim, execution=execution
    )
    case.owner.store(result, "result_identity")
    receipt = case.verifier.issue_receipt(result.to_bytes())
    case.owner.store(receipt, "receipt_identity")
    envelope = CoverageEnvelope(
        contract_version="policy-qualified-downstream-envelope-v2",
        core=receipt.core,
        receipt=receipt,
        execution_authorized=False,
    )
    (out / "result.json").write_bytes(result.to_bytes())
    (out / "receipt.json").write_bytes(receipt.to_bytes())
    original = envelope.model_dump(mode="json")
    vectors = [("complete", original, 0)]
    for field in (
        "scope",
        "measurement",
        "baseline",
        "assessment",
        "historical_coverage",
        "historical_object_continuity",
        "hidden_contents",
    ):
        value = copy.deepcopy(original)
        value["core"]["history"]["coverage"].pop(field)
        vectors.append(("missing-" + field, value, 2))
    for name, change in (
        (
            "old-envelope",
            lambda v: v.update(contract_version="policy-qualified-downstream-envelope-v1"),
        ),
        ("historical-equal", lambda v: v["core"]["history"].update(historical_equal=True)),
        (
            "unknown-as-complete",
            lambda v: v["core"]["history"]["coverage"].update(historical_coverage="COMPLETE"),
        ),
        (
            "receipt-downgrade",
            lambda v: v["receipt"].update(
                contract_version="policy-qualified-acceptance-receipt-v1"
            ),
        ),
        ("claim-missing-coverage", lambda v: v["core"]["lineage"].pop("coverage")),
        ("live-authority", lambda v: v.update(execution_authorized=True)),
    ):
        value = copy.deepcopy(original)
        change(value)
        vectors.append((name, value, 2))
    commands = []
    for name, value, expected in vectors:
        # Native control documents require exact canonical bytes, without LF.
        # This framing differs deliberately from retained Python artifact bytes.
        raw = canonical_bytes(value)
        path = out / (name + ".json")
        path.write_bytes(raw)
        pin = hashlib.sha256(raw).hexdigest()
        completed = subprocess.run(
            [str(args.native_fixture), str(path), pin],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (out / (name + ".stdout")).write_bytes(completed.stdout)
        (out / (name + ".stderr")).write_bytes(completed.stderr)
        commands.append(
            {
                "vector": name,
                "input_sha256": pin,
                "byte_length": len(raw),
                "exit_code": completed.returncode,
                "expected": expected,
            }
        )
        if completed.returncode != expected:
            raise ValueError(name + ": " + completed.stderr.decode("utf-8", "replace"))
    report = {
        "status": "NATIVE_COVERAGE_CONSUMER_FIXTURE_PASS",
        "cases": commands,
        "native_pin": {
            "path": str(args.native_fixture),
            "sha256": hashlib.sha256(args.native_fixture.read_bytes()).hexdigest(),
        },
        "elapsed_seconds": time.monotonic() - started,
        "native_owner_calls": 0,
        "human_authentication": False,
        "qualification": False,
        "execution_authorized": False,
    }
    (out / "NATIVE-RESULTS.json").write_bytes(canonical_bytes(report) + b"\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
