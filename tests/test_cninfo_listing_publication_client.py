import base64
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from tradingagents.dataflows.cninfo_listing_publication import (
    CNINFO_LISTING_QUERY_LOOKBACK_DAYS,
    CninfoFailureReason,
    CninfoHttpResponse,
    CninfoListingBoard,
    CninfoListingPublicationAdapter,
    CninfoListingPublicationRequest,
    CninfoSourcePolicy,
    RequestsCninfoHttpTransport,
)
from tradingagents.dataflows.listing_publication_evidence import (
    ListingEffectiveEvidence,
    ListingEvidenceArtifactBinding,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
)
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
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.coordinator import record_active_physical_attempt_io

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "cninfo"
_NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def _identity(
    symbol: str = "601328.SS",
    venue: str = "XSHG",
) -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue=venue,
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="fixture-registry:v1",
            retrieved_at="2026-09-04T12:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _effective(
    identity: InstrumentIdentityEvidence,
    listing_date: date = date(2007, 5, 15),
) -> ListingEffectiveEvidence:
    raw = b"baostock-six-field-lifecycle-row"
    digest = sha256(raw).hexdigest()
    return ListingEffectiveEvidence(
        instrument_identity=identity,
        provider_id="baostock",
        venue="XSHG",
        listing_effective_date=listing_date,
        lifecycle_status="active",
        source_field="ipoDate",
        artifact=ListingEvidenceArtifactBinding(
            artifact_identity=f"provider-subrequest-artifact=sha256:{digest}",
            artifact_sha256=digest,
            revision_identity=f"provider-subrequest-artifact=sha256:{digest}",
            row_identity="provider-row:v1:" + "b" * 64,
            byte_length=len(raw),
            media_type="application/json",
        ),
        observed_at=_NOW,
        retrieved_at=_NOW,
    )


class _FixtureTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        record_active_physical_attempt_io()
        self.calls.append(("POST", url, dict(form)))
        fixture = (
            "601328-security-lookup.json"
            if url.endswith("/new/information/topSearch/query")
            else "601328-announcement-query.json"
        )
        return CninfoHttpResponse(
            status_code=200,
            content_type="application/json; charset=UTF-8",
            final_url=url,
            body=(_FIXTURE_ROOT / fixture).read_bytes(),
        )

    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        raise AssertionError(f"unexpected document request: {url}")


class _FirstResponseTransport(_FixtureTransport):
    def __init__(self, response: CninfoHttpResponse) -> None:
        super().__init__()
        self._response = response

    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        record_active_physical_attempt_io()
        self.calls.append(("POST", url, dict(form)))
        return self._response


class _TimeoutTransport(_FixtureTransport):
    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        record_active_physical_attempt_io()
        self.calls.append(("POST", url, dict(form)))
        raise TimeoutError


class _LegacyFixtureTransport(_FixtureTransport):
    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        record_active_physical_attempt_io()
        self.calls.append(("POST", url, dict(form)))
        fixture = (
            "600895-security-lookup.json"
            if url.endswith("/new/information/topSearch/query")
            else "600895-announcement-query.json"
        )
        return CninfoHttpResponse(
            status_code=200,
            content_type="application/json; charset=UTF-8",
            final_url=url,
            body=(_FIXTURE_ROOT / fixture).read_bytes(),
        )

    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        record_active_physical_attempt_io()
        self.calls.append(("GET", url, {}))
        raw_html = base64.b64decode(
            (_FIXTURE_ROOT / "141828-legacy-gb2312.html.b64").read_text(
                encoding="ascii"
            )
        )
        return CninfoHttpResponse(
            status_code=200,
            content_type="text/html; charset=gb2312",
            final_url=url,
            body=raw_html,
        )


