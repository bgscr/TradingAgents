"""Portable handoff exercises only temporary synthetic release inputs on Windows."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_tool():
    path = Path(__file__).absolute().parents[1] / "scripts/handoff_ordinary_full.py"
    spec = importlib.util.spec_from_file_location("handoff", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


H = load_tool()


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@unittest.skipUnless(os.name == "nt", "Windows directory binding required")
class PortableHandoff(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ordinary public handoff ")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.authority, self.origin, self.pkg, self.target, self.base = (
            root / n
            for n in (
                "independent reviewer",
                "evidence source",
                "received",
                "fresh target B",
                "runtime",
            )
        )
        self.pwsh = write(root, "host/pwsh.exe", b"fixture; never executed")
        self.policy = "config/ordinary-full-pair-policy.json"
        self.verifier = "scripts/analyze_mainland_bounded.py"
        self.payload = {
            "notes.txt": b"public fixture\r\n",
            self.policy: H.canonical(
                {"executable_name": "pwsh.exe", "executable_sha256": H.sha(self.pwsh.read_bytes())}
            ),
            self.verifier: (
                b"def verify_release(manifest, *, source, python_root):\n"
                b"    if str(source) != manifest['deployment_root'] or python_root != source / '.venv':\n"
                b"        raise RuntimeError('wrong_fixture_release')\n"
            ),
        }
        self.evidence = {
            f"evidence/sample-{i}.txt": f"local synthetic {i}".encode() for i in range(3)
        }
        self.manifest = {
            "deployment_root": str(self.origin),
            "files": [
                {"root": "project", "relative": p, "sha256": H.sha(data)}
                for p, data in self.evidence.items()
            ],
        }
        self.proof = write(self.origin, H.R["FORMAL"], H.canonical(self.manifest))
        for p, data in self.evidence.items():
            write(self.origin, p, data)
        self.spec = {
            "project": {p: H.sha(b) for p, b in (self.payload | self.evidence).items()},
            "runtime": {
                "python": {"runtime.txt": H.sha(b"runtime")},
                "stdlib": {"base.txt": H.sha(b"base")},
            },
            "local_evidence": {
                "paths": list(self.evidence),
                "source_manifest_sha256": H.sha(self.proof.read_bytes()),
            },
        }
        raw = H.canonical(self.spec)
        write(self.authority, H.LOCK, raw)
        builder = b"def source_manifest_path(origin, backup, target):\n    return origin / 'config/bounded_mainland_release.json'\n"
        write(self.authority, "scripts/build_ordinary_full.py", builder)
        for owner, name, value in (
            (H, "PIN", H.sha(raw)),
            (H, "BUILDER_SHA", H.sha(builder)),
            (sys, "base_prefix", str(self.base)),
        ):
            self.enterContext(patch.object(owner, name, value))
        self.enterContext(
            patch.dict(
                H.R["review"].__globals__, LOCK_SHA256=H.sha(raw), machine_binding=lambda: "lab"
            )
        )
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("TRADINGAGENTS_ORDINARY_FULL_PWSH", None)
        self.assertEqual(H.recipe(self.authority), self.spec)
        git_root = root / "source A"
        git_root.mkdir()
        public_dir = root / "source handoff"

        def read_git(repo, args, request=b""):
            self.assertEqual(repo, git_root)
            if args == ["rev-parse", "--show-toplevel"]:
                return str(repo).encode() + b"\n"
            self.assertEqual(args, ["cat-file", "--batch"])
            lines = request.decode().splitlines()
            self.assertEqual(lines, [H.RELEASE + ":" + n for n in self.payload])
            chunks = []
            for n in self.payload:
                data = self.payload[n].replace(b"\r\n", b"\n")
                chunks.append(f"fixture blob {len(data)}\n".encode() + data + b"\n")
            return b"".join(chunks)

        with patch.object(H, "git", read_git):
            H.export(git_root, public_dir, self.spec)
        shutil.copytree(public_dir, self.pkg)
        self.assertEqual(set(snapshot(self.pkg)), set(self.payload) | {H.INDEX})
        H.reserve(self.authority, self.target)
        for n, data in self.evidence.items():
            write(self.target, n, data)
        write(self.base, "base.txt", b"base")
        write(self.target, ".venv/runtime.txt", b"runtime")
        write(
            self.target,
            ".venv/pyvenv.cfg",
            f"home = {self.base}\ninclude-system-site-packages = false\n".encode(),
        )

    def stage(self):
        H.stage(self.authority, self.pkg, self.origin, self.target, self.pwsh, None, self.spec)

    def rejected(self, code):
        before = snapshot(self.target)
        with self.assertRaisesRegex(H.R["Refused"], code):
            self.stage()
        self.assertEqual(snapshot(self.target), before)

    def test_relocated_public_bytes_stage_and_independent_review(self):
        before = snapshot(self.target)
        self.stage()
        after = snapshot(self.target)
        self.assertEqual(before, {name: after[name] for name in before})
        self.assertEqual(set(after) - set(before), set(self.payload) | {H.R["STAGED"]})
        self.assertEqual(json.loads(after[H.R["STAGED"]])["target"], str(self.target))
        request = H.R["review"](self.authority, self.target, self.pwsh)
        self.assertEqual(request["bound_files"], len(self.spec["project"]) + 2)
        self.assertEqual(
            (request["authorization"], request["status"]), ("absent", "awaiting_independent_review")
        )
        self.assertNotIn(H.R["FORMAL"], after)
        self.rejected("unexpected_member")

    def test_refuse_extra_files_and_old_receipts(self):
        for n in [".env", H.R["STAGED"], H.R["FORMAL"], next(iter(self.evidence))]:
            with self.subTest(n=n):
                path = write(self.pkg, n, b"ordinary sample")
                self.rejected("unexpected_member")
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()

    def test_refuse_modified_content_and_self_declared_index(self):
        data = b"unreviewed notes"
        write(self.pkg, "notes.txt", data)
        self.rejected("transfer_digest_mismatch")
        packet = json.loads((self.pkg / H.INDEX).read_bytes())
        packet["files"]["notes.txt"] = H.sha(data)
        write(self.pkg, H.INDEX, H.canonical(packet))
        self.rejected("transfer_index_mismatch")

    def test_wrong_version(self):
        packet = json.loads((self.pkg / H.INDEX).read_bytes())
        packet["revision"] = "unreviewed"
        write(self.pkg, H.INDEX, H.canonical(packet))
        self.rejected("transfer_index_mismatch")

    def test_authority_recipe_remains_independent(self):
        write(self.authority, H.LOCK, b"{}")
        with self.assertRaisesRegex(H.R["Refused"], "reviewed_recipe_required"):
            H.recipe(self.authority)

    def test_reject_reserved_or_parent_components_and_overlapping_locations(self):
        for component in ["..", "reserved.", "con.txt"]:
            with self.subTest(component=component), self.assertRaises(H.R["Refused"]):
                H.plain(self.target / component / "ordinary.txt")
        with self.assertRaisesRegex(H.R["Refused"], "separate_directories_required"):
            H.reserve(self.authority, self.authority / "child")

    def test_existing_target_and_formal_preserved(self):
        marker = write(self.target, H.R["FORMAL"], b"prior sample")
        with self.assertRaises(FileExistsError):
            H.reserve(self.authority, self.target)
        self.rejected("unexpected_member")
        self.assertEqual(marker.read_bytes(), b"prior sample")

    def test_missing_and_wrong_target_evidence(self):
        name = next(iter(self.evidence))
        (self.target / name).unlink()
        self.rejected("missing_member")
        write(self.target, name, b"unverified")
        self.rejected("evidence_binding_mismatch")

    def test_wrong_source_and_wrong_manifest(self):
        manifest_dir = self.origin.with_name("other local origin")
        shutil.copytree(self.origin, manifest_dir)
        self.origin = manifest_dir
        self.rejected("evidence_root_mismatch")
        write(manifest_dir, H.R["FORMAL"], b"{}")
        self.rejected("evidence_source_mismatch")

    def test_runtime_and_host_fail_before_any_public_copy(self):
        write(self.base, "base.txt", b"other runtime")
        self.rejected("runtime_digest_mismatch")
        write(self.base, "base.txt", b"base")
        self.pwsh.write_bytes(b"other executable")
        self.rejected("powershell_mismatch")

    def test_verifier_refusal_leaves_no_staging_receipt(self):
        with (
            patch.object(H, "release_check", side_effect=RuntimeError("fixture rejection")),
            self.assertRaisesRegex(RuntimeError, "fixture rejection"),
        ):
            self.stage()
        self.assertFalse((self.target / H.R["STAGED"]).exists())
        self.assertFalse((self.target / H.R["FORMAL"]).exists())
        self.rejected("unexpected_member")

    def test_install_marker_appearing_during_verification_is_preserved(self):
        installed = b"independent sample"
        with (
            patch.object(
                H,
                "release_check",
                side_effect=lambda target, spec: write(target, H.R["FORMAL"], installed),
            ),
            self.assertRaisesRegex(H.R["Refused"], "already_installed"),
        ):
            self.stage()
        self.assertFalse((self.target / H.R["STAGED"]).exists())
        self.assertEqual((self.target / H.R["FORMAL"]).read_bytes(), installed)


if __name__ == "__main__":
    unittest.main()
