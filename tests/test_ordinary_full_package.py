"""Offline tests for ordinary Full packaging trust boundaries."""
import hashlib
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
        self.assertFalse(any(name.startswith("config/full_evidence/") for name in names))

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
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid",
                   "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-C", str(repo)]
            subprocess.run(git + ["init", "--quiet"], check=True)
            path = repo / "module.py"
            approved = b"value = 1\n"
            path.write_bytes(approved)
            subprocess.run(git + ["add", "module.py"], check=True)
            subprocess.run(git + ["commit", "-qm", "fixture"], check=True)
            recipe = {"project": {"module.py": hashlib.sha256(approved).hexdigest()}}
            path.write_bytes(b"value = 2\n")
            self.assertEqual(builder.read_sources(repo, "HEAD", recipe), {"module.py": approved})
            subprocess.run(git + ["commit", "-qam", "changed fixture"], check=True)
            with self.assertRaisesRegex(ValueError, "unreviewed_git_source"):
                builder.read_sources(repo, "HEAD", recipe)
            with self.assertRaisesRegex(ValueError, "missing_git_source"):
                builder.read_sources(repo, "HEAD", {"project": {"absent.py": "0" * 64}})


if __name__ == "__main__":
    unittest.main()
