from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from tradingagents.dataflows.cninfo_listing_publication import (
    CninfoListingClassification,
)
from tradingagents.dataflows.listing_publication_evidence import (
    ListingDocumentEvidence,
    ListingEffectiveEvidence,
    ListingEvidenceArtifactBinding,
    ListingPublicationClassification,
    ListingPublicationEvidence,
    ListingTemporalProofError,
    ListingTemporalProofFailureReason,
    ListingTemporalProofMode,
    build_composite_listing_temporal_proof,
)
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)

_OBSERVED = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def _identity(symbol: str = "601328.SS", venue: str = "XSHG") -> InstrumentIdentityEvidence:
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


def _artifact(
    label: str,
    *,
    row: str,
    media_type: str = "application/json",
) -> ListingEvidenceArtifactBinding:
    digest = sha256(label.encode()).hexdigest()
    return ListingEvidenceArtifactBinding(
        artifact_identity=f"provider-subrequest-artifact=sha256:{digest}",
        artifact_sha256=digest,
        revision_identity=f"provider-subrequest-artifact=sha256:{digest}",
        row_identity=f"provider-row:v1:{sha256(row.encode()).hexdigest()}",
        byte_length=len(label),
        media_type=media_type,
    )


def test_composite_listing_proof_keeps_effective_and_publication_sources_separate() -> None:
    identity = _identity()
    effective = ListingEffectiveEvidence(
        instrument_identity=identity,
        provider_id="baostock",
        venue="XSHG",
        listing_effective_date=date(2007, 5, 15),
        lifecycle_status="active",
        source_field="ipoDate",
        artifact=_artifact("baostock-six-field-row", row="sh.601328"),
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )
    publication = ListingPublicationEvidence(
        instrument_identity=identity,
        provider_id="cninfo",
        sec_code="601328",
        org_id="dynamic-org",
        venue="XSHG",
        authoritative_board="xshg_main",
        page_column="SHZB",
        announcement_id="23418766",
        publication_timestamp=datetime(2007, 5, 14, 10, tzinfo=timezone.utc),
        publication_date=date(2007, 5, 14),
        exact_title="首次公开发行A股股票上市公告书",
        normalized_title="首次公开发行A股股票上市公告书",
        adjunct_url="finalpage/2007-05-14/23418766.PDF",
        classification=CninfoListingClassification.INITIAL_LISTING,
        classifier_version="cninfo-listing-metadata-classifier-v1",
        security_lookup_artifact=_artifact("lookup", row="601328,dynamic-org"),
        metadata_artifact=_artifact("announcement", row="23418766"),
        security_lookup_request_identity="provider-subrequest:v1:" + "b" * 64,
        announcement_query_request_identity="provider-subrequest:v1:" + "c" * 64,
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )

    proof = build_composite_listing_temporal_proof(
        instrument_identity=identity,
        requested_session=date(2026, 9, 3),
        effective_evidence=effective,
        publication_evidence=publication,
    )

    assert proof.contract_version == "financial-listing-temporal-proof-v2"
    assert proof.listing_effective_date == date(2007, 5, 15)
    assert proof.listing_publication_date == date(2007, 5, 14)
    assert proof.effective_artifact_identity != proof.publication_artifact_identity
    assert proof.publication_announcement_identity == "cninfo-announcement:v1:23418766"


