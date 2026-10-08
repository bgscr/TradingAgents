import copy
import importlib.util

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    CNINFO_LISTING_PUBLICATION_CANDIDATE_PROFILE,
    MainlandCapability,
    preflight_mainland_capability_routing,
    prepare_mainland_listing_publication_candidate_plan,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceCapability,
    InstrumentKind,
    capability_profile_for,
)


def _plan_1_4(monkeypatch: pytest.MonkeyPatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ("statements", "financial_indicators"),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration("601328.SS", config=copy.deepcopy(DEFAULT_CONFIG))
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert preflight.plan is not None
    return prepare_mainland_temporal_capability_routing_plan(preflight.plan)


def test_listing_publication_capability_is_forward_only_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _plan_1_4(monkeypatch)

    with pytest.raises(KeyError):
        predecessor.route_for(MainlandCapability.LISTING_PUBLICATION_EVIDENCE)

    candidate = prepare_mainland_listing_publication_candidate_plan(predecessor)

    assert candidate.plan_version == "1.5"
    assert candidate.route_for(MainlandCapability.LISTING_PUBLICATION_EVIDENCE) == (
        "cninfo",
    )
    assert candidate.qualification_profile == predecessor.qualification_profile
    assert (
        candidate.listing_publication_candidate_profile
        == CNINFO_LISTING_PUBLICATION_CANDIDATE_PROFILE
    )
    assert candidate.mode.value == "qualified_v1_shadow"
    assert candidate.temporal_contract_bindings is not None
    assert (
        candidate.temporal_contract_bindings.statement_routing_result_version
        == "financial-statement-routing-result-v1"
    )
    assert (
        candidate.temporal_contract_bindings.statement_manifest_version
        == "financial-manifest-v2"
    )
    assert EvidenceCapability.LISTING_PUBLICATION_EVIDENCE.value == (
        "listing_publication_evidence"
    )
    assert (
        EvidenceCapability.LISTING_PUBLICATION_EVIDENCE
        not in capability_profile_for(InstrumentKind.EQUITY).all_capabilities
    )


def test_listing_publication_capability_rejects_unsupported_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = prepare_mainland_listing_publication_candidate_plan(
        _plan_1_4(monkeypatch)
    )
    payload = candidate.signature_payload()
    routes = list(payload["routes"])
    routes[-1] = {
        "capability": "listing_publication_evidence",
        "providers": ["baostock"],
    }
    payload["routes"] = routes

    with pytest.raises(ValueError):
        type(candidate)(**payload, plan_signature=candidate.plan_signature)
