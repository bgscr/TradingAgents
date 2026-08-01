from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from threading import Event, Lock

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestCacheError,
    ProviderSubrequestCacheFailureReason,
    ProviderSubrequestKey,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestPriority,
    upstream_service_identity_for_provider,
)


def _identity(
    *,
    symbol: str = "600519.SS",
    venue: str = "XSHG",
) -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue=venue,
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="mainland-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _key(**changes: object) -> ProviderSubrequestKey:
    upstream_service_id, _ = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )
    values: dict[str, object] = {
        "provider_id": "tushare",
        "upstream_service_id": upstream_service_id,
        "account_scope": "personal-research-primary",
        "capacity_scope": "income",
        "instrument_identity": _identity(),
        "requested_range_start": "2021-01-01",
        "requested_range_end": "2026-07-29",
        "requested_fields": ("revenue", "total_profit"),
        "as_of_date": date(2026, 7, 29),
        "qualification_profile": "cn-a-2000-20260729-v1",
        "normalizer_version": "mainland-financial-normalizer-v1",
        "data_usage_mode": DataUsageMode.PERSONAL_RESEARCH,
    }
    values.update(changes)
    return ProviderSubrequestKey.create(**values)


def _config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


@pytest.mark.unit
def test_provider_subrequest_key_is_canonical_and_materially_scoped() -> None:
    original = _key()
    reordered_fields = _key(requested_fields=("total_profit", "revenue"))

    assert reordered_fields == original
    assert len(
        {
            original.subrequest_key,
            _key(capacity_scope="balancesheet").subrequest_key,
            _key(
                instrument_identity=_identity(symbol="000001.SZ", venue="XSHE")
            ).subrequest_key,
            _key(requested_range_start="2022-01-01").subrequest_key,
            _key(requested_fields=("revenue",)).subrequest_key,
            _key(qualification_profile="cn-a-next-v2").subrequest_key,
            _key(normalizer_version="mainland-financial-normalizer-v2").subrequest_key,
            _key(data_usage_mode=DataUsageMode.PRODUCTION).subrequest_key,
        }
    ) == 8


@pytest.mark.unit
def test_unsafe_account_scope_fails_without_echoing_secret_text() -> None:
    unsafe_scope = "token=must-not-enter-key-or-exception"

    with pytest.raises(ProviderSubrequestCacheError) as exc_info:
        _key(account_scope=unsafe_scope)

    assert (
        exc_info.value.reason
        is ProviderSubrequestCacheFailureReason.UNSAFE_CACHE_IDENTITY
    )
    assert unsafe_scope not in str(exc_info.value)


@pytest.mark.unit
def test_tushare_key_rejects_service_identity_for_a_different_account() -> None:
    wrong_identity, _ = upstream_service_identity_for_provider(
        "tushare",
        account_scope="different-research-account",
    )

    with pytest.raises(ProviderSubrequestCacheError) as exc_info:
        _key(upstream_service_id=wrong_identity)

    assert (
        exc_info.value.reason
        is ProviderSubrequestCacheFailureReason.UPSTREAM_IDENTITY_MISMATCH
    )


@pytest.mark.unit
def test_secret_like_range_or_field_fails_without_entering_cache_identity() -> None:
    unsafe_text = "token:must-not-enter-key"

    for changes in (
        {"requested_range_start": unsafe_text},
        {"requested_fields": ("revenue", unsafe_text)},
        {"qualification_profile": unsafe_text},
    ):
        with pytest.raises(ProviderSubrequestCacheError) as exc_info:
            _key(**changes)
        assert (
            exc_info.value.reason
            is ProviderSubrequestCacheFailureReason.UNSAFE_CACHE_IDENTITY
        )
        assert unsafe_text not in str(exc_info.value)


@pytest.mark.unit
def test_identical_subrequests_share_one_inflight_and_terminal_physical_attempt(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    follower_started = Event()
    calls_lock = Lock()
    physical_calls = 0
    follower = None
    executor = ThreadPoolExecutor(max_workers=1)

    def physical_request() -> bytes:
        nonlocal follower, physical_calls
        with calls_lock:
            physical_calls += 1
        follower = executor.submit(execute, "follower")
        assert follower_started.wait(timeout=5)
        return b'{"annual":[2025],"reporting":["2026-Q2"]}'

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-single-flight",
        )

        def execute(owner_id: str):
            if owner_id == "follower":
                follower_started.set()
            return cache.execute(
                key,
                owner_id=owner_id,
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial-statement:income",
                media_type="application/json",
                physical_request=physical_request,
            )

        first = execute("leader")
        assert follower is not None
        second = follower.result(timeout=5)
        completed_duplicate = execute("completed-duplicate")
        persisted_handoff = store._connection.execute(
            "SELECT result_payload FROM provider_request_sequences "
            "WHERE sequence_id = ?",
            (first.sequence_id,),
        ).fetchone()
        executor.shutdown(wait=True)

    assert physical_calls == 1
    assert first is second is completed_duplicate
    assert first.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert first.value == b'{"annual":[2025],"reporting":["2026-Q2"]}'
    assert len(first.attempt_events) == 1
    assert first.attempt_events[0].capacity_scope == "income"
    assert persisted_handoff is not None
    assert b'"type":"bytes"' not in persisted_handoff[0]
    assert b"provider-subrequest-artifact=sha256:" in persisted_handoff[0]


