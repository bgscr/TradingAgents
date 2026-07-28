from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.dataflows.market_snapshot import (
    AuthoritativeMarketSnapshot,
    AuthoritativeTradingStatusValidationError,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    TradingStatusProvenanceEvidence,
    build_evidence_state,
    evaluate_preflight_gate,
)
from tradingagents.evidence_artifacts import (
    AuditMarketSnapshot,
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.graph.evidence_gate import create_preflight_gate_node
from tradingagents.market_history import TradingStatus, TradingStatusProvenance


def _status_provenance() -> TradingStatusProvenance:
    return TradingStatusProvenance(
        provider="baostock",
        provider_dataset_id="provider:baostock-strict-v1",
        session_date=date(2026, 7, 24),
        status=TradingStatus.SUSPENDED,
        observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        revision_id="status:2026-07-24:suspended",
    )


def _suspended_evidence() -> EvidenceState:
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="600519.SS",
            venue="shanghai",
            instrument_kind="equity",
            currency="CNY",
            provenance=IdentityProvenance(
                provider="mainland-registry",
                source_ref="registry:mainland-v1",
                retrieved_at="2026-07-24T10:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="600519.SS",
            provider="baostock",
            retrieved_at="2026-07-24T10:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-24",
            effective_trading_date="2026-07-24",
            history_rows=21,
            frame_sha256="b" * 64,
            snapshot_id="snapshot:suspended",
            current_tradeability="suspended",
            current_status_provenance=TradingStatusProvenanceEvidence(
                provider="baostock",
                provider_dataset_id="provider:baostock-strict-v1",
                session_date="2026-07-24",
                status="suspended",
                observed_at="2026-07-24T10:00:00+00:00",
                revision_id="status:2026-07-24:suspended",
            ),
            latest_traded_close=Decimal("10.5"),
            carried_suspension_close=Decimal("10.5"),
        ),
    )


@pytest.mark.unit
def test_current_mainland_suspension_blocks_direction_with_dedicated_outcome() -> None:
    evidence = _suspended_evidence()

    preflight = evaluate_preflight_gate(evidence)
    graph_result = create_preflight_gate_node()(
        {"evidence_state": evidence.model_dump(mode="json")}
    )

    assert preflight.passed is False
    assert preflight.diagnostic_codes == (
        AnalysisDiagnosticCode.INSTRUMENT_CURRENTLY_SUSPENDED,
    )
    assert "latest genuinely traded close: 10.5" in preflight.blockers[0]
    assert (
        graph_result["analysis_outcome_contract"]["reason"]
        == "instrument_currently_suspended"
    )
    assert "No Trading Decision was issued." in graph_result["analysis_outcome"]


@pytest.mark.unit
def test_authoritative_snapshot_preserves_tradeability_in_canonical_evidence() -> None:
    snapshot = AuthoritativeMarketSnapshot(
        symbol="600519.SS",
        frame=pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.5],
                "High": [10.5],
                "Low": [10.5],
                "Close": [10.5],
                "Volume": [0],
            }
        ),
        provider="baostock",
        retrieved_at="2026-07-24T10:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-24",
        effective_trading_date="2026-07-24",
        current_tradeability="suspended",
        current_status_provenance=_status_provenance(),
        latest_traded_close=Decimal("10.25"),
        carried_suspension_close=Decimal("10.5"),
        history_store_status="degraded",
        history_store_diagnostic="shadow_write_failed:disk_full",
    )

    evidence = build_evidence_state(
        symbol="600519.SS",
        identity={
            "canonical_symbol": "600519.SS",
            "venue": "shanghai",
            "instrument_kind": "equity",
            "currency": "CNY",
            "provider": "mainland-registry",
            "source_ref": "registry:mainland-v1",
            "retrieved_at": "2026-07-24T10:00:00+00:00",
            "artifact_sha256": "a" * 64,
        },
        snapshot=snapshot,
    )

    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.current_tradeability == "suspended"
    assert evidence.market_snapshot.current_status_provenance is not None
    assert evidence.market_snapshot.current_status_provenance.provider == "baostock"
    assert (
        evidence.market_snapshot.current_status_provenance.provider_dataset_id
        == "provider:baostock-strict-v1"
    )
    assert evidence.market_snapshot.current_status_provenance.session_date == "2026-07-24"
    assert evidence.market_snapshot.current_status_provenance.status == "suspended"
    assert evidence.market_snapshot.current_status_provenance.revision_id == (
        "status:2026-07-24:suspended"
    )
    assert evidence.market_snapshot.latest_traded_close == Decimal("10.25")
    assert evidence.market_snapshot.carried_suspension_close == Decimal("10.5")
    assert evidence.market_snapshot.history_store_status == "degraded"
    assert evidence.market_snapshot.snapshot_id_version == "v2"
    assert evidence.market_snapshot.pin_membership_digest == snapshot.pin_membership_digest
    assert (
        evidence.market_snapshot.history_store_diagnostic
        == "shadow_write_failed:disk_full"
    )
    audit_projection = project_evidence_for_audit(evidence, SourceArtifactManifest())
    assert audit_projection.market_snapshot is not None
    audit_snapshot = audit_projection.market_snapshot
    assert audit_snapshot.history_store_status == "degraded"
    assert audit_snapshot.snapshot_id_version == "v2"
    assert audit_snapshot.pin_membership_digest == snapshot.pin_membership_digest
    assert audit_snapshot.history_store_diagnostic == "shadow_write_failed:disk_full"
    assert audit_snapshot.current_status_provenance is not None
    assert audit_snapshot.current_status_provenance.status == "suspended"
    assert audit_snapshot.latest_traded_close == Decimal("10.25")
    assert audit_snapshot.carried_suspension_close == Decimal("10.5")


