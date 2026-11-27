"""Compatibility with CURRENT statement keys and SQLite rows written before the refactor."""

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)
from tradingagents.dataflows.current_statement_diagnostics import scopes_from_binding


BINDING = {
    "account": "offline-scope",
    "instrument_identity_revision": "identity:v1",
    "destination": "https://api.waditu.com/dataapi",
    "normalizer_version": "current-tushare-statements-v1",
}
# Captured independently with both pre-refactor implementations on df741efe.
CASES = [
    (
        "get_balance_sheet",
        "balancesheet",
        "ecae620ab9946a894e01ce2bdb6f5130612ee1cb8bb1ef4200fee8d938eeb0bf",
    ),
    (
        "get_income_statement",
        "income",
        "2f43846e5a349e3b0aec940cfb46b4fc88bfa6fab97b0ed593268f5daf8c4f0d",
    ),
    (
        "get_cashflow",
        "cashflow",
        "af3110de02dc5a2a5ef560d6f7ca968b72d86a7c15654cbcd86aaca8a9952927",
    ),
]


def scope(endpoint):
    return StatementCacheScope(
        BINDING["account"],
        BINDING["instrument_identity_revision"],
        endpoint,
        hashlib.sha256(BINDING["destination"].encode()).hexdigest(),
        BINDING["normalizer_version"],
    )


@pytest.mark.parametrize("tool,endpoint,golden", CASES)
def test_previous_sqlite_key_remains_a_hit(tmp_path, tool, endpoint, golden):
    assert scopes_from_binding(BINDING)[tool] == scope(endpoint).key == golden
    db = tmp_path / "statements.sqlite3"
    day = date(2026, 1, 2)
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)
    raw = b'{"rows":[]}'
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript("""
            CREATE TABLE observations (id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
                range_start TEXT NOT NULL, range_end TEXT NOT NULL, checked_at TEXT NOT NULL,
                payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL);
            CREATE TABLE active_windows (scope TEXT NOT NULL, range_start TEXT NOT NULL,
                range_end TEXT NOT NULL, observation_id INTEGER NOT NULL REFERENCES observations(id),
                PRIMARY KEY (scope, range_start));
        """)
        conn.execute(
            "INSERT INTO observations VALUES (1,?,?,?,?,?,?)",
            (
                golden,
                day.isoformat(),
                day.isoformat(),
                now.isoformat(),
                raw,
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        conn.execute(
            "INSERT INTO active_windows VALUES (?,?,?,1)",
            (golden, day.isoformat(), day.isoformat()),
        )
        conn.commit()
    result = CurrentStatementCache(db).read_through(
        scope(endpoint), day, day, now, lambda *_: pytest.fail("unexpected fetch")
    )
    assert result.reused_ranges == ((day, day),)
    assert not result.fetched_ranges and result.failure is None
    assert result.chunks[0].payload == raw


@pytest.mark.parametrize(
    "field",
    [
        "account_scope",
        "instrument_identity_revision",
        "endpoint_id",
        "transport_identity",
        "normalizer_version",
    ],
)
@pytest.mark.parametrize(
    "invalid", [None, 7, b"field", "", "_leading", "bad value", "字段", "a" * 257]
)
def test_invalid_scope_rejected_at_construction(field, invalid):
    with pytest.raises(
        ValueError, match="^cache scope requires bounded non-secret opaque identifiers$"
    ):
        replace(scope("balancesheet"), **{field: invalid})


def test_self_consistent_receipts_must_match_each_independent_binding(tmp_path, monkeypatch):
    from copy import deepcopy
    from tests.test_financial_acceptance_scope import fake_https, sample_rows
    from tests.test_shared_statement_binding import REQUEST, shared
    from tradingagents import statement_acceptance
    from tradingagents.dataflows.current_statement_diagnostics import (
        canonical_digest,
        validate_prepared_views,
    )

    binding, storage = shared.__wrapped__(tmp_path)
    calls = fake_https(
        monkeypatch,
        lambda endpoint, symbol: (
            sample_rows(endpoint, symbol).assign(cost_fin_assets=None, fair_value_fin_assets=None)
            if endpoint == "balancesheet"
            else sample_rows(endpoint, symbol)
        ),
    )
    result = statement_acceptance.acquire(
        tmp_path / "run",
        **REQUEST,
        token="synthetic-only",
        shared_binding=binding,
        shared_storage=storage,
        allow_optional_gaps=True,
    )
    assert result["status"] == "acquired_degraded" and len(calls) == 3
    expected = scopes_from_binding(binding)
    options = dict(
        ticker=result["canonical_symbol"],
        as_of=REQUEST["as_of"],
        allow_optional_gaps=True,
        expected_scopes=expected,
    )
    assert validate_prepared_views(result["views"], **options) > 0
    original = StatementCacheScope(
        binding["account"],
        binding["instrument_identity_revision"],
        "balancesheet",
        hashlib.sha256(binding["destination"].encode()).hexdigest(),
        binding["normalizer_version"],
    )
    for field in (
        "account_scope",
        "instrument_identity_revision",
        "endpoint_id",
        "transport_identity",
        "normalizer_version",
    ):
        views = deepcopy(result["views"])
        view = views["get_balance_sheet:quarterly"]
        assert view["diagnostics"]["cache_scope"] == original.key == expected["get_balance_sheet"]
        view["diagnostics"]["cache_scope"] = replace(original, **{field: "other"}).key
        view["sha256"] = canonical_digest(view["diagnostics"])
        with pytest.raises(ValueError, match="unverified_optional_financial_gap"):
            validate_prepared_views(views, **options)

@pytest.mark.parametrize(
    "field,invalid,error",
    [
        ("account", 5, ValueError),
        ("account", "not a scope", ValueError),
        ("instrument_identity_revision", "a" * 257, ValueError),
        ("normalizer_version", None, ValueError),
        ("destination", None, AttributeError),
    ],
)
def test_binding_rejects_invalid_input_at_original_boundary(field, invalid, error):
    with pytest.raises(error):
        scopes_from_binding({**BINDING, field: invalid})


def test_binding_lookup_precedes_identifier_validation():
    binding = {**BINDING, "account": None}
    del binding["normalizer_version"]
    with pytest.raises(KeyError, match="normalizer_version"):
        scopes_from_binding(binding)
