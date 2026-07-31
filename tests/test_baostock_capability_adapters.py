from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Lock

import pandas as pd
import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import preflight_mainland_capability_routing
from tradingagents.dataflows.baostock_capabilities import (
    BaoStockCapabilityAdapter,
    BaoStockCapabilityOutcomeKind,
    BaoStockCapabilityTransport,
    BaoStockCurrentTradeability,
    BaoStockStrictBundleState,
    assemble_baostock_history_bundle,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestOutcome,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.tushare_factors_names import (
    TushareAdjustmentFactorAdapterResult,
    TushareNameEventAdapterResult,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    ProvenanceClass,
    SnapshotPurpose,
    StrictReplayUnavailable,
    TradingStatus,
)
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)

_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _identity(
    symbol: str = "600895.SS",
    venue: str = "XSHG",
) -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue=venue,
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _legacy_plan():
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert preflight.plan is not None
    return preflight.plan


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode="personal_research",
    )


def _raw_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-07-23", "2026-07-24"],
            "code": ["sh.600895", "sh.600895"],
            "open": ["20.000", "21.000"],
            "high": ["22.000", "23.000"],
            "low": ["18.000", "20.000"],
            "close": ["21.000", "22.000"],
            "preclose": ["19.500", "21.000"],
            "volume": ["100", "120"],
            "amount": ["2000.00", "2520.00"],
        }
    )


def _factor_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["sh.600895"],
            "dividOperateDate": ["2026-07-01"],
            "foreAdjustFactor": ["0.5000000000"],
            "backAdjustFactor": ["2.0000000000"],
            "adjustFactor": ["1.0000000000"],
        }
    )


def _status_rows(*, latest_status: str = "1", latest_is_st: str = "0") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-07-23", "2026-07-24"],
            "code": ["sh.600895", "sh.600895"],
            "tradestatus": ["1", latest_status],
            "isST": ["0", latest_is_st],
            "preclose": ["19.500", "21.000"],
        }
    )


def _lifecycle_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["sh.600895"],
            "code_name": ["张江高科"],
            "ipoDate": ["1996-04-22"],
            "outDate": [""],
            "type": ["1"],
            "status": ["1"],
        }
    )


class _FakeTransport:
    def __init__(
        self,
        *,
        raw: object | None = None,
        factors: object | None = None,
        statuses: object | None = None,
        lifecycle: object | None = None,
    ) -> None:
        self.results = {
            "raw": _raw_rows() if raw is None else raw,
            "factors": _factor_rows() if factors is None else factors,
            "statuses": _status_rows() if statuses is None else statuses,
            "lifecycle": _lifecycle_rows() if lifecycle is None else lifecycle,
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _result(self, endpoint: str, kwargs: dict[str, object]) -> pd.DataFrame:
        self.calls.append((endpoint, kwargs))
        result = self.results[endpoint]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, pd.DataFrame):
            return result.copy(deep=True)
        return result  # type: ignore[return-value]

    def query_raw_daily(self, **kwargs: object) -> pd.DataFrame:
        return self._result("raw", kwargs)

    def query_forward_adjustment_factors(self, **kwargs: object) -> pd.DataFrame:
        return self._result("factors", kwargs)

    def query_session_status(self, **kwargs: object) -> pd.DataFrame:
        return self._result("statuses", kwargs)

    def query_lifecycle(self, **kwargs: object) -> pd.DataFrame:
        return self._result("lifecycle", kwargs)


class _BlockingTransport(_FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()
        self._lock = Lock()

    def query_raw_daily(self, **kwargs: object) -> pd.DataFrame:
        with self._lock:
            self.calls.append(("raw", dict(kwargs)))
        self.started.set()
        assert self.release.wait(timeout=5)
        return _raw_rows()


class _RateLimitError(RuntimeError):
    retry_after_seconds = 17


class _BaoStockLoginCapacityError(_RateLimitError):
    error_code = "10001005"


@dataclass(frozen=True)
class _Harness:
    adapter: BaoStockCapabilityAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_harness(
    *,
    tmp_path,
    store: MarketHistoryStore,
    transport: BaoStockCapabilityTransport,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
    observed_at: datetime = _OBSERVED_AT,
) -> _Harness:
    coordinator = coordinator or ProviderRequestCoordinator(store)
    upstream_id, service_name = upstream_service_identity_for_provider("baostock")
    coordinator.register_upstream_service(upstream_id, service_name)
    cache = ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=run_scope_id,
        checkpoint=checkpoint,
    )
    return _Harness(
        adapter=BaoStockCapabilityAdapter(
            routing_plan=_legacy_plan(),
            subrequest_cache=cache,
            transport=transport,
            owner_id=run_scope_id,
            now=lambda: observed_at,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ),
        cache=cache,
        coordinator=coordinator,
    )


