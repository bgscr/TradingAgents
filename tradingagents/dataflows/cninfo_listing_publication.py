"""Bounded CNINFO listing-publication acquisition and classification."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tradingagents.dataflows.listing_publication_evidence import (
    CNINFO_METADATA_CLASSIFIER_VERSION,
    CompositeListingTemporalProof,
    ListingDocumentEvidence,
    ListingEffectiveEvidence,
    ListingEvidenceArtifactBinding,
    ListingPublicationClassification,
    ListingPublicationEvidence,
    ListingTemporalProofError,
    build_composite_listing_temporal_proof,
    classify_listing_publication_title,
    normalize_listing_publication_title,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
    ProviderSubrequestOutcomeKind,
    ProviderSubrequestResult,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RequestPriority,
    record_active_physical_attempt_io,
    upstream_service_identity_for_provider,
)

CNINFO_SOURCE_POLICY_VERSION = "cninfo-source-policy-v1"
CNINFO_CANDIDATE_PROFILE = "cninfo-listing-publication-candidate-v1"
CNINFO_SECURITY_LOOKUP_OPERATION = "cninfo_security_lookup"
CNINFO_ANNOUNCEMENT_QUERY_OPERATION = "cninfo_listing_announcement_query"
CNINFO_DOCUMENT_FETCH_OPERATION = "cninfo_listing_document_fetch"
CNINFO_LISTING_QUERY_LOOKBACK_DAYS = 5
CNINFO_SECURITY_LOOKUP_URL = (
    "https://www.cninfo.com.cn/new/information/topSearch/query"
)
CNINFO_ANNOUNCEMENT_QUERY_URL = (
    "https://www.cninfo.com.cn/new/hisAnnouncement/query"
)
_CNINFO_DOCUMENT_ORIGIN = "https://static.cninfo.com.cn/"
_CNINFO_ORG_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_CNINFO_METADATA_RESPONSE_MAX_BYTES = 1_048_576
_CNINFO_LEGACY_HTML_MAX_BYTES = 1_000_000
_CNINFO_RESPONSE_CHUNK_BYTES = 65_536

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    populate_by_name=True,
)


class CninfoFailureReason(str, Enum):
    """Closed payload-free failure vocabulary for bounded CNINFO evidence."""

    LOOKUP_UNAVAILABLE = "cninfo_lookup_unavailable"
    ORG_ID_AMBIGUOUS = "cninfo_org_id_ambiguous"
    ANNOUNCEMENT_UNAVAILABLE = "cninfo_announcement_unavailable"
    ANNOUNCEMENT_AMBIGUOUS = "cninfo_announcement_ambiguous"
    IDENTITY_MISMATCH = "cninfo_identity_mismatch"
    VENUE_MISMATCH = "cninfo_venue_mismatch"
    INCOMPLETE_PAGINATION = "cninfo_incomplete_pagination"
    SCHEMA_INCOMPATIBLE = "cninfo_schema_incompatible"
    DOCUMENT_REQUIRED = "cninfo_document_required"
    DOCUMENT_AUTOMATION_UNAVAILABLE = "document_automation_unavailable"
    DOCUMENT_PARSER_UNAVAILABLE = "cninfo_document_parser_unavailable"
    DOCUMENT_PARSE_FAILED = "cninfo_document_parse_failed"
    DOCUMENT_BINDING_INVALID = "cninfo_document_binding_invalid"
    DATA_USAGE_MODE_NOT_PERMITTED = "cninfo_data_usage_mode_not_permitted"
    SOURCE_POLICY_UNAVAILABLE = "cninfo_source_policy_unavailable"
    ACCESS_DENIED = "cninfo_access_denied"
    RATE_LIMITED = "cninfo_rate_limited"
    AUTHENTICATION_REQUIRED = "cninfo_authentication_required"
    TIMEOUT = "cninfo_timeout"
    TRANSPORT_UNAVAILABLE = "cninfo_transport_unavailable"
    REQUEST_BOUNDS_INVALID = "cninfo_request_bounds_invalid"
    TEMPORAL_PROOF_CONFLICT = "listing_effective_publication_conflict"


class CninfoListingPublicationError(ValueError):
    """Typed fail-closed CNINFO acquisition or validation result."""

    def __init__(self, reason: CninfoFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class _CninfoSecurityLookupRow(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    code: str = Field(pattern=r"^[0-9]{6}$")
    pinyin: str
    sjsts_bond: str = Field(alias="sjstsBond")
    category: str
    type: str
    delisted: str
    org_id: str = Field(alias="orgId", pattern=_CNINFO_ORG_ID_PATTERN)
    short_name: str = Field(alias="zwjc")


class CninfoSecurityMatch(BaseModel):
    """Exact dynamically resolved CNINFO organization identity."""

    model_config = _CLOSED_MODEL_CONFIG

    sec_code: str = Field(pattern=r"^[0-9]{6}$")
    org_id: str = Field(pattern=_CNINFO_ORG_ID_PATTERN)


class CninfoSourcePolicy(BaseModel):
    """Explicit bounded non-commercial source-policy gate."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["cninfo-source-policy-v1"] = CNINFO_SOURCE_POLICY_VERSION
    data_usage_mode: DataUsageMode
    approved_usage: Literal["personal_research_non_commercial"] = (
        "personal_research_non_commercial"
    )
    approval_identity: Literal["cninfo-human-source-policy-approval-v1"] = (
        "cninfo-human-source-policy-approval-v1"
    )
    operator_minimum_interval_seconds: float = Field(ge=1, le=60)
    max_physical_attempts_per_operation: Literal[1] = 1
    max_operations_per_acquisition: Literal[3] = 3
    allow_prewarming: Literal[False] = False
    allow_crawling: Literal[False] = False
    allow_pdf_text_extraction: Literal[False] = False

    @property
    def eligible(self) -> bool:
        return self.data_usage_mode is DataUsageMode.PERSONAL_RESEARCH


class CninfoHttpResponse(BaseModel):
    """One inert response returned by an injected HTTP transport."""

    model_config = _CLOSED_MODEL_CONFIG

    status_code: int = Field(ge=100, le=599)
    content_type: str = Field(min_length=1, max_length=256)
    final_url: str = Field(min_length=1, max_length=512)
    body: bytes = Field(repr=False, max_length=_CNINFO_METADATA_RESPONSE_MAX_BYTES)


