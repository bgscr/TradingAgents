"""Prospective fixtures only: no owner, capture, installed state or real approval."""

from copy import deepcopy

import pytest

from tradingagents.policy_coverage_contract import build_scope
from tradingagents.policy_qualified_contract import canonical_bytes, content_identity


def historical():
    rows = [
        {"path": ".", "kind": "directory"},
        {"path": "gap", "kind": "directory"},
        {"path": "gap", "kind": "unreadable_directory", "attributes": 16, "mtime_ns": 10},
        {"path": "known.txt", "kind": "file", "byte_length": 3, "sha256": "a" * 64},
        {"path": "link", "kind": "reparse", "size": 0, "mtime_ns": 20},
        {"path": "target", "kind": "directory"},
    ]
    root = {
        "path": "D:\\FIXTURE_ONLY\\root",
        "entries": rows,
        "file_count": 1,
        "unreadable_count": 1,
        "security_descriptors_requested": False,
        "inventory_identity": content_identity(
            "crash-protection-historical-root-inventory:v1", rows
        ),
    }
    result = {}
    for phase in ("before", "after"):
        body = {
            "phase": phase,
            "roots": [root],
            "file_count": 1,
            "unreadable_count": 1,
            "no_acl_write_or_recovery": True,
            "elapsed_seconds": 1.0,
        }
        body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
        result[phase] = canonical_bytes(body) + b"\n"
    return result


def test_scope_preserves_marker_and_directory_as_distinct_records():
    values = historical()
    scope = build_scope(values["before"], values["after"], fixture_only=True)
    assert len(scope.gaps) == 1
    assert scope.gaps[0].row["kind"] == "unreadable_directory"
    assert scope.gaps[0].companion["kind"] == "directory"
    assert scope.gaps[0].row_index != scope.gaps[0].companion_index
    assert len(scope.links) == 1
    assert scope.historical_coverage == "UNKNOWN"
    assert scope.historical_object_continuity == "NOT_ESTABLISHED"


def test_complete_new_baseline_matches_known_fields_without_claiming_history_complete():
    from tests.fixtures.policy_coverage_fixture import make_coverage_fixture
    from tradingagents.policy_coverage_contract import compare_known_fields

    f = make_coverage_fixture()
    assessment = compare_known_fields(
        f["scope"], f["before"], f["after"], f["baseline"], f["measurement"]
    )
    assert assessment.known_recorded_fields == "MATCH"
    assert assessment.historical_coverage == "UNKNOWN"
    assert assessment.historical_object_continuity == "NOT_ESTABLISHED"
    assert assessment.hidden_contents == "UNKNOWN_NOT_RETROACTIVELY_COMPLETED"
    assert [x["path"] for x in assessment.expansions] == ["gap/newly-observed.txt"]