def _publish_calendar(
    store: MarketHistoryStore,
    bundle,
) -> None:
    store.publish_session_calendar(
        MarketSessionCalendarPublication(
            provider=replace(
                bundle.publication.provider,
                provider_dataset_id="provider-dataset:baostock-calendar-ticket-08",
                dataset_name="mainland-session-calendar-v1",
                adjustment_methodology="not-applicable",
                strict_history_qualified=False,
            ),
            reference_market=bundle.publication.instrument.reference_market,
            timezone_name="Asia/Shanghai",
            observed_at=bundle.publication.observed_at,
            provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            raw_payload=b'{"calendar":"ticket-08-deterministic"}',
            sessions=(
                MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
                MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
            ),
        )
    )


@pytest.mark.unit
def test_complete_raw_factor_status_bundle_reconstructs_exact_qfq_and_replay_fails_closed(
    tmp_path,
) -> None:
    transport = _FakeTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-complete-bundle",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        assert bundle.publication is not None
        _publish_calendar(store, bundle)
        published = store.publish_history_bundle(bundle.publication)
        current = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        with pytest.raises(
            StrictReplayUnavailable,
            match="Observed Point-in-Time History",
        ):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=_OBSERVED_AT + timedelta(seconds=1),
            )

    assert bundle.strict_bundle_state is BaoStockStrictBundleState.COMPLETE
    assert bundle.provider_history_bundle_identity.startswith(
        "baostock-provider-history-bundle:v1:"
    )
    assert bundle.raw.candidates[0].session_date == date(2026, 7, 23)
    assert bundle.raw.candidates[0].original_close == "21.000"
    assert bundle.raw.candidates[1].close == Decimal("22.000")
    assert bundle.factors.candidates[0].effective_date == date(2026, 7, 1)
    assert bundle.factors.candidates[0].original_factor == "0.5000000000"
    assert bundle.statuses.candidates[1].original_tradestatus == "1"
    assert bundle.statuses.candidates[1].original_is_st == "0"
    assert bundle.statuses.current_tradeability is BaoStockCurrentTradeability.TRADEABLE
    assert current.frame["Close"].tolist() == [10.5, 11.0]
    assert bundle.publication.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL
    binding_manifest = json.loads(bundle.publication.raw_payload)
    assert binding_manifest["raw_provider_artifact"] == (
        bundle.raw.provider_artifact.model_dump(mode="json")
    )
    assert binding_manifest["factor_provider_artifact"] == (
        bundle.factors.provider_artifact.model_dump(mode="json")
    )
    assert binding_manifest["status_provider_artifact"] == (
        bundle.statuses.provider_artifact.model_dump(mode="json")
    )
    assert [call[0] for call in transport.calls] == ["raw", "factors", "statuses"]
    assert (
        sum(
            len(result.attempt_events)
            for result in (
                bundle.raw,
                bundle.factors,
                bundle.statuses,
            )
        )
        == 3
    )


