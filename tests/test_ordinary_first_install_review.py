"""Offline first-install contracts. Run directly with Windows Python -I -S -B."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_review", ROOT / "scripts/prepare_ordinary_first_install_review.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
lab = runpy.run_path(str(ROOT / "tests/ordinary_first_install_fixture.py"))


@unittest.skipUnless(os.name == "nt", "Windows publication semantics required")
class FirstInstall(unittest.TestCase):
    def setUp(self):
        original_env = patch.dict(os.environ)
        original_env.start()
        self.addCleanup(original_env.stop)
        os.environ.pop("TRADINGAGENTS_ORDINARY_FULL_PWSH", None)
        td = tempfile.TemporaryDirectory(prefix="approval fixture ")
        self.addCleanup(td.cleanup)
        self.parent = Path(td.name)
        self.target = self.parent / "target"
        self.target.mkdir()
        (self.target / lab["ARTIFACT"]).write_bytes(lab["TEXT"])
        self.host = "synthetic-machine-A"
        approve, self.recipient = lab["training_station"]()
        self.packet = approve(self.target, self.host)

    def install(self, **changes):
        args = dict(target=self.target, host=self.host, approval=self.packet)
        return self.recipient(**{**args, **changes})

    def snapshot(self):
        return {p.relative_to(self.target): p.read_bytes() if p.is_file() else None
                for p in self.target.rglob("*")}

    def refusal(self, reason, **changes):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
            self.install(**changes)
        self.assertEqual(before, self.snapshot())

    def test_independently_approved_example_installs_once(self):
        plan = self.install()
        out = self.target / lab["ACTIVATED"]
        self.assertEqual((out / lab["ARTIFACT"]).read_bytes(), lab["TEXT"])
        self.assertEqual(json.loads((out / "training-receipt.json").read_bytes()), plan)
        self.refusal("already_installed")

    def test_unapproved_and_self_described_candidates(self):
        self.refusal("independent_approval_required", approval=None)
        claims = lab["prepare"](self.target, self.host)
        self.refusal("approval_pin_mismatch", approval=entry.canonical(claims))
        forged = entry.canonical(dict(purpose="approve-training-install", claims=claims, issuance="f"*32))
        self.refusal("approval_pin_mismatch", approval=forged)
        with self.assertRaises(TypeError):
            self.install(approval=forged, independent_pin=entry.sha(forged))
        other_authority, unapproved = lab["training_station"]()
        with self.assertRaisesRegex(ValueError, "independent_approval_required"):
            unapproved(self.target, self.host, self.packet)
        rival = other_authority(self.target, self.host)
        self.refusal("approval_pin_mismatch", approval=rival)

    def test_host_version_locator_and_bytes(self):
        self.refusal("approval_scope_mismatch", host="synthetic-machine-B")
        self.refusal("approval_scope_mismatch", version="training-release-2")
        other = self.parent / "other target"
        other.mkdir()
        (other / lab["ARTIFACT"]).write_bytes(lab["TEXT"])
        self.refusal("approval_scope_mismatch", target=other)
        self.assertEqual([p.name for p in other.iterdir()], [lab["ARTIFACT"]])
        (self.target / lab["ARTIFACT"]).write_bytes(b"changed")
        self.refusal("approval_scope_mismatch")

    def test_unexpected_files_and_production_request_cannot_install(self):
        request = entry.canonical(dict(contract="ordinary-first-install-review-request-v1", authorization="absent"))
        self.refusal("approval_pin_mismatch", approval=request)
        (self.target / "extra.txt").write_bytes(b"original")
        self.refusal("not_a_training_stage")

    def test_existing_install_and_interrupted_publication(self):
        with patch.object(Path, "rename", side_effect=OSError("controlled interruption")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.snapshot(), {Path(lab["ARTIFACT"]): lab["TEXT"]})
        present = self.target / lab["ACTIVATED"]
        present.mkdir()
        (present / "previous.txt").write_bytes(b"original")
        self.refusal("already_installed")

    def test_readonly_target_materials_and_fixture_rejection(self):
        source = self.parent / "reviewer"
        (source / "config").mkdir(parents=True)
        (self.target / "config").mkdir()
        (self.target / ".venv").mkdir()
        host_file = self.parent / "pwsh.exe"
        host_file.write_bytes(b"synthetic host, never executed")
        policy_name = "config/ordinary-full-pair-policy.json"
        policy_bytes = entry.canonical(dict(executable_name="pwsh.exe", executable_sha256=entry.sha(host_file.read_bytes())))
        (self.target / policy_name).write_bytes(policy_bytes)
        base = self.parent / "synthetic-python"
        base.mkdir()
        config_base = patch.object(entry.sys, "base_prefix", str(base))
        config_base.start()
        self.addCleanup(config_base.stop)
        cfg = f"home = {base}\ninclude-system-site-packages = false\n"
        (self.target / ".venv/pyvenv.cfg").write_text(cfg)
        runtime_files = {"python": self.target / ".venv/fixture-python", "stdlib": base / "fixture-stdlib"}
        runtime = {}
        for group, path in runtime_files.items():
            data = f"synthetic {group}".encode()
            path.write_bytes(data)
            runtime[group] = {path.name: entry.sha(data)}
        flags = {"project": {policy_name: entry.sha(policy_bytes)}, "runtime": runtime}
        raw = entry.canonical(flags)
        (source / "config/ordinary-full-release-lock.json").write_bytes(raw)
        test_pin = entry.sha(raw)
        receipt = dict(contract="ordinary-full-staged-candidate-v1", target=str(self.target), recipe_sha256=test_pin)
        (self.target / entry.STAGED).write_bytes(entry.canonical(receipt))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "reviewed_recipe_required"):
            entry.review(source, self.target, host_file)
        # In-memory unit seam only; the public command has no recipe override.
        with patch.object(entry, "LOCK_SHA256", test_pin), patch.object(entry, "machine_binding", return_value=self.host):
            result = entry.review(source, self.target, host_file)
            self.assertEqual(result["authorization"], "absent")
            self.assertEqual(result["bound_files"], 3)
            files = [dict(root="project", relative=policy_name, sha256=entry.sha(policy_bytes))]
            for group, digest_map in runtime.items():
                for name, value in digest_map.items():
                    files.append(dict(root=group, relative=name, sha256=value))
            expected = dict(contract="bounded-mainland-release-v1", deployment_root=str(self.target),
                            files=files, old_authorization_reused=False, installation_required=True)
            self.assertEqual(result["manifest_sha256"], entry.sha(json.dumps(expected, indent=2).encode()))
            self.assertEqual(before, self.snapshot())
            for path in runtime_files.values():
                original = path.read_bytes()
                path.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "bound_file_mismatch"):
                    entry.review(source, self.target, host_file)
                path.write_bytes(original)
            with patch.dict(os.environ, {"TRADINGAGENTS_ORDINARY_FULL_PWSH": str(self.parent / "another.exe")}):
                with self.assertRaisesRegex(ValueError, "powershell_locator_conflict"):
                    entry.review(source, self.target, host_file)
            (self.target / entry.FORMAL).write_bytes(b"existing")
            with self.assertRaisesRegex(ValueError, "already_installed"):
                entry.review(source, self.target, host_file)
            self.assertEqual((self.target / entry.FORMAL).read_bytes(), b"existing")

    def test_runtime_base_and_isolation_require_unambiguous_settings(self):
        cfg = self.parent / "pyvenv.cfg"
        correct = f"home = {self.parent}\ninclude-system-site-packages = false\n"
        for duplicate in ["HOME=elsewhere", "home = elsewhere", " include-system-site-packages=true"]:
            with self.subTest(duplicate=duplicate):
                cfg.write_text(correct + duplicate)
                with self.assertRaisesRegex(ValueError, "ambiguous_runtime_settings"):
                    entry.verify_venv(cfg, self.parent)


if __name__ == "__main__":
    unittest.main()
