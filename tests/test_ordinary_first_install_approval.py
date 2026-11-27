"""Offline OpenSSH verification with temporary test keys and non-executable fixture bytes."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "approval", ROOT / "scripts/verify_ordinary_first_install_approval.py"
)
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)


def write(root, name, data):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@unittest.skipUnless(os.name == "nt", "Windows target and OpenSSH required")
class Approval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ephemeral fixture signers ")
        cls.addClassCleanup(tmp.cleanup)
        cls.lab = Path(tmp.name)
        cls.exe = A.system_ssh_keygen()
        cls.env = A.openssh_environment(cls.exe)
        for key in ("lab", "other"):
            cls.ssh(
                "-q", "-t", "ed25519", "-N", "", "-C", "training-only", "-f", str(cls.lab / key)
            )

    @classmethod
    def ssh(cls, *args):
        subprocess.run(
            [str(cls.exe), *args],
            cwd=cls.lab,
            env=cls.env,
            input=b"",
            capture_output=True,
            timeout=10,
            check=True,
        )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="offline approval fixture ")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.source, self.target, self.inbox = (
            root / n for n in ("maintenance", "review target", "inbox")
        )
        self.inbox.mkdir()
        self.signer = write(
            root,
            "ordinary-full-first-install-authority/allowed_signers",
            b"tradingagents-first-install "
            + b" ".join((self.lab / "lab.pub").read_bytes().split()[:2])
            + b"\n",
        )
        self.host = write(root, "host/pwsh.exe", b"never executed")
        self.policy_name = "config/ordinary-full-pair-policy.json"
        policy = A.R["canonical"](
            {"executable_name": "pwsh.exe", "executable_sha256": A.R["sha"](self.host.read_bytes())}
        )
        self.product = write(self.target, self.policy_name, policy)
        base = root / "fixture base"
        write(base, "stdlib.txt", b"stdlib")
        write(self.target, ".venv/fixture.txt", b"runtime")
        write(
            self.target,
            ".venv/pyvenv.cfg",
            f"home = {base}\ninclude-system-site-packages = false\n".encode(),
        )
        recipe = A.R["canonical"](
            {
                "project": {self.policy_name: A.R["sha"](policy)},
                "runtime": {
                    "python": {"fixture.txt": A.R["sha"](b"runtime")},
                    "stdlib": {"stdlib.txt": A.R["sha"](b"stdlib")},
                },
            }
        )
        self.pin = A.R["sha"](recipe)
        write(self.source, "config/ordinary-full-release-lock.json", recipe)
        self.staged = {
            "contract": "ordinary-full-staged-candidate-v1",
            "recipe_sha256": self.pin,
            "target": str(self.target),
        }
        write(self.target, A.R["STAGED"], A.R["canonical"](self.staged))
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("TRADINGAGENTS_ORDINARY_FULL_PWSH", None)
        self.enterContext(patch.object(sys, "base_prefix", str(base)))
        self.enterContext(
            patch.dict(
                A.R["review"].__globals__,
                LOCK_SHA256=self.pin,
                machine_binding=lambda: "lab-machine",
            )
        )
        measured = A.R["review"](self.source, self.target, self.host)
        now = int(time.time())
        self.claims = {k: measured[k] for k in A.BINDINGS} | {
            "contract": "ordinary-first-install-approval-v1",
            "purpose": A.PURPOSE,
            "not_before": now - 60,
            "expires": now + 600,
        }

    def sign(self, *, key="lab", namespace=A.NAMESPACE, **overrides):
        # Test-only identities are generated in the test's private TemporaryDirectory.
        raw = A.R["canonical"](self.claims | overrides)
        path = write(self.inbox, "approval.json", raw)
        sig = self.inbox / "approval.json.sig"
        sig.unlink(missing_ok=True)
        self.ssh("-Y", "sign", "-f", str(self.lab / key), "-n", namespace, str(path))

    def verify(self):
        return A.verify(self.source, self.target, self.inbox, self.host)

    def test_valid_signature_measured_again_and_no_mutation(self):
        self.sign()

        def state():
            return {p: p.read_bytes() for p in self.target.rglob("*") if p.is_file()}

        before = state()
        result = self.verify()
        self.assertEqual(result["manifest_sha256"], self.claims["manifest_sha256"])
        self.assertEqual(result["status"], "signature_and_target_verified")
        self.assertFalse(result["deployment_authority_granted_by_tool"])
        self.assertFalse(result["deployment_performed"])
        self.assertEqual(before, state())
        self.assertFalse((self.target / A.R["FORMAL"]).exists())

    def test_wrong_signer_training_namespace_and_changed_envelope(self):
        for kwargs in ({"key": "other"}, {"namespace": "training-first-install"}):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(ValueError, "signature_not_verified"),
            ):
                self.sign(**kwargs)
                self.verify()
        self.sign()
        write(
            self.inbox,
            "approval.json",
            A.R["canonical"](self.claims | {"expires": self.claims["expires"] + 10}),
        )
        with self.assertRaisesRegex(ValueError, "signature_not_verified"):
            self.verify()

    def test_signed_claims_must_match_the_remeasured_target(self):
        for field in A.BINDINGS:
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "approval_mismatch_" + field),
            ):
                self.sign(**{field: "different"})
                self.verify()
        self.sign(purpose="training-or-deployment")
        with self.assertRaisesRegex(ValueError, "wrong_approval_purpose"):
            self.verify()
        self.sign(issuer_override="claimant")
        with self.assertRaisesRegex(ValueError, "invalid_approval_fields"):
            self.verify()

    def test_native_verifier_must_confirm_the_independently_measured_key(self):
        self.sign()
        with self.assertRaisesRegex(ValueError, "verified_signer_not_confirmed"):
            A.verify_signature(
                self.exe,
                self.signer,
                self.inbox / "approval.json.sig",
                (self.inbox / "approval.json").read_bytes(),
                b"different fingerprint",
            )

    def test_copy_of_target_with_new_staging_receipt_still_needs_its_own_approval(self):
        self.sign()
        new_target = self.target.with_name("relocated target")
        shutil.copytree(self.target, new_target)
        write(
            new_target, A.R["STAGED"], A.R["canonical"](self.staged | {"target": str(new_target)})
        )
        with self.assertRaisesRegex(ValueError, "approval_mismatch_target_binding"):
            A.verify(self.source, new_target, self.inbox, self.host)

    def test_changed_bytes_and_already_installed_are_rejected(self):
        self.sign()
        self.product.write_bytes(b"changed target")
        with self.assertRaisesRegex(ValueError, "bound_file_mismatch"):
            self.verify()
        old = write(self.target, A.R["FORMAL"], b"existing example")
        with self.assertRaisesRegex(ValueError, "already_installed"):
            self.verify()
        self.assertEqual(old.read_bytes(), b"existing example")

    def test_expired_future_and_expiry_during_remeasurement(self):
        now = int(time.time())
        for a, b in ((now - 2, now), (now + 100, now + 200)):
            self.sign(not_before=a, expires=b)
            with self.assertRaisesRegex(ValueError, "approval_outside_validity_window"):
                self.verify()
        self.sign(not_before=100, expires=200)
        with (
            patch.object(A.time, "time", side_effect=[199, 200]),
            self.assertRaisesRegex(ValueError, "approval_outside_validity_window"),
        ):
            self.verify()

    def test_absent_issuer_cannot_be_supplied_by_candidate_or_inbox(self):
        self.sign()
        key = self.signer.read_bytes()
        self.signer.unlink()
        write(self.inbox, "allowed_signers", key)
        write(self.target, "ordinary-full-first-install-authority/allowed_signers", key)
        with self.assertRaisesRegex(ValueError, "independent_signer_missing"):
            self.verify()
        write(self.signer.parent, "allowed_signers", key.replace(A.PRINCIPAL.encode(), b"*"))
        with self.assertRaisesRegex(ValueError, "invalid_independent_signer"):
            self.verify()
        with self.assertRaisesRegex(ValueError, "separate_directories_required"):
            A.verify(self.source, self.target, self.target, self.host)

    def test_public_cli_refuses_fixture_recipe_despite_valid_fixture_signature(self):
        self.sign()
        for script in (
            "verify_ordinary_first_install_approval.py",
            "prepare_ordinary_first_install_review.py",
        ):
            write(self.source, "scripts/" + script, (ROOT / "scripts" / script).read_bytes())
        q = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(self.source / "scripts/verify_ordinary_first_install_approval.py"),
                "--candidate",
                str(self.target),
                "--approval-dir",
                str(self.inbox),
                "--pwsh",
                str(self.host),
            ],
            input=b"",
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(q.returncode, 1)
        self.assertEqual(json.loads(q.stdout)["reason"], "reviewed_recipe_required")
        self.assertFalse((self.target / A.R["FORMAL"]).exists())


if __name__ == "__main__":
    unittest.main()
