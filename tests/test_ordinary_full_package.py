"""Offline tests for ordinary Full packaging trust boundaries."""
import json
import os
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_ordinary_full.py"
spec = importlib.util.spec_from_file_location("ordinary_package", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class OrdinaryPackageTests(unittest.TestCase):
    def test_recipe_contains_cache_verification_and_excludes_historical_release(self):
        recipe = builder.load_recipe()
        names = recipe["project"]
        self.assertIn("scripts/Get-PublicFundflow.ps1", names)
        self.assertIn("tradingagents/market_history/current_cache_compatibility.json", names)
        self.assertIn("config/ordinary-full-pair-policy.json", names)
        self.assertNotIn("tradingagents/full_five_snapshot.py", names)
        self.assertNotIn("config/bounded_mainland_release.json", names)
        self.assert_private_scope(recipe)
        tracked = subprocess.check_output(
            ["git", "-C", str(SCRIPT.parents[1]), "ls-tree", "-r", "--name-only", "HEAD", "--",
             "config/full_evidence", "config/bounded_mainland_release.json", "config/ordinary-full-package.json",
             "tradingagents.local.json", ".env", "reports/bounded-runs"],
        )
        self.assertEqual(tracked, b"")

    def assert_private_scope(self, recipe):
        allowed = {
            "config/full_evidence/XSHG/2026-11-20/completed-day.json",
            "config/full_evidence/originals/6e18d19404469c5037adbe52ddd0c3176076909a4e732a36171f99174e5cc101.bin",
            "config/full_evidence/originals/d2be3c22548e1f6f55ddf05a0ec990f08d01f26b462e593f84b2eceacb15dd31.bin",
        }
        paths = recipe["local_evidence"]["paths"]
        self.assertCountEqual(paths, allowed)
        members = recipe["project"]
        self.assertEqual({n for n in members if n.startswith("config/full_evidence/")}, allowed)
        for n in allowed:
            if n.endswith(".bin"):
                self.assertEqual(Path(n).stem, members[n])
        for n in members:
            self.assertNotIn(Path(n).name, {"tradingagents.local.json", "bounded_mainland_release.json", "ordinary-full-package.json"})
            self.assertFalse(Path(n).name.startswith(".env") or n.startswith(("reports/", "data/", "logs/", ".scratch/", "cache/")))

    def test_recipe_rejects_undeclared_evidence_and_machine_outputs(self):
        for n in ("config/full_evidence/unreviewed.bin", "reports/bounded-runs/result.json",
                  "data/market_history/ledger.sqlite3", "config/ordinary-full-package.json"):
            with self.subTest(name=n):
                recipe = builder.load_recipe()
                recipe["project"][n] = "0" * 64
                with self.assertRaises(AssertionError):
                    self.assert_private_scope(recipe)
        recipe = builder.load_recipe()
        extra = "config/full_evidence/extra.json"
        recipe["local_evidence"]["paths"].append(extra)
        recipe["project"][extra] = "0" * 64
        with self.assertRaises(AssertionError):
            self.assert_private_scope(recipe)

    def test_default_windows_checkout_preserves_recipe_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            path.write_bytes(builder.RECIPE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            expected = builder.load_recipe()
            with patch.object(builder, "RECIPE", path):
                self.assertEqual(builder.load_recipe(), expected)

    def test_changed_recipe_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.json"
            path.write_bytes(b"{}")
            with patch.object(builder, "RECIPE", path), self.assertRaisesRegex(ValueError, "release_recipe_changed"):
                builder.load_recipe()

    def test_reads_reviewed_blob_ignores_worktree_and_rejects_changed_revision(self):
        # Read immutable Git objects from this checkout. The local fixture contains only
        # an inert text file and a synthetic evidence manifest; no second checkout is needed.
        source = SCRIPT.parents[1]
        git = ["git", "--no-replace-objects", "-C", str(source)]
        git_dir = subprocess.check_output([*git, "rev-parse", "--absolute-git-dir"], text=True).strip()
        name = "README.md"
        blob = subprocess.check_output([*git, "show", "HEAD:" + name])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GIT_DIR": git_dir}):
            repo = Path(tmp).resolve()
            (repo / name).write_bytes(b"unreviewed fixture")
            config = repo / "config"
            config.mkdir()
            manifest = json.dumps({"deployment_root": str(repo), "files": []}).encode()
            (config / "bounded_mainland_release.json").write_bytes(manifest)
            recipe = {"project": {name: builder.digest(blob)},
                      "local_evidence": {"source_manifest_sha256": builder.digest(manifest), "paths": []}}
            self.assertEqual(builder.read_sources(repo, "HEAD", recipe), {name: blob})
            bad = dict(recipe, project={name: builder.digest(b"different reviewed bytes")})
            with self.assertRaisesRegex(ValueError, "unreviewed_git_source"):
                builder.read_sources(repo, "HEAD", bad)
            # Full clones also exercise real revision drift. The digest rejection above
            # is unconditional and remains meaningful in a one-commit shallow clone.
            history = subprocess.check_output([*git, "rev-list", "-2", "HEAD", "--", name], text=True).splitlines()
            if len(history) == 2 and subprocess.check_output([*git, "show", history[1] + ":" + name]) != blob:
                with self.assertRaisesRegex(ValueError, "unreviewed_git_source"):
                    builder.read_sources(repo, history[1], recipe)
            with self.assertRaisesRegex(ValueError, "missing_git_source"):
                builder.read_sources(repo, "HEAD", dict(recipe, project={"absent-fixture.txt": "0" * 64}))
            (config / "bounded_mainland_release.json").write_bytes(manifest + b" ")
            with self.assertRaisesRegex(ValueError, "local_evidence_manifest_changed"):
                builder.read_sources(repo, "HEAD", recipe)


if __name__ == "__main__":
    unittest.main()
