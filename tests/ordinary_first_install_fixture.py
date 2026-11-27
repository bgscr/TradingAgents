"""Offline lab only. Atomically install a fixed, non-executable example in a temporary directory."""
import hmac
import json
import os
from pathlib import Path
import runpy
import secrets
import sys
import tempfile

review = runpy.run_path(str(Path(__file__).absolute().parents[1] / "scripts/prepare_ordinary_first_install_review.py"))
canonical = review["canonical"]
sha = review["sha"]
require = review["require"]
ordinary = review["ordinary"]
target_binding = review["target_binding"]
VERSION = "training-release-1"
TEXT = b"Ordinary first-install synthetic fixture.\n"
ARTIFACT = "example.txt"
ACTIVATED = "installed-example"
DOMAIN = "training-only-first-install-v1"


def describe(target, host, content, version):
    return dict(domain=DOMAIN, version=version, target=target_binding(target), host=host,
                artifact=ARTIFACT, artifact_sha256=sha(content))


def prepare(target, host, version=VERSION):
    ordinary(target)
    return describe(target, host, review["small_file"](target / ARTIFACT, 1024), version)


def training_station():
    """Separate reviewer and recipient interfaces. Only the reviewer changes trusted state."""
    issued_digest = None

    def approve(approved_target, approved_host):
        nonlocal issued_digest
        claims = describe(approved_target, approved_host, TEXT, VERSION)
        packet = canonical(dict(purpose="approve-training-install", issuance=secrets.token_hex(16), claims=claims))
        issued_digest = sha(packet)
        return packet

    def install(target, host, approval, version=VERSION):
        require(issued_digest is not None and isinstance(approval, bytes), "independent_approval_required")
        require(hmac.compare_digest(sha(approval), issued_digest), "approval_pin_mismatch")
        return _publish_verified_example(target, host, approval, version)

    return approve, install


def _publish_verified_example(target, host, approval, version):
    require(os.name == "nt", "windows_required")
    ordinary(target)
    # Only the two fixed training names are allowed in this new fixture stage.
    names = {p.name for p in target.iterdir()}
    require(ACTIVATED not in names, "already_installed")
    require(names == {ARTIFACT}, "not_a_training_stage")
    claims = json.loads(approval, object_pairs_hook=review["unique"])
    contents = review["small_file"](target / ARTIFACT, 1024)
    plan = describe(target, host, contents, version)
    require(isinstance(claims, dict) and set(claims) == {"purpose", "issuance", "claims"}
            and claims["purpose"] == "approve-training-install" and claims["claims"] == plan
            and isinstance(claims["issuance"], str) and len(claims["issuance"]) == 32,
            "approval_scope_mismatch")
    require(version == VERSION and contents == TEXT, "fixture_release_mismatch")
    # Private rehearsal directories are exclusively owned by this test. Windows rename
    # refuses an existing destination, including one created after the initial check.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix="fixture-publish-") as holder:
        staging = Path(holder) / "publication"
        staging.mkdir()
        (staging / ARTIFACT).write_bytes(contents)
        (staging / "training-receipt.json").write_bytes(canonical(plan))
        require(prepare(target, host, version) == plan, "candidate_changed")
        require({p.name for p in target.iterdir()} == {ARTIFACT}, "stage_changed")
        staging.rename(target / ACTIVATED)
    return plan


def main():
    if os.name != "nt" or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("Use Windows Python -I -S -B")
    with tempfile.TemporaryDirectory(prefix="first install example ") as folder:
        target = Path(folder) / "target"
        target.mkdir()
        (target / ARTIFACT).write_bytes(TEXT)
        approve, install = training_station()
        packet = approve(target, "lab-machine-A")
        plan = install(target, "lab-machine-A", packet)
        assert (target / ACTIVATED / ARTIFACT).read_bytes() == TEXT
        assert json.loads((target / ACTIVATED / "training-receipt.json").read_bytes()) == plan
        print(json.dumps(dict(installed="temporary_training_example", production_approval=False)))


if __name__ == "__main__":
    main()
