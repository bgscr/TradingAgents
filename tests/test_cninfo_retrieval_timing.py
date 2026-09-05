"""Offline acquisition timing through the real CNINFO/cache/coordinator path."""

import base64
import json
from datetime import date, datetime, timedelta
from threading import Event

import pytest

from tests.test_cninfo_listing_publication_client import (
    _FIXTURE_ROOT,
    _NOW,
    _cache,
    _effective,
    _FixtureTransport,
    _history_config,
    _identity,
    _LegacyFixtureTransport,
    _request,
)
from tests.test_financial_eligibility_metadata_acquisition import _asset, _qualified_plan
from tests.test_financial_metadata_temporal_eligibility import _evidence as _company_proof
from tests.test_provider_subrequest_cache import _key as _other_provider_key
from tradingagents.capability_routing import (
    MainlandCapability,
    prepare_mainland_listing_publication_candidate_plan,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.dataflows.cninfo_listing_publication import (
    CninfoFailureReason,
    CninfoListingBoard,
    CninfoListingPublicationAdapter,
    CninfoListingPublicationRequest,
    CninfoSourcePolicy,
    RequestsCninfoHttpTransport,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCompanyTypeEvidenceProviderProjection,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeClassifierInput,
    FinancialConsolidationScope,
    FinancialListingProvenance,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialListingMetadata,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
    FinancialEligibilityMetadataAcquisitionRequest,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    TemporalEligibilityMode,
    build_financial_eligibility_acquisition_window,
    build_financial_eligibility_temporal_transaction_binding,
    build_market_session_eligibility_binding,
)
from tradingagents.dataflows.financial_temporal_proof_projection import (
    FinancialEligibilityTemporalEvaluationTransaction,
)
from tradingagents.dataflows.listing_publication_evidence import (
    ListingCompositeEvidenceOrigins,
    evaluate_composite_listing_temporal_proof,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
)
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionStatus,
    PhysicalAttemptNotMade,
    ProvenanceClass,
    ProviderRequestCoordinator,
    RequestPriority,
    StoredSessionCalendar,
)
from tradingagents.market_history.coordinator import record_active_physical_attempt_io


class _Clock:
    def __init__(self):
        self.value = _NOW

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _timing_request(legacy):
    if not legacy:
        request = _request()
    else:
        identity = _identity("600895.SS")
        request = CninfoListingPublicationRequest(
            instrument_identity=identity,
            effective_evidence=_effective(identity, date(1996, 4, 22)),
            authoritative_board=CninfoListingBoard.XSHG_MAIN,
            requested_session=date(2026, 9, 3),
            window_start=date(1996, 4, 18),
            window_end=date(1996, 4, 22),
        )
    # At noon UTC on September 4, that session has already closed. Keep the
    # original fixture/query bounds but use a valid terminal calendar frontier.
    return request.model_copy(update={"requested_session": _NOW.date()})


def _adapter(cache, transport, clock):
    return CninfoListingPublicationAdapter(
        subrequest_cache=cache,
        transport=transport,
        source_policy=CninfoSourcePolicy(
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            operator_minimum_interval_seconds=2,
        ),
        owner_id="run:cninfo-timing",
        now=clock.now,
        sleep=clock.advance,
        lease_duration=timedelta(seconds=30),
    )


class _DelayedTransport:
    def __init__(self, clock, *, legacy, delays=(3, 3, 3)):
        self.clock = clock
        self.fixture = _LegacyFixtureTransport() if legacy else _FixtureTransport()
        self.delays = iter(delays)
        self.completions = []

    def _complete(self, response):
        self.clock.advance(next(self.delays))
        self.completions.append(self.clock.now())
        return response

    def post_form(self, *args, **kwargs):
        return self._complete(self.fixture.post_form(*args, **kwargs))

    def get_document(self, *args, **kwargs):
        return self._complete(self.fixture.get_document(*args, **kwargs))