@pytest.mark.unit
def test_single_flight_leader_may_run_on_worker_thread(tmp_path) -> None:
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
    key = _key()

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-worker-leader",
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                cache.execute,
                key,
                owner_id="worker-leader",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial-statement:income",
                media_type="application/json",
                physical_request=lambda: b'{"safe_fixture":true}',
            ).result(timeout=5)

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1


@pytest.mark.unit
def test_checkpoint_resume_reuses_terminal_artifact_and_outcome_without_io(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    physical_calls = 0
    artifact_store = ProviderSubrequestArtifactStore(
        tmp_path / "subrequest-artifacts"
    )

    def physical_request() -> bytes:
        nonlocal physical_calls
        physical_calls += 1
        return b'{"safe_fixture":"payload-not-in-checkpoint"}'

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="ticket-03-checkpoint-run",
        )
        original = cache.execute(
            key,
            owner_id="original",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            media_type="application/json",
            physical_request=physical_request,
        )
        checkpoint = cache.checkpoint()
        checkpoint_text = json.dumps(checkpoint, sort_keys=True)

        restored_cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            checkpoint=checkpoint,
            run_scope_id="ticket-03-checkpoint-run",
        )
        restored = restored_cache.execute(
            key,
            owner_id="resumed",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now + timedelta(minutes=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            media_type="application/json",
            physical_request=lambda: pytest.fail("resume must perform zero provider I/O"),
        )
        with pytest.raises(ProviderSubrequestCacheError) as mismatch_info:
            ProviderSubrequestCache(
                coordinator=coordinator,
                artifact_store=artifact_store,
                checkpoint=checkpoint,
                run_scope_id="different-run",
            )

    assert physical_calls == 1
    assert "payload-not-in-checkpoint" not in checkpoint_text
    assert restored.value == original.value
    assert restored.artifact == original.artifact
    assert restored.outcome == original.outcome
    assert restored.attempt_events == original.attempt_events
    assert mismatch_info.value.reason is (
        ProviderSubrequestCacheFailureReason.RUN_SCOPE_MISMATCH
    )


@pytest.mark.unit
def test_secret_like_checkpoint_key_fails_typed_without_echo_or_io(tmp_path) -> None:
    unsafe_text = "token:must-not-enter-checkpoint"
    key_payload = _key().model_dump(mode="json")
    key_payload["qualification_profile"] = unsafe_text
    canonical_payload = dict(key_payload)
    canonical_payload.pop("subrequest_key")
    digest = sha256(
        json.dumps(
            canonical_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    key_payload["subrequest_key"] = f"provider-subrequest:v1:{digest}"
    checkpoint = {
        "contract_version": "1.0",
        "run_scope_id": "ticket-03-unsafe-checkpoint",
        "entries": [
            {
                "key": key_payload,
                "artifact": None,
                "outcome": {"kind": "rate_limited", "retryable": True},
                "sequence_id": "",
                "attempt_events": [],
            }
        ],
    }

    with (
        MarketHistoryStore.open(_config(tmp_path)) as store,
        pytest.raises(ProviderSubrequestCacheError) as exc_info,
    ):
        ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            checkpoint=checkpoint,
            run_scope_id="ticket-03-unsafe-checkpoint",
        )

    assert (
        exc_info.value.reason
        is ProviderSubrequestCacheFailureReason.MALFORMED_CHECKPOINT
    )
    assert unsafe_text not in str(exc_info.value)


@pytest.mark.unit
def test_corrupt_checkpoint_artifact_fails_typed_before_provider_io(tmp_path) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    physical_calls = 0
    artifact_root = tmp_path / "subrequest-artifacts"
    artifact_store = ProviderSubrequestArtifactStore(artifact_root)

    def physical_request() -> bytes:
        nonlocal physical_calls
        physical_calls += 1
        return b'{"safe_fixture":"immutable"}'

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="ticket-03-corrupt-artifact",
        )
        result = cache.execute(
            key,
            owner_id="original",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            media_type="application/json",
            physical_request=physical_request,
        )
        checkpoint = cache.checkpoint()
        assert result.artifact is not None
        digest = result.artifact.artifact_sha256
        (artifact_root / "sha256" / digest[:2] / f"{digest}.bin").write_bytes(
            b"corrupt"
        )

        with pytest.raises(ProviderSubrequestCacheError) as exc_info:
            ProviderSubrequestCache(
                coordinator=coordinator,
                artifact_store=artifact_store,
                checkpoint=checkpoint,
                run_scope_id="ticket-03-corrupt-artifact",
            )

    assert physical_calls == 1
    assert (
        exc_info.value.reason
        is ProviderSubrequestCacheFailureReason.ARTIFACT_CORRUPT
    )
    assert str(exc_info.value) == "provider_subrequest_artifact_corrupt"


@pytest.mark.unit
def test_contradictory_cache_checkpoint_fails_typed_before_provider_io(
    tmp_path,
) -> None:
    key = _key()
    artifact_store = ProviderSubrequestArtifactStore(
        tmp_path / "subrequest-artifacts"
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        contradictory = {
            "contract_version": "1.0",
            "run_scope_id": "ticket-03-contradictory",
            "entries": [
                {
                    "key": key.model_dump(mode="json"),
                    "artifact": None,
                    "outcome": {"kind": "available", "retryable": False},
                    "sequence_id": "provider-request-sequence=sha256:" + "b" * 64,
                    "attempt_events": [],
                }
            ],
        }

        with pytest.raises(ProviderSubrequestCacheError) as exc_info:
            ProviderSubrequestCache(
                coordinator=coordinator,
                artifact_store=artifact_store,
                checkpoint=contradictory,
                run_scope_id="ticket-03-contradictory",
            )

    assert (
        exc_info.value.reason
        is ProviderSubrequestCacheFailureReason.MALFORMED_CHECKPOINT
    )
    assert "payload" not in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("transport_case", "expected_outcome"),
    [
        ("success", ProviderSubrequestOutcomeKind.AVAILABLE),
        ("permission", ProviderSubrequestOutcomeKind.PERMISSION_DENIED),
        ("rate_limit", ProviderSubrequestOutcomeKind.RATE_LIMITED),
        ("authentication", ProviderSubrequestOutcomeKind.AUTHENTICATION),
        ("empty", ProviderSubrequestOutcomeKind.EMPTY),
        ("malformed", ProviderSubrequestOutcomeKind.MALFORMED),
    ],
)
def test_transport_boundary_records_one_typed_terminal_attempt(
    tmp_path,
    transport_case: str,
    expected_outcome: ProviderSubrequestOutcomeKind,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key(capacity_scope=f"case-{transport_case}")
    physical_calls = 0

    def physical_request() -> bytes:
        nonlocal physical_calls
        physical_calls += 1
        if transport_case == "success":
            return b"{}"
        if transport_case == "empty":
            return b""
        if transport_case == "malformed":
            return {"payload": "must-not-cross-boundary"}  # type: ignore[return-value]
        outcome = {
            "permission": PhysicalAttemptOutcome.PERMISSION_DENIED,
            "rate_limit": PhysicalAttemptOutcome.RATE_LIMITED,
            "authentication": PhysicalAttemptOutcome.AUTHENTICATION,
        }[transport_case]
        raise PhysicalAttemptFailure(
            outcome=outcome,
            retryable=transport_case == "rate_limit",
            retry_after_seconds=(15 if transport_case == "rate_limit" else None),
        )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id=f"ticket-03-transport-{transport_case}",
        )
        result = cache.execute(
            key,
            owner_id=f"transport-{transport_case}",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation=f"transport:{transport_case}",
            media_type="application/json",
            physical_request=physical_request,
        )

    assert physical_calls == 1
    assert result.outcome.kind is expected_outcome
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].outcome is expected_outcome
    assert result.attempt_events[0].capacity_scope == f"case-{transport_case}"
    assert (result.artifact is not None) is (
        expected_outcome is ProviderSubrequestOutcomeKind.AVAILABLE
    )


