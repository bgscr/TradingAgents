"""Deterministic synthetic statement replay and negative controls; no live transport."""

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from tests.test_financial_acceptance_scope import fake_https, sample_rows


def frames(endpoint, symbol):
    """Build 47 synthetic revisions across 24 quarters, omitting two optional bank fields."""
    from datetime import date, timedelta
    from calendar import monthrange
    import pandas as pd

    frame = sample_rows(endpoint, symbol)
    if endpoint != "balancesheet":
        return frame
    row = frame.iloc[0].to_dict()
    row.update(cost_fin_assets=None, fair_value_fin_assets=None)
    rows = []
    for revision in range(47):
        year, index = divmod(revision // 2 + 3, 4)
        month = 3 * (index + 1)
        end = date(2020 + year, month, monthrange(2020 + year, month)[1])
        announced = (end + timedelta(days=20 + revision % 2)).strftime("%Y%m%d")
        rows.append(dict(row, end_date=end.strftime("%Y%m%d"), ann_date=announced, f_ann_date=announced))
    return pd.DataFrame(rows, dtype=object)


def test_synthetic_47_rows_become_verified_optional_gap(tmp_path, monkeypatch):
    from tradingagents import statement_acceptance as acceptance
    calls = fake_https(monkeypatch, frames)
    optional = (
        {"allow_optional_gaps": True}
        if "allow_optional_gaps" in inspect.signature(acceptance.acquire).parameters
        else {}
    )
    result = acceptance.acquire(
        tmp_path,
        ticker="601328.SH",
        destination="https://api.waditu.com/dataapi",
        account="offline-fixture",
        as_of="2026-11-01",
        token="synthetic-only",
        **optional,
    )
    (tmp_path / "baseline-result.json").write_text(
        json.dumps(result, default=str, indent=2), encoding="utf-8"
    )
    assert result["status"] == "acquired_degraded", result
    assert len(calls) == 3
    assert len(result["views"]) == 6
    for freq in ("quarterly", "annual"):
        gap = result["views"]["get_balance_sheet:" + freq]
        assert gap["status"] == "unavailable"
        assert gap["financial_fact_created"] is False
        assert gap["diagnostics"]["gap_eligible"] is True
        assert gap["diagnostics"]["row_count"] == 47
        assert set(gap["diagnostics"]["periods"][0]["missing_core_fields"]) == {
            "financial_assets_at_amortized_cost",
            "financial_assets_at_fair_value",
        }


REQUEST = {
    "ticker": "601328.SH",
    "destination": "https://api.waditu.com/dataapi",
    "account": "offline-fixture",
    "as_of": "2026-11-01",
}


def acquire_case(tmp_path, monkeypatch, transform=lambda frame: frame, *, optional=True):
    from tradingagents import statement_acceptance as acceptance
    calls = fake_https(
        monkeypatch,
        lambda endpoint, symbol: (
            transform(frames(endpoint, symbol))
            if endpoint == "balancesheet"
            else frames(endpoint, symbol)
        ),
    )
    result = acceptance.acquire(
        tmp_path, token="synthetic-only", allow_optional_gaps=optional, **REQUEST
    )
    return acceptance, calls, result


def test_same_six_outcomes_and_original_timestamps_reused(tmp_path, monkeypatch):
    acceptance, calls, first = acquire_case(tmp_path, monkeypatch)
    second = acceptance.reuse(tmp_path, allow_optional_gaps=True, **REQUEST)
    assert first["status"] == "acquired_degraded"
    assert second["status"] == "reused_degraded"
    assert second["client_attempts"] == second["physical_requests"] == 0
    assert first["views"] == second["views"]
    assert len(calls) == 3
    for view in first["views"].values():
        assert view["observed_at"] and view["retrieved_at"]
    # The provider response is CURRENT; this does not claim historical PIT.
    assert all(
        p["core_coverage"] == "0.8"
        for p in first["statement_diagnostics"]["get_balance_sheet:quarterly"]["periods"]
    )


def test_ninety_percent_control_remains_available(tmp_path, monkeypatch):
    # Explicitly synthetic control: supply one missing, semantically exact field.
    _, calls, result = acquire_case(
        tmp_path, monkeypatch, lambda frame: frame.assign(cost_fin_assets=100)
    )
    assert result["status"] == "acquired"
    assert len(calls) == 3
    assert all(v["status"] == "available" for v in result["views"].values())
    diagnostic = result["statement_diagnostics"]["get_balance_sheet:quarterly"]
    assert diagnostic["selected_periods"] > 0
    assert all(p["core_coverage"] == "0.9" for p in diagnostic["periods"])


@pytest.mark.parametrize(
    "changes",
    [
        {"depos": None},
        {"ts_code": "600895.SH"},
        {"end_date": "20261231"},
        {"ann_date": "20261102", "f_ann_date": "20261102"},
        {"currency": "USD"},
        {"unit": "CNY_10000"},
        {"depos": "not-a-number"},
        {"comp_type": "3"},
    ],
)
def test_invalid_data_cannot_become_optional_gap(tmp_path, monkeypatch, changes):
    _, calls, result = acquire_case(tmp_path, monkeypatch, lambda frame: frame.assign(**changes))
    assert result["status"] == "stopped", result
    assert result["views"] == {}
    assert len(calls) == 1
    assert result["failure"]["retryable"] is False


def test_legacy_strict_mode_still_stops(tmp_path, monkeypatch):
    _, calls, result = acquire_case(tmp_path, monkeypatch, optional=False)
    assert result["status"] == "stopped"
    assert result["failure"]["error_code"] == "current_financial_ineligible_rows"
    assert len(calls) == 1
    assert result["statement_diagnostics"]["get_balance_sheet:quarterly"]["gap_eligible"] is True


def test_financial_required_profile_does_not_degrade(tmp_path, monkeypatch):
    import tradingagents.evidence as evidence

    original = evidence.capability_profile_for

    def required(kind):
        profile = original(kind)
        finance = evidence.EvidenceCapability.COMPANY_FINANCIALS
        return profile.model_copy(
            update={
                "required_capabilities": (*profile.required_capabilities, finance),
                "optional_capabilities": tuple(
                    x for x in profile.optional_capabilities if x != finance
                ),
            }
        )

    monkeypatch.setattr(evidence, "capability_profile_for", required)
    _, calls, result = acquire_case(tmp_path, monkeypatch)
    assert result["status"] == "stopped"
    assert len(calls) == 1


def test_failure_code_without_normalizer_record_is_not_gap(tmp_path, monkeypatch):
    from tradingagents.dataflows.acquisition import AcquisitionFailure
    from tradingagents.evidence import AcquisitionUnavailableReason

    from tradingagents import statement_acceptance as acceptance

    def unsupported(*args, **kwargs):
        raise AcquisitionFailure(
            AcquisitionUnavailableReason.EMPTY_FRAME, error_code="current_financial_ineligible_rows"
        )

    from tradingagents.dataflows import current_tushare_statements as current

    monkeypatch.setattr(current.CurrentTushareStatements, "__call__", unsupported)
    result = acceptance.acquire(
        tmp_path, token="synthetic-only", allow_optional_gaps=True, **REQUEST
    )
    assert result["status"] == "stopped"
    assert result["views"] == {}
    assert not result["statement_diagnostics"]


@pytest.mark.parametrize(
    "case",
    [
        "digest",
        "ticker",
        "scope",
        "date",
        "critical",
        "coverage",
        "metadata",
        "payload_hash",
        "fact",
        "status_available",
        "status_missing",
    ],
)
def test_forged_or_tampered_gap_receipt_rejected(tmp_path, monkeypatch, case):
    import copy

    from tradingagents.dataflows.current_statement_diagnostics import (
        canonical_digest,
        validate_prepared_views,
    )

    _, _, result = acquire_case(tmp_path, monkeypatch)
    views = copy.deepcopy(result["views"])
    gap = views["get_balance_sheet:quarterly"]
    diag = gap["diagnostics"]
    scopes = {
        tool: result["statement_diagnostics"][tool + ":quarterly"]["cache_scope"]
        for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow")
    }
    if case == "digest":
        gap["sha256"] = "0" * 64
    elif case == "ticker":
        diag["ticker"] = "600895.SS"
    elif case == "scope":
        diag["cache_scope"] = "0" * 64
    elif case == "date":
        diag["periods"][0]["ann_date"] = "2026-11-02"
    elif case == "critical":
        diag["periods"][0]["missing_critical_fields"] = ["customer_deposits"]
    elif case == "coverage":
        diag["periods"][0]["core_coverage"] = "0.9"
    elif case == "metadata":
        diag["extra"] = "synthetic-only"
    elif case == "payload_hash":
        diag["cache_observations"][0]["payload_sha256"] = "invalid"
    elif case == "fact":
        gap["financial_fact_created"] = True
    elif case == "status_available":
        gap["status"] = "available"
    elif case == "status_missing":
        del gap["status"]
    if case != "digest":
        gap["sha256"] = canonical_digest(diag)
    with pytest.raises(ValueError):
        validate_prepared_views(
            views,
            ticker="601328.SS",
            as_of=REQUEST["as_of"],
            allow_optional_gaps=True,
            expected_scopes=scopes,
        )


def test_tampered_cached_payload_cannot_reuse_as_gap(tmp_path, monkeypatch):
    import sqlite3

    acceptance, calls, result = acquire_case(tmp_path, monkeypatch)
    assert result["status"] == "acquired_degraded"
    path = tmp_path / "cache/current-financial-statements.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE observations SET payload=? WHERE id=1", (b"{}",))
    second = acceptance.reuse(tmp_path, allow_optional_gaps=True, **REQUEST)
    assert second["status"] == "stopped"
    assert second["views"] == {}
    assert second["physical_requests"] == 0
    assert len(calls) == 3