def _calendar():
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "c" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:cninfo-timing-fixture",
        observed_at=_NOW - timedelta(days=3),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(date(2026, 9, 3), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 9, 4), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 9, 7), MarketSessionStatus.OPEN),
        ),
    )


def _assert_temporal_acceptance(result, request, evaluated_at):
    calendar = _calendar()
    transaction = build_financial_eligibility_temporal_transaction_binding(
        canonical_run_id="run:" + "1" * 64,
        routing_plan_signature="mainland-routing-plan:v1:" + "2" * 64,
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        requested_session=request.requested_session,
        eligibility_started_at=_NOW,
        calendar_binding=build_market_session_eligibility_binding(
            calendar,
            requested_session=request.requested_session,
            eligibility_evaluated_at=_NOW,
        ),
    )
    window = build_financial_eligibility_acquisition_window(
        calendar,
        transaction_binding=transaction,
        requested_session=request.requested_session,
        eligibility_started_at=_NOW,
        eligibility_evaluated_at=evaluated_at,
    )
    decision = evaluate_composite_listing_temporal_proof(
        result.composite_proof,
        acquisition_window=window,
        evidence_origins=ListingCompositeEvidenceOrigins(
            effective="fresh",
            publication="fresh",
            document="fresh" if result.document_evidence is not None else None,
        ),
    )
    assert decision.result == "accepted", decision.reasons


@pytest.mark.parametrize("legacy", (False, True), ids=("metadata", "legacy-html"))
def test_preserved_blocker_retrieval_follows_complete_response(tmp_path, legacy):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=legacy)
    request = _timing_request(legacy)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, transport, clock).acquire(request)
        checkpoint = cache.checkpoint()
        sequences = store._connection.execute(
            "SELECT sequence_id, started_at, completed_at, status FROM provider_request_sequences"
        ).fetchall()

    assert result.kind.value == "available", result.reason
    publication = result.publication_evidence
    if legacy:
        assert result.document_evidence.retrieved_at >= transport.completions[2]
        assert result.document_evidence.retrieved_at == _NOW + timedelta(seconds=9)
        assert result.document_evidence.observed_at == transport.completions[2]
    assert publication.retrieved_at >= transport.completions[1] == _NOW + timedelta(seconds=6)
    assert publication.observed_at == transport.completions[0]
    entries = {entry["sequence_id"]: entry for entry in checkpoint["entries"]}
    terminals = {row[0]: row for row in sequences}
    assert len(entries) == len(sequences) == len(transport.completions)
    assert len(result.physical_attempt_ids) == len(transport.completions)
    for event, completed in zip(result.attempt_events, transport.completions, strict=True):
        retained = datetime.fromisoformat(entries[event.sequence_id]["artifact"]["retrieved_at"])
        _, started, terminal, status = terminals[event.sequence_id]
        assert datetime.fromisoformat(started) <= event.attempted_at <= completed <= retained
        assert retained <= datetime.fromisoformat(terminal)
        assert status == "succeeded"
        assert event.outcome.value == "available"
        assert event.attempt_index == event.final_physical_attempt_count == 1
        assert event.request_key in result.subrequest_keys
    _assert_temporal_acceptance(result, request, clock.now())


def test_document_request_at_three_finishes_at_nine(tmp_path):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=True, delays=(1, 1, 6))
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, transport, clock).acquire(_timing_request(True))
    assert result.kind.value == "available", result.reason
    assert result.attempt_events[-1].attempted_at == _NOW + timedelta(seconds=3)
    assert result.document_evidence.retrieved_at >= _NOW + timedelta(seconds=9)


