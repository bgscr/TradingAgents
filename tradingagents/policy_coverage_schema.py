"""Versioned schema descriptor; no candidate, approval time or self-reference."""

from tradingagents import policy_coverage_contract as evidence, policy_coverage_models as qualified
from tradingagents.policy_qualified_contract import content_identity


def coverage_contract_descriptor():
    from tradingagents.policy_qualified_native import (
        CoverageNativeCompletion,
        PreparedCoverageProtectionAttempt,
    )
    from tradingagents.qualified_routing_composition import (
        CoverageProductionQualifiedRoutingPreparation,
        CoverageProductionQualifiedRoutingSourceBinding,
    )
    from tradingagents.runtime_protection import CoverageAttemptInputs, CoverageProtectionAttempt
    from tradingagents.runtime_protection_handoff import CoverageHandoffContract

    models = [
        evidence.HistoricalCoverageScope,
        evidence.CoverageMeasurement,
        evidence.CoverageAssessment,
        *qualified._MODELS.values(),
        CoverageProductionQualifiedRoutingSourceBinding,
        CoverageProductionQualifiedRoutingPreparation,
        CoverageAttemptInputs,
        CoverageProtectionAttempt,
        PreparedCoverageProtectionAttempt,
        CoverageNativeCompletion,
        CoverageHandoffContract,
    ]
    definitions = {}
    entries = {}
    for model in models:
        schema = model.model_json_schema()
        nested = schema.pop("$defs", {})
        for name, definition in {**nested, model.__name__: schema}.items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("ambiguous coverage schema definition: " + name)
            definitions[name] = definition
        entries[model.__name__] = {"$ref": "#/$defs/" + model.__name__}
    body = {
        "contract_version": "policy-coverage-result-contract-v2",
        "entry_points": entries,
        "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": definitions},
        "complete_current_inventory_validator": "tradingagents.policy_coverage_contract.validate_current_inventory",
        "known_field_validator": "tradingagents.policy_coverage_contract.compare_known_fields",
        "current_window_validator": "tradingagents.policy_coverage_contract.compare_current_inventories",
        "authority": "distinct-v2-policy-and-authenticated-owner-with-irreversible-baseline-claim",
        "fresh_identical_state_acquisition": {
            "intake_version": "policy-owner-evidence-intake-v3",
            "descriptor_kind": "artifact-acquisition",
            "registration_key": "exact-sealed-descriptor-identity",
            "observation_field": "artifact_descriptor_identity",
            "payload_identity_unchanged": True,
            "fresh_separate_descriptor_authentication_required": True,
            "old_acquisition_replay_or_renewal": False,
            "old_v1_v2_semantics_unchanged": True,
        },
        "historical_coverage": "UNKNOWN",
        "execution_authorized": False,
    }
    return {
        **body,
        "contract_identity": content_identity("policy-coverage-result-contract:v2", body),
    }
