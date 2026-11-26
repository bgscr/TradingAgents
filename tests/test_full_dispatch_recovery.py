"""Offline regression checks for Full request ownership and terminal recovery."""
import pytest

from tradingagents import full_dispatch as dispatch


def test_replaying_request_cannot_claim_a_second_run(tmp_path):
    database = tmp_path / "requests.sqlite3"
    params = {"contract": "dispatch-test"}
    dispatch.initialize(database)
    dispatch.prepare(database, "key", params)
    run = dispatch.claim(database, "key", params)
    assert run
    dispatch.prepare(database, "key", params)
    assert dispatch.claim(database, "key", params) is None
    assert dispatch.lookup(database, "key", params) == ("accepted_terminal_unknown", run, None)
    with pytest.raises(ValueError, match="request parameter mismatch"):
        dispatch.prepare(database, "key", {"contract": "different"})

    digest = "a" * 64
    dispatch.finish(database, "key", params, run, "completed", digest)
    dispatch.finish(database, "key", params, run, "completed", digest)
    assert dispatch.lookup(database, "key", params) == ("completed", run, digest)
    with pytest.raises(ValueError, match="terminal binding mismatch"):
        dispatch.finish(database, "key", params, run, "failed", digest)


def test_read_only_lookup_does_not_create_a_database(tmp_path):
    database = tmp_path / "missing.sqlite3"
    assert dispatch.lookup(database, "key", {}) == ("unknown", None, None)
    assert not database.exists()