class _StreamingResponse:
    def __init__(self, url, body, clock, *, html=False, failure=None):
        self.url = url
        self.status_code = 403 if failure == "denied" else 200
        self.headers = {"Content-Type": "text/html" if html else "application/json"}
        self.body = body
        self.clock = clock
        self.failure = failure
        self.closed = False
        clock.advance(1)
        self.headers_at = clock.now()

    def iter_content(self, *, chunk_size):
        assert chunk_size > 0
        self.clock.advance(1)
        self.first_chunk_at = self.clock.now()
        yield self.body[:20]
        self.clock.advance(1)
        if self.failure == "interrupted":
            raise OSError("offline interrupted stream")
        if self.failure == "oversize":
            yield b"x" * 1_000_000
        else:
            yield self.body[20:]
        # Exhaustion is later than headers, first chunk, and even last yield.
        self.clock.advance(1)
        self.completed_at = self.clock.now()

    def close(self):
        self.closed = True


def _install_streams(monkeypatch, clock, *, legacy, failure=None):
    responses = []
    symbol = "600895" if legacy else "601328"

    def post(url, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        operation = "security-lookup" if url.endswith("/topSearch/query") else "announcement-query"
        body = (_FIXTURE_ROOT / f"{symbol}-{operation}.json").read_bytes()
        response = _StreamingResponse(url, body, clock)
        responses.append(response)
        return response

    def get(url, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        body = base64.b64decode((_FIXTURE_ROOT / "141828-legacy-gb2312.html.b64").read_bytes())
        response = _StreamingResponse(url, body, clock, html=True, failure=failure)
        responses.append(response)
        return response

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    return responses


@pytest.mark.parametrize("legacy", (False, True), ids=("metadata", "html"))
def test_retrieval_waits_for_stream_exhaustion_and_cas(tmp_path, monkeypatch, legacy):
    clock = _Clock()
    responses = _install_streams(monkeypatch, clock, legacy=legacy)
    persisted = []
    install = ProviderSubrequestArtifactStore.install

    def delayed_install(self, payload, **kwargs):
        artifact = install(self, payload, **kwargs)
        clock.advance(1)
        persisted.append(clock.now())
        return artifact

    monkeypatch.setattr(ProviderSubrequestArtifactStore, "install", delayed_install)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, RequestsCninfoHttpTransport(), clock).acquire(
            _timing_request(legacy)
        )
    assert result.kind.value == "available", result.reason
    evidence = result.document_evidence if legacy else result.publication_evidence
    assert evidence.retrieved_at >= persisted[-1] > responses[-1].completed_at
    assert evidence.retrieved_at > responses[-1].first_chunk_at > responses[-1].headers_at
    assert all(response.closed for response in responses)
    assert len(result.physical_attempt_ids) == len(responses)


@pytest.mark.parametrize("legacy", (False, True), ids=("metadata", "html"))
def test_replay_retains_original_acquisition_timestamps_without_io(tmp_path, legacy):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=legacy)
    request = _timing_request(legacy)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        adapter = _adapter(cache, transport, clock)
        first = adapter.acquire(request)
        checkpoint = cache.checkpoint()
        frozen = json.dumps(checkpoint, sort_keys=True)
        original_files = {
            path: path.read_bytes() for path in (tmp_path / "artifacts").rglob("*.bin")
        }
        clock.advance(86_400)
        cached = adapter.acquire(request)
        replay_cache = ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="run:cninfo-timing",
            checkpoint=json.loads(frozen),
        )
        replayed = _adapter(replay_cache, transport, clock).acquire(request)
    assert first.kind.value == "available", first.reason
    assert cached == first == replayed
    assert first.publication_evidence.retrieved_at >= transport.completions[1]
    assert json.dumps(replay_cache.checkpoint(), sort_keys=True) == frozen
    assert {path: path.read_bytes() for path in original_files} == original_files
    assert len(transport.fixture.calls) == (3 if legacy else 2)


