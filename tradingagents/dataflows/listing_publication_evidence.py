"""Provider-neutral forward composite listing temporal-proof contracts."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from html import unescape
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

from tradingagents.dataflows.financial_temporal_eligibility import (
    FinancialEligibilityAcquisitionWindow,
    TemporalEvidenceAcquisitionOrigin,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

COMPOSITE_LISTING_TEMPORAL_PROOF_VERSION = "financial-listing-temporal-proof-v2"
LISTING_PUBLICATION_CAPABILITY_VERSION = "listing-publication-evidence-v1"
CNINFO_METADATA_CLASSIFIER_VERSION = "cninfo-listing-metadata-classifier-v1"

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    revalidate_instances="always",
)
_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
_PROVIDER_ID = r"^[a-z][a-z0-9_]{0,63}$"
_ORG_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_INITIAL_LISTING_TITLE_PATTERN = re.compile(
    r"^首次公开发行(?:A股)?股票"
    r"(?:(?:(?:并在)?(?:上海证券交易所|深圳证券交易所|主板|创业板|科创板)上市之)"
    r"|(?:主板|创业板|科创板))?上市公告书$"
)


class ListingEventKind(str, Enum):
    INITIAL_A_SHARE_LISTING = "initial_a_share_listing"


class ListingPublicationClassification(str, Enum):
    INITIAL_LISTING = "initial_listing"
    RELATED_IPO_BUT_NOT_LISTING = "related_ipo_but_not_listing"
    LATER_SHARE_LISTING = "later_share_listing"
    AMBIGUOUS = "ambiguous"
    UNRELATED = "unrelated"


def normalize_listing_publication_title(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("listing publication title is unavailable")
    return "".join(unicodedata.normalize("NFKC", unescape(title)).split())


def classify_listing_publication_title(
    title: str,
) -> ListingPublicationClassification:
    """Conservative anchored listing-event classification shared by adapters."""

    normalized = normalize_listing_publication_title(title)
    if _INITIAL_LISTING_TITLE_PATTERN.fullmatch(normalized) is not None:
        return ListingPublicationClassification.INITIAL_LISTING
    if "上市" in normalized and any(
        marker in normalized
        for marker in (
            "非公开发行",
            "增发",
            "配股",
            "限售股",
            "员工持股",
            "股权激励",
            "新增股份",
            "转板",
            "重新上市",
            "恢复上市",
        )
    ):
        return ListingPublicationClassification.LATER_SHARE_LISTING
    if "首次公开发行" in normalized or (
        "上市" in normalized
        and any(marker in normalized for marker in ("法律意见书", "保荐书"))
    ):
        return ListingPublicationClassification.RELATED_IPO_BUT_NOT_LISTING
    if normalized.endswith("股票上市公告书"):
        return ListingPublicationClassification.AMBIGUOUS
    return ListingPublicationClassification.UNRELATED


class ListingTemporalProofMode(str, Enum):
    CURRENT_ANALYSIS_ONLY = "current_analysis_only"
    STRICT_HISTORICAL_PIT = "strict_historical_pit"


class ListingTemporalProofFailureReason(str, Enum):
    INSTRUMENT_MISMATCH = "listing_instrument_mismatch"
    VENUE_MISMATCH = "listing_venue_mismatch"
    WRONG_EVENT_KIND = "listing_wrong_event_kind"
    EFFECTIVE_AFTER_REQUESTED_SESSION = "listing_effective_after_requested_session"
    PUBLICATION_AFTER_REQUESTED_SESSION = "listing_publication_after_requested_session"
    PUBLICATION_AFTER_EFFECTIVE = "listing_publication_after_effective"
    DOCUMENT_REQUIRED = "listing_document_required"
    UNEXPECTED_DOCUMENT = "listing_unexpected_document"
    DOCUMENT_BINDING_INVALID = "listing_document_binding_invalid"
    ARTIFACT_BINDING_MISMATCH = "listing_artifact_binding_mismatch"
    STRICT_PIT_UNAVAILABLE = "listing_strict_pit_unavailable"
    SOURCE_POLICY_UNAVAILABLE = "listing_source_policy_unavailable"
    ACQUISITION_WINDOW_MISMATCH = "listing_acquisition_window_mismatch"
    OBSERVATION_AFTER_RETRIEVAL = "listing_observation_after_retrieval"
    OBSERVATION_BEFORE_ELIGIBILITY_START = (
        "listing_observation_before_eligibility_start"
    )
    RETRIEVAL_BEFORE_ELIGIBILITY_START = (
        "listing_retrieval_before_eligibility_start"
    )
    RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION = (
        "listing_retrieval_after_eligibility_evaluation"
    )
    REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER = (
        "listing_requested_session_not_latest_completed_frontier"
    )


class ListingTemporalProofError(ValueError):
    """Typed payload-free failure raised before context exposure."""

    def __init__(self, reason: ListingTemporalProofFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _identity(prefix: str, material: object) -> str:
    encoded = json.dumps(
        _canonical_value(material),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return prefix + sha256(encoded).hexdigest()


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_identity(identity: InstrumentIdentityEvidence, *, venue: str) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue != venue
        or venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("listing evidence requires authoritative mainland Equity identity")


class ListingEvidenceArtifactBinding(BaseModel):
    """Content-addressed artifact plus exact revision and row identities."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["listing-evidence-artifact-binding-v1"] = (
        "listing-evidence-artifact-binding-v1"
    )
    artifact_identity: str = Field(pattern=_SAFE_ID)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_identity: str = Field(pattern=_SAFE_ID)
    row_identity: str = Field(pattern=_SAFE_ID)
    byte_length: int = Field(ge=0, le=8_388_608)
    media_type: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+/-]{0,127}$")

    @model_validator(mode="after")
    def _validate_content_address(self) -> Self:
        direct_cas = f"provider-subrequest-artifact=sha256:{self.artifact_sha256}"
        if self.artifact_identity.startswith("provider-subrequest-artifact=sha256:") and (
            self.artifact_identity != direct_cas
        ):
            raise ValueError("listing artifact identity contradicts content digest")
        if self.revision_identity != self.artifact_identity:
            raise ValueError("listing artifact revision identity mismatch")
        return self