def test_composite_listing_proof_accepts_ambiguous_metadata_only_with_matching_html() -> None:
    identity = _identity("600895.SS")
    effective = ListingEffectiveEvidence(
        instrument_identity=identity,
        provider_id="baostock",
        venue="XSHG",
        listing_effective_date=date(1996, 4, 22),
        lifecycle_status="active",
        source_field="ipoDate",
        artifact=_artifact("baostock-600895", row="sh.600895"),
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )
    publication = ListingPublicationEvidence(
        instrument_identity=identity,
        sec_code="600895",
        org_id="dynamic-legacy-org",
        venue="XSHG",
        authoritative_board="xshg_main",
        page_column="SHZB",
        announcement_id="141828",
        publication_timestamp=datetime(1996, 4, 17, 16, tzinfo=timezone.utc),
        publication_date=date(1996, 4, 18),
        exact_title="上海张江高科技园区开发股份有限公司股票上市公告书",
        normalized_title="上海张江高科技园区开发股份有限公司股票上市公告书",
        adjunct_url="finalpage/1996-04-18/141828.html",
        classification=CninfoListingClassification.AMBIGUOUS,
        security_lookup_artifact=_artifact("legacy-lookup", row="600895,dynamic-org"),
        metadata_artifact=_artifact("legacy-announcement", row="141828"),
        security_lookup_request_identity="provider-subrequest:v1:" + "d" * 64,
        announcement_query_request_identity="provider-subrequest:v1:" + "e" * 64,
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )
    document = ListingDocumentEvidence(
        instrument_identity=identity,
        sec_code="600895",
        issuer="上海张江高科技园区开发股份有限公司",
        venue="XSHG",
        announcement_id="141828",
        document_request_identity="provider-subrequest:v1:" + "6" * 64,
        exact_title=publication.exact_title,
        original_url="https://static.cninfo.com.cn/finalpage/1996-04-18/141828.html",
        listing_effective_date=date(1996, 4, 22),
        publication_date=date(1996, 4, 18),
        parser_version="cninfo_legacy_html_v1+lxml-6.1.1",
        artifact=_artifact(
            "legacy-html",
            row="141828-document",
            media_type="text/html",
        ),
        structured_evidence_sha256="f" * 64,
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )

    proof = build_composite_listing_temporal_proof(
        instrument_identity=identity,
        requested_session=date(2026, 9, 3),
        effective_evidence=effective,
        publication_evidence=publication,
        document_evidence=document,
    )

    assert proof.document_evidence == document
    with pytest.raises(ListingTemporalProofError) as exc_info:
        build_composite_listing_temporal_proof(
            instrument_identity=identity,
            requested_session=date(2026, 9, 3),
            effective_evidence=effective,
            publication_evidence=publication,
            document_evidence=document.model_copy(
                update={"listing_effective_date": date(1996, 4, 23)}
            ),
        )

    assert (
        exc_info.value.reason
        is ListingTemporalProofFailureReason.DOCUMENT_BINDING_INVALID
    )

    with pytest.raises(ListingTemporalProofError) as exc_info:
        build_composite_listing_temporal_proof(
            instrument_identity=identity,
            requested_session=date(2026, 9, 3),
            effective_evidence=effective,
            publication_evidence=publication,
            document_evidence=document.model_copy(
                update={
                    "document_request_identity": (
                        publication.announcement_query_request_identity
                    )
                }
            ),
        )

    assert (
        exc_info.value.reason
        is ListingTemporalProofFailureReason.DOCUMENT_BINDING_INVALID
    )

    with pytest.raises(ListingTemporalProofError) as exc_info:
        build_composite_listing_temporal_proof(
            instrument_identity=identity,
            requested_session=date(2026, 9, 3),
            effective_evidence=effective,
            publication_evidence=publication,
            document_evidence=document.model_copy(update={"issuer": "其他发行人"}),
        )

    assert (
        exc_info.value.reason
        is ListingTemporalProofFailureReason.DOCUMENT_BINDING_INVALID
    )

    with pytest.raises(ListingTemporalProofError) as exc_info:
        build_composite_listing_temporal_proof(
            instrument_identity=identity,
            requested_session=date(2026, 9, 3),
            effective_evidence=effective,
            publication_evidence=publication,
            document_evidence=document.model_copy(
                update={
                    "original_url": (
                        "https://static.cninfo.com.cn/"
                        "finalpage/1996-04-19/141828.html"
                    )
                }
            ),
        )

    assert (
        exc_info.value.reason
        is ListingTemporalProofFailureReason.DOCUMENT_BINDING_INVALID
    )