class _MismatchedLegacyTransport(_LegacyFixtureTransport):
    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        response = super().get_document(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        changed = response.body.decode("gb2312").replace(
            "1996年4月22日", "1996年4月23日"
        ).encode("gb2312")
        return response.model_copy(update={"body": changed})


class _PdfOnlyLegacyTransport(_LegacyFixtureTransport):
    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        response = super().post_form(
            url,
            form=form,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        if url.endswith("/new/hisAnnouncement/query"):
            payload = __import__("json").loads(response.body)
            payload["announcements"][0]["adjunctUrl"] = (
                "finalpage/1996-04-18/141828.PDF"
            )
            payload["announcements"][0]["adjunctType"] = "PDF"
            response = response.model_copy(
                update={
                    "body": __import__("json").dumps(
                        payload,
                        ensure_ascii=False,
                    ).encode()
                }
            )
        return response

    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        raise AssertionError(f"PDF document must not be fetched: {url}")


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _cache(tmp_path, store, *, run_scope_id: str) -> ProviderSubrequestCache:
    coordinator = ProviderRequestCoordinator(store)
    for provider in ("cninfo_metadata", "cninfo_documents"):
        upstream_id, service_name = upstream_service_identity_for_provider(provider)
        coordinator.register_upstream_service(upstream_id, service_name)
        coordinator.configure_operator_ceiling(
            upstream_service_id=upstream_id,
            minimum_interval=timedelta(seconds=2),
            allow_prewarming=False,
            configured_at=_NOW,
        )
    return ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=run_scope_id,
    )


def _request() -> CninfoListingPublicationRequest:
    identity = _identity()
    return CninfoListingPublicationRequest(
        instrument_identity=identity,
        effective_evidence=_effective(identity),
        authoritative_board=CninfoListingBoard.XSHG_MAIN,
        requested_session=date(2026, 9, 3),
        window_start=date(2007, 5, 10),
        window_end=date(2007, 5, 15),
    )


def test_cninfo_request_window_uses_the_contract_bound() -> None:
    identity = _identity()

    assert CNINFO_LISTING_QUERY_LOOKBACK_DAYS == 5
    with pytest.raises(ValueError, match="outside policy bounds"):
        CninfoListingPublicationRequest(
            instrument_identity=identity,
            effective_evidence=_effective(identity),
            authoritative_board=CninfoListingBoard.XSHG_MAIN,
            requested_session=date(2026, 9, 3),
            window_start=date(2007, 5, 9),
            window_end=date(2007, 5, 15),
        )

    invalid_identity = _identity("ABCDEF.SS")
    with pytest.raises(ValueError, match="outside policy bounds"):
        CninfoListingPublicationRequest(
            instrument_identity=invalid_identity,
            effective_evidence=_effective(invalid_identity),
            authoritative_board=CninfoListingBoard.XSHG_MAIN,
            requested_session=date(2026, 9, 3),
            window_start=date(2007, 5, 10),
            window_end=date(2007, 5, 15),
        )


def test_cninfo_client_uses_two_bounded_ledgered_requests_for_metadata_success(
    tmp_path,
) -> None:
    identity = _identity()
    transport = _FixtureTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(
            tmp_path,
            store,
            run_scope_id="run:cninfo-client-fixture",
        )
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=cache,
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-client-fixture",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(
            CninfoListingPublicationRequest(
                instrument_identity=identity,
                effective_evidence=_effective(identity),
                authoritative_board=CninfoListingBoard.XSHG_MAIN,
                requested_session=date(2026, 9, 3),
                window_start=date(2007, 5, 10),
                window_end=date(2007, 5, 15),
            )
        )
        checkpoint = cache.checkpoint()

    assert result.kind.value == "available"
    assert result.publication_evidence is not None
    assert result.publication_evidence.publication_date == date(2007, 5, 14)
    assert result.document_evidence is None
    assert result.composite_proof is not None
    assert len(result.subrequest_keys) == 2
    assert len(result.physical_attempt_ids) == 2
    assert [event.capacity_scope for event in result.attempt_events] == [
        "cninfo.metadata",
        "cninfo.metadata",
    ]
    assert [event.upstream_service_id for event in result.attempt_events] == [
        "upstream-service=sha256:"
        "c40b4b6eeac076e4b1c8d1b821184683db9679d339013ff10c0e0855f095fbac",
        "upstream-service=sha256:"
        "c40b4b6eeac076e4b1c8d1b821184683db9679d339013ff10c0e0855f095fbac",
    ]
    assert [event.attempted_at for event in result.attempt_events] == [_NOW, _NOW]
    assert [event.final_physical_attempt_count for event in result.attempt_events] == [
        1,
        1,
    ]
    assert [call[0:2] for call in transport.calls] == [
        ("POST", "https://www.cninfo.com.cn/new/information/topSearch/query"),
        ("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query"),
    ]
    assert transport.calls[0][2] == {
        "keyWord": "601328",
        "maxSecNum": "10",
        "maxListNum": "5",
    }
    assert transport.calls[1][2]["stock"].startswith("601328,")
    assert transport.calls[1][2]["seDate"] == "2007-05-10~2007-05-15"
    assert transport.calls[1][2]["pageNum"] == "1"
    assert transport.calls[1][2]["pageSize"] == "30"
    keys = [entry["key"] for entry in checkpoint["entries"]]
    assert {
        next(
            field
            for field in key["requested_fields"]
            if field.startswith("cninfo_")
        )
        for key in keys
    } == {
        "cninfo_security_lookup",
        "cninfo_listing_announcement_query",
    }
    assert all(key["data_usage_mode"] == "personal_research" for key in keys)
    assert all(key["account_scope"] == "cninfo-public-research" for key in keys)
    lookup_key = next(
        key
        for key in keys
        if "cninfo_security_lookup" in key["requested_fields"]
    )
    assert {
        "lookup.max_list_num.5",
        "lookup.max_sec_num.10",
    }.issubset(lookup_key["requested_fields"])
    query_key = next(
        key
        for key in keys
        if "cninfo_listing_announcement_query" in key["requested_fields"]
    )
    assert {
        "board.xshg_main",
        "org_id.9900002841",
        "page_column.SHZB",
        "query.column.sse",
        "query.page_num.1",
        "query.page_size.30",
        "query.plate.none",
        "query.tab.fulltext",
    }.issubset(query_key["requested_fields"])
    assert result.publication_evidence.security_lookup_artifact.artifact_sha256 == (
        sha256((_FIXTURE_ROOT / "601328-security-lookup.json").read_bytes()).hexdigest()
    )
    assert result.publication_evidence.metadata_artifact.artifact_sha256 == (
        sha256((_FIXTURE_ROOT / "601328-announcement-query.json").read_bytes()).hexdigest()
    )