class CninfoHttpTransport(Protocol):
    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse: ...

    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse: ...


class RequestsCninfoHttpTransport:
    """Stateless requests adapter; redirects, sessions, and implicit retries are disabled."""

    @staticmethod
    def _response(
        value: object,
        *,
        max_body_bytes: int,
    ) -> CninfoHttpResponse:
        headers = getattr(value, "headers", {})
        content_type = (
            str(headers.get("Content-Type", "application/octet-stream"))
            if isinstance(headers, Mapping)
            else "application/octet-stream"
        )
        status_code = int(value.status_code)
        content_length = headers.get("Content-Length") if isinstance(headers, Mapping) else None
        if (
            status_code == 200
            and isinstance(content_length, str)
            and content_length.isdecimal()
            and int(content_length) > max_body_bytes
        ):
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                retryable=False,
                error_code="cninfo_response_too_large",
            )
        chunks: list[bytes] = []
        byte_length = 0
        error_preview_only = status_code != 200
        for raw_chunk in value.iter_content(chunk_size=_CNINFO_RESPONSE_CHUNK_BYTES):
            chunk = bytes(raw_chunk)
            if not chunk:
                continue
            remaining = max_body_bytes - byte_length
            if len(chunk) > remaining:
                if error_preview_only:
                    chunks.append(chunk[:remaining])
                    break
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                    error_code="cninfo_response_too_large",
                )
            chunks.append(chunk)
            byte_length += len(chunk)
        return CninfoHttpResponse(
            status_code=status_code,
            content_type=content_type,
            final_url=str(value.url),
            body=b"".join(chunks),
        )

    def post_form(
        self,
        url: str,
        *,
        form: dict[str, str],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        import requests

        record_active_physical_attempt_io()
        try:
            response = requests.post(
                url,
                data=form,
                headers=headers,
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            raise TimeoutError from None
        try:
            return self._response(
                response,
                max_body_bytes=_CNINFO_METADATA_RESPONSE_MAX_BYTES,
            )
        finally:
            response.close()

    def get_document(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> CninfoHttpResponse:
        import requests

        record_active_physical_attempt_io()
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            raise TimeoutError from None
        try:
            return self._response(
                response,
                max_body_bytes=_CNINFO_LEGACY_HTML_MAX_BYTES,
            )
        finally:
            response.close()

class CninfoListingPublicationRequest(BaseModel):
    """Exact-instrument request bounded around independent effective evidence."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["cninfo-listing-publication-request-v1"] = (
        "cninfo-listing-publication-request-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    effective_evidence: ListingEffectiveEvidence
    authoritative_board: CninfoListingBoard
    requested_session: date
    window_start: date
    window_end: date

    @model_validator(mode="after")
    def _validate_request(self) -> CninfoListingPublicationRequest:
        expected_suffix = {"XSHG": "SS", "XSHE": "SZ"}.get(
            self.instrument_identity.venue
        )
        if (
            not self.instrument_identity.is_authoritative
            or self.instrument_identity.instrument_kind is not InstrumentKind.EQUITY
            or self.instrument_identity.currency != "CNY"
            or re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", self.instrument_identity.symbol)
            is None
            or self.instrument_identity.symbol.rpartition(".")[2] != expected_suffix
            or self.instrument_identity.venue != self.authoritative_board.venue
            or self.effective_evidence.instrument_identity != self.instrument_identity
            or self.effective_evidence.venue != self.instrument_identity.venue
            or self.window_end != self.effective_evidence.listing_effective_date
            or self.window_start > self.window_end
            or (
                self.window_end - self.window_start
            ).days > CNINFO_LISTING_QUERY_LOOKBACK_DAYS
            or self.requested_session < self.window_end
        ):
            raise ValueError("CNINFO listing-publication request is outside policy bounds")
        return self


class CninfoListingPublicationResultKind(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CninfoListingPublicationResult(BaseModel):
    """Closed terminal acquisition with either a composite proof or safe reason."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["cninfo-listing-publication-result-v1"] = (
        "cninfo-listing-publication-result-v1"
    )
    kind: CninfoListingPublicationResultKind
    reason: CninfoFailureReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    publication_evidence: ListingPublicationEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    document_evidence: ListingDocumentEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    composite_proof: CompositeListingTemporalProof | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    retained_artifact_identities: tuple[str, ...] = ()
    subrequest_keys: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self) -> CninfoListingPublicationResult:
        available = self.kind is CninfoListingPublicationResultKind.AVAILABLE
        if available != (
            self.reason is None
            and self.publication_evidence is not None
            and self.composite_proof is not None
        ):
            raise ValueError("CNINFO acquisition terminal shape is inconsistent")
        if not available and self.reason is None:
            raise ValueError("unavailable CNINFO result requires a typed reason")
        if not available and any(
            value is not None
            for value in (
                self.publication_evidence,
                self.document_evidence,
                self.composite_proof,
            )
        ):
            raise ValueError("unavailable CNINFO result carries admitted evidence")
        if (
            len(self.retained_artifact_identities)
            != len(set(self.retained_artifact_identities))
            or len(self.subrequest_keys) != len(set(self.subrequest_keys))
            or len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids))
        ):
            raise ValueError("CNINFO acquisition accounting identities are not unique")
        event_request_keys = {event.request_key for event in self.attempt_events}
        issued_request_keys = set(self.subrequest_keys)
        if self.physical_attempt_ids != tuple(
            event.attempt_event_id for event in self.attempt_events
        ) or (
            issued_request_keys != event_request_keys
            if available
            else not event_request_keys.issubset(issued_request_keys)
        ):
            raise ValueError("CNINFO physical-attempt accounting is inconsistent")
        if available:
            assert self.publication_evidence is not None
            assert self.composite_proof is not None
            publication = self.publication_evidence
            required_artifacts = {
                publication.security_lookup_artifact.artifact_identity,
                publication.metadata_artifact.artifact_identity,
            }
            required_requests = {
                publication.security_lookup_request_identity,
                publication.announcement_query_request_identity,
            }
            expected_operation_count = 2
            if self.document_evidence is not None:
                required_artifacts.add(
                    self.document_evidence.artifact.artifact_identity
                )
                required_requests.add(
                    self.document_evidence.document_request_identity
                )
                expected_operation_count = 3
            if (
                self.composite_proof.publication_evidence != publication
                or self.composite_proof.document_evidence != self.document_evidence
                or required_artifacts != set(self.retained_artifact_identities)
                or not required_requests.issubset(self.subrequest_keys)
                or len(self.subrequest_keys) != expected_operation_count
                or len(self.physical_attempt_ids) != expected_operation_count
                or len(self.attempt_events) != expected_operation_count
                or any(
                    event.outcome is not ProviderSubrequestOutcomeKind.AVAILABLE
                    for event in self.attempt_events
                )
            ):
                raise ValueError("CNINFO admitted evidence accounting is inconsistent")
        return self


