"""Synthetic-only contextual preflight tests; no live providers or credentials."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest

from tradingagents import budget_readiness as readiness
from tradingagents.dataflows import config as config_module
from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)
from tradingagents.dataflows.current_tushare_statements import NORMALIZER_VERSION, TOOL_ENDPOINTS
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import InstrumentIdentityEvidence, SourceArtifact
from tradingagents.market_history.calendar import (
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
)
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.current import baostock_history_binding
from tradingagents.market_history.models import ProvenanceClass
from tradingagents.market_history.store import MarketHistoryStore, ProviderFramePublication

SYMBOL = "600895.SS"
END = date(2024, 2, 29)
START = date(2019, 2, 28)


@pytest.fixture
def config(tmp_path):
    values = copy.deepcopy(DEFAULT_CONFIG)
    values.update(
        analysis_budget_enabled=True, market_history_mode="current",
        data_usage_mode="personal_research", mainland_capability_routing_mode="legacy",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        data_cache_dir=str(tmp_path / "cache"),
        market_data_vendors={"cn_a": {"core_stock_apis": "akshare"}},
        tool_vendors=dict.fromkeys(TOOL_ENDPOINTS, "tushare"),
        financial_cache_only=True, tushare_financial_force_refresh=False,
        tushare_financial_https_endpoint="https://api.tushare.pro",
        tushare_account_scope_label="synthetic-test",
        instrument_identity_registry_path="", instrument_identity_registry_sha256="",
    )
    return values


@pytest.fixture
def identity():
    return InstrumentIdentityEvidence(
        symbol=SYMBOL, venue="XSHG", instrument_kind="equity", currency="CNY",
        provenance={"provider": "synthetic-registry", "source_ref": "synthetic:identity",
                    "retrieved_at": "2024-01-01T00:00:00Z", "artifact_sha256": "a" * 64},
    )


def publish_calendar(config, *, missing=None, corrupt=False):
    provider, _ = baostock_history_binding(SYMBOL)
    days = pd.date_range(START, END)
    publication = MarketSessionCalendarPublication(
        provider=replace(provider, provider_dataset_id="synthetic-calendar-v1", dataset_name="synthetic-calendar-v1"),
        reference_market="mainland-cn", timezone_name="Asia/Shanghai",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=b"synthetic-calendar-membership",
        sessions=tuple(MarketSession(day.date(), MarketSessionStatus.OPEN if day.weekday() < 5 else MarketSessionStatus.CLOSED)
                       for day in days if day.date() != missing),
    )
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        stored = store.publish_session_calendar(publication)
        if corrupt:
            store._connection.execute("DELETE FROM market_sessions WHERE calendar_revision_id=? AND session_date=?",
                                      (stored.calendar_revision_id, START.isoformat()))


def inspect(config, **kwargs):
    return readiness.inspect_budget_readiness(config, ticker=SYMBOL, as_of_date=END,
                                              selected_analysts=("market",), **kwargs)


def tree_bytes(root):
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def forbid_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted a mutating or provider operation")

    import tradingagents.dataflows.current_tushare_statements as statements
    import tradingagents.market_history.calendar_discovery as calendars
    monkeypatch.setattr(MarketHistoryStore, "open", forbidden)
    monkeypatch.setattr(MarketHistoryStore, "open_provider_request_authority", forbidden)
    monkeypatch.setattr(statements, "create_financial_statement_client", forbidden)
    monkeypatch.setattr(calendars, "ensure_mainland_calendar_window", forbidden)
    monkeypatch.setattr(calendars, "acquire_mainland_session_calendar", forbidden)


def test_disabled_preflight_does_not_inspect_context(monkeypatch):
    monkeypatch.setattr(readiness, "_inspect_market", lambda *args: pytest.fail("disabled inspection"))
    assert readiness.inspect_budget_readiness({}, ticker=None, as_of_date=None) == readiness.BudgetReadinessReport(False, (), ())


def test_missing_store_blocks_without_creating_files(config, tmp_path, monkeypatch):
    before = tree_bytes(tmp_path)
    forbid_runtime_io(monkeypatch)
    report = inspect(config)
    assert report.issues == (readiness.BudgetReadinessIssue("calendar", "missing"),)
    assert tree_bytes(tmp_path) == before


def test_exact_five_year_leap_window_accepts_full_calendar_and_cache_miss(config, tmp_path, monkeypatch):
    publish_calendar(config)
    before = tree_bytes(tmp_path)
    forbid_runtime_io(monkeypatch)
    report = inspect(config)
    assert not report.issues
    assert readiness.BudgetReadinessCheck("calendar", "verified_full_natural_date_window", False) in report.checked
    assert readiness.BudgetReadinessCheck("market_cache.akshare", "missing", True) in report.checked
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("missing", [START, date(2020, 2, 29), END])
def test_calendar_requires_every_natural_date_including_weekends(config, missing, monkeypatch):
    publish_calendar(config, missing=missing)
    forbid_runtime_io(monkeypatch)
    assert readiness.BudgetReadinessIssue("calendar", "incomplete_natural_date_window") in inspect(config).issues


def test_calendar_payload_membership_corruption_is_blocking(config, monkeypatch):
    publish_calendar(config, corrupt=True)
    forbid_runtime_io(monkeypatch)
    assert readiness.BudgetReadinessIssue("calendar", "corrupt_or_unreadable") in inspect(config).issues


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecars_are_rejected_even_when_empty(config, suffix, monkeypatch):
    publish_calendar(config)
    Path(config["market_history_database_path"] + suffix).write_bytes(b"")
    forbid_runtime_io(monkeypatch)
    assert readiness.BudgetReadinessIssue("calendar", "concurrent_database_state") in inspect(config).issues


def test_database_change_during_read_invalidates_success(config, monkeypatch):
    publish_calendar(config)
    original = readiness._file_stamp
    calls = []

    def changed(path):
        value = original(path)
        calls.append(path)
        return value if len(calls) == 1 else (*value[:-1], value[-1] + 1)

    monkeypatch.setattr(readiness, "_file_stamp", changed)
    assert readiness.BudgetReadinessIssue("calendar", "concurrent_database_state") in inspect(config).issues


def test_readonly_connection_rejects_writes_and_never_opens_store(config, monkeypatch):
    publish_calendar(config)
    forbid_runtime_io(monkeypatch)
    with readiness._readonly_history(config) as store:
        with pytest.raises(sqlite3.DatabaseError):
            store._connection.execute("CREATE TABLE forbidden (value TEXT)")
        with pytest.raises(sqlite3.DatabaseError):
            store._connection.execute("ATTACH DATABASE ':memory:' AS forbidden")


def publish_frame(config, monkeypatch, *, age=timedelta(minutes=1), omit=False):
    from tradingagents.dataflows.market_snapshot import (
        SNAPSHOT_PROVIDERS,
        AuthoritativeMarketSnapshot,
        _normalized_frame_text,
    )
    from tradingagents.market_history import current_cache
    from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window

    publish_calendar(config)
    monkeypatch.setattr(config_module, "_config", config)
    frame = pd.DataFrame({"Date": pd.bdate_range(START, END), "Open": 10.0, "High": 12.0,
                          "Low": 9.0, "Close": 11.0, "Volume": 100.0})
    text = _normalized_frame_text(frame)
    artifact = SourceArtifact(artifact_sha256=sha256(text.encode()).hexdigest(), source_ref="synthetic:market",
                              tool_call_id="synthetic", tool_name="get_stock_data", raw_text=text)
    snapshot = AuthoritativeMarketSnapshot(
        symbol=SYMBOL, frame=frame, provider="akshare", retrieved_at=(datetime.now(timezone.utc) - age).isoformat(),
        adjustment_basis=SNAPSHOT_PROVIDERS["akshare"].adjustment_basis,
        requested_date=END.isoformat(), effective_trading_date=END.isoformat(), source_artifact=artifact,
    )
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        calendar = read_mainland_calendar_window(store, START, END)
        metadata, provider, instrument = current_cache.encode(snapshot, SNAPSHOT_PROVIDERS["akshare"],
                                                              START.isoformat(), END.isoformat(), calendar, store=store)
        if omit:
            frame = frame.iloc[1:]
            text = _normalized_frame_text(frame)
        # The normal codec authors the fixture; corruption variants change only
        # the explicit dimension under test after that production encode.
        publication = ProviderFramePublication(
            upstream_service_id=provider.upstream_service_id, upstream_service_name=provider.upstream_service_name,
            provider_dataset_id=provider.provider_dataset_id, provider_name=provider.provider_name,
            dataset_name=provider.dataset_name, adjustment_methodology=provider.adjustment_methodology,
            strict_history_qualified=False, **asdict(instrument),
            snapshot_id="current-cache-record=sha256:" + current_cache.digest(metadata),
            requested_start=START.isoformat(), requested_end=END.isoformat(),
            effective_trading_date=END.isoformat(), adjustment_basis=snapshot.adjustment_basis,
            frame_digest=sha256(text.encode()).hexdigest(), history_rows=len(frame),
            retrieved_at=snapshot.retrieved_at, normalized_frame=text.encode(), snapshot_metadata=metadata,
        )
        stored = store.publish_provider_frame(publication)
        if not omit and age < timedelta(days=1):
            restored = current_cache.decode(store.load_provider_frame_revision(stored.frame_revision_id),
                SNAPSHOT_PROVIDERS["akshare"], SYMBOL, START.isoformat(), END.isoformat())
            assert restored.snapshot_id == snapshot.snapshot_id


@pytest.mark.parametrize(("age", "omit", "expected"), [
    (timedelta(minutes=1), False, "evidence_checked_runtime_revalidation_required"),
    (timedelta(days=2), False, "stale"),
    (timedelta(minutes=1), True, "gap"),
])
def test_real_cache_envelope_read_without_runtime_decoder(config, tmp_path, monkeypatch, age, omit, expected):
    publish_frame(config, monkeypatch, age=age, omit=omit)
    before = tree_bytes(tmp_path)
    forbid_runtime_io(monkeypatch)
    report = inspect(config)
    assert not report.issues
    check = next(item for item in report.checked if item.location == "market_cache.akshare")
    assert check.status == expected
    assert check.requires_provider_request is (None if expected.startswith("evidence_checked") else True)
    assert tree_bytes(tmp_path) == before


def statement_scope(config, identity, endpoint):
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity
    return StatementCacheScope(config["tushare_account_scope_label"],
        ProviderSubrequestInstrumentIdentity.from_authoritative(identity).identity_revision, endpoint,
        sha256(config["tushare_financial_https_endpoint"].encode()).hexdigest(), NORMALIZER_VERSION)


def test_corrupt_market_payload_is_informational_counted_live_requirement(config, monkeypatch):
    publish_frame(config, monkeypatch)
    with closing(sqlite3.connect(config["market_history_database_path"])) as connection:
        relative = connection.execute("SELECT p.relative_path FROM provider_frame_revisions f "
                                      "JOIN payload_artifacts p ON p.digest=f.payload_digest").fetchone()[0]
    (Path(config["market_history_payload_root"]) / relative).write_bytes(b"corrupted-synthetic-payload")
    forbid_runtime_io(monkeypatch)
    report = inspect(config)
    assert not report.issues
    assert readiness.BudgetReadinessCheck("market_cache.akshare", "corrupt_or_unreadable", True) in report.checked


def test_denied_compatibility_gate_is_preserved_alongside_structural_findings(config, monkeypatch):
    from tradingagents.market_history import current_cache

    publish_frame(config, monkeypatch)
    # Force denial, never success: fixture evidence cannot qualify a manifest.
    monkeypatch.setattr(current_cache, "compatibility_diagnostic", lambda: "denied:private-detail")
    forbid_runtime_io(monkeypatch)
    report = inspect(config)
    assert not report.issues
    assert readiness.BudgetReadinessCheck("market_cache", "compatibility_gate_denied", True) in report.checked
    assert readiness.BudgetReadinessCheck("market_cache.akshare",
        "evidence_checked_runtime_revalidation_required", None) in report.checked
    assert "private-detail" not in repr(report)


def publish_statements(config, identity, *, age=timedelta(minutes=1), gap=False, corrupt=False, wrong_symbol=False, empty=False):
    from tradingagents.dataflows._tushare_statement_artifacts import encode_provider_artifact

    cache = CurrentStatementCache(Path(config["data_cache_dir"]) / "current-financial-statements.sqlite3")
    now = datetime.now(timezone.utc) - age
    for endpoint in TOOL_ENDPOINTS.values():
        # Empty successful responses are valid cache coverage, not missing data.
        rows = [{"ts_code": "000001.SZ" if wrong_symbol else "600895.SH", "ann_date": "20230420",
                 "f_ann_date": "20230420", "end_date": "20221231", "report_type": "1", "comp_type": "1",
                 "update_flag": "1", "total_assets": 1000, "total_liab": 400, "total_hldr_eqy_inc_min_int": 600,
                 "total_revenue": 900, "n_income_attr_p": 160, "n_cashflow_act": 160}]
        frame = pd.DataFrame(rows)
        if empty:
            frame = frame.iloc[:0]
        response_at = now + timedelta(seconds=1)
        payload = encode_provider_artifact(endpoint_id=endpoint, frame=frame, retrieved_at=response_at, observed_at=response_at)
        cache.read_through(scope=statement_scope(config, identity, endpoint),
                           range_start=date(END.year - 6, 1, 2 if gap else 1), range_end=END,
                           now=now, fetch=lambda *args, result=payload: result)
    if corrupt:
        with closing(sqlite3.connect(cache.path)) as connection, connection:
            connection.execute("UPDATE observations SET payload_sha256=?", ("0" * 64,))


def financial_report(config, identity):
    return readiness.inspect_budget_readiness(config, ticker=SYMBOL, as_of_date=END,
        selected_analysts=("fundamentals",), instrument_identity=identity)


@pytest.mark.parametrize("case", ["fresh", "missing", "stale", "gap", "corrupt", "wrong_symbol", "empty", "refresh", "wrong_scope"])
def test_all_explicit_financial_tables_are_cache_only_and_side_effect_free(config, identity, tmp_path, monkeypatch, case):
    publish_calendar(config)
    if case != "missing":
        publish_statements(config, identity, age=timedelta(days=2) if case == "stale" else timedelta(minutes=1),
            gap=case == "gap", corrupt=case == "corrupt", wrong_symbol=case == "wrong_symbol", empty=case == "empty")
    if case == "refresh":
        config["tushare_financial_force_refresh"] = True
    if case == "wrong_scope":
        config["tushare_account_scope_label"] = "different-account"
    before = tree_bytes(tmp_path)
    forbid_runtime_io(monkeypatch)
    report = financial_report(config, identity)
    findings = [item for item in report.issues if item.location.startswith("financial.")]
    if case in {"fresh", "empty"}:
        assert not findings
        checks = [item for item in report.checked if item.location.startswith("financial.")]
        assert len(checks) == 3
        assert all(item.requires_provider_request is False for item in checks)
        if case == "empty":
            assert {item.status for item in checks} == {"validated_empty_response"}
    else:
        assert len(findings) == 3
        expected = {"missing": "missing", "stale": "stale", "gap": "incomplete_announcement_window",
                    "corrupt": "corrupt", "wrong_symbol": "invalid_provider_rows",
                    "refresh": "refresh_conflict", "wrong_scope": "missing"}[case]
        assert {item.reason for item in findings} == {expected}
    assert tree_bytes(tmp_path) == before


def test_identity_not_inferred_from_ticker_and_diagnostics_are_safe(config, identity, monkeypatch):
    publish_calendar(config)
    forbid_runtime_io(monkeypatch)
    config["instrument_identity_registry_path"] = "private-secret-path"
    config["instrument_identity_registry_sha256"] = "invalid-secret-digest"
    report = financial_report(config, None)
    assert len([issue for issue in report.issues if issue.reason == "authoritative_identity_unavailable"]) == 3
    with pytest.raises(readiness.BudgetReadinessError) as captured:
        readiness.validate_budget_readiness(config, ticker=SYMBOL, as_of_date=END)
    assert "secret" not in str(captured.value)
    assert "secret" not in repr(report)


def test_financial_config_and_identity_are_bound_separately(config, identity):
    publish_calendar(config)
    publish_statements(config, identity)
    other = {**config, "tushare_account_scope_label": "other"}
    report = readiness.inspect_budget_readiness(config, ticker=SYMBOL, as_of_date=END,
        financial_config=other, instrument_identity=identity)
    assert len([issue for issue in report.issues if issue.location.startswith("financial.")]) == 3


def test_registry_artifact_resolves_authoritative_identity(config, identity, tmp_path):
    publish_calendar(config)
    registry = {"schema_version": "1.0", "registry_id": "synthetic", "rows": [{
        "canonical_symbol": SYMBOL, "venue": "XSHG", "instrument_kind": "equity", "currency": "CNY",
        "provenance": {"provider": "synthetic", "source_ref": "synthetic:registry", "retrieved_at": "2024-01-01T00:00:00Z"},
    }]}
    raw = json.dumps(registry).encode()
    path = tmp_path / "registry.json"
    path.write_bytes(raw)
    config.update(instrument_identity_registry_path=str(path), instrument_identity_registry_sha256=sha256(raw).hexdigest())
    resolved = readiness._financial_identity(config, SYMBOL, None)
    publish_statements(config, resolved)
    assert not financial_report(config, None).issues