def test_cninfo_source_policy_blocks_non_personal_mode_before_io(tmp_path) -> None:
    transport = _FixtureTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id="run:cninfo-production-block",
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PRODUCTION,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-production-block",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(_request())

    assert result.kind.value == "unavailable"
    assert result.reason is CninfoFailureReason.DATA_USAGE_MODE_NOT_PERMITTED
    assert result.subrequest_keys == ()
    assert result.physical_attempt_ids == ()
    assert transport.calls == []


def test_cninfo_source_policy_binds_the_persisted_run_data_usage_mode(tmp_path) -> None:
    transport = _FixtureTransport()
    config = replace(
        _history_config(tmp_path),
        data_usage_mode=DataUsageMode.PRODUCTION,
    )
    with MarketHistoryStore.open(config) as store:
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id="run:cninfo-production-store-block",
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-production-store-block",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(_request())

    assert result.reason is CninfoFailureReason.DATA_USAGE_MODE_NOT_PERMITTED
    assert result.physical_attempt_ids == ()
    assert transport.calls == []


def test_cninfo_source_policy_requires_operator_safety_ceilings_before_io(
    tmp_path,
) -> None:
    transport = _FixtureTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = ProviderSubrequestCache(
            coordinator=ProviderRequestCoordinator(store),
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="run:cninfo-missing-ceiling",
        )
        result = CninfoListingPublicationAdapter(
            subrequest_cache=cache,
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-missing-ceiling",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(_request())

    assert result.reason is CninfoFailureReason.SOURCE_POLICY_UNAVAILABLE
    assert result.physical_attempt_ids == ()
    assert transport.calls == []


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (
            CninfoHttpResponse(
                status_code=401,
                content_type="text/html",
                final_url="https://www.cninfo.com.cn/new/information/topSearch/query",
                body=b"authentication required",
            ),
            CninfoFailureReason.AUTHENTICATION_REQUIRED,
        ),
        (
            CninfoHttpResponse(
                status_code=403,
                content_type="text/html",
                final_url="https://www.cninfo.com.cn/new/information/topSearch/query",
                body=b"forbidden",
            ),
            CninfoFailureReason.ACCESS_DENIED,
        ),
        (
            CninfoHttpResponse(
                status_code=429,
                content_type="text/html",
                final_url="https://www.cninfo.com.cn/new/information/topSearch/query",
                body=b"too many requests",
            ),
            CninfoFailureReason.RATE_LIMITED,
        ),
        (
            CninfoHttpResponse(
                status_code=200,
                content_type="text/html",
                final_url="https://www.cninfo.com.cn/new/information/topSearch/query",
                body="请完成验证码".encode(),
            ),
            CninfoFailureReason.ACCESS_DENIED,
        ),
    ),
)
def test_cninfo_access_controls_stop_without_retry_escalation(
    tmp_path,
    response: CninfoHttpResponse,
    reason: CninfoFailureReason,
) -> None:
    transport = _FirstResponseTransport(response)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id=f"run:cninfo-{reason.value}",
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id=f"run:cninfo-{reason.value}",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(_request())

    assert result.kind.value == "unavailable"
    assert result.reason is reason
    assert len(result.physical_attempt_ids) == 1
    assert len(transport.calls) == 1


