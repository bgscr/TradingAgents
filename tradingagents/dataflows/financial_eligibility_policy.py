"""Closed mainland financial reporting-period eligibility policy contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
    "MainlandFinancialPublicationDeadline",
    "MainlandFinancialPublicationState",
    "MainlandFinancialReportingCalendar",
    "build_mainland_financial_eligibility_policy_v2",
    "mainland_financial_eligibility_policy_signature",
    "restore_mainland_financial_eligibility_policy",
]
