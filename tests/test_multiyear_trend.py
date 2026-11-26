"""Synthetic price paths only; run through the task's network-denying runner."""

import copy
import csv
import io
import json
from dataclasses import asdict
from decimal import ROUND_DOWN, localcontext
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents import reporting
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.dataflows import config
from tradingagents.evidence import (
    AnalystEvidenceReport,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    SourceAcquisitionAvailable,
    SourceArtifact,
)
from tradingagents.multiyear_trend import (
    TrendUnavailable,
    build_multiyear_trend,
    multiyear_trend_markdown,
    render_multiyear_trend,
)
from tradingagents.terminal_contract import apply_terminal_contract

SYMBOL = "600895.SS"
ASOF = "2026-11-13"
POINTS = [
    ("2021-11-15", "100"),
    ("2021-12-31", "120"),
    ("2022-01-04", "90"),
    ("2022-12-30", "150"),
    ("2023-06-01", "75"),
    ("2023-12-29", "120"),
    ("2024-12-31", "180"),
    ("2025-12-31", "160"),
    ("2026-11-13", "200"),
]


def synthetic_evidence(points=None, *, as_of=ASOF, symbol=SYMBOL):
    points = POINTS if points is None else points
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
    for day, price in points:
        writer.writerow([day, price, price, price, price, "1000"])
    raw = buf.getvalue()
    digest = sha256(raw.encode()).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        raw_text=raw,
        source_ref="synthetic:bound-price",
        tool_call_id="synthetic-prices",
        tool_name="market_snapshot",
    )
    manifest = {
        "manifest_version": "2.0",
        "frame": {"rows": len(points), "sha256": digest},
        "instrument": {
            "canonical_symbol": symbol,
            "currency": "CNY",
            "instrument_kind": "equity",
            "reference_market": "shanghai",
        },
        "provider": {"provider_name": "synthetic"},
        "adjustment_basis": "qfq",
        "requested_date": as_of,
        "effective_trading_date": points[-1][0] if points else as_of,
        "provenance_class": "retrospective_backfill",
        "live_artifact_identity": "artifact:sha256:" + digest,
    }
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    md = sha256(text.encode()).hexdigest()
    identity = InstrumentIdentityEvidence(
        symbol=symbol,
        venue="XSHG",
        instrument_kind="equity",
        currency="CNY",
        provenance=IdentityProvenance(
            provider="synthetic",
            source_ref="synthetic:identity",
            retrieved_at="2026-11-14T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )
    snapshot = MarketSnapshotEvidence(
        symbol=symbol,
        provider="synthetic",
        retrieved_at="2026-11-14T00:00:00Z",
        adjustment_basis="qfq",
        requested_date=as_of,
        effective_trading_date=manifest["effective_trading_date"],
        history_rows=len(points),
        frame_sha256=digest,
        snapshot_id="snapshot:v2:" + md,
        snapshot_id_version="v2",
        pin_membership_digest=md,
        snapshot_manifest_json=text,
    )
    outcome = SourceAcquisitionAvailable(
        provider="synthetic",
        capability="market_snapshot",
        attempt=1,
        source_ref=artifact.source_ref,
        retrieved_at=snapshot.retrieved_at,
        artifact=artifact,
    )
    return EvidenceState(
        instrument_identity=identity,
        market_snapshot=snapshot,
        source_artifacts=(artifact,),
        acquisition_outcomes=(outcome,),
    )


def calculate(evidence=None, *, as_of=ASOF, symbol=SYMBOL):
    return build_multiyear_trend(
        evidence if evidence is not None else synthetic_evidence(), symbol=symbol, as_of=as_of
    )


def test_exact_price_return_year_baselines_drawdown_and_lineage():
    ev = synthetic_evidence()
    before = ev.model_dump_json()
    value = calculate(ev)
    assert value.price_return == "1.00000000"
    assert value.max_drawdown == "0.50000000"
    assert (value.peak_date, value.trough_date, value.peak_close, value.trough_close) == (
        "2022-12-30",
        "2023-06-01",
        "150",
        "75",
    )
    first, second, *_, last = value.annual
    assert (first.basis, first.baseline_date, first.price_return) == (
        "first_observed_close_partial",
        "2021-11-15",
        "0.20000000",
    )
    assert (second.baseline_date, second.end_date, second.price_return) == (
        "2021-12-31",
        "2022-12-30",
        "0.25000000",
    )
    assert (last.baseline_date, last.cutoff_partial, last.price_return) == (
        "2025-12-31",
        True,
        "0.25000000",
    )
    assert value.lineage.input_artifact_sha256 == ev.market_snapshot.frame_sha256
    assert value.lineage.input_snapshot_id == ev.market_snapshot.snapshot_id
    assert value.lineage.observations_used == len(POINTS)
    assert len(value.lineage.result_digest) == 64
    assert before == ev.model_dump_json() and not ev.source_facts and not ev.material_claims


@pytest.mark.parametrize(
    "points,drawdown,peak,trough",
    [
        (["10", "20", "30"], "0.00000000", None, None),
        (["10", "10", "10"], "0.00000000", None, None),
        (["20", "10", "5"], "0.75000000", "2026-11-11", "2026-11-13"),
        (["20", "20", "10"], "0.50000000", "2026-11-11", "2026-11-13"),
    ],
)
def test_drawdown_extrema(points, drawdown, peak, trough):
    value = calculate(
        synthetic_evidence(
            list(zip(["2026-11-11", "2026-11-12", "2026-11-13"], points, strict=True))
        )
    )
    assert (value.max_drawdown, value.peak_date, value.trough_date) == (drawdown, peak, trough)


def test_missing_prior_year_is_partial_and_single_endpoint_is_unavailable():
    ev = synthetic_evidence([("2022-06-01", "10"), ("2024-06-03", "30"), ("2026-11-13", "20")])
    rows = calculate(ev).annual
    assert all(r.basis == "first_observed_close_partial" and r.price_return is None for r in rows)
    assert "full-year session completeness is unverified" in render_multiyear_trend(calculate(ev))


def test_decimal_precision_is_independent_of_caller_context_and_leap_window():
    ev = synthetic_evidence(
        [("2019-02-28", "3.000000000000000001"), ("2024-02-29", "4.000000000000000001")],
        as_of="2024-02-29",
    )
    a = calculate(ev, as_of="2024-02-29")
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_DOWN
        b = calculate(ev, as_of="2024-02-29")
    assert a == b and a.requested_start == "2019-02-28" and a.price_return == "0.33333333"


@pytest.mark.parametrize(
    "price", ["0", "-1", "NaN", "Infinity", "-Infinity", "", "None", "True", "1e101", "x" * 81]
)
def test_illegal_prices_fail_closed_even_if_hashed(price):
    with pytest.raises(TrendUnavailable):
        calculate(synthetic_evidence([("2026-11-12", price), ("2026-11-13", "10")]))


@pytest.mark.parametrize(
    "points",
    [
        [],
        [("2026-11-13", "10")],
        [("2026-11-13", "10"), ("2026-11-13", "11")],
        [("2026-11-13", "10"), ("2026-11-12", "11")],
        [("2026-11-13", "10"), ("2026-11-14", "11")],
        [("2021-11-12", "10"), ("2026-11-13", "11")],
        [("2026-11-12T01:00:00", "10"), ("2026-11-13", "11")],
    ],
)
def test_invalid_dates_and_history_are_rejected_without_silent_filtering(points):
    with pytest.raises(TrendUnavailable):
        calculate(synthetic_evidence(points))


@pytest.mark.parametrize(
    "mutation",
    [
        "stock",
        "asof",
        "currency",
        "kind",
        "identity",
        "hash",
        "manifest",
        "count",
        "end",
        "basis",
        "gap",
        "artifact",
        "outcome",
        "provider",
        "source",
        "venue",
        "timestamp",
    ],
)
def test_binding_negatives_never_render_numbers(mutation):
    ev = synthetic_evidence().model_dump(mode="json")
    if mutation == "stock":
        ev["instrument_identity"]["symbol"] = "600000.SS"
    elif mutation == "asof":
        ev["market_snapshot"]["requested_date"] = "2026-11-12"
    elif mutation == "currency":
        ev["instrument_identity"]["currency"] = "USD"
    elif mutation == "kind":
        ev["instrument_identity"]["instrument_kind"] = "fund"
    elif mutation == "identity":
        ev["instrument_identity"]["provenance"] = None
    elif mutation == "hash":
        ev["source_artifacts"][0]["raw_text"] += "secret synthetic string"
    elif mutation == "manifest":
        ev["market_snapshot"]["snapshot_manifest_json"] += " "
    elif mutation == "count":
        ev["market_snapshot"]["history_rows"] += 1
    elif mutation == "end":
        ev["market_snapshot"]["effective_trading_date"] = "2026-11-12"
    elif mutation == "basis":
        ev["market_snapshot"]["adjustment_basis"] = "hfq"
    elif mutation == "gap":
        ev["market_snapshot"]["history_gap_dates"] = ["2026-11-12"]
    elif mutation == "artifact":
        ev["source_artifacts"] = []
    elif mutation == "outcome":
        ev["acquisition_outcomes"] = []
    elif mutation == "provider":
        ev["acquisition_outcomes"][0]["provider"] = "other"
    elif mutation == "source":
        ev["source_artifacts"][0]["source_ref"] = "synthetic:other"
    elif mutation == "venue":
        ev["instrument_identity"]["venue"] = "NYSE"
    elif mutation == "timestamp":
        ev["market_snapshot"]["retrieved_at"] = "synthetic-secret-not-a-timestamp"
    text = multiyear_trend_markdown(ev, symbol=SYMBOL, as_of=ASOF)
    assert "unavailable" in text and "%" not in text and "secret" not in text


@pytest.mark.parametrize("language", ["English", "Chinese", "zh-CN"])
def test_localized_rendering_retains_limits_and_provenance(language):
    value = calculate()
    text = render_multiyear_trend(value, language)
    assert value.lineage.input_snapshot_id in text and value.lineage.input_artifact_sha256 in text
    assert value.lineage.result_digest in text and "2021-11-15" in text and "2025-12-31" in text
    assert "100.00%" in text and "50.00%" in text and "retrospective_backfill" in text
    if language == "English":
        assert (
            "partial year" in text
            and "not a full year" in text
            and "not dividend-reinvested total return" in text
        )
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in text)
    else:
        assert "非完整年度" in text and "非全年" in text and "非红利再投资总回报" in text


@pytest.mark.parametrize("language", ["Chinese", "English"])
@pytest.mark.parametrize("mode", ["prose", "repair", "direct", "data"])
def test_market_node_keeps_report_only_trend_out_of_model_inputs(
    monkeypatch, language, mode
):
    ev = synthetic_evidence()
    before = ev.model_dump(mode="json")
    monkeypatch.setitem(config._config, "output_language", language)
    monkeypatch.setattr(
        "tradingagents.agents.analysts.market_analyst.get_instrument_context_from_state",
        lambda state: "Synthetic identity",
    )
    prompts = []
    args = {"report_markdown": "Synthetic commentary", "material_claims": [], "evidence_notes": []}

    def draft(prompt):
        prompts.append(prompt.to_string())
        if mode in {"direct", "data"}:
            name = "AnalystEvidenceReport" if mode == "direct" else "get_stock_data"
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "id": "synthetic-call",
                        "args": args
                        if mode == "direct"
                        else {"symbol": SYMBOL, "start_date": "2021-11-13", "end_date": ASOF},
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Synthetic draft")

    finalizer = MagicMock()
    result = AnalystEvidenceReport(**args)
    finalizer.invoke.side_effect = (
        [ValueError("synthetic schema failure"), result] if mode == "repair" else [result]
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(draft)
    llm.with_structured_output.return_value = finalizer
    output = create_market_analyst(llm)(
        {
            "company_of_interest": SYMBOL,
            "trade_date": ASOF,
            "asset_type": "stock",
            "messages": [],
            "evidence_state": before,
        }
    )
    assert len(prompts) == 1 and finalizer.invoke.call_count == (
        2 if mode == "repair" else 1 if mode == "prose" else 0
    )
    for prompt in [prompts[0], *(c.args[0] for c in finalizer.invoke.call_args_list)]:
        assert "100.00%" not in prompt and ev.market_snapshot.snapshot_id not in prompt
        assert "多年价格趋势（确定性辅助观察）" not in prompt and "Multi-year price trend (deterministic advisory observations)" not in prompt
        assert "Include a concise multi-year commentary section in report_markdown" not in prompt
    updated = EvidenceState.model_validate(output.get("evidence_state", before))
    assert not updated.source_facts and not updated.material_claims
    assert ev.model_dump(mode="json") == before


def _replace_strings(value):
    if isinstance(value, dict):
        return {k: v if k == "raw_text" else _replace_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_strings(v) for v in value]
    if isinstance(value, str):
        return value.replace("AAPL", SYMBOL).replace("NASDAQ", "XSHG").replace("USD", "CNY")
    return value


@pytest.mark.parametrize("language", ["English", "Chinese"])
def test_complete_report_and_separate_trend_file_preserve_decision_and_audit_inputs(
    monkeypatch, language
):
    from tests.test_reporting import _decision_state

    state = _replace_strings(_decision_state())
    ev = synthetic_evidence(
        [("2022-01-04", "10"), ("2025-12-31", "12"), ("2026-07-17", "15")], as_of="2026-07-18"
    )
    existing = EvidenceState.model_validate(state["evidence_state"])
    # Keep the existing synthetic decision facts/claims untouched; add only bound price evidence.
    state["evidence_state"] = existing.model_copy(
        update={
            "instrument_identity": ev.instrument_identity,
            "market_snapshot": ev.market_snapshot,
            "source_artifacts": existing.source_artifacts + ev.source_artifacts,
            "acquisition_outcomes": existing.acquisition_outcomes + ev.acquisition_outcomes,
        }
    ).model_dump(mode="json")
    before = copy.deepcopy(state)
    audit_inputs = []

    def audit(value, *args, **kwargs):
        audit_inputs.append(copy.deepcopy(value))
        apply_terminal_contract(value, audit_digest="b" * 64)
        value["decision_audit_created_at"] = "2026-11-14T00:00:00Z"

    monkeypatch.setattr(reporting, "write_immutable_decision_audit", audit)
    monkeypatch.setattr(
        reporting,
        "publish_report_export",
        lambda publication, identity, language, documents, stages: documents,
    )

    def publish():
        return reporting._write_report_tree(
            copy.deepcopy(before), SYMBOL, SimpleNamespace(root=Path("synthetic")), language
        )

    documents = publish()
    title = "多年价格趋势" if language == "Chinese" else "Multi-year price trend"
    assert (
        title in documents["complete_report.md"]
        and title in documents["1_analysts/multiyear_trend.md"]
    )
    assert "50.00%" in documents["1_analysts/multiyear_trend.md"]
    monkeypatch.setattr(reporting, "multiyear_trend_markdown", lambda *args, **kwargs: "")
    baseline = publish()
    assert documents["5_portfolio/decision.md"] == baseline["5_portfolio/decision.md"]
    assert audit_inputs[0] == audit_inputs[1] == before
    assert state == before


def test_reproducible_result_digest_and_no_facts_from_summary():
    a = calculate()
    b = calculate()
    assert asdict(a) == asdict(b)
    assert "advisory." in a.lineage.calculation_id
    assert multiyear_trend_markdown({}, symbol="AAPL", as_of=ASOF) == ""


def test_real_snapshot_constructor_uses_hash_identity_not_source_ref_and_distinct_observation_times():
    import pandas as pd

    from tradingagents.dataflows.market_snapshot import AuthoritativeMarketSnapshot
    from tradingagents.market_history.frames import normalized_frame_text

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2025-12-31", "2026-11-13"]),
            "Open": [10.0, 12.0],
            "High": [10.0, 12.0],
            "Low": [10.0, 12.0],
            "Close": [10.0, 12.0],
            "Volume": [1000.0, 2000.0],
        }
    )
    raw = normalized_frame_text(frame)
    artifact = SourceArtifact(
        artifact_sha256=sha256(raw.encode()).hexdigest(),
        raw_text=raw,
        source_ref="acq.v1:market-snapshot:synthetic",
        tool_call_id="synthetic-call",
        tool_name="market_snapshot",
    )
    snap = AuthoritativeMarketSnapshot(
        symbol=SYMBOL,
        frame=frame,
        provider="synthetic",
        retrieved_at="2026-11-14T00:00:00.123456+00:00",
        adjustment_basis="qfq",
        requested_date=ASOF,
        effective_trading_date=ASOF,
        source_artifact=artifact,
        current_history_lineage='{"contract":"synthetic-only-view"}',
    )
    snapshot = MarketSnapshotEvidence(
        symbol=snap.symbol,
        provider=snap.provider,
        retrieved_at=snap.retrieved_at,
        adjustment_basis=snap.adjustment_basis,
        requested_date=snap.requested_date,
        effective_trading_date=snap.effective_trading_date,
        history_rows=len(frame),
        frame_sha256=snap.frame_sha256,
        snapshot_id=snap.snapshot_id,
        snapshot_id_version="v2",
        pin_membership_digest=snap.pin_membership_digest,
        snapshot_manifest_json=snap.snapshot_manifest_json,
    )
    outcome = SourceAcquisitionAvailable(
        provider="synthetic",
        capability="market_snapshot",
        attempt=1,
        source_ref=artifact.source_ref,
        artifact=artifact,
        retrieved_at="2026-11-14T00:00:00Z",
    )
    evidence = EvidenceState(
        instrument_identity=synthetic_evidence().instrument_identity,
        market_snapshot=snapshot,
        source_artifacts=(artifact,),
        acquisition_outcomes=(outcome,),
    )
    result = calculate(evidence)
    assert result.price_return == "0.20000000"
    assert result.lineage.input_snapshot_id == snap.snapshot_id


