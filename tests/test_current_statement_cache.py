"""Synthetic public-boundary checks for CURRENT announcement-window caching."""

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)

SCOPE = StatementCacheScope("account-a", "instrument-v1", "income", "https-v1", "v1")
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def day(number):
    return date(2026, 1, number)


def test_independent_instances_reuse_fresh_windows_and_only_fetch_gaps(tmp_path):
    path = tmp_path / "statements.sqlite3"
    first = CurrentStatementCache(path).read_through(
        SCOPE, day(3), day(5), NOW, lambda start, end: b'{"rows":[1]}'
    )
    requests = []

    def fetch(start, end):
        requests.append((start, end))
        return b'{"rows":[]}'

    second = CurrentStatementCache(path).read_through(
        SCOPE, day(1), day(7), NOW + timedelta(hours=1), fetch
    )
    assert requests == [(day(1), day(2)), (day(6), day(7))]
    assert second.reused_ranges == ((day(3), day(5)),)
    assert second.fetched_ranges == tuple(requests)
    assert second.missing_ranges == second.stale_ranges == ()
    assert second.failure is None and second.cache_failure is None
    assert [(c.range_start, c.range_end) for c in second.chunks] == [
        (day(1), day(2)),
        (day(3), day(5)),
        (day(6), day(7)),
    ]
    assert second.chunks[1].payload == first.chunks[0].payload
    assert second.chunks[1].checked_at == NOW


def test_expiry_and_explicit_refresh_replace_only_selected_windows(tmp_path):
    path = tmp_path / "statements.sqlite3"
    cache = CurrentStatementCache(path)
    original = cache.read_through(SCOPE, day(1), day(7), NOW, lambda *_: b'{"v":1}')
    refreshed = cache.read_through(
        SCOPE,
        day(3),
        day(5),
        NOW + timedelta(hours=1),
        lambda *_: b'{"v":2}',
        refresh=True,
    )
    assert refreshed.fetched_ranges == ((day(3), day(5)),)
    assert refreshed.reused_ranges == ()
    assert original.chunks[0].payload == b'{"v":1}'
    requests = []

    def revised(start, end):
        requests.append((start, end))
        return b'{"v":3}'

    expired = CurrentStatementCache(path).read_through(
        SCOPE, day(1), day(7), NOW + timedelta(hours=24), revised
    )
    assert requests == [(day(1), day(2)), (day(6), day(7))]
    assert [c.payload for c in expired.chunks] == [b'{"v":3}', b'{"v":2}', b'{"v":3}']
    assert expired.chunks[1].checked_at == NOW + timedelta(hours=1)
    assert expired.stale_ranges == ()


def test_partial_failure_retains_success_and_original_stale_observation(tmp_path):
    path = tmp_path / "statements.sqlite3"
    cache = CurrentStatementCache(path)
    cache.read_through(SCOPE, day(3), day(5), NOW + timedelta(hours=2), lambda *_: b"{}")
    cache.read_through(SCOPE, day(8), day(9), NOW, lambda *_: b'{"old":true}')
    failure = RuntimeError("synthetic unavailable")
    requests = []

    def fetch(start, end):
        requests.append((start, end))
        if start == day(6):
            raise failure
        return b'{"new":true}'

    result = CurrentStatementCache(path).read_through(
        SCOPE, day(1), day(10), NOW + timedelta(hours=25), fetch
    )
    assert requests == [(day(1), day(2)), (day(6), day(10))]
    assert result.failure is failure
    assert result.failure_range == (day(6), day(10))
    assert result.fetched_ranges == ((day(1), day(2)),)
    assert result.reused_ranges == ((day(3), day(5)),)
    assert result.stale_ranges == ((day(8), day(9)),)
    assert result.missing_ranges == ((day(6), day(7)), (day(10), day(10)))
    assert [chunk.checked_at for chunk in result.chunks] == [
        NOW + timedelta(hours=25),
        NOW + timedelta(hours=2),
        NOW,
    ]
    preserved = CurrentStatementCache(path).read_through(
        SCOPE,
        day(8),
        day(9),
        NOW + timedelta(hours=26),
        lambda *_: (_ for _ in ()).throw(failure),
    )
    assert preserved.chunks[0].payload == b'{"old":true}'
    assert preserved.chunks[0].checked_at == NOW
    assert preserved.stale_ranges == ((day(8), day(9)),)