class CninfoListingClassification(str, Enum):
    """Closed metadata classification for a CNINFO announcement title."""

    INITIAL_LISTING = "initial_listing"
    RELATED_IPO_BUT_NOT_LISTING = "related_ipo_but_not_listing"
    LATER_SHARE_LISTING = "later_share_listing"
    AMBIGUOUS = "ambiguous"
    UNRELATED = "unrelated"


class CninfoListingBoard(str, Enum):
    """Externally authoritative board choices covered by qualification."""

    XSHG_MAIN = "xshg_main"
    XSHE_CHINEXT = "xshe_chinext"
    XSHG_STAR = "xshg_star"

    @property
    def venue(self) -> str:
        return {
            CninfoListingBoard.XSHG_MAIN: "XSHG",
            CninfoListingBoard.XSHE_CHINEXT: "XSHE",
            CninfoListingBoard.XSHG_STAR: "XSHG",
        }[self]

    @property
    def page_column(self) -> str:
        return {
            CninfoListingBoard.XSHG_MAIN: "SHZB",
            CninfoListingBoard.XSHE_CHINEXT: "SZCY",
            CninfoListingBoard.XSHG_STAR: "SHKCB",
        }[self]

    @property
    def query_route(self) -> tuple[str, str]:
        return {
            CninfoListingBoard.XSHG_MAIN: ("sse", ""),
            CninfoListingBoard.XSHE_CHINEXT: ("szse", "szcy"),
            CninfoListingBoard.XSHG_STAR: ("sse", "shkcp"),
        }[self]


CninfoListingPublicationRequest.model_rebuild()


