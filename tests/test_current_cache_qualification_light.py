"""Import-light manifest contract tests; synthetic sources only, no runtime imports."""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tradingagents.dataflows import current_cache_qualification as q


class QualificationLightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qualification-light-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for rel in (*q.SOURCE_FILES, *q.VERIFICATION_FILES):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# synthetic qualification source\r\n")
        self.path = self.root / "manifest.json"
        details = {
            name: {"live": "synthetic", "restored": "synthetic", "lineage": "fixture", "rows": 21}
            for name in q.SCENARIOS
        }
        matrix = {
            name: {"passed": True, "evidence_sha256": q.digest(proof)}
            for name, proof in details.items()
        }
        sources = q.source_fingerprints(source_root=self.root)
        report = {
            "matrix": matrix, "details": details,
            "verification_sources": {name: sources[name] for name in q.VERIFICATION_FILES},
            "scope": "isolated-synthetic-current-cache-only",
            "mechanism_phase": "direct-codec-before-manifest",
        }
        self.manifest = {
            "contract": q.MANIFEST_CONTRACT, "mechanism": q.CONTRACT,
            "sources": sources, "matrix": matrix, "details": details,
            "matrix_sha256": q.digest(matrix),
            "evidence": {"path": "synthetic.json", "sha256": q.digest(report),
                         "scope": "isolated-synthetic-current-cache-only"},
        }

    def diagnostic(self, value=None):
        self.path.write_text(q.canonical(self.manifest if value is None else value), encoding="utf-8")
        return q.compatibility_diagnostic(manifest_path=self.path, source_root=self.root)

    def test_complete_synthetic_contract(self):
        self.assertIsNone(self.diagnostic())
        self.assertEqual(len(q.SOURCE_FILES + q.VERIFICATION_FILES), 39)
        self.assertEqual(len(q.SCENARIOS), 12)

    def test_extracted_validator_bytes_are_bound(self):
        (self.root / "tradingagents/dataflows/current_cache_qualification.py").write_bytes(b"changed")
        self.assertIn("source_mismatch", self.diagnostic())

    def test_wrong_explicit_root_cannot_fall_back(self):
        self.diagnostic()
        self.assertIsNotNone(q.compatibility_diagnostic(
            manifest_path=self.path, source_root=self.root / "missing"))

    def test_no_source_or_scenario_may_be_omitted(self):
        for category, key in (("sources", q.SOURCE_FILES[-1]), ("matrix", q.SCENARIOS[0]),
                              ("details", q.SCENARIOS[-1])):
            with self.subTest(category=category):
                changed = deepcopy(self.manifest)
                del changed[category][key]
                self.assertIsNotNone(self.diagnostic(changed))

    def test_unknown_manifest_field_rejected(self):
        changed = {**self.manifest, "extra": True}
        self.assertIn("shape", self.diagnostic(changed))

    def test_matrix_and_detail_tampering_rejected(self):
        for category, field, value in (("matrix", "passed", False),
                                       ("details", "restored", "different"),
                                       ("details", "rows", 20), ("details", "lineage", "")):
            with self.subTest(category=category, field=field):
                changed = deepcopy(self.manifest)
                changed[category][q.SCENARIOS[0]][field] = value
                self.assertIsNotNone(self.diagnostic(changed))

    def test_semantic_failures_with_consistent_hashes_rejected(self):
        for category, field, value, expected in (
            ("matrix", "passed", False, ":scenario"),
            ("details", "restored", "different", ":scenario_evidence"),
            ("details", "rows", 20, ":scenario_evidence"),
            ("details", "lineage", "", ":scenario_evidence"),
        ):
            with self.subTest(category=category, field=field):
                changed = deepcopy(self.manifest)
                changed[category][q.SCENARIOS[0]][field] = value
                for name, proof in changed["details"].items():
                    changed["matrix"][name]["evidence_sha256"] = q.digest(proof)
                changed["matrix_sha256"] = q.digest(changed["matrix"])
                report = {
                    "matrix": changed["matrix"], "details": changed["details"],
                    "verification_sources": {
                        name: changed["sources"][name] for name in q.VERIFICATION_FILES
                    },
                    "scope": "isolated-synthetic-current-cache-only",
                    "mechanism_phase": "direct-codec-before-manifest",
                }
                changed["evidence"]["sha256"] = q.digest(report)
                self.assertTrue(self.diagnostic(changed).endswith(expected))

    def test_evidence_digest_and_scope_rejected(self):
        for field, value in (("sha256", "0" * 64), ("scope", "real-data")):
            with self.subTest(field=field):
                changed = deepcopy(self.manifest)
                changed["evidence"][field] = value
                self.assertIsNotNone(self.diagnostic(changed))

    def test_duplicate_json_key_rejected(self):
        self.path.write_text('{"contract":1,"contract":2}', encoding="utf-8")
        self.assertIn("duplicate JSON object key", q.compatibility_diagnostic(
            manifest_path=self.path, source_root=self.root))

    def test_only_crlf_normalized(self):
        path = self.root / q.SOURCE_FILES[0]
        path.write_bytes(b"a\r\nb\rc\n")
        from hashlib import sha256
        self.assertEqual(q.source_fingerprints(source_root=self.root)[q.SOURCE_FILES[0]],
                         sha256(b"a\nb\rc\n").hexdigest())

    def test_verification_digest_contract_preserved(self):
        changed = deepcopy(self.manifest)
        changed["sources"][q.VERIFICATION_FILES[0]] = "Z" * 64
        self.assertIn("source_mismatch", self.diagnostic(changed))

    def test_canonical_rejects_nan_and_keeps_unicode(self):
        with self.assertRaises(ValueError):
            q.canonical({"bad": float("nan")})
        self.assertEqual(q.canonical({"b": 2, "a": "中"}), '{"a":"中","b":2}')