@pytest.mark.unit
def test_conflicting_factor_duplicates_fail_typed_and_prevent_strict_bundle(
    tmp_path,
) -> None:
    factors = pd.concat(
        [
            _factor_rows(),
            _factor_rows().assign(foreAdjustFactor="0.7500000000"),
        ],
        ignore_index=True,
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(factors=factors),
            run_scope_id="ticket-08-factor-conflict",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert bundle.factors.capability_outcome.kind is (
        BaoStockCapabilityOutcomeKind.CONFLICTING_FACTOR
    )
    assert bundle.factors.provider_artifact is not None
    assert bundle.factors.candidates == ()
    assert len(bundle.factors.row_rejections) == 2
    assert bundle.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert bundle.strict_outcome.kind is (BaoStockCapabilityOutcomeKind.INCOMPLETE_STRICT_BUNDLE)
    assert bundle.publication is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("latest_status", "expected", "expected_status"),
    [
        ("1", BaoStockCurrentTradeability.TRADEABLE, TradingStatus.TRADED),
        ("0", BaoStockCurrentTradeability.SUSPENDED, TradingStatus.SUSPENDED),
        ("", BaoStockCurrentTradeability.UNKNOWN, None),
    ],
)
def test_only_explicit_session_status_establishes_current_tradeability(
    tmp_path,
    latest_status: str,
    expected: BaoStockCurrentTradeability,
    expected_status: TradingStatus | None,
) -> None:
    raw = _raw_rows()
    if latest_status != "1":
        raw.loc[1, ["open", "high", "low", "close", "volume", "amount"]] = ""
    transport = _FakeTransport(
        raw=raw,
        statuses=_status_rows(latest_status=latest_status),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id=f"ticket-08-status-{latest_status or 'blank'}",
        )
        result = harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.current_tradeability is expected
    assert result.candidates[-1].trading_status is expected_status
    assert result.source_facts_created is False
    assert result.decision_gate_changed is False


@pytest.mark.unit
def test_dated_is_st_is_preserved_independently_of_name_text(tmp_path) -> None:
    transport = _FakeTransport(statuses=_status_rows(latest_is_st="1"))
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-dated-st",
        )
        result = harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.candidates[-1].is_st is True
    assert result.candidates[-1].st_status_authoritative is True
    assert result.candidates[-1].session_date == date(2026, 7, 24)
    assert "name" not in result.candidates[-1].model_dump(mode="json")


@pytest.mark.unit
def test_lifecycle_preserves_exact_ipo_listing_out_and_delisting_provenance(
    tmp_path,
) -> None:
    lifecycle = pd.DataFrame(
        {
            "code": ["sh.600895"],
            "code_name": ["*ST looking text is descriptive only"],
            "ipoDate": ["1996-04-22"],
            "outDate": ["2027-03-15"],
            "type": ["1"],
            "status": ["0"],
        }
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(lifecycle=lifecycle),
            run_scope_id="ticket-08-lifecycle",
        )
        result = harness.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2027, 3, 15),
        )

    candidate = result.candidates[0]
    assert candidate.ipo_date == date(1996, 4, 22)
    assert candidate.listing_date == date(1996, 4, 22)
    assert candidate.out_date == date(2027, 3, 15)
    assert candidate.delisting_date == date(2027, 3, 15)
    assert candidate.original_status == "0"
    assert candidate.original_type == "1"
    assert candidate.provider_id == "baostock"
    assert candidate.st_status_established is False
    provenance = candidate.financial_listing_provenance()
    assert provenance.authoritative is True
    assert provenance.provider_id == "baostock"
    assert provenance.observed_at == _OBSERVED_AT


@pytest.mark.unit
def test_missing_membership_and_factor_only_input_fail_closed(tmp_path) -> None:
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(),
            run_scope_id="ticket-08-missing-membership",
        )
        factors = harness.adapter.acquire_forward_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        factor_only = assemble_baostock_history_bundle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
            raw=None,
            factors=factors,
            statuses=None,
        )

    assert factors.capability_outcome.kind is BaoStockCapabilityOutcomeKind.AVAILABLE
    assert factor_only.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert factor_only.strict_outcome.kind is (
        BaoStockCapabilityOutcomeKind.INCOMPLETE_STRICT_BUNDLE
    )
    assert factor_only.publication is None


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["raw", "factors", "statuses"])
def test_each_missing_strict_bundle_component_fails_closed(tmp_path, missing: str) -> None:
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(),
            run_scope_id=f"ticket-08-missing-{missing}",
        )
        raw = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        factors = harness.adapter.acquire_forward_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        statuses = harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        components = {"raw": raw, "factors": factors, "statuses": statuses}
        components[missing] = None
        result = assemble_baostock_history_bundle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
            raw=components["raw"],
            factors=components["factors"],
            statuses=components["statuses"],
        )

    assert result.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert result.publication is None