@pytest.mark.parametrize(
    "mutation",
    [
        "known_file",
        "marker_metadata",
        "omit_gap",
        "omit_directory",
        "new_path",
        "missing_descriptor",
        "new_unreadable",
        "candidate",
        "scope",
        "version",
        "undeclared_link",
        "external_link",
        "dangling_link",
        "cycle",
        "unsupported_tag",
        "same_prefix_escape",
        "target_object",
    ],
)
def test_new_baseline_rejects_incomplete_or_misbound_evidence(mutation):
    import base64

    from tests.fixtures.policy_coverage_fixture import (
        junction_buffer,
        make_coverage_fixture,
        seal_current,
    )
    from tradingagents.policy_coverage_contract import compare_known_fields

    f = make_coverage_fixture()
    value = deepcopy(f["baseline"])
    rows = value["roots"][0]["entries"]
    by_path = {r["path"]: r for r in rows}
    link = by_path["link"]
    if mutation == "known_file":
        by_path["known.txt"]["sha256"] = "0" * 64
    elif mutation == "marker_metadata":
        by_path["gap"]["mtime_ns"] = 11
    elif mutation == "omit_gap":
        rows.remove(by_path["gap"])
        rows.remove(by_path["gap/newly-observed.txt"])
    elif mutation == "omit_directory":
        rows.remove(by_path["target"])
    elif mutation == "new_path":
        by_path["gap/newly-observed.txt"]["path"] = "unexpected.txt"
        rows.sort(key=lambda x: x["path"].casefold())
    elif mutation == "missing_descriptor":
        by_path["gap"].pop("descriptor_sha256")
    elif mutation == "new_unreadable":
        by_path["known.txt"]["kind"] = "unreadable_file"
    elif mutation == "candidate":
        value["candidate"]["commit"] = "0" * 40
    elif mutation == "scope":
        value["scope"]["pin"]["sha256"] = "0" * 64
    elif mutation == "version":
        value["contract_version"] = "policy-coverage-inventory-v1"
    elif mutation == "undeclared_link":
        link["path"] = "new-link"
        rows.sort(key=lambda x: x["path"].casefold())
    elif mutation == "unsupported_tag":
        link["tag"] = 0xA000000C
    elif mutation == "target_object":
        link["target_object_identity"]["file_index"] = "0000000000000999"
    else:
        target = {
            "external_link": "E:\\outside",
            "same_prefix_escape": "D:\\FIXTURE_ONLY\\root-suffix\\target",
            "dangling_link": "D:\\FIXTURE_ONLY\\root\\absent",
            "cycle": "D:\\FIXTURE_ONLY\\root\\link",
        }[mutation]
        link["reparse_data_base64"] = base64.b64encode(junction_buffer(target)).decode("ascii")
        link["target_relative_path"] = target.rsplit("\\", 1)[-1]
    value = seal_current(value)
    with pytest.raises(ValueError):
        compare_known_fields(f["scope"], f["before"], f["after"], value, f["measurement"])


def test_scope_cannot_drop_error_marker_while_preserving_directory():
    from tests.fixtures.policy_coverage_fixture import make_coverage_fixture
    from tradingagents.policy_coverage_contract import HistoricalCoverageScope, validate_scope

    f = make_coverage_fixture()
    value = f["scope"].model_dump(mode="json")
    value.pop("identity")
    value["gaps"] = []
    altered = HistoricalCoverageScope.seal(**value)
    with pytest.raises(ValueError, match="complete exact original record set"):
        validate_scope(altered, f["before"], f["after"])


def test_strict_new_window_rejects_known_value_change_and_reused_observation():
    from tests.fixtures.policy_coverage_fixture import make_coverage_fixture, seal_current
    from tradingagents.policy_coverage_contract import compare_current_inventories

    f = make_coverage_fixture()
    after = deepcopy(f["baseline"])
    after["phase"] = "after"
    after["capture"].update(
        nonce="b" * 32,
        started_utc="2099-01-01T00:00:02+00:00",
        finished_utc="2099-01-01T00:00:03+00:00",
    )
    after = seal_current(after)
    assert (
        compare_current_inventories(f["baseline"], after, f["scope"], f["measurement"])["equal"]
        is True
    )
    after["roots"][0]["entries"][3]["sha256"] = "0" * 64
    after = seal_current(after)
    assert (
        compare_current_inventories(f["baseline"], after, f["scope"], f["measurement"])["equal"]
        is False
    )
    with pytest.raises(ValueError, match="reused"):
        compare_current_inventories(f["baseline"], f["baseline"], f["scope"], f["measurement"])


def test_marker_repetition_is_not_a_complete_current_inventory():
    from tests.fixtures.policy_coverage_fixture import make_coverage_fixture, seal_current
    from tradingagents.policy_coverage_contract import validate_current_inventory

    f = make_coverage_fixture()
    value = deepcopy(f["baseline"])
    value["roots"][0]["entries"].append(f["scope"].gaps[0].row)
    value = seal_current(value)
    with pytest.raises(ValueError):
        validate_current_inventory(value, f["scope"], f["measurement"])
