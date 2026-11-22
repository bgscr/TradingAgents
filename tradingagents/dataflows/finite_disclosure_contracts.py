"""Closed finite-cache read evidence, separate from physical acquisitions."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FiniteSourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    table: Literal["balancesheet", "income", "cashflow"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def original_ttl(self):
        if (
            self.observed_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at != self.observed_at + timedelta(seconds=86400)
        ):
            raise ValueError("finite_original_ttl_binding")
        return self


class FiniteDisclosureCacheEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract: Literal["finite-disclosure-cache-evidence-v2"] = "finite-disclosure-cache-evidence-v2"
    profile: Literal["601328-2026H1-v2"] = "601328-2026H1-v2"
    release_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(pattern=r"^finite-cache:v2:[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1, max_length=160)
    tool_name: Literal[
        "get_fundamentals", "get_balance_sheet", "get_income_statement", "get_cashflow"
    ]
    capability: Literal["company_financials"] = "company_financials"
    read_at: datetime
    historical_observations: tuple[FiniteSourceObservation, ...] = Field(min_length=3, max_length=3)
    new_physical_requests: Literal[0] = 0
    full_announcement_window_coverage: Literal[False] = False
    strict_pit_eligible: Literal[False] = False
    selected_artifact: dict

    @model_validator(mode="before")
    @classmethod
    def exact_flags(cls, value):
        if isinstance(value, dict):
            if type(value.get("new_physical_requests", 0)) is not int:
                raise ValueError("finite_request_count_type")
            for key in ("full_announcement_window_coverage", "strict_pit_eligible"):
                if type(value.get(key, False)) is not bool:
                    raise ValueError("finite_coverage_flag_type")
        return value

    @model_validator(mode="after")
    def binding(self):
        from tradingagents.evidence import SourceArtifact

        artifact = SourceArtifact.model_validate(self.selected_artifact)
        if (
            artifact.tool_call_id != self.tool_call_id
            or artifact.tool_name != self.tool_name
            or artifact.source_ref != self.source_ref
            or self.read_at.tzinfo is None
            or len({x.table for x in self.historical_observations}) != 3
            or any(
                not x.observed_at <= self.read_at < x.expires_at
                for x in self.historical_observations
            )
        ):
            raise ValueError("finite_cache_read_binding")
        return self

    @property
    def acquisition_outcomes(self):
        return ()

    def assert_fresh(self, now=None):
        now = now or datetime.now(timezone.utc)
        if self.read_at > now or any(
            not x.observed_at <= now < x.expires_at for x in self.historical_observations
        ):
            raise ValueError("finite_cache_expired")
        return self
