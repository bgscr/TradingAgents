"""Exercise Basic publication and retrospective cache reads with synthetic records."""

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_bounded_mainland_entry import entry
from tradingagents import analysis_budget, powershell_fundflow as flow, research_stream
from tradingagents.dataflows import financial_dispatch as fd
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    stable_acquisition_source_ref,
)
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryStore,
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.config import provider_request_authority_config
from tradingagents.market_history.result_codec import encode_coordinated_result


def seed(config, key, cached, case):
    cfg = MarketHistoryConfig.from_mapping(config)
    with MarketHistoryStore.open_provider_request_authority(cfg) as store:
        upstream, service = upstream_service_identity_for_provider("akshare")
        ProviderRequestCoordinator(store).register_upstream_service(upstream, service)
    db = provider_request_authority_config(cfg).database_path
    if case == "missing":
        return db
    payload = encode_coordinated_result(cached)
    operation = "stock_individual_fund_flow.powershell"
    material = "full-pair-v1:" + key if case == "full_key" else key
    physical = stable_acquisition_source_ref(
        "financial-provider-physical-request", material, "1", operation
    )
    owner, seq, scope = "offline-owner", "offline-sequence", "market-snapshot"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute(
            """INSERT INTO provider_request_sequences
            (sequence_id,request_key,upstream_service_id,owner_id,started_at,completed_at,status,
             final_physical_attempt_count,result_payload,result_sha256,capacity_scope)
            VALUES (?,?,?,?,?,?,'succeeded',1,?,?,?)""",
            (
                seq,
                physical,
                upstream,
                owner,
                "2026-07-03T05:00:00+00:00",
                "2026-07-03T05:00:03+00:00",
                payload,
                "0" * 64
                if case == "bad_digest"
                else hashlib.sha256(payload).hexdigest(),
                scope,
            ),
        )
        con.execute(
            """INSERT INTO provider_request_attempts
            (sequence_id,attempt_index,request_key,upstream_service_id,upstream_service_name,owner_id,
             priority,operation,attempted_at,pacing_event,pacing_wait_seconds,outcome,retryable,status_code,
             cooldown_changed,final_physical_attempt_count,terminal_outcome,terminal_outcome_kind,capacity_scope)
             VALUES (?,1,?,?,?,?,1,?,?,'permit_acquired',0,'available',0,200,0,1,'available','available',?)""",
            (
                seq,
                physical,
                upstream,
                service,
                owner,
                "financial-fundamentals:" + operation,
                "2026-07-03T05:00:01+00:00",
                scope,
            ),
        )
    return db


