"""Synthetic A-share failures through production seams; no live qualification."""

import copy
import json
from types import SimpleNamespace

import pytest

from tests.report_export_helpers import export_root
from tests.test_reporting import _decision_state, _outcome_state
from tradingagents.dataflows.eastmoney_news import NewsResponseError, parse_news_page
from tradingagents.evidence import (
    ClaimValidation,
    EvidenceSource,
    MaterialClaim,
    SourceAcquisitionUnavailable,
)
from tradingagents.evidence_diagnostics import source_diagnostic
from tradingagents.reporting import write_report_tree
from tradingagents.run_diagnostics import DiagnosticPersistenceError, diagnostic_scope


def parse(payload, callback="other"):
    return parse_news_page(
        (callback + "(" + json.dumps(payload) + ");").encode(),
        expected_callback="cb", ticker="600919.SH", start_date="2026-11-04",
        end_date="2026-11-11",
    )


@pytest.mark.parametrize("result,shape", [
    ({"passportWeb": "synthetic-private-value"}, "missing"),
    ({"cmsArticleWebOld": None}, "null"),
    ({"cmsArticleWebOld": {"private-key": "private-value"}}, "wrong_type"),
    ({"cmsArticleWebOld": []}, "array"),
    (None, "result_null"),
    ([], "result_wrong_type"),
])
def test_failed_business_status_still_records_target_collection_shape(result, shape):
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError) as failure:
        parse({"code": 0, "result": result})
    assert failure.value.reason == "business_status_unverified"
    shape_event = next(event for event in events if event["kind"] == "news_response_shape")
    assert shape_event["qualification_authority"] is False
    assert shape_event["payload"]["target_collection"] == shape
    assert shape_event["payload"]["callback_matches"] is False
    assert shape_event["payload"]["coverage_status"] == "unknown"
    assert "private" not in json.dumps(events)


def test_missing_result_has_distinct_shape_and_unchanged_failure():
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError, match="schema_invalid"):
        parse({}, callback="cb")
    shape = next(event["payload"] for event in events if event["kind"] == "news_response_shape")
    assert shape["target_collection"] == "result_missing"
    assert shape["callback_matches"] is True


def test_no_usable_news_keeps_mutually_exclusive_exclusions():
    rows = [
        {"title": "600919 synthetic", "code": "ok", "date": "2026-11-03"},
        {"title": "unrelated synthetic", "code": "ok", "date": "2026-11-05"},
        {"title": "600919 synthetic", "code": "ok", "date": "undated"},
        {"title": 100, "code": "private", "date": "2026-11-05"},
    ]
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError, match="no_usable_articles"):
        parse({"result": {"cmsArticleWebOld": rows}})
    selected = next(event["payload"] for event in events if event["kind"] == "news_selection")
    assert selected["selected_count"] == 0
    assert selected["excluded_counts"] == {
        "malformed": 1, "undated": 1, "outside_window": 1, "wrong_stock": 1,
    }
    assert selected["coverage_status"] == "unknown"
    assert "synthetic" not in json.dumps(events) and "private" not in json.dumps(events)


@pytest.mark.parametrize("kind", ["news_response_shape", "news_selection"])
def test_new_diagnostic_retention_failure_cannot_be_reported_as_news_success(kind):
    def failed_sink(event):
        if event["kind"] == kind:
            raise OSError("synthetic failure")

    with diagnostic_scope(failed_sink), pytest.raises(DiagnosticPersistenceError):
        parse({"result": {"cmsArticleWebOld": [
            {"title": "600919 synthetic", "code": "a", "date": "2026-11-05"},
        ]}})


def test_selection_counters_are_bounded_and_unknown_fields_are_not_retained():
    from tradingagents.run_diagnostics import record_news_selection

    events = []
    with diagnostic_scope(events.append):
        record_news_selection(selected_count=100001, excluded_counts={
            "malformed": True, "undated": -1, "wrong_stock": "private", "outside_window": 3,
            "private-field": "private-value",
        })
    payload = events[0]["payload"]
    assert payload["selected_count"] == 100000 and payload["counts_capped"] is True
    assert payload["excluded_counts"] == {
        "malformed": None, "undated": None, "wrong_stock": None, "outside_window": 3,
    }
    assert "private" not in json.dumps(events)


def test_production_news_to_cli_retains_shape_once_without_retry(tmp_path, monkeypatch):
    from tests.test_run_diagnostic_retention import capture, events
    from tradingagents.dataflows import akshare_data
    from tradingagents.dataflows.acquisition import AcquisitionFailure

    calls, closes = [], []

    def response(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200,
            content=b'other({"code":0,"result":{"passportWeb":"private"}})',
            close=lambda: closes.append(True))

    monkeypatch.setattr(akshare_data.requests, "get", response)
    scope, writer, artifacts = capture(tmp_path)
    try:
        with scope, pytest.raises(AcquisitionFailure) as failure:
            akshare_data._stock_news_em("600919", "2026-11-04", "2026-11-11")
        assert failure.value.error_code == "news_business_status_unverified"
        retained = events(artifacts)
        assert len([row for row in retained if row["kind"] == "news_response_shape"]) == 1
        assert calls == closes == [True]
        assert "private" not in json.dumps(retained)
    finally:
        writer.close()