@pytest.mark.unit
def test_tushare_factor_or_name_result_cannot_replace_baostock_bundle_membership(
    tmp_path,
) -> None:
    unavailable = ProviderSubrequestOutcome(kind=ProviderSubrequestOutcomeKind.EMPTY)
    tushare_factor = TushareAdjustmentFactorAdapterResult(
        subrequest_key="provider-subrequest:v1:" + "1" * 64,
        outcome=unavailable,
        sequence_id="",
        attempt_events=(),
    )
    tushare_name = TushareNameEventAdapterResult(
        subrequest_key="provider-subrequest:v1:" + "2" * 64,
        outcome=unavailable,
        sequence_id="",
        attempt_events=(),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(),
            run_scope_id="ticket-08-no-tushare-membership",
        )
        raw = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        result = assemble_baostock_history_bundle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
            raw=raw,
            factors=tushare_factor,  # type: ignore[arg-type]
            statuses=tushare_name,  # type: ignore[arg-type]
        )

    assert result.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert result.publication is None


@pytest.mark.unit
def test_suspended_bundle_uses_explicit_status_and_official_carried_close(tmp_path) -> None:
    raw = _raw_rows()
    raw.loc[1, ["open", "high", "low", "close", "volume", "amount"]] = ""
    transport = _FakeTransport(
        raw=raw,
        statuses=_status_rows(latest_status="0", latest_is_st="1"),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-suspended-bundle",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert bundle.strict_bundle_state is BaoStockStrictBundleState.COMPLETE
    assert bundle.publication is not None
    assert bundle.statuses is not None
    assert bundle.statuses.current_tradeability is BaoStockCurrentTradeability.SUSPENDED
    assert bundle.publication.observations[-1].close == Decimal("21.000")
    assert bundle.publication.observations[-1].volume == Decimal("0")
    assert bundle.publication.trading_statuses[-1].status.value == "suspended"
    assert bundle.publication.trading_statuses[-1].official_carried_close == Decimal("21.000")


@pytest.mark.unit
def test_blank_status_and_zero_volume_do_not_establish_tradeability_or_bundle(
    tmp_path,
) -> None:
    raw = _raw_rows()
    raw.loc[1, "volume"] = "0"
    statuses = _status_rows(latest_status="")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(raw=raw, statuses=statuses),
            run_scope_id="ticket-08-blank-status",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert bundle.statuses is not None
    assert bundle.statuses.current_tradeability is BaoStockCurrentTradeability.UNKNOWN
    assert bundle.statuses.candidates[-1].trading_status is None
    assert bundle.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert bundle.publication is None


@pytest.mark.unit
def test_contradictory_status_duplicates_fail_typed_and_establish_no_current_state(
    tmp_path,
) -> None:
    statuses = pd.concat(
        [
            _status_rows(),
            _status_rows().iloc[[-1]].assign(tradestatus="0"),
        ],
        ignore_index=True,
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(statuses=statuses),
            run_scope_id="ticket-08-contradictory-status",
        )
        result = harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.capability_outcome.kind is (BaoStockCapabilityOutcomeKind.CONTRADICTORY_STATUS)
    assert result.provider_artifact is not None
    assert len(result.row_rejections) == 2
    assert result.current_tradeability is BaoStockCurrentTradeability.UNKNOWN


@pytest.mark.unit
def test_incompatible_metadata_retains_artifact_and_fails_typed(tmp_path) -> None:
    wrong_symbol = _raw_rows().assign(code="sz.000001")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(raw=wrong_symbol),
            run_scope_id="ticket-08-incompatible-metadata",
        )
        result = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.capability_outcome.kind is (BaoStockCapabilityOutcomeKind.INCOMPATIBLE_METADATA)
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert result.candidates == ()
    assert len(result.row_rejections) == 2


@pytest.mark.unit
def test_raw_artifact_preserves_original_fields_times_and_capability_identity(
    tmp_path,
) -> None:
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(),
            run_scope_id="ticket-08-original-artifact",
        )
        result = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
    assert result.provider_artifact is not None
    artifact_bytes = ProviderSubrequestArtifactStore(tmp_path / "artifacts").read(
        result.provider_artifact
    )
    payload = json.loads(artifact_bytes)

    assert payload["provider_id"] == "baostock"
    assert payload["endpoint_scope"] == "raw_daily"
    assert payload["capability_identities"] == ["baostock_raw_daily_observations"]
    assert payload["retrieved_at"] == "2026-07-30T12:00:00Z"
    assert payload["observed_at"] == "2026-07-30T12:00:00Z"
    assert payload["rows"][0] == {
        "amount": "2000.00",
        "close": "21.000",
        "code": "sh.600895",
        "date": "2026-07-23",
        "high": "22.000",
        "low": "18.000",
        "open": "20.000",
        "preclose": "19.500",
        "volume": "100",
    }
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert "raw_daily_observations" in rendered
    assert "rows" not in result.model_dump(mode="json")


