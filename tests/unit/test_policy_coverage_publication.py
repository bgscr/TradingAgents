"""Actual no-replace publication while source object and ancestors stay held."""

import os

import pytest

from tests.native_policy_gate import native_policy_test
from tradingagents.policy_evidence_acquisition import new_evidence_output

pytestmark = native_policy_test


def test_publication_uses_held_parent_not_process_working_directory(tmp_path, monkeypatch):
    target = tmp_path / "evidence"
    target.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    output = target / "complete.json"
    with new_evidence_output(output) as stream:
        stream.write(b'{"complete":true}\n')
        with pytest.raises(OSError):
            os.rename(target, tmp_path / "substituted")
    assert output.read_bytes() == b'{"complete":true}\n'
    assert not (unrelated / output.name).exists()
    assert not output.with_name(output.name + ".partial").exists()


def test_existing_destination_is_never_replaced(tmp_path):
    output = tmp_path / "retained.json"
    output.write_bytes(b"old evidence")
    with pytest.raises(FileExistsError), new_evidence_output(output):
        pytest.fail("existing output unexpectedly opened")
    assert output.read_bytes() == b"old evidence"


def test_destination_created_during_stream_does_not_get_replaced(tmp_path):
    output = tmp_path / "raced.json"
    with pytest.raises(OSError), new_evidence_output(output) as stream:
        stream.write(b"complete new bytes")
        output.write_bytes(b"other complete object")
    assert output.read_bytes() == b"other complete object"
    assert (tmp_path / "raced.json.partial").read_bytes() == b"complete new bytes"


def test_interrupted_stream_retains_partial_and_no_published_output(tmp_path):
    output = tmp_path / "incomplete.json"
    with pytest.raises(RuntimeError, match="cancel"), new_evidence_output(output) as stream:
        stream.write(b"partial")
        raise RuntimeError("cancel")
    assert not output.exists()
    assert (tmp_path / "incomplete.json.partial").read_bytes() == b"partial"


def test_preexisting_partial_alias_is_not_followed_or_changed(tmp_path):
    from tests.unit.test_policy_coverage_native import make_junction

    target = tmp_path / "other"
    target.mkdir()
    output = tmp_path / "alias.json"
    make_junction(tmp_path / "alias.json.partial", target)
    with pytest.raises(OSError), new_evidence_output(output):
        pytest.fail("alias output unexpectedly opened")
    assert not output.exists() and list(target.iterdir()) == []