def test_unavailable_dispatch_has_no_financial_facts_and_required_market_still_blocks(
    tmp_path, monkeypatch
):
    from tests.test_financial_acceptance_scope import dispatch_sample
    from tradingagents.evidence import (
        EvidenceReadiness,
        EvidenceState,
        EvidenceStatus,
        InstrumentIdentityEvidence,
        MarketSnapshotEvidence,
        build_tool_evidence_state,
        evaluate_admission_gate,
    )

    dispatch = dispatch_sample(
        tmp_path,
        monkeypatch,
        endpoint="balancesheet",
        transform=lambda frame: frame.assign(cost_fin_assets=None, fair_value_fin_assets=None),
    )
    assert dispatch.artifact is None
    source = dispatch.request_key.request_key
    message = dispatch.to_tool_message()
    evidence = build_tool_evidence_state(
        [message],
        (),
        tool_call_ids_by_source={source: (dispatch.tool_call_id,)},
        tool_calls_by_id={dispatch.tool_call_id: {"name": "get_balance_sheet", "args": {}}},
        instrument_identity=InstrumentIdentityEvidence(symbol="601328.SS"),
        include_catalog_sources=True,
    )
    assert not evidence.source_facts
    assert evidence.acquisition_outcomes
    assert all(x.outcome == "unavailable" for x in evidence.acquisition_outcomes)
    assert evidence.sources and all(
        x.status == EvidenceStatus.UNAVAILABLE for x in evidence.sources
    )
    assert evaluate_admission_gate(evidence).admitted is False
    # The evidence builder returns a subset; Graph merges identity separately.
    identified = evidence.model_copy(
        update={"instrument_identity": InstrumentIdentityEvidence(symbol="601328.SS")}
    )
    missing_market = evaluate_admission_gate(identified)
    assert missing_market.admitted is False
    assert [code.value for code in missing_market.diagnostic_codes] == ["snapshot_unavailable"]
    snapshot = MarketSnapshotEvidence(
        symbol="601328.SS",
        provider="synthetic-fixture",
        retrieved_at="2026-11-03T00:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-10-16",
        effective_trading_date="2026-10-16",
        history_rows=1200,
        frame_sha256="a" * 64,
        snapshot_id="snapshot:synthetic",
    )
    with_market = EvidenceState(
        **{
            **evidence.model_dump(),
            "market_snapshot": snapshot,
            "instrument_identity": InstrumentIdentityEvidence(symbol="601328.SS"),
        }
    )
    gate = evaluate_admission_gate(with_market, minimum_history_rows=200)
    assert gate.admitted is True and gate.readiness == EvidenceReadiness.DEGRADED, gate
    assert not with_market.source_facts