class ListingEffectiveEvidence(BaseModel):
    """Independent lifecycle provider proof of listing effectiveness."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["listing-effective-evidence-v1"] = (
        "listing-effective-evidence-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    provider_id: str = Field(pattern=_PROVIDER_ID)
    venue: Literal["XSHG", "XSHE"]
    event_kind: Literal[ListingEventKind.INITIAL_A_SHARE_LISTING] = (
        ListingEventKind.INITIAL_A_SHARE_LISTING
    )
    listing_effective_date: date
    lifecycle_status: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    artifact: ListingEvidenceArtifactBinding
    observed_at: datetime
    retrieved_at: datetime

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _normalize_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_effective_evidence(self) -> Self:
        _validate_identity(self.instrument_identity, venue=self.venue)
        if self.provider_id == "cninfo" or self.observed_at > self.retrieved_at:
            raise ValueError("listing effective evidence is not independent lifecycle proof")
        return self


class ListingPublicationEvidence(BaseModel):
    """Source-bound CNINFO publication metadata under the neutral capability."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["listing-publication-evidence-v1"] = (
        LISTING_PUBLICATION_CAPABILITY_VERSION
    )
    instrument_identity: InstrumentIdentityEvidence
    provider_id: Literal["cninfo"] = "cninfo"
    sec_code: str = Field(pattern=r"^[0-9]{6}$")
    org_id: str = Field(pattern=_ORG_ID)
    venue: Literal["XSHG", "XSHE"]
    authoritative_board: Literal["xshg_main", "xshe_chinext", "xshg_star"]
    page_column: Literal["SHZB", "SZCY", "SHKCB"]
    event_kind: Literal[ListingEventKind.INITIAL_A_SHARE_LISTING] = (
        ListingEventKind.INITIAL_A_SHARE_LISTING
    )
    announcement_id: str = Field(pattern=r"^[0-9]+$")
    publication_timestamp: datetime
    publication_date: date
    exact_title: str = Field(min_length=1)
    normalized_title: str = Field(min_length=1)
    adjunct_url: str = Field(
        pattern=r"^finalpage/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]+\.(?:PDF|html)$"
    )
    classification: ListingPublicationClassification
    classifier_version: Literal["cninfo-listing-metadata-classifier-v1"] = (
        CNINFO_METADATA_CLASSIFIER_VERSION
    )
    security_lookup_artifact: ListingEvidenceArtifactBinding
    metadata_artifact: ListingEvidenceArtifactBinding
    security_lookup_request_identity: str = Field(
        pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$"
    )
    announcement_query_request_identity: str = Field(
        pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$"
    )
    observed_at: datetime
    retrieved_at: datetime

    @field_validator("publication_timestamp", "observed_at", "retrieved_at")
    @classmethod
    def _normalize_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_publication_evidence(self) -> Self:
        _validate_identity(self.instrument_identity, venue=self.venue)
        board_binding = {
            "xshg_main": ("XSHG", "SHZB"),
            "xshe_chinext": ("XSHE", "SZCY"),
            "xshg_star": ("XSHG", "SHKCB"),
        }[self.authoritative_board]
        adjunct = re.fullmatch(
            r"finalpage/(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})/"
            r"(?P<announcement>[0-9]+)\.(?:PDF|html)",
            self.adjunct_url,
        )
        classified = classify_listing_publication_title(self.exact_title)
        if (
            (self.venue, self.page_column) != board_binding
            or self.sec_code != self.instrument_identity.symbol.partition(".")[0]
            or self.publication_timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date()
            != self.publication_date
            or adjunct is None
            or adjunct.group("date") != self.publication_date.isoformat()
            or adjunct.group("announcement") != self.announcement_id
            or self.security_lookup_artifact.artifact_identity
            == self.metadata_artifact.artifact_identity
            or self.security_lookup_artifact.media_type != "application/json"
            or self.metadata_artifact.media_type != "application/json"
            or self.security_lookup_request_identity
            == self.announcement_query_request_identity
            or self.observed_at > self.retrieved_at
            or self.normalized_title
            != normalize_listing_publication_title(self.exact_title)
            or classified is not self.classification
            or classified
            not in {
                ListingPublicationClassification.INITIAL_LISTING,
                ListingPublicationClassification.AMBIGUOUS,
            }
        ):
            raise ValueError("listing publication evidence classification is invalid")
        return self


