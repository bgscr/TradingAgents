"""Actual disposable captures; no retained/protected historical roots."""

import json

import pytest

from tradingagents.policy_evidence_acquisition import (
    capture_roots,
    compare_captures,
    validate_capture_provenance,
)


def request(root):
    return {
        "contract_version": "policy-evidence-root-list-v1",
        "fixture_only": True,
        "roots": [{"path": str(root), "security_descriptors_requested": True}],
    }


@pytest.mark.windows
def test_two_real_captures_match_and_a_new_difference_blocks(tmp_path):
    root = tmp_path / "disposable-capture-root"
    root.mkdir()
    (root / "sample.txt").write_bytes(b"original fixture bytes")
    before = capture_roots(
        request(root), tmp_path / "before.json", phase="before", maximum_seconds=10
    )
    after = capture_roots(
        request(root), tmp_path / "after.json", phase="after", previous=before, maximum_seconds=10
    )
    assert before["capture"]["fixture_only"] is True
    assert before["capture"]["observation_identity"] != after["capture"]["observation_identity"]
    assert compare_captures(before, after)["equal"] is True
    assert before["capture"]["complete"] is True
    assert validate_capture_provenance(before) == before["capture"]
    (root / "sample.txt").write_bytes(b"deliberate NEW difference")
    changed = capture_roots(
        request(root), tmp_path / "changed.json", phase="after", previous=before, maximum_seconds=10
    )
    assert compare_captures(before, changed)["equal"] is False
    assert changed["equal"] is False


@pytest.mark.windows
def test_capture_is_additive_and_missing_objects_remain_visible(tmp_path):
    path = tmp_path / "missing.json"
    result = capture_roots(request(tmp_path / "absent"), path, phase="before", maximum_seconds=10)
    assert result["capture"]["complete"] is False
    assert result["unreadable_count"] > 0
    assert json.loads(path.read_bytes()) == result
    with pytest.raises(FileExistsError):
        capture_roots(request(tmp_path / "absent"), path, phase="before", maximum_seconds=10)


@pytest.mark.windows
def test_capture_refuses_an_output_inside_its_scope(tmp_path):
    root = tmp_path / "fixture-root"
    root.mkdir()
    with pytest.raises(ValueError, match="output|scope"):
        capture_roots(request(root), root / "observation.json", phase="before", maximum_seconds=10)


@pytest.mark.windows
def test_two_incomplete_captures_never_report_equal(tmp_path):
    missing = tmp_path / "MISSING"
    before = capture_roots(
        request(missing), tmp_path / "incomplete-before.json", phase="before", maximum_seconds=10
    )
    after = capture_roots(
        request(missing),
        tmp_path / "incomplete-after.json",
        phase="after",
        previous=before,
        maximum_seconds=10,
    )
    assert compare_captures(before, after)["equal"] is False
    assert after["equal"] is False


@pytest.mark.windows
@pytest.mark.parametrize(
    "field,value",
    [
        ("collector_sha256", "0" * 64),
        ("runtime", {"implementation": "invented"}),
        ("method", "unreviewed-method"),
        ("observed_entries", 0),
        ("read_bytes", 999),
        ("root_list_sha256", "0" * 64),
        ("contract_version", "policy-evidence-acquisition-v0"),
    ],
)
def test_capture_provenance_does_not_accept_resealed_wrong_method_or_coverage(
    tmp_path, field, value
):
    from tradingagents.policy_qualified_contract import content_identity

    root = tmp_path / "observed"
    root.mkdir()
    (root / "file").write_bytes(b"actual bytes")
    result = capture_roots(
        request(root), tmp_path / "observed.json", phase="before", maximum_seconds=10
    )
    result["capture"][field] = value
    result["capture"]["observation_identity"] = content_identity(
        "policy-evidence-observation:v1",
        {key: item for key, item in result["capture"].items() if key != "observation_identity"},
    )
    with pytest.raises(ValueError, match="provenance|binding"):
        validate_capture_provenance(result)
