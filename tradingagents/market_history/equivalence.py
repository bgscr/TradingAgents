from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class MainlandEquivalenceScenario(str, Enum):
    PRIMARY_PROVIDER = "primary_provider"
    BAOSTOCK_FALLBACK = "baostock_fallback"
    YAHOO_FALLBACK = "yahoo_fallback"
    CONFIRMED_SUSPENSION = "confirmed_suspension"
    REPRESENTATION_NOISE = "representation_noise"
    IN_RANGE_GAP = "in_range_gap"
    FACTOR_REVISION = "factor_revision"
    RATE_LIMIT_FALLBACK = "rate_limit_fallback"
    STORE_CORRUPTION_DEGRADATION = "store_corruption_degradation"
    CONCURRENT_WRITE = "concurrent_write"
    CRASH_RECOVERY = "crash_recovery"


DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS = tuple(MainlandEquivalenceScenario)


@dataclass(frozen=True)
class SnapshotEquivalenceSubject:
    instrument_identity_id: str
    provider_dataset_id: str
    adjustment_basis: str
    effective_dates: tuple[str, ...]
    eligible_observation_ids: tuple[str, ...]
    frame_sha256: str
    derived_fact_digests: tuple[str, ...]
    lineage_digests: tuple[str, ...]


@dataclass(frozen=True)
class MainlandEquivalenceComparison:
    identity_matches: bool
    provider_matches: bool
    adjustment_basis_matches: bool
    effective_range_matches: bool
    eligible_observations_match: bool
    frame_digest_matches: bool
    derived_facts_match: bool
    lineage_matches: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.identity_matches,
                self.provider_matches,
                self.adjustment_basis_matches,
                self.effective_range_matches,
                self.eligible_observations_match,
                self.frame_digest_matches,
                self.derived_facts_match,
                self.lineage_matches,
            )
        )


def compare_snapshot_equivalence(
    live: SnapshotEquivalenceSubject,
    reconstructed: SnapshotEquivalenceSubject,
) -> MainlandEquivalenceComparison:
    return MainlandEquivalenceComparison(
        identity_matches=(
            live.instrument_identity_id == reconstructed.instrument_identity_id
        ),
        provider_matches=(live.provider_dataset_id == reconstructed.provider_dataset_id),
        adjustment_basis_matches=(
            live.adjustment_basis == reconstructed.adjustment_basis
        ),
        effective_range_matches=(live.effective_dates == reconstructed.effective_dates),
        eligible_observations_match=(
            live.eligible_observation_ids == reconstructed.eligible_observation_ids
        ),
        frame_digest_matches=(live.frame_sha256 == reconstructed.frame_sha256),
        derived_facts_match=(
            live.derived_fact_digests == reconstructed.derived_fact_digests
        ),
        lineage_matches=(live.lineage_digests == reconstructed.lineage_digests),
    )


@dataclass(frozen=True)
class MainlandCompatibilityReport:
    cutover_ready: bool
    missing_scenarios: tuple[MainlandEquivalenceScenario, ...]
    failed_scenarios: tuple[MainlandEquivalenceScenario, ...]
    comparisons: Mapping[
        MainlandEquivalenceScenario,
        MainlandEquivalenceComparison,
    ]


class MainlandCompatibilityGate:
    def __init__(
        self,
        required_scenarios: tuple[
            MainlandEquivalenceScenario, ...
        ] = DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS,
    ) -> None:
        if not required_scenarios:
            raise ValueError("at least one mainland equivalence scenario is required")
        self.required_scenarios = tuple(dict.fromkeys(required_scenarios))

    def evaluate(
        self,
        comparisons: Mapping[
            MainlandEquivalenceScenario,
            MainlandEquivalenceComparison,
        ],
    ) -> MainlandCompatibilityReport:
        copied = {
            scenario: comparison
            for scenario, comparison in comparisons.items()
            if scenario in self.required_scenarios
        }
        missing = tuple(
            scenario
            for scenario in self.required_scenarios
            if scenario not in copied
        )
        failed = tuple(
            scenario
            for scenario in self.required_scenarios
            if scenario in copied and not copied[scenario].passed
        )
        return MainlandCompatibilityReport(
            cutover_ready=not missing and not failed,
            missing_scenarios=missing,
            failed_scenarios=failed,
            comparisons=MappingProxyType(copied),
        )