@pytest.mark.parametrize("damage", ["hash", "json", "timestamp", "scope", "window"])
def test_damaged_cached_observations_are_missing_not_fresh_hits(tmp_path, damage):
    path = tmp_path / "statements.sqlite3"
    CurrentStatementCache(path).read_through(SCOPE, day(1), day(5), NOW, lambda *_: b"{}")
    # Inject damage at the SQLite persistence boundary; observe only read_through.
    with sqlite3.connect(path) as connection:
        if damage == "hash":
            connection.execute("UPDATE observations SET payload=?", (b'{"bad":1}',))
        elif damage == "json":
            connection.execute(
                "UPDATE observations SET payload=?,payload_sha256=?",
                (b"broken", sha256(b"broken").hexdigest()),
            )
        elif damage == "timestamp":
            connection.execute("UPDATE observations SET checked_at='2026-10-15T00:00:00'")
        elif damage == "scope":
            connection.execute("UPDATE observations SET scope='different-scope'")
        else:
            connection.execute("UPDATE observations SET range_end='2026-01-02'")
    failure = RuntimeError("synthetic unavailable")
    result = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW + timedelta(hours=1),
        lambda *_: (_ for _ in ()).throw(failure),
    )
    assert result.chunks == result.reused_ranges == ()
    assert result.missing_ranges == ((day(1), day(5)),)
    assert result.failure is failure
    assert result.cache_failure == "cache_corrupt"