@pytest.mark.parametrize("change", ["none", "status", "scope", "missing_outcome", "no_optin"])
def test_bounded_entry_consumes_exact_six_receipts(tmp_path, monkeypatch, change):
    import copy
    from types import SimpleNamespace

    from scripts import analyze_mainland_bounded as bounded
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity

    acceptance, _, acquired = acquire_case(tmp_path / "store", monkeypatch)
    reused = acceptance.reuse(tmp_path / "store", allow_optional_gaps=True, **REQUEST)
    assert acquired["status"] == "acquired_degraded" and reused["status"] == "reused_degraded"
    config, asset = acceptance.validate_request(tmp_path / "store", **REQUEST)
    binding = {
        "account": REQUEST["account"],
        "destination": REQUEST["destination"],
        "instrument_identity_revision": ProviderSubrequestInstrumentIdentity.from_authoritative(
            asset.instrument_identity
        ).identity_revision,
        "normalizer_version": "current-tushare-statements-v1",
    }
    live, reuse = copy.deepcopy(acquired), copy.deepcopy(reused)
    live["pid"], reuse["pid"] = 111, 222
    contract = {"request": {"shared_binding": binding}, "allow_optional_financial_gaps": True}
    if change == "status":
        live["status"], reuse["status"] = "acquired", "reused"
    elif change == "scope":
        binding["account"] = "different"
    elif change == "missing_outcome":
        del live["views"]["get_balance_sheet:annual"]
        reuse["views"] = copy.deepcopy(live["views"])
    elif change == "no_optin":
        del contract["allow_optional_financial_gaps"]
    root = tmp_path / "session"
    (root / "refresh").mkdir(parents=True)
    data = {
        "refresh/live.json": live,
        "refresh/reuse.json": reuse,
        "refresh/contract.json": contract,
        "shared-binding.json": binding,
        "refresh/result.json": {
            "passed": True,
            "status": "passed_degraded",
            "same_six_outcomes_and_timestamps": True,
        },
    }
    for name, value in data.items():
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    # Only the already-separate identity/clock check is substituted. The new
    # six-outcome, original timestamp and scope/status validation is real.
    session = SimpleNamespace(
        root=root,
        _supplemental_report_period=None,
        selection={"ticker": "601328.SS", "analysis_date": REQUEST["as_of"]},
        check_bank_identity=lambda: None,
        clock=SimpleNamespace(remaining=lambda: 1200),
    )
    if change == "none":
        assert bounded.Session.verify_refresh(session) == [3, 0]
    else:
        with pytest.raises((ValueError, bounded.Stop)):
            bounded.Session.verify_refresh(session)