@pytest.mark.parametrize("failure", ("interrupted", "oversize", "denied"))
def test_failed_stream_has_no_successful_document_timestamp(tmp_path, monkeypatch, failure):
    clock = _Clock()
    responses = _install_streams(monkeypatch, clock, legacy=True, failure=failure)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, RequestsCninfoHttpTransport(), clock).acquire(
            _timing_request(True)
        )
        checkpoint = cache.checkpoint()
        sequence = store._connection.execute(
            "SELECT completed_at, status FROM provider_request_sequences WHERE sequence_id = ?",
            (result.attempt_events[-1].sequence_id,),
        ).fetchone()
    assert result.kind.value == "unavailable"
    assert result.document_evidence is result.publication_evidence is result.composite_proof is None
    assert len(result.physical_attempt_ids) == 3
    failed = next(
        entry for entry in checkpoint["entries"] if entry["outcome"]["kind"] != "available"
    )
    assert failed["artifact"] is None
    assert (
        result.attempt_events[-1].attempted_at < datetime.fromisoformat(sequence[0]) == clock.now()
    )
    assert sequence[1] == "failed"
    assert responses[-1].closed


def test_document_retrieval_is_retained_before_later_html_parsing(tmp_path, monkeypatch):
    from tradingagents.dataflows import cninfo_listing_publication as cninfo

    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=True)
    verify = cninfo.verify_cninfo_listing_document

    def slow_verify(*args, **kwargs):
        clock.advance(5)
        return verify(*args, **kwargs)

    monkeypatch.setattr(cninfo, "verify_cninfo_listing_document", slow_verify)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, transport, clock).acquire(_timing_request(True))
    assert result.kind.value == "available", result.reason
    assert result.document_evidence.retrieved_at == transport.completions[-1]
    assert result.document_evidence.retrieved_at < clock.now()


@pytest.mark.parametrize("legacy", (False, True), ids=("metadata", "html"))
def test_unstamped_historical_artifacts_fail_closed_without_restamping(tmp_path, legacy):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=legacy)
    request = _timing_request(legacy)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        first = _adapter(cache, transport, clock).acquire(request)
        assert first.kind.value == "available", first.reason
        historical = cache.checkpoint()
        for entry in historical["entries"]:
            if not legacy or entry["key"]["capacity_scope"] == "cninfo.documents":
                entry["artifact"].pop("retrieved_at", None)
        frozen = json.dumps(historical, sort_keys=True)
        clock.advance(86_400)
        replay_cache = ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="run:cninfo-timing",
            checkpoint=json.loads(frozen),
        )
        replayed = _adapter(replay_cache, transport, clock).acquire(request)
    assert replayed.kind.value == "unavailable"
    assert replayed.reason is CninfoFailureReason.TRANSPORT_UNAVAILABLE
    assert (
        replayed.publication_evidence
        is replayed.document_evidence
        is replayed.composite_proof
        is None
    )
    assert json.dumps(replay_cache.checkpoint(), sort_keys=True) == frozen
    assert len(transport.fixture.calls) == (3 if legacy else 2)


def test_metadata_timeout_keeps_attempt_start_and_terminal_distinct(tmp_path):
    clock = _Clock()

    class TimeoutTransport:
        def post_form(self, *_args, **_kwargs):
            record_active_physical_attempt_io()
            clock.advance(6)
            raise TimeoutError

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, TimeoutTransport(), clock).acquire(_timing_request(False))
        checkpoint = cache.checkpoint()
        completed_at, status = store._connection.execute(
            "SELECT completed_at, status FROM provider_request_sequences"
        ).fetchone()
    assert result.kind.value == "unavailable"
    assert result.reason is CninfoFailureReason.TIMEOUT
    assert result.publication_evidence is result.composite_proof is None
    assert checkpoint["entries"][0]["artifact"] is None
    assert len(result.physical_attempt_ids) == 1
    assert result.attempt_events[0].attempted_at == _NOW
    assert result.attempt_events[0].outcome.value == "timeout"
    assert result.attempt_events[0].retryable is False
    assert datetime.fromisoformat(completed_at) == _NOW + timedelta(seconds=6)
    assert status == "failed"


