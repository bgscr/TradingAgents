"""Financial quote/semantic boundary regressions using synthetic source rows.

Use the production 13-column CURRENT statement shape with public synthetic
values, actual submission/quote validation and the real Graph admission node.
The original four red cases are retained under the separate diagnostic snapshot.
These checks distinguish financial semantics from exact quote/token support.
"""

import csv
import io
import json
import os
from pathlib import Path

import pytest

from tests.test_evidence_notes import DATE, LIMIT, SYMBOL, claim, note, state, submit
from tradingagents.evidence import decision_ready_material_claims, evaluate_admission_gate
from tradingagents.graph.evidence_gate import create_admission_gate_node
from tradingagents.strategy_registry import (
    DEFAULT_DECISION_HORIZON,
    create_production_decision_policy,
)

FIELDS = (
    "period_end",
    "ann_date",
    "f_ann_date",
    "retrieved_at",
    "observed_at",
    "provider_field",
    "original_value",
    "original_unit",
    "field",
    "value",
    "unit",
    "artifact_identity",
    "value_status",
)
ROWS = (
    {
        "period_end": "2024-12-31",
        "ann_date": "2025-03-20",
        "f_ann_date": "2025-03-20",
        "retrieved_at": "2026-10-16T00:00:00+00:00",
        "observed_at": "2026-10-16T00:00:00+00:00",
        "provider_field": "n_income_attr_p",
        "original_value": "123.45",
        "original_unit": "CNY",
        "field": "net_income",
        "value": "123.45",
        "unit": "CNY",
        "artifact_identity": "a" * 64,
        "value_status": "reported",
    },
    {
        "period_end": "2025-12-31",
        "ann_date": "2026-03-20",
        "f_ann_date": "2026-03-20",
        "retrieved_at": "2026-10-16T00:00:00+00:00",
        "observed_at": "2026-10-16T00:00:00+00:00",
        "provider_field": "n_income_attr_p",
        "original_value": "678.90",
        "original_unit": "CNY",
        "field": "net_income",
        "value": "678.90",
        "unit": "CNY",
        "artifact_identity": "b" * 64,
        "value_status": "reported",
    },
)
stream = io.StringIO()
writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
writer.writeheader()
writer.writerows(ROWS)
TABLE = stream.getvalue().rstrip("\n")
CONTENT = "# SYNTHETIC DIAGNOSTIC DATA — NOT OBSERVED COMPANY DATA\n" + TABLE + "\n"
CASES = {
    "wrong_field": f"{SYMBOL} revenue was 123.45 CNY for 2024-12-31.",
    "wrong_currency": f"{SYMBOL} net income was 123.45 USD for 2024-12-31.",
    "wrong_period": f"{SYMBOL} net income was 123.45 CNY for 2025-12-31.",
    "wrong_scale": f"{SYMBOL} net income was 123.45 million CNY for 2024-12-31.",
}


def inspect_case(name, statement, *, quote=TABLE, content=CONTENT):
    evidence, _ = submit(state(content=content), claims=[claim(statement, quote)])
    policy = create_production_decision_policy()
    assert policy.configuration_blockers == ()
    candidates = policy.candidate_applications(evidence, horizon=DEFAULT_DECISION_HORIZON)
    gate = create_admission_gate_node(policy, DEFAULT_DECISION_HORIZON)(
        {
            "trade_date": DATE,
            "evidence_state": evidence.model_dump(mode="json"),
            "run_id": "run:" + "d" * 64,
        }
    )
    record = {
        "case": name,
        "synthetic_only": True,
        "statement": statement,
        "source_quote": quote,
        "source_rows": ROWS,
        "validation": evidence.claim_validations[0].model_dump(mode="json"),
        "fact_kind": [f.fact_kind for f in evidence.source_facts],
        "fact_semantics": [
            {
                "field": f.canonical_field,
                "unit": f.unit,
                "effective_date": f.effective_date,
                "lineage": f.calculation_lineage,
            }
            for f in evidence.source_facts
        ],
        "provenance_ready_claim_count": len(decision_ready_material_claims(evidence)),
        "preliminary_admission": evaluate_admission_gate(evidence).model_dump(mode="json"),
        "canonical_candidate_count": len(candidates),
        "production_graph_gate": gate["admission_gate"],
        "financial_semantic_qualification_established": False,
    }
    if output := os.environ.get("BOUNDARY_TEST_OUTPUT"):
        path = Path(output) / (name + ".json")
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
    return evidence, gate, record


@pytest.mark.parametrize("name,statement", CASES.items())
def test_wrong_financial_semantics_must_not_look_ready(name, statement):
    evidence, gate, record = inspect_case(name, statement)
    # Both the actual Graph policy and individual premise boundary stay closed.
    assert gate["admission_gate"]["admitted"] is False
    assert record["canonical_candidate_count"] == 0
    assert not decision_ready_material_claims(evidence), (
        "Desired semantic boundary is absent: wrong financial statement is "
        "still returned by the provenance-only ready-claims helper"
    )


def test_correct_quote_is_not_automatically_a_canonical_financial_fact():
    evidence, gate, record = inspect_case(
        "correct", f"{SYMBOL} net income was 123.45 CNY for 2024-12-31."
    )
    assert evidence.claim_validations[0].status.value == "supported"
    assert record["fact_kind"] == ["excerpt"]
    assert record["fact_semantics"] == [
        {"field": "", "unit": "", "effective_date": "", "lineage": None}
    ]
    assert gate["admission_gate"]["admitted"] is False


@pytest.mark.parametrize(
    "name,statement,quote",
    [
        ("wrong_amount", f"{SYMBOL} net income was 999.99 CNY.", TABLE),
        ("absent_quote", f"{SYMBOL} net income was 123.45 CNY.", "not in the source"),
        ("wrong_percent", f"{SYMBOL} net income return was 123.45%.", TABLE),
    ],
)
def test_existing_numeric_and_quote_fail_closed_controls(name, statement, quote):
    evidence, gate, _ = inspect_case(name, statement, quote=quote)
    assert evidence.claim_validations[0].status.value == "unsupported"
    assert decision_ready_material_claims(evidence) == ()
    assert gate["admission_gate"]["admitted"] is False


def test_bound_limitation_is_retained_without_becoming_financial_fact():
    evidence, _ = submit(state(content=CONTENT + LIMIT), notes=[note()])
    assert len(evidence.evidence_notes) == 1
    bound = evidence.evidence_notes[0]
    assert bound.binding_status == "source_bound_quote"
    assert bound.semantic_status == "not_established"
    assert bound.decision_qualified is False
    assert evidence.material_claims == evidence.source_facts == ()


def test_bank_metric_absence_is_not_a_new_global_policy_requirement():
    from tradingagents.evidence import EvidenceCapability, capability_profile_for

    profile = capability_profile_for("equity")
    assert profile.required_capabilities == (EvidenceCapability.MARKET_SNAPSHOT,)
    rules = create_production_decision_policy().rules
    fields = {f for r in rules for f in r.required_canonical_fields}
    assert not fields.intersection(
        {
            "net_interest_margin",
            "nonperforming_loan_ratio",
            "capital_adequacy_ratio",
            "provision_coverage_ratio",
        }
    )