class ListingDocumentEvidence(BaseModel):
    """Optional qualified legacy-HTML event verification."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["listing-document-evidence-v1"] = (
        "listing-document-evidence-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    provider_id: Literal["cninfo"] = "cninfo"
    sec_code: str = Field(pattern=r"^[0-9]{6}$")
    issuer: str = Field(min_length=1)
    venue: Literal["XSHG"]
    event_kind: Literal[ListingEventKind.INITIAL_A_SHARE_LISTING] = (
        ListingEventKind.INITIAL_A_SHARE_LISTING
    )
    announcement_id: str = Field(pattern=r"^[0-9]+$")
    document_request_identity: str = Field(
        pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$"
    )
    exact_title: str = Field(min_length=1)
    original_url: str = Field(
        pattern=(
            r"^https://static\.cninfo\.com\.cn/finalpage/"
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]+\.html$"
        )
    )
    listing_effective_date: date
    publication_date: date
    parser_version: Literal["cninfo_legacy_html_v1+lxml-6.1.1"]
    conversion_provenance: Literal["unknown"] = "unknown"
    artifact: ListingEvidenceArtifactBinding
    structured_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    retrieved_at: datetime

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _normalize_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_document_evidence(self) -> Self:
        _validate_identity(self.instrument_identity, venue=self.venue)
        normalized_title = normalize_listing_publication_title(self.exact_title)
        normalized_issuer = normalize_listing_publication_title(self.issuer)
        title_suffix = "股票上市公告书"
        url_match = re.fullmatch(
            r"https://static\.cninfo\.com\.cn/finalpage/"
            r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})/"
            r"(?P<announcement>[0-9]+)\.html",
            self.original_url,
        )
        if (
            self.sec_code != self.instrument_identity.symbol.partition(".")[0]
            or not normalized_title.endswith(title_suffix)
            or normalized_issuer != normalized_title.removesuffix(title_suffix)
            or url_match is None
            or url_match.group("date") != self.publication_date.isoformat()
            or url_match.group("announcement") != self.announcement_id
            or self.artifact.media_type != "text/html"
            or self.observed_at > self.retrieved_at
        ):
            raise ValueError("listing document evidence binding is invalid")
        return self


def _join_failure(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    requested_session: date,
    effective: ListingEffectiveEvidence,
    publication: ListingPublicationEvidence,
    document: ListingDocumentEvidence | None,
    mode: ListingTemporalProofMode,
) -> ListingTemporalProofFailureReason | None:
    if (
        effective.instrument_identity != instrument_identity
        or publication.instrument_identity != instrument_identity
        or (document is not None and document.instrument_identity != instrument_identity)
    ):
        return ListingTemporalProofFailureReason.INSTRUMENT_MISMATCH
    if (
        effective.venue != instrument_identity.venue
        or publication.venue != instrument_identity.venue
        or (document is not None and document.venue != instrument_identity.venue)
    ):
        return ListingTemporalProofFailureReason.VENUE_MISMATCH
    if (
        effective.event_kind is not ListingEventKind.INITIAL_A_SHARE_LISTING
        or publication.event_kind is not ListingEventKind.INITIAL_A_SHARE_LISTING
        or (
            document is not None
            and document.event_kind is not ListingEventKind.INITIAL_A_SHARE_LISTING
        )
    ):
        return ListingTemporalProofFailureReason.WRONG_EVENT_KIND
    if effective.listing_effective_date > requested_session:
        return ListingTemporalProofFailureReason.EFFECTIVE_AFTER_REQUESTED_SESSION
    if publication.publication_date > requested_session:
        return ListingTemporalProofFailureReason.PUBLICATION_AFTER_REQUESTED_SESSION
    if publication.publication_date > effective.listing_effective_date:
        return ListingTemporalProofFailureReason.PUBLICATION_AFTER_EFFECTIVE
    if publication.classification is ListingPublicationClassification.AMBIGUOUS:
        if document is None:
            return ListingTemporalProofFailureReason.DOCUMENT_REQUIRED
    elif document is not None:
        return ListingTemporalProofFailureReason.UNEXPECTED_DOCUMENT
    if document is not None and (
        document.sec_code != publication.sec_code
        or document.announcement_id != publication.announcement_id
        or document.document_request_identity
        in {
            publication.security_lookup_request_identity,
            publication.announcement_query_request_identity,
        }
        or document.exact_title != publication.exact_title
        or normalize_listing_publication_title(document.issuer)
        != normalize_listing_publication_title(publication.exact_title).removesuffix(
            "股票上市公告书"
        )
        or document.original_url
        != f"https://static.cninfo.com.cn/{publication.adjunct_url}"
        or document.publication_date != publication.publication_date
        or document.listing_effective_date != effective.listing_effective_date
    ):
        return ListingTemporalProofFailureReason.DOCUMENT_BINDING_INVALID
    artifact_ids = (
        effective.artifact.artifact_identity,
        publication.security_lookup_artifact.artifact_identity,
        publication.metadata_artifact.artifact_identity,
        *(() if document is None else (document.artifact.artifact_identity,)),
    )
    if len(set(artifact_ids)) != len(artifact_ids):
        return ListingTemporalProofFailureReason.ARTIFACT_BINDING_MISMATCH
    if mode is ListingTemporalProofMode.STRICT_HISTORICAL_PIT:
        return ListingTemporalProofFailureReason.STRICT_PIT_UNAVAILABLE
    return None


class CompositeListingTemporalProof(BaseModel):
    """Forward-only multi-source listing timing proof; old v1 stays untouched."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-listing-temporal-proof-v2"] = (
        COMPOSITE_LISTING_TEMPORAL_PROOF_VERSION
    )
    proof_identity: str = Field(
        pattern=r"^financial-listing-temporal-proof:v2:[0-9a-f]{64}$"
    )
    instrument_identity: InstrumentIdentityEvidence
    requested_session: date
    mode: ListingTemporalProofMode = ListingTemporalProofMode.CURRENT_ANALYSIS_ONLY
    effective_evidence: ListingEffectiveEvidence
    publication_evidence: ListingPublicationEvidence
    document_evidence: ListingDocumentEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    publication_announcement_identity: str = Field(
        pattern=r"^cninfo-announcement:v1:[0-9]+$"
    )

    @property
    def listing_effective_date(self) -> date:
        return self.effective_evidence.listing_effective_date

    @property
    def listing_publication_date(self) -> date:
        return self.publication_evidence.publication_date

    @property
    def effective_artifact_identity(self) -> str:
        return self.effective_evidence.artifact.artifact_identity

    @property
    def publication_artifact_identity(self) -> str:
        return self.publication_evidence.metadata_artifact.artifact_identity

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="python", exclude={"proof_identity"})

    @model_validator(mode="after")
    def _validate_composite(self) -> Self:
        failure = _join_failure(
            instrument_identity=self.instrument_identity,
            requested_session=self.requested_session,
            effective=self.effective_evidence,
            publication=self.publication_evidence,
            document=self.document_evidence,
            mode=self.mode,
        )
        if failure is not None:
            raise ValueError(failure.value)
        if self.publication_announcement_identity != (
            f"cninfo-announcement:v1:{self.publication_evidence.announcement_id}"
        ):
            raise ValueError("listing publication announcement identity mismatch")
        expected = _identity(
            "financial-listing-temporal-proof:v2:",
            self.identity_payload(),
        )
        if self.proof_identity != expected:
            raise ValueError("composite listing temporal proof identity mismatch")
        return self