@pytest.mark.unit
def test_identical_concurrent_and_completed_requests_single_flight(tmp_path) -> None:
    transport = _BlockingTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-single-flight",
        )

        def acquire():
            return harness.adapter.acquire_raw_daily(
                instrument_identity=_identity(),
                range_start=date(2026, 7, 23),
                as_of_date=date(2026, 7, 24),
            )

        def follower():
            assert transport.started.wait(timeout=5)
            return acquire()

        def release_leader():
            assert transport.started.wait(timeout=5)
            transport.release.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            second = executor.submit(follower)
            releaser = executor.submit(release_leader)
            first_result = acquire()
            second_result = second.result(timeout=5)
            releaser.result(timeout=5)
        completed_result = acquire()

    assert first_result == second_result == completed_result
    assert [call[0] for call in transport.calls] == ["raw"]
    assert len(first_result.attempt_events) == 1


@pytest.mark.unit
def test_checkpoint_restores_all_capabilities_without_new_io(tmp_path) -> None:
    original_transport = _FakeTransport()
    resumed_transport = _FakeTransport(
        raw=RuntimeError("must not call token=unsafe"),
        factors=RuntimeError("must not call token=unsafe"),
        statuses=RuntimeError("must not call token=unsafe"),
        lifecycle=RuntimeError("must not call token=unsafe"),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        original = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=original_transport,
            run_scope_id="ticket-08-checkpoint",
        )
        original_bundle = original.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        original_lifecycle = original.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
        )
        checkpoint = original.cache.checkpoint()
        resumed = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=resumed_transport,
            run_scope_id="ticket-08-checkpoint",
            checkpoint=checkpoint,
            coordinator=original.coordinator,
            observed_at=_OBSERVED_AT + timedelta(days=1),
        )
        resumed_bundle = resumed.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        resumed_lifecycle = resumed.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
        )

    assert resumed_transport.calls == []
    assert resumed_bundle == original_bundle
    assert resumed_lifecycle == original_lifecycle
    assert len(checkpoint["entries"]) == 4


@pytest.mark.unit
def test_endpoints_have_distinct_scopes_and_exact_attempt_event_cardinality(
    tmp_path,
) -> None:
    transport = _FakeTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-endpoint-cardinality",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        lifecycle = harness.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
        )
        results = (bundle.raw, bundle.factors, bundle.statuses, lifecycle)
        events = tuple(
            event
            for result in results
            if result is not None
            for event in harness.coordinator.physical_attempt_events(result.sequence_id)
        )

    assert len(transport.calls) == 4
    assert len(events) == 4
    assert {event.capacity_scope for event in events} == {
        "raw_daily",
        "forward_adjustment_factors",
        "session_status",
        "issuer_lifecycle",
    }
    assert len({event.attempt_event_id for event in events}) == 4
    assert len({event.upstream_service_id for event in events}) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider_result", "transport_kind", "capability_kind"),
    [
        (
            RuntimeError("permission denied token=do-not-persist provider.invalid"),
            ProviderSubrequestOutcomeKind.PERMISSION_DENIED,
            BaoStockCapabilityOutcomeKind.PERMISSION_DENIED,
        ),
        (
            RuntimeError("authentication login failed token=do-not-persist"),
            ProviderSubrequestOutcomeKind.AUTHENTICATION,
            BaoStockCapabilityOutcomeKind.AUTHENTICATION_FAILURE,
        ),
        (
            _RateLimitError("10001005 token=do-not-persist"),
            ProviderSubrequestOutcomeKind.RATE_LIMITED,
            BaoStockCapabilityOutcomeKind.RATE_LIMITED,
        ),
        (
            TimeoutError("timeout token=do-not-persist"),
            ProviderSubrequestOutcomeKind.TIMEOUT,
            BaoStockCapabilityOutcomeKind.TIMEOUT,
        ),
        (
            ConnectionError("connection reset token=do-not-persist"),
            ProviderSubrequestOutcomeKind.DISCONNECT,
            BaoStockCapabilityOutcomeKind.DISCONNECT,
        ),
        (
            ["not-a-frame"],
            ProviderSubrequestOutcomeKind.MALFORMED,
            BaoStockCapabilityOutcomeKind.MALFORMED_RESPONSE,
        ),
        (
            RuntimeError("provider exploded token=do-not-persist provider.invalid"),
            ProviderSubrequestOutcomeKind.PROVIDER_ERROR,
            BaoStockCapabilityOutcomeKind.PROVIDER_ERROR,
        ),
    ],
)
def test_transport_failures_are_typed_single_attempt_and_sanitized(
    tmp_path,
    provider_result: object,
    transport_kind: ProviderSubrequestOutcomeKind,
    capability_kind: BaoStockCapabilityOutcomeKind,
) -> None:
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(raw=provider_result),
            run_scope_id=f"ticket-08-failure-{capability_kind.value}",
        )
        result = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        checkpoint = harness.cache.checkpoint()

    assert result.outcome.kind is transport_kind
    assert result.capability_outcome.kind is capability_kind
    assert result.provider_artifact is None
    assert result.candidates == ()
    assert len(result.attempt_events) == 1
    rendered = (
        json.dumps(
            {"result": result.model_dump(mode="json"), "checkpoint": checkpoint},
            sort_keys=True,
        )
        .encode()
        .lower()
    )
    persisted = b"".join(
        path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()
    ).lower()
    surfaces = rendered + persisted
    assert b"do-not-persist" not in surfaces
    assert b"provider.invalid" not in surfaces
    assert b"token=" not in surfaces