class _CninfoAnnouncementRow(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider_record_id: None = Field(alias="id")
    sec_code: str = Field(alias="secCode", pattern=r"^[0-9]{6}$")
    sec_name: str = Field(alias="secName", min_length=1)
    org_id: str = Field(alias="orgId", pattern=_CNINFO_ORG_ID_PATTERN)
    announcement_id: str = Field(alias="announcementId", pattern=r"^[0-9]+$")
    announcement_title: str = Field(alias="announcementTitle", min_length=1)
    announcement_time: int = Field(alias="announcementTime", ge=0)
    adjunct_url: str = Field(alias="adjunctUrl", min_length=1)
    adjunct_size: int = Field(alias="adjunctSize", ge=0)
    adjunct_type: str | None = Field(alias="adjunctType")
    storage_time: None = Field(alias="storageTime")
    column_id: str = Field(alias="columnId", min_length=1)
    page_column: str = Field(alias="pageColumn", min_length=1)
    announcement_type: str = Field(alias="announcementType", min_length=1)
    associate_announcement: None = Field(alias="associateAnnouncement")
    important: None
    batch_num: None = Field(alias="batchNum")
    announcement_content: Literal[""] = Field(alias="announcementContent")
    org_name: str | None = Field(alias="orgName")
    tile_sec_name: str | None = Field(alias="tileSecName")
    short_title: str | None = Field(alias="shortTitle")
    announcement_type_name: str | None = Field(alias="announcementTypeName")
    sec_name_list: None = Field(alias="secNameList")


class _CninfoAnnouncementResponse(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    classified_announcements: None = Field(alias="classifiedAnnouncements")
    total_securities: Literal[0] = Field(alias="totalSecurities")
    total_announcement: int = Field(alias="totalAnnouncement", ge=0)
    total_record_num: int = Field(alias="totalRecordNum", ge=0)
    announcements: tuple[_CninfoAnnouncementRow, ...]
    category_list: None = Field(alias="categoryList")
    has_more: bool = Field(alias="hasMore")
    total_pages: int = Field(alias="totalpages", ge=0)


class CninfoAnnouncementSelection(BaseModel):
    """Validated unique announcement selected from one bounded complete page."""

    model_config = _CLOSED_MODEL_CONFIG

    sec_code: str = Field(pattern=r"^[0-9]{6}$")
    org_id: str = Field(min_length=1, max_length=64)
    sec_name: str
    announcement_id: str = Field(pattern=r"^[0-9]+$")
    publication_timestamp: datetime
    publication_date: date
    exact_title: str
    normalized_title: str
    adjunct_url: str
    adjunct_size: int = Field(ge=0)
    adjunct_type: str | None
    page_column: str
    classification: CninfoListingClassification

    @property
    def document_required(self) -> bool:
        return self.classification is CninfoListingClassification.AMBIGUOUS


class CninfoLegacyHtmlExtraction(BaseModel):
    """Minimal structured evidence extracted by the qualified local parser."""

    model_config = _CLOSED_MODEL_CONFIG

    parser_version: Literal["cninfo_legacy_html_v1+lxml-6.1.1"] = (
        "cninfo_legacy_html_v1+lxml-6.1.1"
    )
    extraction_method: Literal[
        "strict-gb2312+lxml-local-dom+anchored-field-validation"
    ] = "strict-gb2312+lxml-local-dom+anchored-field-validation"
    conversion_provenance: Literal["unknown"] = "unknown"
    announcement_id: str = Field(pattern=r"^[0-9]+$")
    original_url: str
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_title: str
    issuer: str
    security_code: str = Field(pattern=r"^[0-9]{6}$")
    venue: Literal["XSHG"]
    event_kind: Literal["initial_a_share_listing"] = "initial_a_share_listing"
    listing_effective_date: date
    publication_date: date
    document_date: date
    structured_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def classify_cninfo_listing_title(title: str) -> CninfoListingClassification:
    """Classify one exact normalized title without broad ``上市`` matching."""

    return CninfoListingClassification(classify_listing_publication_title(title).value)


def normalize_cninfo_listing_title(title: str) -> str:
    """Normalize representation only; never infer event meaning from issuer text."""

    return normalize_listing_publication_title(title)


def _load_json_without_duplicate_keys(payload: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique_object,
    )


def parse_cninfo_security_lookup(payload: bytes, *, sec_code: str) -> CninfoSecurityMatch:
    """Resolve exactly one active A-share match from a bounded lookup response."""

    try:
        decoded = _load_json_without_duplicate_keys(payload)
        if not isinstance(decoded, list) or not 1 <= len(decoded) <= 10:
            raise ValueError
        rows = tuple(
            _CninfoSecurityLookupRow.model_validate(item, strict=True)
            for item in decoded
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
        raise CninfoListingPublicationError(
            CninfoFailureReason.SCHEMA_INCOMPATIBLE
        ) from None
    matches = tuple(
        row
        for row in rows
        if row.code == sec_code
        and row.category == "A股"
        and row.delisted.casefold() == "false"
    )
    if not matches:
        raise CninfoListingPublicationError(CninfoFailureReason.LOOKUP_UNAVAILABLE)
    if len(matches) != 1 or len({row.org_id for row in matches}) != 1:
        raise CninfoListingPublicationError(CninfoFailureReason.ORG_ID_AMBIGUOUS)
    return CninfoSecurityMatch(sec_code=sec_code, org_id=matches[0].org_id)


def select_cninfo_listing_announcement(
    payload: bytes,
    *,
    sec_code: str,
    org_id: str,
    board: CninfoListingBoard,
    window_start: date,
    window_end: date,
) -> CninfoAnnouncementSelection:
    """Validate one complete response and select exactly one listing candidate."""

    if window_start > window_end:
        raise CninfoListingPublicationError(CninfoFailureReason.ANNOUNCEMENT_UNAVAILABLE)
    try:
        _load_json_without_duplicate_keys(payload)
        response = _CninfoAnnouncementResponse.model_validate_json(
            payload,
            strict=True,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
        raise CninfoListingPublicationError(
            CninfoFailureReason.SCHEMA_INCOMPATIBLE
        ) from None
    if (
        response.has_more
        or response.total_pages != 0
        or response.total_announcement != response.total_record_num
        or response.total_record_num != len(response.announcements)
        or len(response.announcements) > 30
    ):
        raise CninfoListingPublicationError(CninfoFailureReason.INCOMPLETE_PAGINATION)
    if not response.announcements:
        raise CninfoListingPublicationError(
            CninfoFailureReason.ANNOUNCEMENT_UNAVAILABLE
        )
    if len({row.announcement_id for row in response.announcements}) != len(
        response.announcements
    ):
        raise CninfoListingPublicationError(
            CninfoFailureReason.ANNOUNCEMENT_AMBIGUOUS
        )
    candidates: list[CninfoAnnouncementSelection] = []
    adjunct_pattern = re.compile(
        r"^finalpage/(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})/"
        r"(?P<announcement_id>[0-9]+)\.(?P<media_type>PDF|html)$"
    )
    for row in response.announcements:
        if row.sec_code != sec_code or row.org_id != org_id:
            raise CninfoListingPublicationError(CninfoFailureReason.IDENTITY_MISMATCH)
        if row.page_column != board.page_column:
            raise CninfoListingPublicationError(CninfoFailureReason.VENUE_MISMATCH)
        adjunct_match = adjunct_pattern.fullmatch(row.adjunct_url)
        if adjunct_match is None or (
            adjunct_match.group("announcement_id") != row.announcement_id
        ):
            raise CninfoListingPublicationError(CninfoFailureReason.IDENTITY_MISMATCH)
        if row.adjunct_type is not None and (
            row.adjunct_type.casefold()
            != adjunct_match.group("media_type").casefold()
        ):
            raise CninfoListingPublicationError(
                CninfoFailureReason.SCHEMA_INCOMPATIBLE
            )
        try:
            publication_timestamp = datetime.fromtimestamp(
                row.announcement_time / 1000,
                tz=timezone.utc,
            ).astimezone(ZoneInfo("Asia/Shanghai"))
            adjunct_date = date.fromisoformat(adjunct_match.group("date"))
        except (OSError, OverflowError, ValueError):
            raise CninfoListingPublicationError(
                CninfoFailureReason.SCHEMA_INCOMPATIBLE
            ) from None
        if (
            publication_timestamp.date() != adjunct_date
            or not window_start <= adjunct_date <= window_end
        ):
            raise CninfoListingPublicationError(CninfoFailureReason.IDENTITY_MISMATCH)
        classification = classify_cninfo_listing_title(row.announcement_title)
        if classification not in {
            CninfoListingClassification.INITIAL_LISTING,
            CninfoListingClassification.AMBIGUOUS,
        }:
            continue
        candidates.append(
            CninfoAnnouncementSelection(
                sec_code=row.sec_code,
                org_id=row.org_id,
                sec_name=row.sec_name,
                announcement_id=row.announcement_id,
                publication_timestamp=publication_timestamp,
                publication_date=adjunct_date,
                exact_title=row.announcement_title,
                normalized_title=normalize_cninfo_listing_title(
                    row.announcement_title
                ),
                adjunct_url=row.adjunct_url,
                adjunct_size=row.adjunct_size,
                adjunct_type=row.adjunct_type,
                page_column=row.page_column,
                classification=classification,
            )
        )
    if not candidates:
        raise CninfoListingPublicationError(
            CninfoFailureReason.ANNOUNCEMENT_UNAVAILABLE
        )
    if len(candidates) != 1:
        raise CninfoListingPublicationError(
            CninfoFailureReason.ANNOUNCEMENT_AMBIGUOUS
        )
    return candidates[0]


def parse_cninfo_legacy_listing_html(
    payload: bytes,
    *,
    selection: CninfoAnnouncementSelection,
    expected_venue: str,
) -> CninfoLegacyHtmlExtraction:
    """Parse one qualified legacy GB2312 document with local, inert DOM handling."""

    if (
        selection.classification is not CninfoListingClassification.AMBIGUOUS
        or not selection.adjunct_url.endswith(".html")
        or expected_venue != "XSHG"
    ):
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        )
    if (
        not isinstance(payload, bytes)
        or not 1_000 <= len(payload) <= _CNINFO_LEGACY_HTML_MAX_BYTES
    ):
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_PARSE_FAILED)
    if payload.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_PARSE_FAILED)
    declarations = tuple(
        item.decode("ascii").casefold()
        for item in re.findall(
            br"charset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)",
            payload,
            flags=re.IGNORECASE,
        )
    )
    if declarations != ("gb2312",):
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_PARSE_FAILED)
    if re.search(br"<!\s*(?:ENTITY|DOCTYPE[^>]*(?:SYSTEM|PUBLIC))", payload, re.I):
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_PARSE_FAILED)
    try:
        decoded = payload.decode("gb2312", errors="strict")
        if decoded.encode("gb2312", errors="strict") != payload:
            raise UnicodeError
    except UnicodeError:
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_PARSE_FAILED
        ) from None
    try:
        if version("lxml") != "6.1.1":
            raise CninfoListingPublicationError(
                CninfoFailureReason.DOCUMENT_PARSER_UNAVAILABLE
            )
        from lxml import etree
    except (ImportError, PackageNotFoundError):
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_PARSER_UNAVAILABLE
        ) from None
    try:
        parser = etree.HTMLParser(
            no_network=True,
            recover=False,
            remove_comments=True,
        )
        root = etree.fromstring(payload, parser=parser)
        if root is None or any(
            len(root.xpath(f"//{tag}")) != 1 for tag in ("html", "head", "body")
        ):
            raise ValueError
        disclosure_nodes = root.xpath(
            "//td[contains(concat(' ', normalize-space(@class), ' '), ' px13h ')]"
        )
        if len(disclosure_nodes) != 1:
            raise ValueError
        disclosure = disclosure_nodes[0]
        for node in disclosure.xpath(
            ".//script|.//style|.//link|.//img|.//iframe|.//object|.//embed"
        ):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        text = etree.tostring(disclosure, method="text", encoding="unicode")
    except (CninfoListingPublicationError, ValueError, etree.LxmlError):
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_PARSE_FAILED
        ) from None
    normalized_text = " ".join(unicodedata.normalize("NFKC", text).split())
    if len(normalized_text) < 10_000:
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_PARSE_FAILED)
    normalized_title = normalize_cninfo_listing_title(selection.exact_title)
    title_suffix = "股票上市公告书"
    if not normalized_title.endswith(title_suffix):
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        )
    issuer = normalized_title.removesuffix(title_suffix)
    security_codes = set(
        re.findall(r"股票代码\s*[:：]\s*([0-9]{6})", normalized_text)
    )
    listing_date_fields = re.findall(
        r"股票上市日期\s*[:：]\s*([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日",
        normalized_text,
    )
    if security_codes != {selection.sec_code} or not listing_date_fields:
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        )
    try:
        listing_dates = {
            date(*(int(item) for item in fields))
            for fields in listing_date_fields
        }
    except ValueError:
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        ) from None
    if len(listing_dates) != 1:
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        )
    listing_date = next(iter(listing_dates))
    required_anchors = (
        normalized_title,
        issuer,
        "股票上市申请经上海证券交易所",
        f"于{listing_date.year}年{listing_date.month}月{listing_date.day}日在上海证券交易所挂牌交易",
    )
    if (
        any(anchor not in normalized_text for anchor in required_anchors)
        or any(
            marker in normalized_text
            for marker in (
                "深圳证券交易所",
                "非公开发行",
                "向特定对象",
                "新增股份上市",
                "限售股上市",
                "配股上市",
                "增发股票上市",
            )
        )
        or re.search(r"向社会公开发行[0-9]+万股A股", normalized_text) is None
        or selection.publication_date.isoformat() not in decoded
    ):
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_BINDING_INVALID
        )
    original_url = f"https://static.cninfo.com.cn/{selection.adjunct_url}"
    raw_digest = sha256(payload).hexdigest()
    material: dict[str, object] = {
        "parser_version": "cninfo_legacy_html_v1+lxml-6.1.1",
        "extraction_method": (
            "strict-gb2312+lxml-local-dom+anchored-field-validation"
        ),
        "conversion_provenance": "unknown",
        "announcement_id": selection.announcement_id,
        "original_url": original_url,
        "raw_sha256": raw_digest,
        "exact_title": selection.exact_title,
        "issuer": issuer,
        "security_code": selection.sec_code,
        "venue": "XSHG",
        "event_kind": "initial_a_share_listing",
        "listing_effective_date": listing_date.isoformat(),
        "publication_date": selection.publication_date.isoformat(),
        "document_date": selection.publication_date.isoformat(),
    }
    canonical_evidence = {
        "document_date": selection.publication_date.isoformat(),
        "document_identity": {
            "announcement_id": selection.announcement_id,
            "original_url": original_url,
            "raw_sha256": raw_digest,
            "title": selection.exact_title,
        },
        "event_kind": "initial_a_share_listing",
        "extraction_method": (
            "strict-gb2312+lxml-local-dom+anchored-field-validation"
        ),
        "issuer": issuer,
        "listing_effective_date": listing_date.isoformat(),
        "parser_version": "cninfo_legacy_html_v1+lxml-6.1.1",
        "publication_date": selection.publication_date.isoformat(),
        "security_code": selection.sec_code,
        "venue": "XSHG",
    }
    structured_digest = sha256(
        json.dumps(
            canonical_evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return CninfoLegacyHtmlExtraction(
        **material,
        structured_evidence_sha256=structured_digest,
    )


def verify_cninfo_listing_document(
    payload: bytes | None,
    *,
    selection: CninfoAnnouncementSelection,
    expected_venue: str,
) -> CninfoLegacyHtmlExtraction | None:
    """Verify only required qualified HTML; PDF automation remains unavailable."""

    if not selection.document_required:
        return None
    media_is_pdf = (selection.adjunct_type or "").casefold() == "pdf" or (
        selection.adjunct_url.casefold().endswith(".pdf")
    )
    if media_is_pdf:
        raise CninfoListingPublicationError(
            CninfoFailureReason.DOCUMENT_AUTOMATION_UNAVAILABLE
        )
    if payload is None or not selection.adjunct_url.casefold().endswith(".html"):
        raise CninfoListingPublicationError(CninfoFailureReason.DOCUMENT_REQUIRED)
    return parse_cninfo_legacy_listing_html(
        payload,
        selection=selection,
        expected_venue=expected_venue,
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _accounting(
    results: Sequence[ProviderSubrequestResult],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[ProviderSubrequestAttemptEvent, ...],
]:
    artifacts = _unique(
        tuple(
            result.artifact.artifact_ref
            for result in results
            if result.artifact is not None
        )
    )
    keys = _unique(tuple(result.key.subrequest_key for result in results))
    events = tuple(event for result in results for event in result.attempt_events)
    attempts = _unique(tuple(event.attempt_event_id for event in events))
    return artifacts, keys, attempts, events


def _unavailable_result(
    reason: CninfoFailureReason,
    results: Sequence[ProviderSubrequestResult] = (),
) -> CninfoListingPublicationResult:
    artifacts, keys, attempts, events = _accounting(results)
    return CninfoListingPublicationResult(
        kind=CninfoListingPublicationResultKind.UNAVAILABLE,
        reason=reason,
        retained_artifact_identities=artifacts,
        subrequest_keys=keys,
        physical_attempt_ids=attempts,
        attempt_events=events,
    )


def _outcome_reason(result: ProviderSubrequestResult) -> CninfoFailureReason:
    return {
        ProviderSubrequestOutcomeKind.PERMISSION_DENIED: CninfoFailureReason.ACCESS_DENIED,
        ProviderSubrequestOutcomeKind.RATE_LIMITED: CninfoFailureReason.RATE_LIMITED,
        ProviderSubrequestOutcomeKind.AUTHENTICATION: (
            CninfoFailureReason.AUTHENTICATION_REQUIRED
        ),
        ProviderSubrequestOutcomeKind.TIMEOUT: CninfoFailureReason.TIMEOUT,
        ProviderSubrequestOutcomeKind.MALFORMED: CninfoFailureReason.SCHEMA_INCOMPATIBLE,
    }.get(result.outcome.kind, CninfoFailureReason.TRANSPORT_UNAVAILABLE)


def _result_time(result: ProviderSubrequestResult) -> datetime:
    artifact = result.artifact
    if (
        not result.attempt_events
        or artifact is None
        or artifact.retrieved_at is None
        or result.attempt_events[-1].attempted_at.utcoffset() is None
        or artifact.retrieved_at < result.attempt_events[-1].attempted_at
    ):
        raise CninfoListingPublicationError(CninfoFailureReason.TRANSPORT_UNAVAILABLE)
    return artifact.retrieved_at.astimezone(timezone.utc)


def _artifact_binding(
    result: ProviderSubrequestResult,
    *,
    row_identity: str,
) -> ListingEvidenceArtifactBinding:
    artifact = result.artifact
    if artifact is None:
        raise CninfoListingPublicationError(CninfoFailureReason.TRANSPORT_UNAVAILABLE)
    return ListingEvidenceArtifactBinding(
        artifact_identity=artifact.artifact_ref,
        artifact_sha256=artifact.artifact_sha256,
        revision_identity=artifact.artifact_ref,
        row_identity=row_identity,
        byte_length=artifact.byte_length,
        media_type=artifact.media_type,
    )


def _row_identity(prefix: str, material: Mapping[str, object]) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _validated_http_body(
    response: CninfoHttpResponse,
    *,
    expected_url: str,
    expected_media_type: str,
) -> bytes:
    response = CninfoHttpResponse.model_validate(response)
    if response.status_code == 401:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            status_code=401,
            error_code="cninfo_authentication_required",
        )
    if response.status_code == 403:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            status_code=403,
            error_code="cninfo_access_denied",
        )
    if response.status_code == 429:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=False,
            status_code=429,
            error_code="cninfo_rate_limited",
        )
    if response.status_code != 200:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            status_code=response.status_code,
            error_code="cninfo_http_failure",
        )
    preview = response.body[:16_384].decode("utf-8", errors="ignore").casefold()
    if any(marker in preview for marker in ("captcha", "验证码", "人机验证")):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="cninfo_captcha",
        )
    if any(marker in preview for marker in ("access denied", "forbidden", "访问被拒绝")):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="cninfo_access_denied",
        )
    if any(marker in preview for marker in ("请登录", "authentication required")):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="cninfo_authentication_required",
        )
    if any(marker in preview for marker in ("访问过于频繁", "too many requests")):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=False,
            error_code="cninfo_rate_limited",
        )
    if (
        response.final_url != expected_url
        or not response.content_type.casefold().startswith(expected_media_type)
        or not response.body
    ):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="cninfo_response_incompatible",
        )
    return response.body