@pytest.mark.unit
def test_bulk_statement_subrequest_supplies_annual_and_reporting_projections_once(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    run_asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    key = _key(
        instrument_identity=run_asset_configuration.instrument_identity,
    )
    physical_calls = 0

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-bulk-statements",
        )

        class BulkStatementInvoker:
            def __init__(self, *, allow_io: bool = True) -> None:
                self._allow_io = allow_io

            def __call__(self, _request: FinancialToolRequest) -> object:
                raise AssertionError("dispatcher must provide the subrequest cache")

            def invoke_with_provider_subrequest_cache(
                self,
                request: FinancialToolRequest,
                _canonical_request_key: str,
                provider_subrequest_cache: ProviderSubrequestCache,
            ) -> str:
                nonlocal physical_calls

                def physical_request() -> bytes:
                    nonlocal physical_calls
                    if not self._allow_io:
                        pytest.fail("checkpoint resume must perform zero provider I/O")
                    physical_calls += 1
                    return b'{"annual":"2025-12-31","reporting":"2026-06-30"}'

                result = provider_subrequest_cache.execute(
                    key,
                    owner_id=f"bulk-{request.frequency.value}",
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=lambda: now,
                    sleep=lambda _seconds: None,
                    lease_duration=timedelta(seconds=30),
                    operation="financial-statement:income",
                    media_type="application/json",
                    physical_request=physical_request,
                )
                assert result.value is not None
                payload = json.loads(result.value)
                period = (
                    payload["annual"]
                    if request.frequency is FinancialReportingFrequency.ANNUAL
                    else payload["reporting"]
                )
                return (
                    f"# Balance Sheet data for 601328.SS ({request.frequency.value})\n"
                    "# Data retrieved on: 2026-07-29 10:00:00\n\n"
                    f"metric,{period}\nTotal,100"
                )

        dispatcher = FinancialToolDispatcher(
            instrument_identity=run_asset_configuration.instrument_identity,
            run_asset_configuration=run_asset_configuration,
            provider_chains={
                "get_balance_sheet": (
                    FinancialProvider(
                        name="tushare",
                        variants=(
                            FinancialProviderVariant(
                                variant_id="income-bulk",
                                invoke=BulkStatementInvoker(),
                            ),
                        ),
                    ),
                )
            },
            provider_subrequest_cache=cache,
            clock=lambda: now,
            sleeper=lambda _seconds: None,
        )

        annual_request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.ANNUAL,
            as_of_date=date(2026, 7, 29),
            tool_call_id="annual-view",
        )
        reporting_request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 29),
            tool_call_id="reporting-view",
        )
        annual = dispatcher.dispatch(annual_request)
        reporting = dispatcher.dispatch(reporting_request)
        ledger = dispatcher.checkpoint_ledger()

        restored_cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            checkpoint=ledger["provider_subrequest_cache"],
            run_scope_id="ticket-03-bulk-statements",
        )
        restored_dispatcher = FinancialToolDispatcher(
            instrument_identity=run_asset_configuration.instrument_identity,
            run_asset_configuration=run_asset_configuration,
            provider_chains={
                "get_balance_sheet": (
                    FinancialProvider(
                        name="tushare",
                        variants=(
                            FinancialProviderVariant(
                                variant_id="income-bulk",
                                invoke=BulkStatementInvoker(allow_io=False),
                            ),
                        ),
                    ),
                )
            },
            provider_subrequest_cache=restored_cache,
            checkpoint_ledger=ledger,
            clock=lambda: now,
            sleeper=lambda _seconds: None,
        )
        resumed_annual = restored_dispatcher.dispatch(annual_request)
        resumed_reporting = restored_dispatcher.dispatch(reporting_request)

        empty_cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "empty-subrequest-artifacts"
            ),
            run_scope_id="ticket-03-bulk-statements",
        )
        with pytest.raises(FinancialDispatchCheckpointError) as exc_info:
            FinancialToolDispatcher(
                instrument_identity=run_asset_configuration.instrument_identity,
                run_asset_configuration=run_asset_configuration,
                provider_chains={
                    "get_balance_sheet": (
                        FinancialProvider(
                            name="tushare",
                            variants=(
                                FinancialProviderVariant(
                                    variant_id="income-bulk",
                                    invoke=BulkStatementInvoker(allow_io=False),
                                ),
                            ),
                        ),
                    )
                },
                provider_subrequest_cache=empty_cache,
                checkpoint_ledger=ledger,
                clock=lambda: now,
                sleeper=lambda _seconds: None,
            )

    assert physical_calls == 1
    assert "2025-12-31" in (annual.value or "")
    assert "2026-06-30" in (reporting.value or "")
    assert annual.request_key != reporting.request_key
    assert len(cache.checkpoint()["entries"]) == 1
    assert ledger["provider_subrequest_cache"] == cache.checkpoint()
    assert resumed_annual.value == annual.value
    assert resumed_reporting.value == reporting.value
    assert exc_info.value.reason is (
        FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
    )