def test_cninfo_timeout_stops_after_one_physical_attempt(tmp_path) -> None:
    transport = _TimeoutTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id="run:cninfo-timeout",
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-timeout",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(_request())

    assert result.reason is CninfoFailureReason.TIMEOUT
    assert len(result.physical_attempt_ids) == 1
    assert len(transport.calls) == 1


def test_cninfo_schema_drift_is_typed_and_retained_without_fallback(tmp_path) -> None:
    payload = (_FIXTURE_ROOT / "601328-security-lookup.json").read_text(
        encoding="utf-8"
    )
    drifted = payload.replace('"orgId":', '"unexpected":"drift","orgId":')
    transport = _FirstResponseTransport(
        CninfoHttpResponse(
            status_code=200,
            content_type="application/json; charset=UTF-8",
            final_url="https://www.cninfo.com.cn/new/information/topSearch/query",
            body=drifted.encode(),
        )
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id="run:cninfo-schema-drift",
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-schema-drift",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(_request())

    assert result.reason is CninfoFailureReason.SCHEMA_INCOMPATIBLE
    assert len(result.retained_artifact_identities) == 1
    assert len(result.physical_attempt_ids) == 1
    assert len(transport.calls) == 1


def test_cninfo_cache_hit_and_checkpoint_replay_use_zero_external_io(tmp_path) -> None:
    transport = _FixtureTransport()
    run_scope_id = "run:cninfo-zero-io-replay"
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider in ("cninfo_metadata", "cninfo_documents"):
            upstream_id, service_name = upstream_service_identity_for_provider(provider)
            coordinator.register_upstream_service(upstream_id, service_name)
            coordinator.configure_operator_ceiling(
                upstream_service_id=upstream_id,
                minimum_interval=timedelta(seconds=2),
                allow_prewarming=False,
                configured_at=_NOW,
            )
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id=run_scope_id,
        )
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=cache,
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id=run_scope_id,
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

        first = adapter.acquire(_request())
        second = adapter.acquire(_request())
        checkpoint = cache.checkpoint()

        replay_transport = _FirstResponseTransport(
            CninfoHttpResponse(
                status_code=500,
                content_type="text/plain",
                final_url="https://should-not-be-called.invalid/",
                body=b"must not be called",
            )
        )
        replay_cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id=run_scope_id,
            checkpoint=checkpoint,
        )
        replay = CninfoListingPublicationAdapter(
            subrequest_cache=replay_cache,
            transport=replay_transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id=run_scope_id,
            now=lambda: _NOW + timedelta(days=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(_request())

    assert first.composite_proof is not None
    assert second.composite_proof is not None
    assert replay.composite_proof is not None
    assert first.composite_proof.proof_identity == second.composite_proof.proof_identity
    assert first.composite_proof.proof_identity == replay.composite_proof.proof_identity
    assert len(transport.calls) == 2
    assert replay_transport.calls == []


def test_cninfo_legacy_html_path_ledgers_one_bounded_document_fetch(tmp_path) -> None:
    identity = _identity("600895.SS")
    request = CninfoListingPublicationRequest(
        instrument_identity=identity,
        effective_evidence=_effective(identity, date(1996, 4, 22)),
        authoritative_board=CninfoListingBoard.XSHG_MAIN,
        requested_session=date(2026, 9, 3),
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )
    transport = _LegacyFixtureTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        cache = _cache(
            tmp_path,
            store,
            run_scope_id="run:cninfo-legacy-html",
        )
        adapter = CninfoListingPublicationAdapter(
            subrequest_cache=cache,
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-legacy-html",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        result = adapter.acquire(request)
        checkpoint = cache.checkpoint()

    assert result.kind.value == "available"
    assert result.document_evidence is not None
    assert result.document_evidence.listing_effective_date == date(1996, 4, 22)
    assert result.document_evidence.conversion_provenance == "unknown"
    assert result.document_evidence.document_request_identity in result.subrequest_keys
    assert result.composite_proof is not None
    assert len(result.retained_artifact_identities) == 3
    assert len(result.subrequest_keys) == 3
    assert len(result.physical_attempt_ids) == 3
    assert [event.capacity_scope for event in result.attempt_events] == [
        "cninfo.metadata",
        "cninfo.metadata",
        "cninfo.documents",
    ]
    assert [call[0] for call in transport.calls] == ["POST", "POST", "GET"]
    assert transport.calls[-1][1] == (
        "https://static.cninfo.com.cn/finalpage/1996-04-18/141828.html"
    )
    document_key = next(
        entry["key"]
        for entry in checkpoint["entries"]
        if "cninfo_listing_document_fetch" in entry["key"]["requested_fields"]
    )
    assert {
        "announcement_id.141828",
        "board.xshg_main",
        "document.media_type.html",
        "org_id.gssh0600895",
        "page_column.SHZB",
        "url_sha256."
        "85a3ce7339d2814d2c1309f58e3eb364508700d65cb4e6ed19bc64f2a98ab2f0",
    }.issubset(document_key["requested_fields"])

    assert result.document_evidence is not None
    payload = result.model_dump(mode="python")
    payload["retained_artifact_identities"] = tuple(
        artifact
        for artifact in result.retained_artifact_identities
        if artifact != result.document_evidence.artifact.artifact_identity
    )
    with pytest.raises(ValueError, match="accounting"):
        type(result).model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["subrequest_keys"] = result.subrequest_keys[:2]
    with pytest.raises(ValueError, match="accounting"):
        type(result).model_validate(payload)


@pytest.mark.parametrize(
    ("transport", "reason", "operation_count"),
    (
        (
            _MismatchedLegacyTransport(),
            CninfoFailureReason.TEMPORAL_PROOF_CONFLICT,
            3,
        ),
        (
            _PdfOnlyLegacyTransport(),
            CninfoFailureReason.DOCUMENT_AUTOMATION_UNAVAILABLE,
            2,
        ),
    ),
)
def test_cninfo_required_document_failures_never_expose_composite_proof(
    tmp_path,
    transport: _LegacyFixtureTransport,
    reason: CninfoFailureReason,
    operation_count: int,
) -> None:
    identity = _identity("600895.SS")
    request = CninfoListingPublicationRequest(
        instrument_identity=identity,
        effective_evidence=_effective(identity, date(1996, 4, 22)),
        authoritative_board=CninfoListingBoard.XSHG_MAIN,
        requested_session=date(2026, 9, 3),
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )
    run_scope_id = f"run:cninfo-document-failure-{reason.value}"
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id=run_scope_id,
            ),
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id=run_scope_id,
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(request)

    assert result.kind.value == "unavailable"
    assert result.reason is reason
    assert result.composite_proof is None
    assert len(result.subrequest_keys) == operation_count
    assert len(result.physical_attempt_ids) == operation_count


def test_requests_transport_cannot_bypass_coordinator_attempt_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_post(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP must not start without a coordinator permit")

    monkeypatch.setattr("requests.post", unexpected_post)

    with pytest.raises(RuntimeError, match="no active coordinator permit"):
        RequestsCninfoHttpTransport().post_form(
            "https://www.cninfo.com.cn/new/information/topSearch/query",
            form={"keyWord": "601328"},
            headers={"User-Agent": "fixture"},
            timeout_seconds=10,
        )

    assert called is False


def test_requests_transport_streams_and_rejects_oversized_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class OversizedResponse:
        status_code = 200
        url = "https://www.cninfo.com.cn/new/information/topSearch/query"
        headers = {"Content-Type": "application/json"}
        content = b"x" * 1_048_577
        closed = False

        def iter_content(self, *, chunk_size: int):
            assert chunk_size == 65_536
            yield b"x" * 1_048_576
            yield b"x"

        def close(self) -> None:
            self.closed = True

    response = OversizedResponse()

    def post(*_args, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(
        "tradingagents.dataflows.cninfo_listing_publication.record_active_physical_attempt_io",
        lambda: None,
    )
    monkeypatch.setattr("requests.post", post)

    with pytest.raises(PhysicalAttemptFailure) as exc_info:
        RequestsCninfoHttpTransport().post_form(
            response.url,
            form={"keyWord": "601328"},
            headers={"User-Agent": "fixture"},
            timeout_seconds=10,
        )

    assert exc_info.value.outcome is PhysicalAttemptOutcome.MALFORMED_RESPONSE
    assert calls == [
        {
            "data": {"keyWord": "601328"},
            "headers": {"User-Agent": "fixture"},
            "timeout": 10,
            "allow_redirects": False,
            "stream": True,
        }
    ]
    assert response.closed is True


def test_requests_transport_runs_only_inside_one_coordinator_attempt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_bodies = {
        "https://www.cninfo.com.cn/new/information/topSearch/query": (
            _FIXTURE_ROOT / "601328-security-lookup.json"
        ).read_bytes(),
        "https://www.cninfo.com.cn/new/hisAnnouncement/query": (
            _FIXTURE_ROOT / "601328-announcement-query.json"
        ).read_bytes(),
    }
    calls: list[str] = []
    responses: list[object] = []

    class StreamingResponse:
        status_code = 200
        headers = {"Content-Type": "application/json; charset=UTF-8"}

        def __init__(self, url: str, body: bytes) -> None:
            self.url = url
            self.body = body
            self.closed = False

        def iter_content(self, *, chunk_size: int):
            assert chunk_size == 65_536
            yield self.body

        def close(self) -> None:
            self.closed = True

    def post(url: str, **kwargs: object):
        assert kwargs["stream"] is True
        calls.append(url)
        response = StreamingResponse(url, response_bodies[url])
        responses.append(response)
        return response

    monkeypatch.setattr("requests.post", post)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = CninfoListingPublicationAdapter(
            subrequest_cache=_cache(
                tmp_path,
                store,
                run_scope_id="run:cninfo-requests-coordinator",
            ),
            transport=RequestsCninfoHttpTransport(),
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id="run:cninfo-requests-coordinator",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(_request())

    assert result.kind.value == "available"
    assert calls == list(response_bodies)
    assert all(response.closed for response in responses)
    assert len(result.attempt_events) == 2


@pytest.mark.parametrize(
    ("blocked_by", "expected_reason"),
    (
        ("cooldown", CninfoFailureReason.RATE_LIMITED),
        ("interrupt", CninfoFailureReason.TRANSPORT_UNAVAILABLE),
    ),
)
def test_cninfo_no_attempt_outcomes_remain_typed_and_do_not_run_transport(
    tmp_path,
    blocked_by: str,
    expected_reason: CninfoFailureReason,
) -> None:
    transport = _FixtureTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider_id in ("cninfo_metadata", "cninfo_documents"):
            upstream_id, service_name = upstream_service_identity_for_provider(provider_id)
            coordinator.register_upstream_service(upstream_id, service_name)
            coordinator.configure_operator_ceiling(
                upstream_service_id=upstream_id,
                minimum_interval=timedelta(seconds=2),
                allow_prewarming=False,
                configured_at=_NOW,
            )
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id=f"run:cninfo-no-attempt-{blocked_by}",
        )
        if blocked_by == "cooldown":
            metadata_upstream_id, _ = upstream_service_identity_for_provider(
                "cninfo_metadata"
            )
            coordinator.record_rate_limit(
                upstream_service_id=metadata_upstream_id,
                cooldown_scope="cninfo.metadata",
                observed_at=_NOW,
                retry_after=timedelta(seconds=60),
                provider_code="CNINFO_HTTP_429",
            )
        else:
            cache.interrupt()
        result = CninfoListingPublicationAdapter(
            subrequest_cache=cache,
            transport=transport,
            source_policy=CninfoSourcePolicy(
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_minimum_interval_seconds=2,
            ),
            owner_id=f"run:cninfo-no-attempt-{blocked_by}",
            now=lambda: _NOW + timedelta(seconds=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ).acquire(_request())

    assert result.kind.value == "unavailable"
    assert result.reason is expected_reason
    assert len(result.subrequest_keys) == 1
    assert result.physical_attempt_ids == ()
    assert result.attempt_events == ()
    assert transport.calls == []
