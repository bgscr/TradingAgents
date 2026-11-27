"""Windows maintainer regression for the fixed, unapproved public Git export."""

import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
X = runpy.run_path(str(ROOT / "scripts/export_latest_source_review.py"))
LAB = runpy.run_path(str(ROOT / "tests/test_ordinary_latest_recipe.py"))
H = X["H"]


class FixedLatest(LAB["LatestRecipe"]):
    @classmethod
    def prepare_public(cls):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(ROOT / "scripts/export_latest_source_review.py"),
                str(cls.public),
            ],
            capture_output=True,
            timeout=90,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        cls.draft = json.loads((cls.public / X["INDEX"]).read_bytes())
        cls.pins = cls.draft["public_project"]
        key = "scripts/full_calendar_cache.py"
        expected = cls.draft["proposed_changes"][key]["before"]
        data = H["git"](ROOT, ["cat-file", "blob", H["RELEASE"] + ":" + key]).replace(
            b"\r\n", b"\n"
        )
        old = next(b for b in (data, data.replace(b"\n", b"\r\n")) if H["sha"](b) == expected)
        LAB["write"](cls.root / "frozen control", key, old)

    @staticmethod
    def historical_inputs(key):
        return H["public"](X["parent_recipe"]()), H["git"](
            ROOT, ["cat-file", "blob", H["RELEASE"] + ":" + key]
        )

    def test_fixed_git_bytes_and_formal_stage_rejection(self):
        self.assertEqual(self.draft["source_revision"], X["REVISION"])
        self.assertEqual(self.draft["status"], "unapproved")
        self.assertEqual(len(self.pins), 303)
        for name in X["ISSUE7"]:
            raw = (self.public / name).read_bytes()
            self.assertEqual(raw, H["git"](ROOT, ["cat-file", "blob", X["REVISION"] + ":" + name]))
            self.assertEqual(H["sha"](raw), self.pins[name])
        with self.assertRaisesRegex(ValueError, "unexpected_member"):
            H["verify"](self.public, X["parent_recipe"]())
        with self.assertRaisesRegex(ValueError, "output_exists"):
            X["export"](self.public)

    def test_corrupt_export_is_not_published(self):
        output = self.root / "rejected export"
        original_create = H["create"]

        def changed(path, data):
            if path.name == "current_statement_cache.py":
                data += b"\n"
            original_create(path, data)

        with (
            patch.dict(H, create=changed),
            self.assertRaisesRegex(ValueError, "export_content_changed"),
        ):
            X["export"](output)
        self.assertFalse(output.exists())

    def test_expired_calendar_is_refused(self):
        self.seed("expired")
        code, data = self.launch()
        self.assertNotEqual(code, 0)
        self.assertEqual(data["error"], "latest_calendar_expired")

    def test_review_cannot_change_security_date_or_observation(self):
        self.seed()
        claim = json.loads((self.site / self.review_key).read_bytes())
        for key, value, reason in [
            ("security", "600000.SS", "target_evidence_mismatch_security"),
            ("trading_date", "2026-11-25", "target_evidence_mismatch_trading_date"),
            (
                "calendar_observed_at",
                "2026-11-26T16:59:59+08:00",
                "target_calendar_observation_mismatch",
            ),
        ]:
            with self.subTest(key=key):
                self.review(claim | {key: value})
                code, data = self.launch()
                self.assertNotEqual(code, 0)
                self.assertEqual(data["error"], reason)


if __name__ == "__main__":
    unittest.main()