@pytest.mark.parametrize(
    "case",
    [
        "hit",
        "missing",
        "bad_digest",
        "bad_pin",
        "full_key",
        "report_error",
        "receipt_error",
    ],
)
def test_basic_cache_publication(case, tmp_path, monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents import retrospective_observations as history
    from tradingagents.dataflows import akshare_data
    from tradingagents.market_history import coordinator

    m = entry()
    ticker, day = "601328.SS", "2026-07-02"
    conf = copy.deepcopy(DEFAULT_CONFIG)
    conf.update(
        market_history_mode="shadow",
        data_usage_mode="personal_research",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payload"),
        market_history_backup_root=str(tmp_path / "backup"),
        analysis_skip_optional_fundamentals=True,
        bounded_powershell_fundflow_enabled=True,
        bounded_observation_cache_mode="retrospective",
    )
    conf["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "akshare",
    )
    session = m.Session(
        tmp_path, source=tmp_path, powershell_fundflow_executable=tmp_path / "pwsh.exe"
    )
    session.selection = {"ticker": ticker, "analysis_date": day}
    result_root = session.paths["results_dir"]
    export = "1" * 32
    report = (
        result_root
        / ticker
        / day
        / "runs/20260703_050000/reports/report_exports"
        / export
        / "complete_report.md"
    )

    def publish(*args):
        if case == "report_error":
            raise OSError("synthetic failure")
        report.parent.mkdir(parents=True)
        report.write_bytes(b"synthetic published Basic")
        (report.parent / "manifest.json").write_text(
            json.dumps(
                dict(
                    export_id=export,
                    files={
                        report.name: hashlib.sha256(report.read_bytes()).hexdigest()
                    },
                )
            )
        )
        return report

    original_write = m.write_once

    def write(root, name, obj):
        if case == "receipt_error" and name == "basic-publication.json":
            raise OSError("synthetic failure")
        return original_write(root, name, obj)

    monkeypatch.setattr(m, "write_once", write)
    cli = SimpleNamespace(
        DEFAULT_CONFIG={},
        get_user_selections=Mock(),
        _build_run_config=Mock(),
        _prepare_run_artifacts=Mock(),
        _mark_run_failed=Mock(),
        _write_run_reports=publish,
    )
    budget = analysis_budget.AnalysisBudget(3, 60)
    monkeypatch.setattr(analysis_budget, "active_budget", lambda: budget)
    monkeypatch.setattr(research_stream, "full_graph_valid", lambda state: True)

    class SynchronizedWorker(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            f = super().submit(*args, **kwargs)
            f.result(timeout=5)
            return f

    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", SynchronizedWorker)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected physical request or process")

    import socket, subprocess

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(
        coordinator.ProviderRequestCoordinator,
        "execute_direct_physical_request",
        forbidden,
    )
    real_lookup = history.retrospective_observation
    reads = []

    def checked_read(*args, **kw):
        assert m.verified_basic(tmp_path, result_root) is not None
        reads.append(kw["request_key"])
        return real_lookup(*args, **kw)

    monkeypatch.setattr(history, "retrospective_observation", checked_read)
    core = fd._invoke_dispatcher_owned_akshare_fundamentals

    def core_or_aux(*args, **kwargs):
        if kwargs.get("fundflow_owner") is None:
            return "Synthetic core financial data"
        return core(*args, **kwargs)

    monkeypatch.setattr(
        fd, "_invoke_dispatcher_owned_akshare_fundamentals", core_or_aux
    )
    with session.analysis_hooks(cli, conf, {}):
        owner = flow.configured_owner(conf)
        identity = InstrumentIdentityEvidence(
            symbol=ticker,
            venue="XSHG",
            currency="CNY",
            instrument_kind=InstrumentKind.EQUITY,
            display_name="Synthetic equity",
            provenance=IdentityProvenance(
                provider="test-registry",
                source_ref="registry:test:v1",
                retrieved_at="2026-07-01T00:00:00Z",
                artifact_sha256="a" * 64,
            ),
        )
        dispatcher = fd.FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=identity, config=conf
        )
        request = fd.FinancialToolRequest(
            tool_name="get_fundamentals",
            statement_type=fd.FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=fd.FinancialReportingFrequency.NOT_APPLICABLE,
            as_of_date=date.fromisoformat(day),
            material_arguments={},
            tool_call_id="fixture-call",
            graph_message_id="fixture-message",
            process_id=101,
            dispatched_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        )
        key = dispatcher.canonical_request_key(request).request_key
        value = {
            "received_at": "2026-07-03T05:00:02+00:00",
            "projection": {
                "rows": [[day, "1", "2", "3", "4", "5", "6"]],
                "counts": {
                    "returned": 1,
                    "duplicate": 0,
                    "older": 0,
                    "future": 0,
                    "accepted": 1,
                },
            },
        }
        value["normalized_sha256"] = owner._observation(
            "available_partial", value["projection"], value["received_at"]
        )["normalized_sha256"]
        if case == "bad_pin":
            value["normalized_sha256"] = "0" * 64
        db = seed(conf, key, value, case)

        def rows():
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as c:
                return {
                    n: c.execute("SELECT * FROM " + n).fetchall()
                    for n in (
                        "provider_request_sequences",
                        "provider_request_attempts",
                        "request_queue",
                        "request_leases",
                    )
                }

        pre = rows()
        invoker = fd._ConfiguredFinancialInvoker(
            akshare_data.get_fundamentals, "get_fundamentals", ticker, owner, dict(conf)
        )
        invoker.invoke_with_request_key(request, key)
        if case in ("report_error", "receipt_error"):
            with pytest.raises(OSError, match="synthetic failure"):
                cli._write_run_reports({}, ticker, {})
            assert not reads
        else:
            assert cli._write_run_reports({}, ticker, {}) == report
            assert session._postpublication_error is None
            assert (
                m.verified_basic(tmp_path, result_root) is not None and len(reads) == 1
            )
            assert session._enhancement_status["fundflow"] == (
                "available_partial" if case == "hit" else "unavailable"
            )
            assert owner.summary()["transport"]["attempted"] is False
            obs = owner.summary()["observation"]
            if case == "hit":
                assert (
                    obs["normalized_sha256"] == value["normalized_sha256"]
                    and obs["rows"] == value["projection"]["rows"]
                )
            else:
                assert obs is None
        assert (
            rows() == pre
            and budget.receipt()["data_attempts"] == 0
            and budget.receipt()["model_attempts"] == 0
        )
