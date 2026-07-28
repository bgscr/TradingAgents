from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.market_history import (
    DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS,
    AdjustmentFactorObservation,
    DataUsageMode,
    InstrumentSpec,
    MainlandCompatibilityGate,
    MainlandEquivalenceScenario,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    RawMarketObservation,
    SnapshotEquivalenceSubject,
    SnapshotPurpose,
    TradingStatus,
    TradingStatusObservation,
    compare_snapshot_equivalence,
)
from tradingagents.market_history.frames import normalized_frame_sha256


def _subject() -> SnapshotEquivalenceSubject:
    return SnapshotEquivalenceSubject(
        instrument_identity_id="mainland:600519.SS:equity:registry-v1",
        provider_dataset_id="baostock.cn-a.daily-v1",
        adjustment_basis="qfq-derived-v1",
        effective_dates=("2026-06-26", "2026-07-24"),
        eligible_observation_ids=("observation-1", "observation-2"),
        frame_sha256="a" * 64,
        derived_fact_digests=("return-20-sessions:b",),
        lineage_digests=("lineage:c",),
    )


@pytest.mark.unit
def test_mainland_gate_requires_every_scenario_and_exact_snapshot_semantics() -> None:
    live = _subject()
    equivalent = compare_snapshot_equivalence(live, live)
    comparisons = dict.fromkeys(DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS, equivalent)

    passed = MainlandCompatibilityGate().evaluate(comparisons)
    missing = MainlandCompatibilityGate().evaluate(
        {
            scenario: comparison
            for scenario, comparison in comparisons.items()
            if scenario is not MainlandEquivalenceScenario.CRASH_RECOVERY
        }
    )
    provider_regression = MainlandCompatibilityGate().evaluate(
        {
            **comparisons,
            MainlandEquivalenceScenario.YAHOO_FALLBACK: compare_snapshot_equivalence(
                live,
                replace(live, provider_dataset_id="yahoo.cn-a.current-v1"),
            ),
        }
    )

    assert passed.cutover_ready is True
    assert passed.missing_scenarios == ()
    assert passed.failed_scenarios == ()
    assert missing.cutover_ready is False
    assert missing.missing_scenarios == (MainlandEquivalenceScenario.CRASH_RECOVERY,)
    assert provider_regression.cutover_ready is False
    assert provider_regression.failed_scenarios == (
        MainlandEquivalenceScenario.YAHOO_FALLBACK,
    )
    assert provider_regression.comparisons[
        MainlandEquivalenceScenario.YAHOO_FALLBACK
    ].provider_matches is False


@pytest.mark.unit
def test_forced_baostock_frame_matches_independently_reconstructed_bundle(
    tmp_path,
) -> None:
    root = tmp_path / "history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    live_frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [100.0, 120.0],
        }
    )
    publication = ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider:baostock-strict-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="mainland-cn",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=b'{"forced_provider":"baostock"}',
        observations=(
            RawMarketObservation(
                date(2026, 7, 23),
                Decimal("20"),
                Decimal("22"),
                Decimal("18"),
                Decimal("21"),
                Decimal("100"),
            ),
            RawMarketObservation(
                date(2026, 7, 24),
                Decimal("10.5"),
                Decimal("11.5"),
                Decimal("10"),
                Decimal("11"),
                Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("0.5")),
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )
    with MarketHistoryStore.open(config) as store:
        published = store.publish_history_bundle(publication)
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=replace(
                    publication.provider,
                    provider_dataset_id="provider:baostock-calendar-v1",
                    dataset_name="mainland-session-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market=publication.instrument.reference_market,
                timezone_name="Asia/Shanghai",
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"2026-07-23/24"}',
                sessions=(
                    MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
                    MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
                ),
            )
        )
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )

    live = SnapshotEquivalenceSubject(
        instrument_identity_id=publication.instrument.identity_revision,
        provider_dataset_id=publication.provider.provider_dataset_id,
        adjustment_basis="qfq",
        effective_dates=tuple(live_frame["Date"].dt.strftime("%Y-%m-%d")),
        eligible_observation_ids=published.observation_revision_ids,
        frame_sha256=normalized_frame_sha256(live_frame),
        derived_fact_digests=("close:11",),
        lineage_digests=(published.bundle_revision_id,),
    )
    stored = replace(
        live,
        effective_dates=tuple(reconstructed.frame["Date"].dt.strftime("%Y-%m-%d")),
        frame_sha256=reconstructed.frame_sha256,
    )

    comparison = compare_snapshot_equivalence(live, stored)

    assert live_frame.equals(reconstructed.frame)
    assert comparison.passed is True