@pytest.mark.unit
def test_cache_bearing_financial_ledger_projects_to_audit_without_provider_io(
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    run_asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=runtime_config,
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        cache = ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-audit-projection",
        )
        dispatcher = FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=run_asset_configuration.instrument_identity,
            run_asset_configuration=run_asset_configuration,
            config=runtime_config,
            provider_subrequest_cache=cache,
        )
        ledger = dispatcher.checkpoint_ledger()
        projection = project_financial_dispatch_ledger(
            ledger,
            run_asset_configuration=run_asset_configuration,
            config=runtime_config,
        )

    assert ledger["provider_subrequest_cache"] == {
        "contract_version": "1.0",
        "run_scope_id": "ticket-03-audit-projection",
        "entries": [],
    }
    assert projection is not None
    assert projection.request_count == 0
    assert "provider_subrequest_cache" not in json.dumps(
        projection.model_dump(mode="json"),
        sort_keys=True,
    )


@pytest.mark.unit
def test_provider_permission_failure_remains_distinct_and_terminal(tmp_path) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    physical_calls = 0

    def permission_denied() -> bytes:
        nonlocal physical_calls
        physical_calls += 1
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
        )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-permission",
        )
        result = cache.execute(
            key,
            owner_id="permission-test",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            media_type="application/json",
            physical_request=permission_denied,
        )

    assert physical_calls == 1
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.PERMISSION_DENIED
    assert result.attempt_events[0].outcome is (
        ProviderSubrequestOutcomeKind.PERMISSION_DENIED
    )