@pytest.mark.unit
def test_endpoint_cooldown_spares_other_capability_but_global_cooldown_blocks_it(
    tmp_path,
) -> None:
    endpoint_transport = _FakeTransport(factors=_RateLimitError("rate limit"))
    with MarketHistoryStore.open(_history_config(tmp_path / "endpoint")) as store:
        endpoint = _build_harness(
            tmp_path=tmp_path / "endpoint",
            store=store,
            transport=endpoint_transport,
            run_scope_id="ticket-08-endpoint-cooldown",
        )
        factor = endpoint.adapter.acquire_forward_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        status = endpoint.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    global_transport = _FakeTransport(
        factors=_BaoStockLoginCapacityError("Baostock login capacity unavailable")
    )
    with MarketHistoryStore.open(_history_config(tmp_path / "global")) as store:
        global_harness = _build_harness(
            tmp_path=tmp_path / "global",
            store=store,
            transport=global_transport,
            run_scope_id="ticket-08-global-cooldown",
        )
        global_factor = global_harness.adapter.acquire_forward_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        blocked_status = global_harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert factor.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert status.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert [call[0] for call in endpoint_transport.calls] == ["factors", "statuses"]
    assert global_factor.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert blocked_status.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert [call[0] for call in global_transport.calls] == ["factors"]
    assert blocked_status.attempt_events == ()


@pytest.mark.unit
def test_legacy_daily_order_and_adapter_non_admission_contract_remain_unchanged(
    tmp_path,
) -> None:
    plan = _legacy_plan()
    assert plan.route_for(next(iter(plan.routes)).capability) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(),
            run_scope_id="ticket-08-no-admission",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        lifecycle = harness.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
        )

    for result in (bundle.raw, bundle.factors, bundle.statuses, lifecycle):
        assert result is not None
        assert result.source_facts_created is False
        assert result.decision_gate_changed is False
    assert lifecycle.candidates[0].observed_name == "张江高科"
    assert lifecycle.candidates[0].st_status_established is False


