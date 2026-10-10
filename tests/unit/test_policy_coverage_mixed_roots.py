"""Synthetic ordinary-root contract vectors; never native qualification evidence."""

import copy
import unittest

from tests.fixtures.policy_coverage_fixture import make_coverage_fixture, seal_current
from tradingagents import policy_coverage_contract as contract
from tradingagents.policy_qualified_contract import canonical_bytes, content_identity


def file_fixture(*, mixed_roots=True):
    fixture = make_coverage_fixture()
    row = {
        "path": ".",
        "kind": "file",
        "attributes": 32,
        "byte_length": 16529,
        "sha256": "a" * 64,
        "mtime_ns": 1,
    }
    for phase in ("before", "after"):
        root = {
            "path": r"D:\FIXTURE_ONLY\exact-file",
            "entries": [row],
            "file_count": 1,
            "unreadable_count": 0,
            "security_descriptors_requested": False,
            "inventory_identity": content_identity(
                "crash-protection-historical-root-inventory:v1", [row]
            ),
        }
        value = {
            "phase": phase,
            "roots": [root],
            "file_count": 1,
            "unreadable_count": 0,
            "no_acl_write_or_recovery": True,
            "elapsed_seconds": 1.0,
        }
        value["identity"] = content_identity("crash-protection-historical-inventory:v1", value)
        fixture[phase] = canonical_bytes(value) + b"\n"
    fixture["scope"] = contract.build_scope(
        fixture["before"], fixture["after"], fixture_only=True, mixed_roots=mixed_roots
    )
    payload = fixture["measurement"].model_dump(mode="json", exclude={"identity"})
    payload["scope"] = contract.artifact_ref(fixture["scope"])
    model = contract.CoverageMeasurement
    if mixed_roots:
        model = contract.RootTypedCoverageMeasurement
        payload.update(
            contract_version="policy-coverage-measurement-v3",
            root_kind_policy="EXACT_SAVED_ROOT_KINDS",
        )
    fixture["measurement"] = model.seal(**payload)
    value = fixture["baseline"]
    value["scope"] = contract.artifact_ref(fixture["scope"]).model_dump(mode="json")
    value["measurement"] = contract.artifact_ref(fixture["measurement"]).model_dump(mode="json")
    current_row = {**value["roots"][0]["entries"][0], **row}
    value["roots"] = [{**value["roots"][0], "path": root["path"], "entries": [current_row]}]
    if mixed_roots:
        value["capture"]["method"] = "generated-mixed-root-coverage-schema-vector-v3"
    fixture["baseline"] = seal_current(value)
    return fixture