@pytest.mark.parametrize("kind", ["news_response_shape", "news_selection"])
@pytest.mark.parametrize("cleanup_retryable", [False, True])
def test_new_retention_failure_survives_cleanup_when_exception_recording_succeeds(
    monkeypatch, kind, cleanup_retryable,
):
    from tradingagents.dataflows import akshare_data

    calls, events = [], []

    def sink(event):
        events.append(event["kind"])
        if event["kind"] == kind:
            raise OSError("synthetic persistence failure")

    def close():
        error = akshare_data.requests.exceptions.ConnectionError if cleanup_retryable else RuntimeError
        raise error("synthetic cleanup failure")

    def response(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, close=close, content=(
            b'cb({"result":{"cmsArticleWebOld":[{"title":"600919 synthetic",'
            b'"code":"a","date":"2026-11-05"}]}})'
        ))

    monkeypatch.setattr(akshare_data.requests, "get", response)
    with diagnostic_scope(sink), pytest.raises(DiagnosticPersistenceError):
        akshare_data._stock_news_em("600919", "2026-11-04", "2026-11-11")
    assert calls == [True]
    assert events.count("news_exception") == 2  # Primary and cleanup were retained.


def financial_outcome(code="current_financial_ineligible_rows", *, provider="tushare"):
    return SourceAcquisitionUnavailable(
        provider=provider, capability="get_balance_sheet", source_ref="get_balance_sheet:fixture",
        attempt=1, retrieved_at="2026-11-11T12:00:00Z", retryable=False,
        reason="empty_frame", error_code=code,
    )


def test_ineligible_financial_rows_are_not_explained_as_upstream_empty():
    source = EvidenceSource(source_id="get_balance_sheet:fixture", status="unavailable",
                            required=False, detail="tool returned unavailable")
    detail = source_diagnostic(source, (financial_outcome(),))
    assert detail.reason.value == "financial_rows_ineligible"
    assert detail.acquisition_reason.value == "empty_frame"  # Original contract is preserved.
    for outcome in (financial_outcome("arbitrary-private-code"), financial_outcome(provider="other")):
        safe = source_diagnostic(source, (outcome,))
        assert safe.reason.value == "source_unavailable"
        assert "private" not in safe.model_dump_json()


def test_financial_explanation_requires_exact_source_and_failure_binding():
    source = EvidenceSource(source_id="get_balance_sheet:fixture", status="unavailable",
                            required=False, detail="tool returned unavailable")
    for updates in (
        {"source_ref": "get_balance_sheet:different"}, {"capability": "get_news"},
        {"reason": "provider_error"}, {"error_code": "current_financial_no_eligible_periods"},
    ):
        outcome = SourceAcquisitionUnavailable.model_validate(
            financial_outcome().model_dump(mode="json") | updates)
        assert source_diagnostic(source, (outcome,)).reason.value != "financial_rows_ineligible"


@pytest.fixture
def report_handles(monkeypatch):
    # Simulate only Windows handle sharing; this is NOT native qualification.
    from tests.test_report_export_safety import model_windows_handles

    model_windows_handles.__wrapped__(monkeypatch)


@pytest.mark.parametrize("language,marker", [
    ("Chinese", "当前经验证评级：买入 (Buy)"),
    ("English", "Authoritative rating: Buy"),
])
def test_export_keeps_decision_context_next_to_conflicting_advisory(
    tmp_path, report_handles, language, marker,
):
    state = _decision_state()
    state["evidence_state"]["material_claims"] = [MaterialClaim(
        claim_id="market.unverified", analyst="market", statement="Unverified amount",
        source_refs=("get_indicators:fixture",), source_quote="missing",
    ).model_dump(mode="json")]
    state["evidence_state"]["claim_validations"] = [ClaimValidation(
        claim_id="market.unverified", status="unsupported", fact_ids=(),
        detail="source does not contain numeric claim(s): 999",
    ).model_dump(mode="json")]
    decision = copy.deepcopy(state["trading_decision"])
    complete = write_report_tree(state, "AAPL", tmp_path, output_language=language).read_text(encoding="utf-8")
    stage = (export_root(tmp_path) / "3_trading/trader.md").read_text(encoding="utf-8")
    assert marker in stage
    assert stage.index(marker) < stage.index("FINAL TRANSACTION PROPOSAL")
    assert "market.unverified" in stage
    assert "FINAL TRANSACTION PROPOSAL: **SELL**" in stage  # Preserve original evidence.
    assert marker in complete
    assert state["trading_decision"] == decision
    assert "FINAL TRANSACTION" not in (export_root(tmp_path) / "5_portfolio/decision.md").read_text(encoding="utf-8")


def test_blocked_export_does_not_invent_hold_or_trust_model_rating(tmp_path, report_handles):
    state = _outcome_state()
    state["trader_investment_plan"] = "MODEL SAYS BUY"
    write_report_tree(state, "AAPL", tmp_path, output_language="Chinese")
    stage = (export_root(tmp_path) / "3_trading/trader.md").read_text(encoding="utf-8")
    assert "本次未形成经验证的交易决定" in stage
    assert "当前经验证评级" not in stage
    assert "Hold" not in stage
    assert "MODEL SAYS BUY" in stage


def test_advisory_warning_never_echoes_raw_claim_text_or_unsafe_ids():
    from tradingagents.evidence import EvidenceState
    from tradingagents.report_presentation import advisory_markdown

    evidence = EvidenceState(claim_validations=(ClaimValidation(
        claim_id="https://private.invalid/?token=do-not-publish", status="unsupported",
        fact_ids=(), detail="private exception text",
    ),))
    result = advisory_markdown("Trader", "original synthetic body", "English", evidence=evidence)
    assert "unpublished-claim-id" in result
    assert "do-not-publish" not in result and "private" not in result
    assert "no validated trading decision" in result