def test_failed_commit_keeps_original_selection_and_fetch_runs_outside_transaction(tmp_path):
    path = tmp_path / "statements.sqlite3"
    cache = CurrentStatementCache(path)
    cache.read_through(SCOPE, day(1), day(7), NOW, lambda *_: b'{"v":1}')

    def fetch(*_):
        with sqlite3.connect(path, timeout=0) as connection:
            # A competing writer succeeds only when fetch is outside the cache transaction.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TRIGGER synthetic_commit_failure BEFORE INSERT ON active_windows
                WHEN NEW.range_start='2026-01-03'
                BEGIN SELECT RAISE(ABORT, 'synthetic persistence failure'); END
            """)
        return b'{"v":2}'

    result = cache.read_through(
        SCOPE,
        day(3),
        day(5),
        NOW + timedelta(hours=1),
        fetch,
        refresh=True,
    )
    assert result.cache_failure == "cache_write_failed"
    assert result.failure is None
    assert result.chunks[0].payload == b'{"v":2}'
    assert result.fetched_ranges == ((day(3), day(5)),)
    assert result.reused_ranges == ()
    retained = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(7),
        NOW + timedelta(hours=2),
        lambda *_: pytest.fail("failed commit must preserve original fresh coverage"),
    )
    assert [(c.range_start, c.range_end) for c in retained.chunks] == [(day(1), day(7))]
    assert retained.chunks[0].payload == b'{"v":1}'
    assert retained.chunks[0].checked_at == NOW


@pytest.mark.parametrize("payload", [b"broken", b"", b"null", b'{"bad":NaN}', "{}"])
def test_invalid_response_cannot_create_coverage(tmp_path, payload):
    path = tmp_path / "statements.sqlite3"
    result = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(2),
        NOW,
        lambda *_: payload,
    )
    assert isinstance(result.failure, ValueError)
    assert result.failure_range == (day(1), day(2))
    assert result.chunks == result.fetched_ranges == ()
    assert result.missing_ranges == ((day(1), day(2)),)
    next_result = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(2),
        NOW,
        lambda *_: b"[]",
    )
    assert next_result.fetched_ranges == ((day(1), day(2)),)


def test_unreadable_database_degrades_to_live_bytes_without_claiming_reuse(tmp_path):
    path = tmp_path / "statements.sqlite3"
    path.write_bytes(b"synthetic damaged SQLite file")
    for _ in range(2):
        result = CurrentStatementCache(path).read_through(
            SCOPE,
            day(1),
            day(2),
            NOW,
            lambda *_: b"[]",
        )
        assert result.cache_failure == "cache_read_failed"
        assert result.failure is None
        assert result.fetched_ranges == ((day(1), day(2)),)
        assert result.reused_ranges == ()
        assert result.chunks[0].payload == b"[]"
    assert path.read_bytes() == b"synthetic damaged SQLite file"


@pytest.mark.parametrize(
    "unsafe", ["", "with spaces", "https://host/?token=x", "a/b", "x\n", "x" * 257]
)
def test_scope_only_accepts_bounded_opaque_safe_identifiers(unsafe):
    with pytest.raises(ValueError):
        replace(SCOPE, account_scope=unsafe)


@pytest.mark.parametrize("invalid", ["reversed", "naive", "datetime", "zero_ttl"])
def test_invalid_query_is_rejected_before_creating_cache(tmp_path, invalid):
    path = tmp_path / "statements.sqlite3"
    with pytest.raises(ValueError):
        cache = CurrentStatementCache(
            path, max_age=timedelta(0) if invalid == "zero_ttl" else timedelta(hours=24)
        )
        cache.read_through(
            SCOPE,
            NOW if invalid == "datetime" else day(2),
            day(1) if invalid == "reversed" else day(3),
            NOW.replace(tzinfo=None) if invalid == "naive" else NOW,
            lambda *_: pytest.fail("invalid query must not fetch"),
        )
    assert not path.exists()


@pytest.mark.parametrize(
    "dimension",
    [
        "account_scope",
        "instrument_identity_revision",
        "endpoint_id",
        "transport_identity",
        "normalizer_version",
    ],
)
def test_each_identity_dimension_has_independent_coverage(tmp_path, dimension):
    path = tmp_path / "statements.sqlite3"
    CurrentStatementCache(path).read_through(SCOPE, day(1), day(5), NOW, lambda *_: b'{"v":1}')
    changed = replace(SCOPE, **{dimension: "different"})
    isolated = CurrentStatementCache(path).read_through(
        changed,
        day(1),
        day(5),
        NOW,
        lambda *_: b'{"v":2}',
    )
    assert isolated.fetched_ranges == ((day(1), day(5)),)
    assert isolated.reused_ranges == ()
    original = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW,
        lambda *_: pytest.fail("original scope must remain fresh"),
    )
    assert original.chunks[0].payload == b'{"v":1}'


@pytest.mark.parametrize("payload", [b'{"rows":[]}', b'{"rows":[1]}'])
def test_old_year_and_successfully_empty_responses_also_expire(tmp_path, payload):
    path = tmp_path / "statements.sqlite3"
    start, end = date(2020, 1, 1), date(2020, 12, 31)
    CurrentStatementCache(path).read_through(SCOPE, start, end, NOW, lambda *_: payload)
    fresh = CurrentStatementCache(path).read_through(
        SCOPE,
        start,
        end,
        NOW + timedelta(hours=23),
        lambda *_: pytest.fail("empty complete responses cover the checked window"),
    )
    assert fresh.reused_ranges == ((start, end),)
    expired = CurrentStatementCache(path).read_through(
        SCOPE,
        start,
        end,
        NOW + timedelta(hours=24),
        lambda *_: b'{"rows":[2]}',
    )
    assert expired.fetched_ranges == ((start, end),)
    assert expired.chunks[0].payload == b'{"rows":[2]}'


def test_failed_refresh_never_renews_original_freshness(tmp_path):
    path = tmp_path / "statements.sqlite3"
    CurrentStatementCache(path).read_through(SCOPE, day(1), day(5), NOW, lambda *_: b"{}")
    error = RuntimeError("synthetic refresh failure")
    failed = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW + timedelta(hours=23),
        lambda *_: (_ for _ in ()).throw(error),
        refresh=True,
    )
    assert failed.failure is error
    assert failed.chunks[0].checked_at == NOW
    assert failed.stale_ranges == failed.missing_ranges == ()
    expired = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW + timedelta(hours=24),
        lambda *_: b'{"v":2}',
    )
    assert expired.fetched_ranges == ((day(1), day(5)),)


def test_refresh_does_not_update_or_delete_historical_observations(tmp_path):
    path = tmp_path / "statements.sqlite3"
    CurrentStatementCache(path).read_through(SCOPE, day(1), day(5), NOW, lambda *_: b'{"v":1}')
    with sqlite3.connect(path) as connection:
        # Persistence boundary guards ensure success cannot hide history mutation.
        connection.executescript("""
            CREATE TRIGGER protect_observation_update BEFORE UPDATE ON observations
            BEGIN SELECT RAISE(ABORT, 'immutable synthetic observation'); END;
            CREATE TRIGGER protect_observation_delete BEFORE DELETE ON observations
            BEGIN SELECT RAISE(ABORT, 'immutable synthetic observation'); END;
        """)
    refreshed = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW + timedelta(hours=1),
        lambda *_: b'{"v":2}',
        refresh=True,
    )
    assert refreshed.cache_failure is None
    selected = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(5),
        NOW + timedelta(hours=2),
        lambda *_: pytest.fail("new observation must be selected"),
    )
    assert selected.chunks[0].payload == b'{"v":2}'


def test_corrupt_replacement_never_resurrects_superseded_payload(tmp_path):
    path = tmp_path / "statements.sqlite3"
    cache = CurrentStatementCache(path)
    cache.read_through(SCOPE, day(1), day(7), NOW, lambda *_: b'{"old":true}')
    cache.read_through(SCOPE, day(3), day(5), NOW, lambda *_: b'{"new":true}', refresh=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE observations SET payload=? WHERE payload=?", (b"damaged", b'{"new":true}')
        )
    failure = RuntimeError("synthetic unavailable")
    result = CurrentStatementCache(path).read_through(
        SCOPE,
        day(1),
        day(7),
        NOW,
        lambda *_: (_ for _ in ()).throw(failure),
    )
    assert result.cache_failure == "cache_corrupt"
    assert result.missing_ranges == ((day(3), day(5)),)
    assert [(c.range_start, c.range_end) for c in result.chunks] == [
        (day(1), day(2)),
        (day(6), day(7)),
    ]