def test_failed_cas_install_has_no_success_timestamp(tmp_path, monkeypatch):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=False)

    def failed_install(self, payload, **kwargs):
        clock.advance(1)
        raise OSError("offline CAS failure")

    monkeypatch.setattr(ProviderSubrequestArtifactStore, "install", failed_install)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        result = _adapter(cache, transport, clock).acquire(_timing_request(False))
        checkpoint = cache.checkpoint()
        terminal, status = store._connection.execute(
            "SELECT completed_at, status FROM provider_request_sequences"
        ).fetchone()
    assert result.kind.value == "unavailable"
    assert result.publication_evidence is result.composite_proof is None
    assert checkpoint["entries"][0]["artifact"] is None
    assert result.attempt_events[0].attempted_at == _NOW
    assert datetime.fromisoformat(terminal) == _NOW + timedelta(seconds=4)
    assert status == "failed"


@pytest.mark.parametrize("legacy", (False, True), ids=("metadata", "html"))
def test_completed_cninfo_evidence_reaches_terminal_context(tmp_path, monkeypatch, legacy):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=legacy)
    source_request = _timing_request(legacy)
    asset = _asset(source_request.instrument_identity.symbol)
    plan = prepare_mainland_listing_publication_candidate_plan(
        prepare_mainland_temporal_capability_routing_plan(_qualified_plan(monkeypatch, asset))
    )
    company_proof = _company_proof(retrieved_at=_NOW)
    company_type = FinancialCompanyType.INDUSTRIAL_NON_BANK if legacy else FinancialCompanyType.BANK
    company_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        contract_version="financial-company-type-evidence-binding-v4",
        instrument_identity=asset.instrument_identity,
        requested_session=source_request.requested_session,
        temporal_evidence=company_proof,
        provider_id="tushare",
        artifact_identity=company_proof.artifact.artifact_identity,
        source_ref=company_proof.artifact.artifact_identity,
        observed_at=_NOW,
        retrieved_at=_NOW,
        provider_declared_type=company_type,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(
                key="metadata:industry", value="industrial" if legacy else "bank"
            ),
            FinancialCompanyTypeClassifierInput(
                key="present_field", value="inventory" if legacy else "customer_deposits"
            ),
        ),
    )
    effective = _effective(
        asset.instrument_identity, source_request.effective_evidence.listing_effective_date
    )
    effective_identity = "provider-artifact:v1:" + effective.artifact.artifact_sha256
    effective = effective.model_copy(
        update={
            "artifact": effective.artifact.model_copy(
                update={
                    "artifact_identity": effective_identity,
                    "revision_identity": effective_identity,
                }
            )
        }
    )
    listing = FinancialListingMetadata(
        contract_version="financial-listing-metadata-v4",
        instrument_identity=asset.instrument_identity,
        requested_session=source_request.requested_session,
        listing_date=effective.listing_effective_date,
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=effective.artifact.artifact_identity,
            observed_at=_NOW,
        ),
        listing_effective_evidence=effective,
    )

    def company_source(request):
        return FinancialCompanyTypeEvidenceProviderProjection(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=company_evidence,
        )

    def lifecycle_source(_request):
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(effective.artifact.artifact_identity,),
            candidate_count=1,
            subrequest_keys=(),
            physical_attempt_ids=(),
            listing_metadata=listing,
        )

    run_id = "run:" + "1" * 64
    transaction = FinancialEligibilityTemporalEvaluationTransaction(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        calendar=_calendar(),
        requested_session=source_request.requested_session,
        eligibility_started_at=_NOW,
        clock=clock.now,
        canonical_run_id=run_id,
        routing_plan_signature=plan.plan_signature,
    )
    request = FinancialEligibilityMetadataAcquisitionRequest(
        contract_version="financial-eligibility-metadata-acquisition-request-v4",
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        requested_session=source_request.requested_session,
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        eligibility_started_at=_NOW,
        calendar_binding=transaction.calendar_binding,
        authoritative_listing_board=CninfoListingBoard.XSHG_MAIN,
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={MainlandCapability.ISSUER_LIFECYCLE: {"baostock": lifecycle_source}},
        company_type_evidence_sources={"tushare": company_source},
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id=run_id)
        result = FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=router,
            stop_signal=Event(),
            listing_publication_source=_adapter(cache, transport, clock).acquire,
        ).acquire(request, temporal_transaction=transaction)
    assert result.outcome == "success", result.failure
    assert result.context.contract_version == "financial-eligibility-acquisition-context-v4"
    proof = result.context.listing_composite_temporal_proof
    assert proof.publication_evidence.retrieved_at == transport.completions[1]
    if legacy:
        assert proof.document_evidence.retrieved_at == transport.completions[2]
    binding = result.context.temporal_eligibility_binding
    assert binding.listing_temporal_decision.result == "accepted"
    assert binding.acquisition_window.eligibility_evaluated_at == clock.now()
    assert transaction.evaluation_count == 1