class MixedRootContractTests(unittest.TestCase):
    def test_exact_file_root_is_preserved_through_known_and_current_comparison(self):
        fixture = file_fixture()
        scope, measurement = fixture["scope"], fixture["measurement"]
        self.assertEqual(scope.root_kinds, ("file",))
        self.assertEqual(contract.validate_scope(scope, fixture["before"], fixture["after"]), scope)
        assessment = contract.compare_known_fields(
            scope, fixture["before"], fixture["after"], fixture["baseline"], measurement
        )
        self.assertEqual(assessment.known_recorded_fields, "MATCH")
        self.assertEqual(assessment.hidden_contents, "UNKNOWN_NOT_RETROACTIVELY_COMPLETED")
        later = copy.deepcopy(fixture["baseline"])
        later["phase"] = "after"
        later["capture"].update(
            nonce="b" * 32,
            started_utc="2099-01-01T00:00:02+00:00",
            finished_utc="2099-01-01T00:00:03+00:00",
        )
        self.assertTrue(
            contract.compare_current_inventories(
                fixture["baseline"], seal_current(later), scope, measurement
            )["equal"]
        )
        later["roots"][0]["entries"][0]["sha256"] = "0" * 64
        self.assertFalse(
            contract.compare_current_inventories(
                fixture["baseline"], seal_current(later), scope, measurement
            )["equal"]
        )

    def test_legacy_file_scope_does_not_gain_new_acceptance(self):
        fixture = file_fixture(mixed_roots=False)
        with self.assertRaisesRegex(ValueError, "ordinary root"):
            contract.validate_current_inventory(
                fixture["baseline"], fixture["scope"], fixture["measurement"]
            )

    def test_file_root_rejects_incomplete_forged_or_drifting_evidence(self):
        cases = (
            "directory",
            "missing_root",
            "extra_root",
            "duplicate_dot",
            "descendant",
            "escape",
            "same_length_content",
            "length",
            "missing_hash",
            "missing_security",
            "changed_security",
            "link_as_file",
            "attribute_unknown_bit",
            "origin",
            "method",
            "version",
            "incomplete",
            "read_bytes",
            "over_time",
            "scope",
            "candidate",
        )
        for case in cases:
            with self.subTest(case=case):
                fixture = file_fixture()
                value = copy.deepcopy(fixture["baseline"])
                root = value["roots"][0]
                row = root["entries"][0]
                if case == "directory":
                    row.update(kind="directory", attributes=16)
                    row.pop("sha256")
                    row.pop("byte_length")
                elif case == "missing_root":
                    value["roots"] = []
                elif case == "extra_root":
                    value["roots"].append({**copy.deepcopy(root), "path": root["path"] + "-extra"})
                elif case == "duplicate_dot":
                    root["entries"].append(copy.deepcopy(row))
                elif case == "descendant":
                    root["entries"].append({**copy.deepcopy(row), "path": "invented"})
                elif case == "escape":
                    row["path"] = "../escape"
                elif case == "same_length_content":
                    row["sha256"] = "0" * 64
                elif case == "length":
                    row["byte_length"] += 1
                elif case == "missing_hash":
                    row.pop("sha256")
                elif case == "missing_security":
                    row.pop("descriptor_sha256")
                elif case == "changed_security":
                    # Security was unavailable historically, but is strict after B0.
                    row["descriptor_sha256"] = "0" * 64
                elif case == "link_as_file":
                    row["attributes"] |= 0x400
                elif case == "attribute_unknown_bit":
                    row["attributes"] |= 0x10000000
                elif case == "origin":
                    value["capture"]["origin"] = "REPLAY"
                elif case == "method":
                    value["capture"]["method"] = "generated-coverage-schema-vector-v2"
                elif case == "version":
                    value["contract_version"] = "policy-coverage-inventory-v3"
                elif case == "incomplete":
                    value["capture"]["complete"] = False
                elif case == "over_time":
                    value["capture"]["finished_utc"] = "2099-01-02T00:00:00+00:00"
                elif case == "scope":
                    value["scope"]["pin"]["sha256"] = "0" * 64
                elif case == "candidate":
                    value["candidate"]["commit"] = "0" * 40
                if case == "changed_security":
                    value["phase"] = "after"
                    value["capture"].update(
                        nonce="b" * 32,
                        started_utc="2099-01-01T00:00:02+00:00",
                        finished_utc="2099-01-01T00:00:03+00:00",
                    )
                    self.assertFalse(
                        contract.compare_current_inventories(
                            fixture["baseline"],
                            seal_current(value),
                            fixture["scope"],
                            fixture["measurement"],
                        )["equal"]
                    )
                    continue
                value = seal_current(value)
                if case == "read_bytes":
                    capture = value["capture"]
                    capture["read_bytes"] = 1
                    capture.pop("observation_identity")
                    capture["observation_identity"] = content_identity(
                        "policy-coverage-fixture-acquisition:v2", capture
                    )
                    value.pop("identity")
                    value["identity"] = content_identity("policy-coverage-inventory:v2", value)
                with self.assertRaises(ValueError):
                    contract.compare_known_fields(
                        fixture["scope"],
                        fixture["before"],
                        fixture["after"],
                        value,
                        fixture["measurement"],
                    )

    def test_capability_pairs_and_saved_root_kind_cannot_be_reinterpreted(self):
        fixture = file_fixture()
        old = file_fixture(mixed_roots=False)
        for scope, measurement in (
            (fixture["scope"], old["measurement"]),
            (old["scope"], fixture["measurement"]),
        ):
            with self.assertRaisesRegex(ValueError, "version and capability"):
                contract.read_coverage_context(scope, measurement)
        body = fixture["scope"].model_dump(mode="json", exclude={"identity"})
        body["root_kinds"] = ["directory"]
        forged = contract.RootTypedCoverageScope.seal(**body)
        with self.assertRaisesRegex(ValueError, "exact original"):
            contract.validate_scope(forged, fixture["before"], fixture["after"])

    def test_root_kind_comes_from_both_saved_acquisitions(self):
        fixture = file_fixture()
        original = __import__("json").loads(fixture["before"])
        rows = original["roots"][0]["entries"]
        rows[:] = [{"path": ".", "kind": "directory"}]
        original["roots"][0].update(
            inventory_identity=content_identity(
                "crash-protection-historical-root-inventory:v1", rows
            ),
            file_count=0,
        )
        original["file_count"] = 0
        original.pop("identity")
        original["identity"] = content_identity(
            "crash-protection-historical-inventory:v1", original
        )
        with self.assertRaisesRegex(ValueError, "original root kind changed"):
            contract.build_scope(
                canonical_bytes(original), fixture["after"], fixture_only=True, mixed_roots=True
            )

    def test_empty_file_and_archive_attributes_are_legal_without_masking_bits(self):
        fixture = file_fixture()
        value = copy.deepcopy(fixture["baseline"])
        value["roots"][0]["entries"][0].update(
            byte_length=0, sha256=__import__("hashlib").sha256(b"").hexdigest()
        )
        for attributes in (32, 128, 0x10000020):
            value["roots"][0]["entries"][0]["attributes"] = attributes
            validated = contract.validate_current_inventory(
                seal_current(value), fixture["scope"], fixture["measurement"]
            )
            self.assertEqual(validated["roots"][0]["entries"][0]["attributes"], attributes)


if __name__ == "__main__":
    unittest.main()