class CninfoListingPublicationAdapter:
    """Deep bounded module for CNINFO lookup, classification, and optional HTML."""

    def __init__(
        self,
        *,
        subrequest_cache: ProviderSubrequestCache,
        transport: CninfoHttpTransport,
        source_policy: CninfoSourcePolicy,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
        timeout_seconds: float = 15,
    ) -> None:
        if (
            not isinstance(subrequest_cache, ProviderSubrequestCache)
            or not isinstance(source_policy, CninfoSourcePolicy)
            or not owner_id.strip()
            or not callable(now)
            or not callable(sleep)
            or lease_duration <= timedelta(0)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("CNINFO adapter configuration is invalid")
        self._cache = subrequest_cache
        self._transport = transport
        self._policy = source_policy
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration
        self._timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _headers(*, document: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "TradingAgents/0.3 bounded-personal-research "
                "listing-publication-evidence"
            ),
            "Accept": "text/html" if document else "application/json, text/plain, */*",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        }
        if not document:
            headers.update(
                {
                    "Origin": "https://www.cninfo.com.cn",
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        return headers

    def _key(
        self,
        request: CninfoListingPublicationRequest,
        *,
        operation: str,
        provider_scope: Literal["cninfo_metadata", "cninfo_documents"],
        range_start: str,
        range_end: str,
        requested_fields: tuple[str, ...],
    ) -> ProviderSubrequestKey:
        upstream_id, _service_name = upstream_service_identity_for_provider(
            provider_scope
        )
        return ProviderSubrequestKey.create(
            provider_id="cninfo",
            upstream_service_id=upstream_id,
            account_scope="cninfo-public-research",
            capacity_scope=provider_scope.replace("_", "."),
            instrument_identity=request.instrument_identity,
            requested_range_start=range_start,
            requested_range_end=range_end,
            requested_fields=(operation, *requested_fields),
            as_of_date=request.requested_session,
            qualification_profile=CNINFO_CANDIDATE_PROFILE,
            normalizer_version="cninfo-listing-publication-normalizer-v1",
            data_usage_mode=self._policy.data_usage_mode,
        )

    def _execute(
        self,
        key: ProviderSubrequestKey,
        *,
        operation: str,
        media_type: str,
        physical_request: Callable[[], bytes],
    ) -> ProviderSubrequestResult:
        return self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            max_physical_attempts=self._policy.max_physical_attempts_per_operation,
            operation=operation,
            media_type=media_type,
            record_at_physical_io=True,
            capture_retrieved_at=True,
            physical_request=physical_request,
        )

    def acquire(
        self,
        request: CninfoListingPublicationRequest,
    ) -> CninfoListingPublicationResult:
        """Return one composite proof or one typed unavailable result."""

        if (
            not self._policy.eligible
            or self._cache.data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH
            or self._cache.data_usage_mode is not self._policy.data_usage_mode
        ):
            return _unavailable_result(
                CninfoFailureReason.DATA_USAGE_MODE_NOT_PERMITTED
            )
        upstream_ids = tuple(
            upstream_service_identity_for_provider(provider_id)[0]
            for provider_id in ("cninfo_metadata", "cninfo_documents")
        )
        if not self._cache.has_operator_safety_ceiling(
            upstream_service_ids=upstream_ids,
            minimum_interval=timedelta(
                seconds=self._policy.operator_minimum_interval_seconds
            ),
            allow_prewarming=False,
        ):
            return _unavailable_result(CninfoFailureReason.SOURCE_POLICY_UNAVAILABLE)
        try:
            request = CninfoListingPublicationRequest.model_validate(
                request.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError):
            return _unavailable_result(CninfoFailureReason.REQUEST_BOUNDS_INVALID)
        sec_code = request.instrument_identity.symbol.partition(".")[0]
        results: list[ProviderSubrequestResult] = []

        lookup_form = {
            "keyWord": sec_code,
            "maxSecNum": "10",
            "maxListNum": "5",
        }
        lookup_key = self._key(
            request,
            operation=CNINFO_SECURITY_LOOKUP_OPERATION,
            provider_scope="cninfo_metadata",
            range_start=sec_code,
            range_end=sec_code,
            requested_fields=(
                "secCode",
                "orgId",
                "lookup.max_list_num.5",
                "lookup.max_sec_num.10",
            ),
        )

        def lookup_request() -> bytes:
            try:
                response = self._transport.post_form(
                    CNINFO_SECURITY_LOOKUP_URL,
                    form=lookup_form,
                    headers=self._headers(),
                    timeout_seconds=self._timeout_seconds,
                )
            except TimeoutError:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.TIMEOUT,
                    retryable=False,
                    error_code="cninfo_timeout",
                ) from None
            return _validated_http_body(
                response,
                expected_url=CNINFO_SECURITY_LOOKUP_URL,
                expected_media_type="application/json",
            )

        lookup = self._execute(
            lookup_key,
            operation=CNINFO_SECURITY_LOOKUP_OPERATION,
            media_type="application/json",
            physical_request=lookup_request,
        )
        results.append(lookup)
        if lookup.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return _unavailable_result(_outcome_reason(lookup), results)
        assert lookup.value is not None
        try:
            match = parse_cninfo_security_lookup(lookup.value, sec_code=sec_code)
            lookup_time = _result_time(lookup)
        except CninfoListingPublicationError as exc:
            return _unavailable_result(exc.reason, results)

        column, plate = request.authoritative_board.query_route
        query_form = {
            "pageNum": "1",
            "pageSize": "30",
            "column": column,
            "tabName": "fulltext",
            "plate": plate,
            "stock": f"{sec_code},{match.org_id}",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{request.window_start.isoformat()}~{request.window_end.isoformat()}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        query_key = self._key(
            request,
            operation=CNINFO_ANNOUNCEMENT_QUERY_OPERATION,
            provider_scope="cninfo_metadata",
            range_start=request.window_start.isoformat(),
            range_end=request.window_end.isoformat(),
            requested_fields=(
                "announcementId",
                "announcementTime",
                "announcementTitle",
                "adjunctUrl",
                "pageColumn",
                f"board.{request.authoritative_board.value}",
                f"org_id.{match.org_id}",
                f"page_column.{request.authoritative_board.page_column}",
                f"query.column.{column}",
                "query.page_num.1",
                "query.page_size.30",
                f"query.plate.{plate or 'none'}",
                "query.tab.fulltext",
            ),
        )

        def query_request() -> bytes:
            try:
                response = self._transport.post_form(
                    CNINFO_ANNOUNCEMENT_QUERY_URL,
                    form=query_form,
                    headers=self._headers(),
                    timeout_seconds=self._timeout_seconds,
                )
            except TimeoutError:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.TIMEOUT,
                    retryable=False,
                    error_code="cninfo_timeout",
                ) from None
            return _validated_http_body(
                response,
                expected_url=CNINFO_ANNOUNCEMENT_QUERY_URL,
                expected_media_type="application/json",
            )

        query = self._execute(
            query_key,
            operation=CNINFO_ANNOUNCEMENT_QUERY_OPERATION,
            media_type="application/json",
            physical_request=query_request,
        )
        results.append(query)
        if query.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return _unavailable_result(_outcome_reason(query), results)
        assert query.value is not None
        try:
            selection = select_cninfo_listing_announcement(
                query.value,
                sec_code=sec_code,
                org_id=match.org_id,
                board=request.authoritative_board,
                window_start=request.window_start,
                window_end=request.window_end,
            )
            query_time = _result_time(query)
            lookup_artifact = _artifact_binding(
                lookup,
                row_identity=_row_identity(
                    "cninfo-security-row:v1",
                    {"secCode": sec_code, "orgId": match.org_id},
                ),
            )
            metadata_artifact = _artifact_binding(
                query,
                row_identity=_row_identity(
                    "cninfo-announcement-row:v1",
                    {
                        "secCode": sec_code,
                        "orgId": match.org_id,
                        "announcementId": selection.announcement_id,
                    },
                ),
            )
            publication = ListingPublicationEvidence(
                instrument_identity=request.instrument_identity,
                sec_code=sec_code,
                org_id=match.org_id,
                venue=request.instrument_identity.venue,
                authoritative_board=request.authoritative_board.value,
                page_column=selection.page_column,
                announcement_id=selection.announcement_id,
                publication_timestamp=selection.publication_timestamp,
                publication_date=selection.publication_date,
                exact_title=selection.exact_title,
                normalized_title=selection.normalized_title,
                adjunct_url=selection.adjunct_url,
                classification=ListingPublicationClassification(
                    selection.classification.value
                ),
                classifier_version=CNINFO_METADATA_CLASSIFIER_VERSION,
                security_lookup_artifact=lookup_artifact,
                metadata_artifact=metadata_artifact,
                security_lookup_request_identity=lookup.key.subrequest_key,
                announcement_query_request_identity=query.key.subrequest_key,
                observed_at=min(lookup_time, query_time),
                retrieved_at=max(lookup_time, query_time),
            )
        except (CninfoListingPublicationError, TypeError, ValueError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, CninfoListingPublicationError)
                else CninfoFailureReason.SCHEMA_INCOMPATIBLE
            )
            return _unavailable_result(reason, results)

        document_evidence: ListingDocumentEvidence | None = None
        if selection.document_required:
            if selection.adjunct_url.casefold().endswith(".pdf"):
                return _unavailable_result(
                    CninfoFailureReason.DOCUMENT_AUTOMATION_UNAVAILABLE,
                    results,
                )
            document_url = _CNINFO_DOCUMENT_ORIGIN + selection.adjunct_url
            document_key = self._key(
                request,
                operation=CNINFO_DOCUMENT_FETCH_OPERATION,
                provider_scope="cninfo_documents",
                range_start=selection.publication_date.isoformat(),
                range_end=selection.announcement_id,
                requested_fields=(
                    "legacyHtml",
                    "listingEvent",
                    f"announcement_id.{selection.announcement_id}",
                    f"board.{request.authoritative_board.value}",
                    "document.media_type.html",
                    f"org_id.{match.org_id}",
                    f"page_column.{selection.page_column}",
                    f"url_sha256.{sha256(document_url.encode()).hexdigest()}",
                ),
            )

            def document_request() -> bytes:
                try:
                    response = self._transport.get_document(
                        document_url,
                        headers=self._headers(document=True),
                        timeout_seconds=self._timeout_seconds,
                    )
                except TimeoutError:
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.TIMEOUT,
                        retryable=False,
                        error_code="cninfo_timeout",
                    ) from None
                return _validated_http_body(
                    response,
                    expected_url=document_url,
                    expected_media_type="text/html",
                )

            document = self._execute(
                document_key,
                operation=CNINFO_DOCUMENT_FETCH_OPERATION,
                media_type="text/html",
                physical_request=document_request,
            )
            results.append(document)
            if document.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
                return _unavailable_result(_outcome_reason(document), results)
            assert document.value is not None
            try:
                extraction = verify_cninfo_listing_document(
                    document.value,
                    selection=selection,
                    expected_venue=request.instrument_identity.venue,
                )
                if extraction is None:
                    raise CninfoListingPublicationError(
                        CninfoFailureReason.DOCUMENT_REQUIRED
                    )
                document_time = _result_time(document)
                document_evidence = ListingDocumentEvidence(
                    instrument_identity=request.instrument_identity,
                    sec_code=extraction.security_code,
                    issuer=extraction.issuer,
                    venue=extraction.venue,
                    announcement_id=extraction.announcement_id,
                    document_request_identity=document.key.subrequest_key,
                    exact_title=extraction.exact_title,
                    original_url=extraction.original_url,
                    listing_effective_date=extraction.listing_effective_date,
                    publication_date=extraction.publication_date,
                    parser_version=extraction.parser_version,
                    conversion_provenance=extraction.conversion_provenance,
                    artifact=_artifact_binding(
                        document,
                        row_identity=_row_identity(
                            "cninfo-document-row:v1",
                            {
                                "announcementId": extraction.announcement_id,
                                "structuredEvidence": (
                                    extraction.structured_evidence_sha256
                                ),
                            },
                        ),
                    ),
                    structured_evidence_sha256=(
                        extraction.structured_evidence_sha256
                    ),
                    observed_at=document_time,
                    retrieved_at=document_time,
                )
            except (CninfoListingPublicationError, TypeError, ValueError) as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, CninfoListingPublicationError)
                    else CninfoFailureReason.DOCUMENT_BINDING_INVALID
                )
                return _unavailable_result(reason, results)
        try:
            proof = build_composite_listing_temporal_proof(
                instrument_identity=request.instrument_identity,
                requested_session=request.requested_session,
                effective_evidence=request.effective_evidence,
                publication_evidence=publication,
                document_evidence=document_evidence,
            )
        except (ListingTemporalProofError, TypeError, ValueError):
            return _unavailable_result(CninfoFailureReason.TEMPORAL_PROOF_CONFLICT, results)
        artifacts, keys, attempts, events = _accounting(results)
        return CninfoListingPublicationResult(
            kind=CninfoListingPublicationResultKind.AVAILABLE,
            publication_evidence=publication,
            document_evidence=document_evidence,
            composite_proof=proof,
            retained_artifact_identities=artifacts,
            subrequest_keys=keys,
            physical_attempt_ids=attempts,
            attempt_events=events,
        )