@pytest.mark.parametrize("no_io", (False, True), ids=("default-other-provider", "no-io"))
def test_optional_retrieval_capture_preserves_unstamped_artifact_contract(tmp_path, no_io):
    clock = _Clock()
    key = _other_provider_key()

    def physical_request():
        if no_io:
            return PhysicalAttemptNotMade(b'{"retained":true}')
        record_active_physical_attempt_io()
        clock.advance(3)
        return b'{"fresh":true}'

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            key.upstream_service_id,
            "Tushare fixture",
            account_scope=key.account_scope,
        )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="run:cninfo-timing",
        )
        result = cache.execute(
            key,
            owner_id="run:cninfo-timing",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=clock.now,
            sleep=clock.advance,
            lease_duration=timedelta(seconds=30),
            operation="fixture_request",
            media_type="application/json",
            record_at_physical_io=True,
            capture_retrieved_at=no_io,
            physical_request=physical_request,
        )
    assert result.outcome.kind.value == "available"
    assert result.artifact.retrieved_at is None
    assert len(result.attempt_events) == (0 if no_io else 1)
    assert set(result.artifact.model_dump()) == {
        "artifact_ref",
        "artifact_sha256",
        "byte_length",
        "media_type",
    }
    assert "retrieved_at" not in cache.checkpoint()["entries"][0]["artifact"]


@pytest.mark.parametrize("receipt_state", ("missing", "before_attempt", "naive_attempt"))
def test_invalid_lookup_receipt_replay_stops_before_query_io(tmp_path, receipt_state):
    clock = _Clock()
    transport = _DelayedTransport(clock, legacy=False)
    request = _timing_request(False)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(tmp_path, store, run_scope_id="run:cninfo-timing")
        first = _adapter(cache, transport, clock).acquire(request)
        assert first.kind.value == "available", first.reason
        historical = cache.checkpoint()
        historical["entries"] = [
            entry
            for entry in historical["entries"]
            if entry["key"]["subrequest_key"]
            == first.publication_evidence.security_lookup_request_identity
        ]
        entry = historical["entries"][0]
        if receipt_state == "missing":
            entry["artifact"].pop("retrieved_at")
        elif receipt_state == "before_attempt":
            entry["artifact"]["retrieved_at"] = (
                (_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            )
        else:
            entry["attempt_events"][0]["attempted_at"] = _NOW.replace(tzinfo=None).isoformat()
        frozen = json.dumps(historical, sort_keys=True)
        clock.advance(86_400)
        replay_cache = ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="run:cninfo-timing",
            checkpoint=json.loads(frozen),
        )
        result = _adapter(replay_cache, transport, clock).acquire(request)
    assert result.kind.value == "unavailable"
    assert result.reason is CninfoFailureReason.TRANSPORT_UNAVAILABLE
    assert len(transport.fixture.calls) == 2
    assert json.dumps(replay_cache.checkpoint(), sort_keys=True) == frozen