@pytest.mark.unit
def test_cross_instrument_and_as_of_components_cannot_form_a_strict_bundle(
    tmp_path,
) -> None:
    sh_identity = _identity()
    sz_identity = _identity("000001.SZ", "XSHE")
    sz_factors = _factor_rows().assign(code="sz.000001")
    sz_statuses = _status_rows().assign(code="sz.000001")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        sh = _build_harness(
            tmp_path=tmp_path / "sh",
            store=store,
            transport=_FakeTransport(),
            run_scope_id="ticket-08-cross-instrument-sh",
        )
        sz = _build_harness(
            tmp_path=tmp_path / "sz",
            store=store,
            transport=_FakeTransport(factors=sz_factors, statuses=sz_statuses),
            run_scope_id="ticket-08-cross-instrument-sz",
            coordinator=sh.coordinator,
        )
        raw = sh.adapter.acquire_raw_daily(
            instrument_identity=sh_identity,
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        factors = sz.adapter.acquire_forward_adjustment_factors(
            instrument_identity=sz_identity,
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 25),
        )
        statuses = sz.adapter.acquire_session_status(
            instrument_identity=sz_identity,
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        result = assemble_baostock_history_bundle(
            instrument_identity=sh_identity,
            as_of_date=date(2026, 7, 24),
            raw=raw,
            factors=factors,
            statuses=statuses,
        )

    assert result.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert result.publication is None


@pytest.mark.unit
def test_partial_or_malformed_latest_status_retains_artifact_but_has_no_authority(
    tmp_path,
) -> None:
    statuses = _status_rows()
    statuses.loc[1, "tradestatus"] = "unexpected"
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(statuses=statuses),
            run_scope_id="ticket-08-malformed-latest-status",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert bundle.statuses is not None
    assert bundle.statuses.provider_artifact is not None
    assert bundle.statuses.capability_outcome.kind is (
        BaoStockCapabilityOutcomeKind.MALFORMED_RESPONSE
    )
    assert bundle.statuses.current_tradeability is BaoStockCurrentTradeability.UNKNOWN
    assert bundle.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE


@pytest.mark.unit
def test_omitted_latest_session_cannot_establish_current_tradeability(tmp_path) -> None:
    statuses = _status_rows().iloc[:1].copy()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(statuses=statuses),
            run_scope_id="ticket-08-stale-current-status",
        )
        result = harness.adapter.acquire_session_status(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.capability_outcome.kind is BaoStockCapabilityOutcomeKind.AVAILABLE
    assert result.candidates[-1].session_date == date(2026, 7, 23)
    assert result.current_tradeability is BaoStockCurrentTradeability.UNKNOWN


@pytest.mark.unit
def test_factor_request_has_no_lower_bound_and_retains_opening_lineage(tmp_path) -> None:
    transport = _FakeTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-factor-full-history",
        )
        result = harness.adapter.acquire_forward_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.candidates[0].effective_date == date(2026, 7, 1)
    assert transport.calls == [
        (
            "factors",
            {
                "code": "sh.600895",
                "start_date": None,
                "end_date": "2026-07-24",
            },
        )
    ]


@pytest.mark.unit
def test_safe_empty_response_is_retained_as_typed_incomplete_artifact(tmp_path) -> None:
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(raw=pd.DataFrame(columns=_raw_rows().columns)),
            run_scope_id="ticket-08-retained-empty",
        )
        result = harness.adapter.acquire_raw_daily(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.capability_outcome.kind is BaoStockCapabilityOutcomeKind.EMPTY_NO_DATA
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert result.candidates == ()


@pytest.mark.unit
def test_lifecycle_rejects_provider_instrument_type_mismatch(tmp_path) -> None:
    lifecycle = _lifecycle_rows().assign(type="2")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(lifecycle=lifecycle),
            run_scope_id="ticket-08-lifecycle-type",
        )
        result = harness.adapter.acquire_lifecycle(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 24),
        )

    assert result.provider_artifact is not None
    assert result.capability_outcome.kind is (
        BaoStockCapabilityOutcomeKind.INCOMPATIBLE_METADATA
    )
    assert result.candidates == ()


@pytest.mark.unit
def test_shenzhen_complete_bundle_uses_same_authority_contract(tmp_path) -> None:
    identity = _identity("000001.SZ", "XSHE")
    transport = _FakeTransport(
        raw=_raw_rows().assign(code="sz.000001"),
        factors=_factor_rows().assign(code="sz.000001"),
        statuses=_status_rows().assign(code="sz.000001"),
        lifecycle=_lifecycle_rows().assign(code="sz.000001"),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-08-shenzhen",
        )
        bundle = harness.adapter.acquire_provider_history_bundle(
            instrument_identity=identity,
            range_start=date(2026, 7, 23),
            as_of_date=date(2026, 7, 24),
        )
        lifecycle = harness.adapter.acquire_lifecycle(
            instrument_identity=identity,
            as_of_date=date(2026, 7, 24),
        )

    assert bundle.strict_bundle_state is BaoStockStrictBundleState.COMPLETE
    assert bundle.publication is not None
    assert bundle.publication.instrument.canonical_symbol == "000001.SZ"
    assert lifecycle.candidates[0].listing_date == date(1996, 4, 22)