def test_standalone_gap_optin_requires_shared_scope_before_child(tmp_path, monkeypatch):
    import sys

    from scripts import accept_financial_statements as entry

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "accept",
            "--output",
            str(tmp_path / "new"),
            "--ticker",
            "601328.SH",
            "--as-of",
            "2026-10-16",
            "--destination",
            "https://api.waditu.com/dataapi",
            "--account",
            "synthetic",
            "--allow-optional-financial-gaps",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 2
    assert not (tmp_path / "new").exists()


def test_scope_hash_matches_original_cache_contract():
    from tradingagents.dataflows.current_statement_cache import StatementCacheScope
    from tradingagents.dataflows.current_statement_diagnostics import scopes_from_binding

    binding = {
        "account": "offline-scope",
        "instrument_identity_revision": "identity:v1",
        "destination": "https://api.waditu.com/dataapi",
        "normalizer_version": "current-tushare-statements-v1",
    }
    scopes = scopes_from_binding(binding)
    for tool, endpoint in {
        "get_balance_sheet": "balancesheet",
        "get_income_statement": "income",
        "get_cashflow": "cashflow",
    }.items():
        expected = StatementCacheScope(
            account_scope=binding["account"],
            instrument_identity_revision=binding["instrument_identity_revision"],
            endpoint_id=endpoint,
            transport_identity=hashlib.sha256(binding["destination"].encode()).hexdigest(),
            normalizer_version=binding["normalizer_version"],
        )
        assert scopes[tool] == expected.key
    for changed in (
        {"account": ""},
        {"normalizer_version": "unsafe value"},
        {"instrument_identity_revision": None},
    ):
        with pytest.raises(ValueError):
            scopes_from_binding({**binding, **changed})