@pytest.mark.unit
def test_baostock_family_year_quarter_keys_have_exact_attempt_cardinality(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "baostock"
    )
    shared = {
        "provider_id": "baostock",
        "upstream_service_id": upstream_service_id,
        "account_scope": "public",
        "instrument_identity": _identity(),
        "requested_fields": ("value",),
    }
    keys = (
        _key(
            **shared,
            capacity_scope="profit",
            requested_range_start="2025-Q1",
            requested_range_end="2025-Q1",
        ),
        _key(
            **shared,
            capacity_scope="profit",
            requested_range_start="2025-Q2",
            requested_range_end="2025-Q2",
        ),
        _key(
            **shared,
            capacity_scope="growth",
            requested_range_start="2025-Q1",
            requested_range_end="2025-Q1",
        ),
    )
    physical_calls = 0

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="public",
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-baostock-cardinality",
        )

        def execute(key: ProviderSubrequestKey):
            def physical_request() -> bytes:
                nonlocal physical_calls
                physical_calls += 1
                return key.subrequest_key.encode("utf-8")

            return cache.execute(
                key,
                owner_id=f"baostock-{physical_calls}",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now + timedelta(seconds=physical_calls),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial-ratio-family",
                media_type="application/json",
                physical_request=physical_request,
            )

        results = tuple(execute(key) for key in keys)
        duplicate = execute(keys[0])

    assert len({key.subrequest_key for key in keys}) == 3
    assert physical_calls == 3
    assert sum(len(result.attempt_events) for result in results) == 3
    assert duplicate is results[0]


@pytest.mark.unit
def test_retained_artifact_is_not_an_implicit_cross_run_response_cache(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    physical_calls = 0
    artifact_store = ProviderSubrequestArtifactStore(
        tmp_path / "subrequest-artifacts"
    )

    def physical_request() -> bytes:
        nonlocal physical_calls
        physical_calls += 1
        return b'{"retained":"evidence"}'

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        for run_number in (1, 2):
            cache = ProviderSubrequestCache(
                coordinator=coordinator,
                artifact_store=artifact_store,
                run_scope_id=f"ticket-03-retained-run-{run_number}",
            )
            cache.execute(
                key,
                owner_id=f"run-{run_number}",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda run_number=run_number: now
                + timedelta(minutes=run_number),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial-statement:income",
                media_type="application/json",
                physical_request=physical_request,
            )

    assert physical_calls == 2


@pytest.mark.unit
def test_raw_provider_exception_text_never_crosses_cache_boundary(tmp_path) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    key = _key()
    unsafe_text = "RAW_PROVIDER_ERROR token=not-for-output payload=private"
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare Pro account",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(
                tmp_path / "subrequest-artifacts"
            ),
            run_scope_id="ticket-03-raw-exception",
        )

        result = cache.execute(
            key,
            owner_id="unsafe-provider-error",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            media_type="application/json",
            physical_request=lambda: (_ for _ in ()).throw(
                RuntimeError(unsafe_text)
            ),
        )
        checkpoint_text = json.dumps(cache.checkpoint(), sort_keys=True)

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.PROVIDER_ERROR
    assert unsafe_text not in checkpoint_text