__all__ = [
    "CNINFO_ANNOUNCEMENT_QUERY_OPERATION",
    "CNINFO_ANNOUNCEMENT_QUERY_URL",
    "CNINFO_DOCUMENT_FETCH_OPERATION",
    "CNINFO_LISTING_QUERY_LOOKBACK_DAYS",
    "CNINFO_SECURITY_LOOKUP_OPERATION",
    "CNINFO_SECURITY_LOOKUP_URL",
    "CNINFO_SOURCE_POLICY_VERSION",
    "CninfoHttpResponse",
    "CninfoHttpTransport",
    "CninfoFailureReason",
    "CninfoAnnouncementSelection",
    "CninfoListingBoard",
    "CninfoListingClassification",
    "CninfoLegacyHtmlExtraction",
    "CninfoListingPublicationError",
    "CninfoListingPublicationAdapter",
    "CninfoListingPublicationRequest",
    "CninfoListingPublicationResult",
    "CninfoListingPublicationResultKind",
    "CninfoSecurityMatch",
    "CninfoSourcePolicy",
    "RequestsCninfoHttpTransport",
    "classify_cninfo_listing_title",
    "normalize_cninfo_listing_title",
    "parse_cninfo_security_lookup",
    "parse_cninfo_legacy_listing_html",
    "select_cninfo_listing_announcement",
    "verify_cninfo_listing_document",
]