def test_retained_raw_cache_payload_preserves_original_timestamps(tmp_path, monkeypatch):
    import time
    from datetime import date, datetime, timedelta

    from tradingagents.dataflows import current_tushare_statements as current
    from tradingagents.dataflows.current_statement_cache import (
        CurrentStatementCache,
        StatementCacheChunk,
    )

    import sqlite3
    observed = datetime.fromisoformat("2026-11-03T09:00:00+00:00")
    origin, start = observed + timedelta(hours=1), time.monotonic()
    original_init = current.CurrentTushareStatements.__init__

    def initialize(self, **kwargs):
        kwargs["clock"] = lambda: origin + timedelta(seconds=time.monotonic() - start)
        original_init(self, **kwargs)

    monkeypatch.setattr(current.CurrentTushareStatements, "__init__", initialize)
    acceptance, calls, first = acquire_case(tmp_path, monkeypatch)
    assert first["status"] == "acquired_degraded"
    scope = first["statement_diagnostics"]["get_balance_sheet:quarterly"]["cache_scope"]
    # Replay this synthetic acquisition in the older completeness-only envelope.
    db = tmp_path / "cache/current-financial-statements.sqlite3"
    from contextlib import closing
    with closing(sqlite3.connect(db)) as conn:
        (data,) = conn.execute("SELECT payload FROM observations WHERE scope=? LIMIT 1", (scope,)).fetchone()
    payload = json.loads(data)
    payload["observed_at"] = observed.isoformat().replace("+00:00", "Z")
    payload["retrieved_at"] = (observed - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    payload["adapter_metadata"].pop("response_values", None)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    replay_digest = hashlib.sha256(raw).hexdigest()
    cache = CurrentStatementCache(tmp_path / "cache/current-financial-statements.sqlite3")
    cache._store(
        scope, StatementCacheChunk(date(2020, 1, 1), date(2026, 11, 1), raw, observed, replay_digest)
    )
    second = acceptance.reuse(tmp_path, allow_optional_gaps=True, **REQUEST)
    third = acceptance.reuse(tmp_path, allow_optional_gaps=True, **REQUEST)
    assert second["status"] == third["status"] == "reused_degraded"
    assert second["views"] == third["views"] and len(second["views"]) == 6
    assert second["physical_requests"] == third["physical_requests"] == 0
    assert second["client_attempts"] == third["client_attempts"] == 0
    assert len(calls) == 3
    for frequency in ("annual", "quarterly"):
        view = second["views"]["get_balance_sheet:" + frequency]
        assert view["observed_at"] == [payload["observed_at"]]
        assert view["retrieved_at"] == [payload["retrieved_at"]]
        assert view["diagnostics"]["cache_observations"][0]["payload_sha256"] == replay_digest
    assert "synthetic-only" not in json.dumps(second)


def test_parent_main_compares_bound_receipts_without_trusting_labels(tmp_path, monkeypatch):
    import contextlib
    import copy
    import io
    import sys
    from functools import partial
    from types import SimpleNamespace

    from scripts import accept_financial_statements as parent
    from tests.test_shared_statement_binding import REQUEST as SHARED_REQUEST, shared
    from tradingagents import statement_acceptance as acceptance
    from tradingagents.dataflows.current_statement_diagnostics import canonical_digest

    # Only this mocked parent workflow gets a synthetic project boundary. The
    # original path validator still checks the owned session and rejects reuse.
    project = tmp_path / "synthetic-project"
    project.mkdir()
    validate_output = partial(parent.output_path_allowed, source=project)
    monkeypatch.setattr(parent, "output_path_allowed", validate_output)
    assert not validate_output(project / "unowned-output")
    binding, storage = shared.__wrapped__(tmp_path)
    calls = fake_https(
        monkeypatch,
        lambda endpoint, symbol: (
            sample_rows(endpoint, symbol).assign(cost_fin_assets=None, fair_value_fin_assets=None)
            if endpoint == "balancesheet"
            else sample_rows(endpoint, symbol)
        ),
    )
    first = acceptance.acquire(
        tmp_path / "data",
        **SHARED_REQUEST,
        token="synthetic-only",
        shared_binding=binding,
        shared_storage=storage,
        allow_optional_gaps=True,
    )
    second = acceptance.reuse(
        tmp_path / "data",
        **SHARED_REQUEST,
        shared_binding=binding,
        shared_storage=storage,
        allow_optional_gaps=True,
    )
    assert first["status"] == "acquired_degraded" and second["status"] == "reused_degraded"
    assert len(calls) == 3
    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(binding), encoding="utf-8")
    for mutation in ("none", "scope", "available_label", "symbol"):
        live, reuse = copy.deepcopy(first), copy.deepcopy(second)
        live["pid"], reuse["pid"] = 111, 222
        reuse.update(
            guard_probes={"socket": "blocked", "environment": "blocked", "file": "blocked"},
            import_access={"credentials": 0},
            access_during_application={
                "network": 0,
                "credentials": 0,
                "subprocess": 0,
                "outside_write": 0,
            },
        )
        for receipt in (live, reuse):
            if mutation == "scope":
                for view in receipt["views"].values():
                    if view["status"] == "unavailable":
                        view["diagnostics"]["cache_scope"] = "0" * 64
                        view["sha256"] = canonical_digest(view["diagnostics"])
            elif mutation == "available_label":
                for view in receipt["views"].values():
                    view["status"] = "available"
            elif mutation == "symbol":
                receipt["canonical_symbol"] = "601658.SS"
        if mutation == "available_label":
            live["status"], reuse["status"] = "acquired", "reused"
        run_id = hashlib.sha256(mutation.encode()).hexdigest()[:32]
        session = project / "reports" / "bounded-runs" / run_id
        session.mkdir(parents=True)
        output = session / "refresh"
        assert not validate_output(output)  # Missing ownership record.
        (session / "session.json").write_text(json.dumps({
            "contract": "bounded-mainland-session-v1", "run_id": run_id,
            "root": str(session), "resume": False,
        }), encoding="utf-8")
        assert validate_output(output)
        phases = []

        def child(command, _output=output, _live=live, _reuse=reuse, _phases=phases, **kwargs):
            phase = command[command.index("--worker") + 1]
            assert phase in {"live", "reuse"}
            _phases.append(phase)
            (_output / (phase + ".json")).write_text(
                json.dumps(_live if phase == "live" else _reuse), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(parent.subprocess, "run", child)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "accept",
                "--output",
                str(output),
                "--ticker",
                SHARED_REQUEST["ticker"],
                "--as-of",
                SHARED_REQUEST["as_of"],
                "--destination",
                SHARED_REQUEST["destination"],
                "--account",
                SHARED_REQUEST["account"],
                "--shared-binding",
                str(binding_file),
                "--authorize-live",
                "--allow-optional-financial-gaps",
            ],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            status = parent.main()
        assert phases == ["live", "reuse"]
        assert not validate_output(output)  # Existing reservation cannot resume.
        report = json.loads((output / "result.json").read_text())
        if mutation == "none":
            assert status == 0 and report["status"] == "passed_degraded"
            assert report["same_six_outcomes_and_timestamps"] is True
            assert report["same_six_views_and_timestamps"] is False
        else:
            assert status == 1 and report["status"] == "stopped"
            assert report["failure"]["error_code"] == "acceptance_comparison_failed"