class ListingCompositeEvidenceOrigins(BaseModel):
    """Fresh/retained origin for each independently acquired proof component."""

    model_config = _CLOSED_MODEL_CONFIG

    effective: TemporalEvidenceAcquisitionOrigin
    publication: TemporalEvidenceAcquisitionOrigin
    document: TemporalEvidenceAcquisitionOrigin | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class CompositeListingTemporalEligibilityDecision(BaseModel):
    """Terminal acquisition-window decision for one composite listing proof."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "composite-listing-temporal-eligibility-decision-v1"
    ] = "composite-listing-temporal-eligibility-decision-v1"
    decision_identity: str = Field(
        pattern=r"^composite-listing-temporal-eligibility-decision:v1:[0-9a-f]{64}$"
    )
    proof_identity: str = Field(
        pattern=r"^financial-listing-temporal-proof:v2:[0-9a-f]{64}$"
    )
    requested_session: date
    mode: ListingTemporalProofMode
    acquisition_window: FinancialEligibilityAcquisitionWindow
    evidence_origins: ListingCompositeEvidenceOrigins
    result: Literal["accepted", "rejected"]
    reasons: tuple[ListingTemporalProofFailureReason, ...]
    effective_artifact_identity: str = Field(pattern=_SAFE_ID)
    effective_row_identity: str = Field(pattern=_SAFE_ID)
    security_lookup_artifact_identity: str = Field(pattern=_SAFE_ID)
    publication_artifact_identity: str = Field(pattern=_SAFE_ID)
    publication_announcement_identity: str = Field(
        pattern=r"^cninfo-announcement:v1:[0-9]+$"
    )
    document_artifact_identity: str | None = Field(
        default=None,
        pattern=_SAFE_ID,
        exclude_if=lambda value: value is None,
    )
    document_request_identity: str | None = Field(
        default=None,
        pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="python", exclude={"decision_identity"})

    @model_validator(mode="after")
    def _validate_decision(self) -> Self:
        accepted = not self.reasons
        if (self.result == "accepted") != accepted or (
            (self.document_artifact_identity is None)
            != (self.document_request_identity is None)
        ):
            raise ValueError("composite listing decision result contradicts reasons")
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("composite listing decision reasons are not canonical")
        expected = _identity(
            "composite-listing-temporal-eligibility-decision:v1:",
            self.identity_payload(),
        )
        if self.decision_identity != expected:
            raise ValueError("composite listing decision identity mismatch")
        return self


def _component_window_reasons(
    *,
    observed_at: datetime,
    retrieved_at: datetime,
    origin: TemporalEvidenceAcquisitionOrigin,
    window: FinancialEligibilityAcquisitionWindow,
) -> set[ListingTemporalProofFailureReason]:
    reasons: set[ListingTemporalProofFailureReason] = set()
    if observed_at > retrieved_at:
        reasons.add(ListingTemporalProofFailureReason.OBSERVATION_AFTER_RETRIEVAL)
    if retrieved_at > window.eligibility_evaluated_at:
        reasons.add(
            ListingTemporalProofFailureReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION
        )
    if origin is TemporalEvidenceAcquisitionOrigin.FRESH:
        if observed_at < window.eligibility_started_at:
            reasons.add(
                ListingTemporalProofFailureReason.OBSERVATION_BEFORE_ELIGIBILITY_START
            )
        if retrieved_at < window.eligibility_started_at:
            reasons.add(
                ListingTemporalProofFailureReason.RETRIEVAL_BEFORE_ELIGIBILITY_START
            )
    return reasons


def evaluate_composite_listing_temporal_proof(
    proof: CompositeListingTemporalProof,
    *,
    acquisition_window: FinancialEligibilityAcquisitionWindow,
    evidence_origins: ListingCompositeEvidenceOrigins,
) -> CompositeListingTemporalEligibilityDecision:
    """Evaluate window/PIT predicates without relabeling either source fact."""

    proof = CompositeListingTemporalProof.model_validate(proof.model_dump(mode="json"))
    window = FinancialEligibilityAcquisitionWindow.model_validate(
        acquisition_window.model_dump(mode="json")
    )
    origins = ListingCompositeEvidenceOrigins.model_validate(
        evidence_origins.model_dump(mode="json")
    )
    reasons: set[ListingTemporalProofFailureReason] = set()
    if (
        proof.requested_session != window.requested_session
        or proof.mode.value != window.transaction_binding.mode.value
        or window.transaction_binding.strict_cutoff is not None
    ):
        reasons.add(ListingTemporalProofFailureReason.ACQUISITION_WINDOW_MISMATCH)
    if proof.mode is ListingTemporalProofMode.STRICT_HISTORICAL_PIT:
        reasons.add(ListingTemporalProofFailureReason.STRICT_PIT_UNAVAILABLE)
    if window.evaluated_latest_completed_session != proof.requested_session:
        reasons.add(
            ListingTemporalProofFailureReason.REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER
        )
    components = (
        (
            proof.effective_evidence.observed_at,
            proof.effective_evidence.retrieved_at,
            origins.effective,
        ),
        (
            proof.publication_evidence.observed_at,
            proof.publication_evidence.retrieved_at,
            origins.publication,
        ),
    )
    for observed_at, retrieved_at, origin in components:
        reasons.update(
            _component_window_reasons(
                observed_at=observed_at,
                retrieved_at=retrieved_at,
                origin=origin,
                window=window,
            )
        )
    document = proof.document_evidence
    if document is not None:
        if origins.document is None:
            reasons.add(ListingTemporalProofFailureReason.ACQUISITION_WINDOW_MISMATCH)
        else:
            reasons.update(
                _component_window_reasons(
                    observed_at=document.observed_at,
                    retrieved_at=document.retrieved_at,
                    origin=origins.document,
                    window=window,
                )
            )
    elif origins.document is not None:
        reasons.add(ListingTemporalProofFailureReason.ACQUISITION_WINDOW_MISMATCH)
    canonical_reasons = tuple(sorted(reasons, key=lambda item: item.value))
    material: dict[str, object] = {
        "contract_version": "composite-listing-temporal-eligibility-decision-v1",
        "proof_identity": proof.proof_identity,
        "requested_session": proof.requested_session,
        "mode": proof.mode,
        "acquisition_window": window,
        "evidence_origins": origins,
        "result": "accepted" if not canonical_reasons else "rejected",
        "reasons": canonical_reasons,
        "effective_artifact_identity": proof.effective_artifact_identity,
        "effective_row_identity": proof.effective_evidence.artifact.row_identity,
        "security_lookup_artifact_identity": (
            proof.publication_evidence.security_lookup_artifact.artifact_identity
        ),
        "publication_artifact_identity": proof.publication_artifact_identity,
        "publication_announcement_identity": proof.publication_announcement_identity,
    }
    if document is not None:
        material["document_artifact_identity"] = document.artifact.artifact_identity
        material["document_request_identity"] = document.document_request_identity
    return CompositeListingTemporalEligibilityDecision(
        **material,
        decision_identity=_identity(
            "composite-listing-temporal-eligibility-decision:v1:",
            material,
        ),
    )


def build_composite_listing_temporal_proof(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    requested_session: date,
    effective_evidence: ListingEffectiveEvidence,
    publication_evidence: ListingPublicationEvidence,
    document_evidence: ListingDocumentEvidence | None = None,
    mode: ListingTemporalProofMode = ListingTemporalProofMode.CURRENT_ANALYSIS_ONLY,
) -> CompositeListingTemporalProof:
    """Join independently sourced facts or raise one typed fail-closed reason."""

    parsed_mode = ListingTemporalProofMode(mode)
    failure = _join_failure(
        instrument_identity=instrument_identity,
        requested_session=requested_session,
        effective=effective_evidence,
        publication=publication_evidence,
        document=document_evidence,
        mode=parsed_mode,
    )
    if failure is not None:
        raise ListingTemporalProofError(failure)
    payload: dict[str, object] = {
        "contract_version": COMPOSITE_LISTING_TEMPORAL_PROOF_VERSION,
        "instrument_identity": instrument_identity,
        "requested_session": requested_session,
        "mode": parsed_mode,
        "effective_evidence": effective_evidence,
        "publication_evidence": publication_evidence,
        "publication_announcement_identity": (
            f"cninfo-announcement:v1:{publication_evidence.announcement_id}"
        ),
    }
    if document_evidence is not None:
        payload["document_evidence"] = document_evidence
    return CompositeListingTemporalProof(
        **payload,
        proof_identity=_identity(
            "financial-listing-temporal-proof:v2:",
            payload,
        ),
    )


__all__ = [
    "CNINFO_METADATA_CLASSIFIER_VERSION",
    "COMPOSITE_LISTING_TEMPORAL_PROOF_VERSION",
    "LISTING_PUBLICATION_CAPABILITY_VERSION",
    "CompositeListingTemporalProof",
    "CompositeListingTemporalEligibilityDecision",
    "ListingDocumentEvidence",
    "ListingEffectiveEvidence",
    "ListingEventKind",
    "ListingEvidenceArtifactBinding",
    "ListingPublicationClassification",
    "ListingPublicationEvidence",
    "ListingCompositeEvidenceOrigins",
    "ListingTemporalProofError",
    "ListingTemporalProofFailureReason",
    "ListingTemporalProofMode",
    "build_composite_listing_temporal_proof",
    "classify_listing_publication_title",
    "evaluate_composite_listing_temporal_proof",
    "normalize_listing_publication_title",
]