@pytest.mark.unit
def test_audit_snapshot_rejects_unknown_history_store_status() -> None:
    snapshot = _suspended_evidence().market_snapshot
    assert snapshot is not None
    payload = snapshot.model_dump(mode="python")
    payload["history_store_status"] = "corrupt"

    with pytest.raises(ValueError, match="history_store_status"):
        AuditMarketSnapshot.model_validate(payload)


@pytest.mark.unit
def test_audit_snapshot_rejects_dropped_authoritative_status_provenance() -> None:
    snapshot = _suspended_evidence().market_snapshot
    assert snapshot is not None
    payload = snapshot.model_dump(mode="python")
    payload["current_status_provenance"] = None

    with pytest.raises(ValueError, match="status provenance"):
        AuditMarketSnapshot.model_validate(payload)


@pytest.mark.unit
def test_authoritative_snapshot_rejects_suspension_row_contradiction() -> None:
    with pytest.raises(
        AuthoritativeTradingStatusValidationError,
        match="suspension row",
    ):
        AuthoritativeMarketSnapshot(
            symbol="600519.SS",
            frame=pd.DataFrame(
                {
                    "Date": pd.to_datetime(["2026-07-24"]),
                    "Open": [10.5],
                    "High": [10.5],
                    "Low": [10.5],
                    "Close": [10.5],
                    "Volume": [1],
                }
            ),
            provider="baostock",
            retrieved_at="2026-07-24T10:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-24",
            effective_trading_date="2026-07-24",
            current_tradeability="suspended",
            current_status_provenance=_status_provenance(),
            latest_traded_close=Decimal("10.25"),
            carried_suspension_close=Decimal("10.5"),
        )


@pytest.mark.unit
def test_zero_volume_without_authoritative_status_remains_explicitly_unknown() -> None:
    snapshot = AuthoritativeMarketSnapshot(
        symbol="600519.SS",
        frame=pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.5],
                "High": [10.5],
                "Low": [10.5],
                "Close": [10.5],
                "Volume": [0],
            }
        ),
        provider="legacy-provider",
        retrieved_at="2026-07-24T10:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-24",
        effective_trading_date="2026-07-24",
    )

    evidence = build_evidence_state(symbol="600519.SS", identity={}, snapshot=snapshot)

    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.current_tradeability == "unknown"
    assert evidence.market_snapshot.current_status_provenance is None
    assert evidence.market_snapshot.latest_traded_close is None
    audit = AuditMarketSnapshot.model_validate(
        evidence.market_snapshot.model_dump(mode="python")
    )
    assert audit.current_tradeability == "unknown"
    assert audit.current_status_provenance is None


@pytest.mark.unit
def test_missing_genuinely_traded_close_keeps_explicit_audit_diagnostic() -> None:
    snapshot = AuthoritativeMarketSnapshot(
        symbol="600519.SS",
        frame=pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.5],
                "High": [10.5],
                "Low": [10.5],
                "Close": [10.5],
                "Volume": [0],
            }
        ),
        provider="baostock",
        retrieved_at="2026-07-24T10:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-24",
        effective_trading_date="2026-07-24",
        current_tradeability="suspended",
        current_status_provenance=_status_provenance(),
        latest_traded_close=None,
        latest_traded_close_diagnostic=(
            "no_genuinely_traded_close_in_retained_history"
        ),
        carried_suspension_close=Decimal("10.5"),
    )

    evidence = build_evidence_state(symbol="600519.SS", identity={}, snapshot=snapshot)

    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.latest_traded_close is None
    assert evidence.market_snapshot.latest_traded_close_diagnostic == (
        "no_genuinely_traded_close_in_retained_history"
    )
    audit = AuditMarketSnapshot.model_validate(
        evidence.market_snapshot.model_dump(mode="python")
    )
    assert audit.latest_traded_close is None
    assert audit.latest_traded_close_diagnostic == (
        "no_genuinely_traded_close_in_retained_history"
    )