@pytest.mark.parametrize(
    "field,value",
    [("reference_market", "shenzhen"), ("currency", "USD"), ("canonical_symbol", "600000.SS")],
)
def test_rehashed_manifest_cannot_contradict_identity(field, value):
    ev = synthetic_evidence().model_dump(mode="json")
    manifest = json.loads(ev["market_snapshot"]["snapshot_manifest_json"])
    manifest["instrument"][field] = value
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = sha256(text.encode()).hexdigest()
    ev["market_snapshot"].update(
        snapshot_manifest_json=text,
        snapshot_id="snapshot:v2:" + digest,
        pin_membership_digest=digest,
    )
    with pytest.raises(TrendUnavailable):
        calculate(ev)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-14T00:00:00",
        "2026-11-14T08:00:00+08:00",
        "2026-99-99T00:00:00Z",
        "2026-11-14 00:00:00Z",
    ],
)
def test_retrieval_time_requires_concrete_utc(timestamp):
    ev = synthetic_evidence().model_dump(mode="json")
    ev["market_snapshot"]["retrieved_at"] = timestamp
    with pytest.raises(TrendUnavailable):
        calculate(ev)


def test_sh_alias_and_shenzhen_identity_use_same_bound_formula():
    assert calculate(synthetic_evidence(), symbol="600895.SH").price_return == "1.00000000"
    ev = synthetic_evidence(symbol="000001.SZ").model_dump(mode="json")
    ev["instrument_identity"]["venue"] = "XSHE"
    manifest = json.loads(ev["market_snapshot"]["snapshot_manifest_json"])
    manifest["instrument"]["reference_market"] = "shenzhen"
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = sha256(text.encode()).hexdigest()
    ev["market_snapshot"].update(
        snapshot_manifest_json=text,
        snapshot_id="snapshot:v2:" + digest,
        pin_membership_digest=digest,
    )
    assert calculate(ev, symbol="000001.SZ").price_return == "1.00000000"