def _valid_composite_inputs():
    identity = _identity()
    effective = ListingEffectiveEvidence(
        instrument_identity=identity,
        provider_id="baostock",
        venue="XSHG",
        listing_effective_date=date(2007, 5, 15),
        lifecycle_status="active",
        source_field="ipoDate",
        artifact=_artifact("matrix-effective", row="sh.601328"),
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )
    publication = ListingPublicationEvidence(
        instrument_identity=identity,
        sec_code="601328",
        org_id="dynamic-org",
        venue="XSHG",
        authoritative_board="xshg_main",
        page_column="SHZB",
        announcement_id="23418766",
        publication_timestamp=datetime(2007, 5, 14, 2, tzinfo=timezone.utc),
        publication_date=date(2007, 5, 14),
        exact_title="首次公开发行A股股票上市公告书",
        normalized_title="首次公开发行A股股票上市公告书",
        adjunct_url="finalpage/2007-05-14/23418766.PDF",
        classification="initial_listing",
        security_lookup_artifact=_artifact("matrix-lookup", row="lookup"),
        metadata_artifact=_artifact("matrix-metadata", row="announcement"),
        security_lookup_request_identity="provider-subrequest:v1:" + "a" * 64,
        announcement_query_request_identity="provider-subrequest:v1:" + "b" * 64,
        observed_at=_OBSERVED,
        retrieved_at=_OBSERVED,
    )
    return identity, effective, publication


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("instrument", ListingTemporalProofFailureReason.INSTRUMENT_MISMATCH),
        ("venue", ListingTemporalProofFailureReason.VENUE_MISMATCH),
        (
            "effective_after_session",
            ListingTemporalProofFailureReason.EFFECTIVE_AFTER_REQUESTED_SESSION,
        ),
        (
            "publication_after_session",
            ListingTemporalProofFailureReason.PUBLICATION_AFTER_REQUESTED_SESSION,
        ),
        (
            "publication_after_effective",
            ListingTemporalProofFailureReason.PUBLICATION_AFTER_EFFECTIVE,
        ),
        ("wrong_event", ListingTemporalProofFailureReason.WRONG_EVENT_KIND),
        ("ambiguous", ListingTemporalProofFailureReason.DOCUMENT_REQUIRED),
        (
            "artifact_alias",
            ListingTemporalProofFailureReason.ARTIFACT_BINDING_MISMATCH,
        ),
        (
            "lookup_artifact_alias",
            ListingTemporalProofFailureReason.ARTIFACT_BINDING_MISMATCH,
        ),
        ("strict_pit", ListingTemporalProofFailureReason.STRICT_PIT_UNAVAILABLE),
    ),
)
def test_composite_listing_proof_rejects_conflicts_before_context_exposure(
    mutation: str,
    reason: ListingTemporalProofFailureReason,
) -> None:
    identity, effective, publication = _valid_composite_inputs()
    mode = ListingTemporalProofMode.CURRENT_ANALYSIS_ONLY
    if mutation == "instrument":
        publication = publication.model_copy(
            update={"instrument_identity": _identity("600000.SS")}
        )
    elif mutation == "venue":
        publication = publication.model_copy(update={"venue": "XSHE"})
    elif mutation == "effective_after_session":
        effective = effective.model_copy(
            update={"listing_effective_date": date(2027, 1, 1)}
        )
    elif mutation == "publication_after_session":
        publication = publication.model_copy(
            update={"publication_date": date(2027, 1, 1)}
        )
    elif mutation == "publication_after_effective":
        publication = publication.model_copy(
            update={"publication_date": date(2007, 5, 16)}
        )
    elif mutation == "wrong_event":
        publication = publication.model_copy(update={"event_kind": "later_share_listing"})
    elif mutation == "ambiguous":
        publication = publication.model_copy(
            update={"classification": ListingPublicationClassification.AMBIGUOUS}
        )
    elif mutation == "artifact_alias":
        publication = publication.model_copy(
            update={"metadata_artifact": effective.artifact}
        )
    elif mutation == "lookup_artifact_alias":
        publication = publication.model_copy(
            update={"security_lookup_artifact": effective.artifact}
        )
    else:
        mode = ListingTemporalProofMode.STRICT_HISTORICAL_PIT

    with pytest.raises(ListingTemporalProofError) as exc_info:
        build_composite_listing_temporal_proof(
            instrument_identity=identity,
            requested_session=date(2026, 9, 3),
            effective_evidence=effective,
            publication_evidence=publication,
            mode=mode,
        )

    assert exc_info.value.reason is reason


def test_listing_publication_evidence_rejects_title_classification_relabeling() -> None:
    _identity_value, _effective_value, publication = _valid_composite_inputs()
    payload = publication.model_dump(mode="python")
    payload["exact_title"] = "公司章程"
    payload["normalized_title"] = "公司章程"

    with pytest.raises(ValueError, match="classification"):
        ListingPublicationEvidence.model_validate(payload)

    payload = publication.model_dump(mode="python")
    payload["metadata_artifact"]["media_type"] = "text/html"
    with pytest.raises(ValueError, match="classification"):
        ListingPublicationEvidence.model_validate(payload)


def test_listing_artifact_rejects_revision_or_content_address_mismatch() -> None:
    artifact = _artifact("revision-binding", row="row")
    payload = artifact.model_dump(mode="python")
    payload["revision_identity"] = "provider-artifact:v1:" + "f" * 64

    with pytest.raises(ValueError, match="revision identity mismatch"):
        ListingEvidenceArtifactBinding.model_validate(payload)

    payload = artifact.model_dump(mode="python")
    payload["artifact_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="content digest"):
        ListingEvidenceArtifactBinding.model_validate(payload)
