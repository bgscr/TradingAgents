"""Closed mainland financial reporting-period eligibility policy contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2 = (
    "mainland-financial-period-eligibility-v2"
)
MAINLAND_FINANCIAL_REPORTING_CALENDAR_V2 = (
    "mainland-financial-reporting-calendar-v2"
)
MAINLAND_FINANCIAL_PUBLICATION_STATES_V2 = (
    "mainland-financial-publication-states-v2"
)
MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2 = (
    "mainland-financial-target-construction-v2"
)
MAINLAND_FINANCIAL_CANDIDATE_SELECTION_V2 = (
    "mainland-financial-candidate-selection-v2"
)
MAINLAND_FINANCIAL_CHECKPOINT_PROJECTION_V2 = (
    "mainland-financial-checkpoint-projection-v2"
)
MAINLAND_FINANCIAL_AUDIT_PROJECTION_V2 = (
    "mainland-financial-audit-projection-v2"
)
MAINLAND_FINANCIAL_REPORT_PROJECTION_V2 = (
    "mainland-financial-report-projection-v2"
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_POLICY_SIGNATURE_PREFIX = "mainland-financial-eligibility-policy:v2:"
_UNORDERED_SEQUENCE_KEYS = frozenset(
    {
        "annual_period_ends",
        "publication_deadlines",
        "publication_states",
        "reporting_period_ends",
    }
)
_NONMATERIAL_IDENTITY_KEYS = frozenset(
    {
        "call_order",
        "created_at",
        "generated_at",
        "model_prose",
        "pid",
        "process_id",
        "runtime_call_order",
        "unrelated_timestamp",
    }
)
_UNSAFE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "payload_body",
    "raw_error",
    "secret",
    "token",
)
_UNSAFE_TEXT_MARKERS = (
    "bearer ",
    "raw provider error",
    "token=",
    "traceback",
)
_URL_QUERY = re.compile(r"https?://\S*\?", re.IGNORECASE)


class MainlandFinancialPublicationState(str, Enum):
    PUBLISHED_ELIGIBLE = "published_eligible"
    ANNOUNCED_AFTER_CUTOFF = "announced_after_cutoff"
    NOT_YET_ELIGIBLE = "not_yet_eligible"
    PUBLICATION_OVERDUE_UNPROVEN = "publication_overdue_unproven"
    PRE_LISTING = "pre_listing"


class MainlandFinancialPeriodTargetFailureReason(str, Enum):
    PUBLICATION_METADATA_UNPROVEN = "publication_metadata_unproven"
    NO_PUBLICATION_FRONTIER = "no_publication_frontier"
    UNSUPPORTED_SINCE_LISTING_COVERAGE = "unsupported_since_listing_coverage"
    LISTING_METADATA_NOT_AUTHORITATIVE = "listing_metadata_not_authoritative"


_STANDARD_REPORTING_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def _v2_publication_deadline(period_end: date) -> date:
    deadline_month, deadline_day, year_offset = {
        (3, 31): (4, 30, 0),
        (6, 30): (8, 31, 0),
        (9, 30): (10, 31, 0),
        (12, 31): (4, 30, 1),
    }[(period_end.month, period_end.day)]
    return date(
        period_end.year + year_offset,
        deadline_month,
        deadline_day,
    )


def _previous_v2_reporting_end(period_end: date) -> date:
    index = _STANDARD_REPORTING_ENDS.index((period_end.month, period_end.day))
    if index:
        month, day = _STANDARD_REPORTING_ENDS[index - 1]
        return date(period_end.year, month, day)
    month, day = _STANDARD_REPORTING_ENDS[-1]
    return date(period_end.year - 1, month, day)


def _v2_reporting_window(frontier: date, count: int) -> tuple[date, ...]:
    periods = [frontier]
    while len(periods) < count:
        periods.append(_previous_v2_reporting_end(periods[-1]))
    return tuple(periods)


class MainlandFinancialPublicationEvidenceBinding(BaseModel):
    """One qualified compatible candidate proving publication by the cutoff."""

    model_config = _CLOSED_MODEL_CONFIG

    period_end: date
    ann_date: date
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class MainlandFinancialPublicationCandidateBinding(BaseModel):
    """Safe candidate provenance retained in the issuer publication index."""

    model_config = _CLOSED_MODEL_CONFIG

    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    ann_date: date | None = None


class MainlandFinancialPublicationPeriod(BaseModel):
    """Publication classification for one standard mainland reporting end."""

    model_config = _CLOSED_MODEL_CONFIG

    period_end: date
    filing_deadline: date
    publication_state: MainlandFinancialPublicationState
    sequence_memberships: tuple[Literal["annual", "reporting"], ...]
    compatible_candidate_identities: tuple[str, ...] = ()
    compatible_candidate_bindings: tuple[
        MainlandFinancialPublicationCandidateBinding, ...
    ] = ()
    valid_announcement_dates: tuple[date, ...] = ()

    @model_validator(mode="after")
    def _validate_canonical_period(self) -> MainlandFinancialPublicationPeriod:
        expected_memberships = (
            ("annual", "reporting")
            if self.period_end.month == 12 and self.period_end.day == 31
            else ("reporting",)
        )
        if self.sequence_memberships != expected_memberships:
            raise ValueError("publication period sequence membership is inconsistent")
        if self.compatible_candidate_identities != tuple(
            sorted(set(self.compatible_candidate_identities))
        ):
            raise ValueError("publication candidates must be unique and canonical")
        if self.compatible_candidate_bindings != tuple(
            sorted(
                self.compatible_candidate_bindings,
                key=lambda item: item.candidate_identity,
            )
        ) or self.compatible_candidate_identities != tuple(
            item.candidate_identity for item in self.compatible_candidate_bindings
        ):
            raise ValueError("publication candidate bindings must be canonical")
        if len(self.compatible_candidate_bindings) != len(
            {item.candidate_identity for item in self.compatible_candidate_bindings}
        ):
            raise ValueError("publication candidate bindings must be unique")
        bound_dates = {
            item.ann_date
            for item in self.compatible_candidate_bindings
            if item.ann_date is not None
        }
        if self.valid_announcement_dates != tuple(
            sorted(set(self.valid_announcement_dates))
        ) or set(self.valid_announcement_dates) != bound_dates:
            raise ValueError("announcement dates must be unique and canonical")
        return self


class MainlandFinancialListingProvenanceBinding(BaseModel):
    """Safe authoritative listing provenance persisted by target construction."""

    model_config = _CLOSED_MODEL_CONFIG

    authoritative: Literal[True] = True
    provider_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$")
    source_ref: str = Field(pattern=r"^[A-Za-z0-9_.:=/-]+$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _canonicalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("listing provenance requires a concrete timestamp")
        return value.astimezone(timezone.utc)


class MainlandFinancialSinceListingTargetException(BaseModel):
    """Explicit reduced target horizon justified only by authoritative listing age."""

    model_config = _CLOSED_MODEL_CONFIG

    reason: Literal["authoritative_since_listing"] = "authoritative_since_listing"
    listing_date: date
    listing_provenance: MainlandFinancialListingProvenanceBinding
    annual_publication_frontier: date | None = None
    reporting_publication_frontier: date | None = None
    annual_target_period_ends: tuple[date, ...]
    reporting_target_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    pre_listing_period_ends: tuple[date, ...]
    supported: bool


class MainlandFinancialPeriodTargetConstruction(BaseModel):
    """Deterministic issuer-level publication target output persisted by v2."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["mainland-financial-target-construction-v2"] = (
        MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] = MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
    run_as_of_date: date
    listing_date: date | None = None
    listing_provenance: (
        MainlandFinancialListingProvenanceBinding | None
    ) = None
    annual_publication_frontier: date | None = None
    reporting_publication_frontier: date | None = None
    annual_target_period_ends: tuple[date, ...]
    reporting_target_period_ends: tuple[date, ...]
    publication_periods: tuple[MainlandFinancialPublicationPeriod, ...]
    excluded_periods: tuple[MainlandFinancialPublicationPeriod, ...]
    publication_evidence_bindings: tuple[
        MainlandFinancialPublicationEvidenceBinding, ...
    ]
    since_listing_exception: MainlandFinancialSinceListingTargetException | None = None
    failure_reason: MainlandFinancialPeriodTargetFailureReason | None = None
    evidence_integrity_status: Literal["Decision-Ready", "Insufficient"]
    construction_identity: str = Field(
        pattern=r"^mainland-financial-target-construction:v2:[0-9a-f]{64}$"
    )

    @property
    def insufficient_evidence(self) -> bool:
        return self.failure_reason is not None

    @model_validator(mode="after")
    def _validate_construction(self) -> MainlandFinancialPeriodTargetConstruction:
        if self.annual_target_period_ends != tuple(
            sorted(set(self.annual_target_period_ends), reverse=True)
        ) or self.reporting_target_period_ends != tuple(
            sorted(set(self.reporting_target_period_ends), reverse=True)
        ):
            raise ValueError("publication targets must be unique and newest first")
        if self.publication_periods != tuple(
            sorted(self.publication_periods, key=lambda item: item.period_end, reverse=True)
        ):
            raise ValueError("publication periods must be newest first")
        listing_metadata_invalid = (self.listing_date is None) != (
            self.listing_provenance is None
        )
        period_by_end = {period.period_end: period for period in self.publication_periods}
        if len(period_by_end) != len(self.publication_periods):
            raise ValueError("publication period ends must be unique")
        for period in self.publication_periods:
            if (
                period.period_end.month,
                period.period_end.day,
            ) not in _STANDARD_REPORTING_ENDS:
                raise ValueError("publication period end is not standard")
            if period.filing_deadline != _v2_publication_deadline(period.period_end):
                raise ValueError("publication filing deadline contradicts v2 policy")
            valid_dates = period.valid_announcement_dates
            if (
                not listing_metadata_invalid
                and self.listing_date is not None
                and period.period_end < self.listing_date
            ):
                expected_state = MainlandFinancialPublicationState.PRE_LISTING
            elif any(value <= self.run_as_of_date for value in valid_dates):
                expected_state = MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            elif valid_dates:
                expected_state = (
                    MainlandFinancialPublicationState.ANNOUNCED_AFTER_CUTOFF
                )
            elif period.filing_deadline >= self.run_as_of_date:
                expected_state = MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
            else:
                expected_state = (
                    MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
                )
            if period.publication_state is not expected_state:
                raise ValueError("publication state contradicts its local evidence")

        def binding_key(
            item: MainlandFinancialPublicationEvidenceBinding,
        ) -> tuple[date, date, str]:
            return (
                item.period_end,
                item.ann_date,
                item.candidate_identity,
            )
        if self.publication_evidence_bindings != tuple(
            sorted(self.publication_evidence_bindings, key=binding_key, reverse=True)
        ):
            raise ValueError("publication evidence bindings must be canonical")
        candidate_ids = tuple(
            binding.candidate_identity
            for binding in self.publication_evidence_bindings
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("publication evidence candidates must be unique")
        binding_periods: set[date] = set()
        for binding in self.publication_evidence_bindings:
            period = period_by_end.get(binding.period_end)
            if (
                period is None
                or binding.ann_date > self.run_as_of_date
                or binding.ann_date not in period.valid_announcement_dates
                or period.publication_state
                is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            ):
                raise ValueError("publication evidence binding is inconsistent")
            candidate_binding = next(
                (
                    item
                    for item in period.compatible_candidate_bindings
                    if item.candidate_identity == binding.candidate_identity
                ),
                None,
            )
            if candidate_binding is None or (
                candidate_binding.artifact_identity != binding.artifact_identity
                or candidate_binding.provider_id != binding.provider_id
                or candidate_binding.ann_date != binding.ann_date
            ):
                raise ValueError("publication evidence provenance is inconsistent")
            binding_periods.add(binding.period_end)
        published_periods = {
            period.period_end
            for period in self.publication_periods
            if period.publication_state
            is MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
        }
        if binding_periods != published_periods:
            raise ValueError("published periods require bound publication evidence")

        expected_annual_frontier = max(
            (
                period.period_end
                for period in self.publication_periods
                if "annual" in period.sequence_memberships
                and period.publication_state
                is MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            ),
            default=None,
        )
        expected_reporting_frontier = max(published_periods, default=None)
        if (
            self.annual_publication_frontier != expected_annual_frontier
            or self.reporting_publication_frontier != expected_reporting_frontier
        ):
            raise ValueError("publication frontier contradicts publication periods")
        ordinary_annual = (
            tuple(
                date(expected_annual_frontier.year - offset, 12, 31)
                for offset in range(5)
            )
            if expected_annual_frontier is not None
            else ()
        )
        ordinary_reporting = (
            _v2_reporting_window(expected_reporting_frontier, 8)
            if expected_reporting_frontier is not None
            else ()
        )
        exception = self.since_listing_exception
        if exception is None:
            if (
                self.annual_target_period_ends != ordinary_annual
                or self.reporting_target_period_ends != ordinary_reporting
            ):
                raise ValueError("ordinary publication target window is inconsistent")
        else:
            if (
                self.listing_date != exception.listing_date
                or self.listing_provenance != exception.listing_provenance
                or self.annual_publication_frontier
                != exception.annual_publication_frontier
                or self.reporting_publication_frontier
                != exception.reporting_publication_frontier
                or self.annual_target_period_ends
                != exception.annual_target_period_ends
                or self.reporting_target_period_ends
                != exception.reporting_target_period_ends
            ):
                raise ValueError("since-listing target exception binding is inconsistent")
            expected_pre_listing = tuple(
                period.period_end
                for period in self.publication_periods
                if period.publication_state
                is MainlandFinancialPublicationState.PRE_LISTING
            )
            if exception.pre_listing_period_ends != expected_pre_listing:
                raise ValueError("since-listing pre-listing periods are inconsistent")
            missing_annual = tuple(
                period_end
                for period_end in self.annual_target_period_ends
                if period_by_end[period_end].publication_state
                is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            )
            missing_reporting = tuple(
                period_end
                for period_end in self.reporting_target_period_ends
                if period_by_end[period_end].publication_state
                is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            )
            if (
                exception.missing_annual_period_ends != missing_annual
                or exception.missing_reporting_period_ends != missing_reporting
                or exception.supported != (not missing_annual and not missing_reporting)
                or (
                    len(self.annual_target_period_ends) >= 5
                    and len(self.reporting_target_period_ends) >= 8
                )
            ):
                raise ValueError("since-listing coverage exception is inconsistent")
        target_periods = set(self.annual_target_period_ends) | set(
            self.reporting_target_period_ends
        )
        if not target_periods <= set(period_by_end):
            raise ValueError("publication target is absent from the publication index")
        expected_excluded = tuple(
            period
            for period in self.publication_periods
            if period.period_end not in target_periods
        )
        if self.excluded_periods != expected_excluded:
            raise ValueError("excluded publication periods are inconsistent")
        if self.failure_reason is None and any(
            period_by_end[period_end].publication_state
            is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
            for period_end in target_periods
        ):
            raise ValueError("decision-ready publication target lacks proof")
        missing_annual_targets = tuple(
            period_end
            for period_end in self.annual_target_period_ends
            if period_by_end[period_end].publication_state
            is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
        )
        missing_reporting_targets = tuple(
            period_end
            for period_end in self.reporting_target_period_ends
            if period_by_end[period_end].publication_state
            is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
        )
        overdue_after_frontier = any(
            period.publication_state
            is MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
            and (
                period.period_end > self.annual_publication_frontier
                if "annual" in period.sequence_memberships
                and self.annual_publication_frontier is not None
                else self.reporting_publication_frontier is not None
                and period.period_end > self.reporting_publication_frontier
            )
            for period in self.publication_periods
        )
        if listing_metadata_invalid:
            expected_failure_reason = (
                MainlandFinancialPeriodTargetFailureReason.LISTING_METADATA_NOT_AUTHORITATIVE
            )
        elif (
            self.annual_publication_frontier is None
            or self.reporting_publication_frontier is None
        ):
            expected_failure_reason = (
                MainlandFinancialPeriodTargetFailureReason.NO_PUBLICATION_FRONTIER
            )
        elif exception is not None and not exception.supported:
            expected_failure_reason = (
                MainlandFinancialPeriodTargetFailureReason.UNSUPPORTED_SINCE_LISTING_COVERAGE
            )
        elif (
            missing_annual_targets
            or missing_reporting_targets
            or overdue_after_frontier
        ):
            expected_failure_reason = (
                MainlandFinancialPeriodTargetFailureReason.PUBLICATION_METADATA_UNPROVEN
            )
        else:
            expected_failure_reason = None
        if self.failure_reason is not expected_failure_reason:
            raise ValueError("publication target failure reason is inconsistent")
        expected_status = (
            "Decision-Ready" if self.failure_reason is None else "Insufficient"
        )
        if self.evidence_integrity_status != expected_status:
            raise ValueError("target-construction evidence status is inconsistent")
        payload = self.model_dump(mode="json", exclude={"construction_identity"})
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_identity = (
            "mainland-financial-target-construction:v2:"
            + sha256(encoded).hexdigest()
        )
        if self.construction_identity != expected_identity:
            raise ValueError("publication target-construction identity mismatch")
        return self


class MainlandFinancialEligibilityPolicyFailureReason(str, Enum):
    MISSING_POLICY_IDENTITY = "missing_financial_eligibility_policy_identity"
    UNKNOWN_POLICY_IDENTITY = "unknown_financial_eligibility_policy_identity"
    CONTRADICTORY_POLICY_IDENTITY = (
        "contradictory_financial_eligibility_policy_identity"
    )
    INVALID_POLICY_CONTRACT = "invalid_financial_eligibility_policy_contract"
    UNSAFE_POLICY_PROJECTION = "unsafe_financial_eligibility_policy_projection"


class MainlandFinancialEligibilityPolicyError(ValueError):
    """Typed, bounded rejection of an invalid eligibility-policy projection."""

    def __init__(
        self,
        reason: MainlandFinancialEligibilityPolicyFailureReason,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class MainlandFinancialPublicationDeadline(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    period_end: Literal["03-31", "06-30", "09-30", "12-31"]
    deadline: Literal["04-30", "08-31", "10-31"]
    deadline_year_offset: Literal[0, 1]

    @model_validator(mode="after")
    def _validate_rule(self) -> MainlandFinancialPublicationDeadline:
        expected = {
            "03-31": ("04-30", 0),
            "06-30": ("08-31", 0),
            "09-30": ("10-31", 0),
            "12-31": ("04-30", 1),
        }[self.period_end]
        if (self.deadline, self.deadline_year_offset) != expected:
            raise ValueError("mainland publication deadline rule is not v2")
        return self


class MainlandFinancialReportingCalendar(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    calendar_version: Literal[
        "mainland-financial-reporting-calendar-v2"
    ] = MAINLAND_FINANCIAL_REPORTING_CALENDAR_V2
    annual_period_ends: tuple[Literal["12-31"], ...]
    reporting_period_ends: tuple[
        Literal["03-31", "06-30", "09-30", "12-31"], ...
    ]
    publication_deadlines: tuple[MainlandFinancialPublicationDeadline, ...]
    year_end_belongs_to_annual_and_reporting: Literal[True] = True
    announcement_cutoff_inclusive: Literal[True] = True
    missing_filing_deadline_day_is_not_overdue: Literal[True] = True

    @field_validator("annual_period_ends", mode="before")
    @classmethod
    def _canonicalize_annual_period_ends(cls, value: object) -> object:
        if isinstance(value, Sequence) and not isinstance(value, str):
            return tuple(sorted(value))
        return value

    @field_validator("reporting_period_ends", mode="before")
    @classmethod
    def _canonicalize_reporting_period_ends(cls, value: object) -> object:
        order = {"03-31": 0, "06-30": 1, "09-30": 2, "12-31": 3}
        if isinstance(value, Sequence) and not isinstance(value, str):
            return tuple(sorted(value, key=lambda item: order.get(str(item), 99)))
        return value

    @field_validator("publication_deadlines", mode="before")
    @classmethod
    def _canonicalize_deadlines(cls, value: object) -> object:
        order = {"03-31": 0, "06-30": 1, "09-30": 2, "12-31": 3}
        if isinstance(value, Sequence) and not isinstance(value, str):
            return tuple(
                sorted(
                    value,
                    key=lambda item: order.get(
                        str(
                            item.period_end
                            if isinstance(item, MainlandFinancialPublicationDeadline)
                            else item.get("period_end", "")
                            if isinstance(item, Mapping)
                            else ""
                        ),
                        99,
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _validate_exact_calendar(self) -> MainlandFinancialReportingCalendar:
        if self.annual_period_ends != ("12-31",):
            raise ValueError("annual period ends do not match the v2 calendar")
        if self.reporting_period_ends != (
            "03-31",
            "06-30",
            "09-30",
            "12-31",
        ):
            raise ValueError("reporting period ends do not match the v2 calendar")
        if tuple(rule.period_end for rule in self.publication_deadlines) != (
            "03-31",
            "06-30",
            "09-30",
            "12-31",
        ):
            raise ValueError("publication deadlines do not match the v2 calendar")
        return self


class MainlandFinancialEligibilityPolicy(BaseModel):
    """Immutable policy shared by every corrected qualified-shadow consumer."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["2.0"] = "2.0"
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] = MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
    reporting_calendar: MainlandFinancialReportingCalendar
    publication_state_contract_version: Literal[
        "mainland-financial-publication-states-v2"
    ] = MAINLAND_FINANCIAL_PUBLICATION_STATES_V2
    publication_states: tuple[MainlandFinancialPublicationState, ...]
    target_construction_contract_version: Literal[
        "mainland-financial-target-construction-v2"
    ] = MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v2"
    ] = "mainland-financial-completeness-v2"
    candidate_selection_contract_version: Literal[
        "mainland-financial-candidate-selection-v2"
    ] = MAINLAND_FINANCIAL_CANDIDATE_SELECTION_V2
    manifest_projection_version: Literal["financial-manifest-v2"] = (
        "financial-manifest-v2"
    )
    checkpoint_projection_version: Literal[
        "mainland-financial-checkpoint-projection-v2"
    ] = MAINLAND_FINANCIAL_CHECKPOINT_PROJECTION_V2
    audit_projection_version: Literal[
        "mainland-financial-audit-projection-v2"
    ] = MAINLAND_FINANCIAL_AUDIT_PROJECTION_V2
    report_projection_version: Literal[
        "mainland-financial-report-projection-v2"
    ] = MAINLAND_FINANCIAL_REPORT_PROJECTION_V2
    authority_disposition: Literal["shadow_non_authoritative"] = (
        "shadow_non_authoritative"
    )
    qualified_v1_authority_enabled: Literal[False] = False
    policy_signature: str = Field(
        pattern=r"^mainland-financial-eligibility-policy:v2:[0-9a-f]{64}$"
    )

    @field_validator("publication_states", mode="before")
    @classmethod
    def _canonicalize_publication_states(cls, value: object) -> object:
        order = {state.value: index for index, state in enumerate(MainlandFinancialPublicationState)}
        if isinstance(value, Sequence) and not isinstance(value, str):
            return tuple(
                sorted(
                    value,
                    key=lambda item: order.get(
                        item.value if isinstance(item, Enum) else str(item),
                        99,
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _validate_exact_policy(self) -> MainlandFinancialEligibilityPolicy:
        if self.publication_states != tuple(MainlandFinancialPublicationState):
            raise ValueError("publication states do not match the v2 policy")
        if self.policy_signature != mainland_financial_eligibility_policy_signature(
            self.signature_payload()
        ):
            raise ValueError("mainland financial eligibility-policy signature mismatch")
        return self

    def signature_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"policy_signature"})


def _assert_safe_policy_projection(value: object) -> None:
    if isinstance(value, BaseModel):
        _assert_safe_policy_projection(value.model_dump(mode="json"))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if any(marker in normalized_key for marker in _UNSAFE_KEY_MARKERS):
                raise MainlandFinancialEligibilityPolicyError(
                    MainlandFinancialEligibilityPolicyFailureReason.UNSAFE_POLICY_PROJECTION
                )
            _assert_safe_policy_projection(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_safe_policy_projection(item)
        return
    if isinstance(value, str) and (
        any(marker in value.casefold() for marker in _UNSAFE_TEXT_MARKERS)
        or _URL_QUERY.search(value) is not None
    ):
        raise MainlandFinancialEligibilityPolicyError(
            MainlandFinancialEligibilityPolicyFailureReason.UNSAFE_POLICY_PROJECTION
        )


def _canonical_policy_identity(value: object, *, parent_key: str = "") -> object:
    if isinstance(value, BaseModel):
        return _canonical_policy_identity(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        projected = {
            str(key): _canonical_policy_identity(item, parent_key=str(key))
            for key, item in value.items()
            if str(key).casefold() not in _NONMATERIAL_IDENTITY_KEYS
            and str(key) != "policy_signature"
        }
        return dict(sorted(projected.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_sequence = [
            _canonical_policy_identity(item) for item in value
        ]
        if parent_key.casefold() in _UNORDERED_SEQUENCE_KEYS:
            projected_sequence.sort(
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return projected_sequence
    if isinstance(value, Enum):
        return value.value
    return value


def mainland_financial_eligibility_policy_signature(
    payload: Mapping[str, Any] | MainlandFinancialEligibilityPolicy,
) -> str:
    """Return the canonical, order-independent and secret-free policy identity."""

    _assert_safe_policy_projection(payload)
    canonical = _canonical_policy_identity(payload)
    encoded = json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_POLICY_SIGNATURE_PREFIX}{sha256(encoded).hexdigest()}"


def build_mainland_financial_eligibility_policy_v2() -> MainlandFinancialEligibilityPolicy:
    """Construct the one canonical v2 eligibility policy without runtime input."""

    payload: dict[str, Any] = {
        "contract_version": "2.0",
        "eligibility_policy_version": MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
        "reporting_calendar": {
            "calendar_version": MAINLAND_FINANCIAL_REPORTING_CALENDAR_V2,
            "annual_period_ends": ["12-31"],
            "reporting_period_ends": ["03-31", "06-30", "09-30", "12-31"],
            "publication_deadlines": [
                {
                    "period_end": "03-31",
                    "deadline": "04-30",
                    "deadline_year_offset": 0,
                },
                {
                    "period_end": "06-30",
                    "deadline": "08-31",
                    "deadline_year_offset": 0,
                },
                {
                    "period_end": "09-30",
                    "deadline": "10-31",
                    "deadline_year_offset": 0,
                },
                {
                    "period_end": "12-31",
                    "deadline": "04-30",
                    "deadline_year_offset": 1,
                },
            ],
            "year_end_belongs_to_annual_and_reporting": True,
            "announcement_cutoff_inclusive": True,
            "missing_filing_deadline_day_is_not_overdue": True,
        },
        "publication_state_contract_version": MAINLAND_FINANCIAL_PUBLICATION_STATES_V2,
        "publication_states": [state.value for state in MainlandFinancialPublicationState],
        "target_construction_contract_version": MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2,
        "completeness_policy_version": "mainland-financial-completeness-v2",
        "candidate_selection_contract_version": MAINLAND_FINANCIAL_CANDIDATE_SELECTION_V2,
        "manifest_projection_version": "financial-manifest-v2",
        "checkpoint_projection_version": MAINLAND_FINANCIAL_CHECKPOINT_PROJECTION_V2,
        "audit_projection_version": MAINLAND_FINANCIAL_AUDIT_PROJECTION_V2,
        "report_projection_version": MAINLAND_FINANCIAL_REPORT_PROJECTION_V2,
        "authority_disposition": "shadow_non_authoritative",
        "qualified_v1_authority_enabled": False,
    }
    return MainlandFinancialEligibilityPolicy(
        **payload,
        policy_signature=mainland_financial_eligibility_policy_signature(payload),
    )


def restore_mainland_financial_eligibility_policy(
    value: Mapping[str, Any] | MainlandFinancialEligibilityPolicy,
) -> MainlandFinancialEligibilityPolicy:
    """Restore v2 policy data with typed missing/unknown/unsafe failures."""

    if isinstance(value, MainlandFinancialEligibilityPolicy):
        return value
    if not isinstance(value, Mapping):
        raise MainlandFinancialEligibilityPolicyError(
            MainlandFinancialEligibilityPolicyFailureReason.INVALID_POLICY_CONTRACT
        )
    _assert_safe_policy_projection(value)
    identity = value.get("eligibility_policy_version")
    if identity is None:
        raise MainlandFinancialEligibilityPolicyError(
            MainlandFinancialEligibilityPolicyFailureReason.MISSING_POLICY_IDENTITY
        )
    if identity != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2:
        raise MainlandFinancialEligibilityPolicyError(
            MainlandFinancialEligibilityPolicyFailureReason.UNKNOWN_POLICY_IDENTITY
        )
    try:
        return MainlandFinancialEligibilityPolicy.model_validate(value)
    except (TypeError, ValidationError, ValueError) as exc:
        raise MainlandFinancialEligibilityPolicyError(
            MainlandFinancialEligibilityPolicyFailureReason.INVALID_POLICY_CONTRACT
        ) from exc


__all__ = [
    "MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2",
    "MAINLAND_FINANCIAL_REPORTING_CALENDAR_V2",
    "MainlandFinancialEligibilityPolicy",
    "MainlandFinancialEligibilityPolicyError",
    "MainlandFinancialEligibilityPolicyFailureReason",
    "MainlandFinancialListingProvenanceBinding",
    "MainlandFinancialPeriodTargetConstruction",
    "MainlandFinancialPeriodTargetFailureReason",
    "MainlandFinancialPublicationDeadline",
    "MainlandFinancialPublicationCandidateBinding",
    "MainlandFinancialPublicationEvidenceBinding",
    "MainlandFinancialPublicationPeriod",
    "MainlandFinancialPublicationState",
    "MainlandFinancialReportingCalendar",
    "MainlandFinancialSinceListingTargetException",
    "build_mainland_financial_eligibility_policy_v2",
    "mainland_financial_eligibility_policy_signature",
    "restore_mainland_financial_eligibility_policy",
]
