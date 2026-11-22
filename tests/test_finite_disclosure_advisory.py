"""Synthetic isolated tests: no provider, filesystem cache, model or credentials."""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel, ValidationError

ROOT = Path(os.environ.get("FINITE_TEST_ROOT", Path(__file__).resolve().parents[1]))
SELECTION = {
    "evidence_ids": ["synthetic-member-1"],
    "statement_ids": ["synthetic-field-1"],
    "relationship": "cites",
}
ROLES = (
    "bull",
    "bear",
    "research_manager",
    "trader",
    "aggressive",
    "conservative",
    "neutral",
    "portfolio_manager",
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


class SyntheticContext:
    def selection_catalog(self, role):
        if role not in ROLES:
            raise ValueError("unregistered role")
        return {"evidence_ids": ["synthetic-member-1"], "statement_ids": ["synthetic-field-1"]}


def require_context(state):
    if state.get("finite_disclosure_context") != {"binding": "synthetic-trusted-pin"}:
        raise ValueError("untrusted synthetic binding")
    return SyntheticContext()


def render_selection(role, selection, state):
    catalog = require_context(state).selection_catalog(role)
    if set(selection) != {"evidence_ids", "statement_ids", "relationship"}:
        raise ValueError("not closed")
    for key in ("evidence_ids", "statement_ids"):
        if not set(selection[key]) <= set(catalog[key]):
            raise ValueError("unregistered identifier")
    if selection["relationship"] not in {"cites", "scope_limited"}:
        raise ValueError("unregistered relationship")
    return "synthetic-field-1: observed 7 CNY; synthetic-member-1; bounded scope."


def runtime_module():
    module = types.ModuleType("tradingagents.finite_disclosure_runtime")
    module.require_context = require_context
    module.render_selection = render_selection
    module.is_finite_state = lambda state: "finite_disclosure_context" in state
    return module


class FakeModel:
    def __init__(self, result=None, error=None):
        self.result = copy.deepcopy(SELECTION if result is None else result)
        self.error = error
        self.calls = 0
        self.prompts = []
        self.bound = []

    def with_structured_output(self, schema, **kwargs):
        self.bound.append((schema, kwargs))
        return self

    def invoke(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


def initial_state():
    return {"finite_disclosure_context": {"binding": "synthetic-trusted-pin"}}


class FiniteAdvisoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {"tradingagents.finite_disclosure_runtime": runtime_module()}):
            cls.advisory = load_module(
                "isolated_finite_advisory",
                ROOT / "tradingagents/agents/finite_disclosure_advisory.py",
            )

    def test_nonempty_positive_through_every_preserved_role(self):
        state = initial_state()
        for role in ROLES:
            model = FakeModel()
            update = self.advisory.finite_advisory_update(model, role, state)
            self.assertEqual(model.calls, 1)
            self.assertEqual(
                len(update["finite_advisory_trace"]),
                len(state.get("finite_advisory_trace", [])) + 1,
            )
            state.update(update)
        self.assertEqual(state["investment_debate_state"]["count"], 2)
        self.assertEqual(state["risk_debate_state"]["count"], 3)
        self.assertIn("observed 7 CNY", state["portfolio_advisory"])
        self.assertNotIn("final_trade_decision", state)
        self.assertNotIn("direction_selection", state)
        self.assertEqual(len(self.advisory.validate_finite_inputs(state)), 8)

    def test_closed_dto_rejects_prose_numbers_relationships_and_duplicates(self):
        mutations = [
            {**SELECTION, "narrative": "invented conclusion"},
            {**SELECTION, "amount": 7},
            {**SELECTION, "relationship": "supports_buy"},
            {**SELECTION, "evidence_ids": [1]},
            {**SELECTION, "statement_ids": []},
            {**SELECTION, "statement_ids": ["synthetic-field-1"] * 2},
            {**SELECTION, "statement_ids": ["synthetic-field-1 "]},
        ]
        for selection in mutations:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.advisory.FiniteAdvisorySelection.model_validate(selection)

    def test_model_construct_does_not_bypass_boundary(self):
        unsafe = self.advisory.FiniteAdvisorySelection.model_construct(
            evidence_ids=[1],
            statement_ids=["synthetic-field-1"],
            relationship="cites",
        )
        with self.assertRaises(ValidationError):
            self.advisory.FiniteAdvisorySelection.model_validate(unsafe)

    def test_unknown_identifiers_and_catalog_override_fail(self):
        for change in (
            {"statement_ids": ["invented-bank-risk"]},
            {"evidence_ids": ["invented-member"]},
        ):
            model = FakeModel({**SELECTION, **change})
            with self.assertRaises(ValueError):
                self.advisory.finite_advisory_update(model, "bull", initial_state())
            self.assertEqual(model.calls, 1)
        state = initial_state()
        state["finite_disclosure_context"]["catalog"] = SELECTION
        model = FakeModel()
        with self.assertRaises(ValueError):
            self.advisory.finite_advisory_update(model, "bull", state)
        self.assertEqual(model.calls, 0)

    def test_no_retry_no_prose_fallback_and_sanitized_error(self):
        model = FakeModel(error=RuntimeError("provider secret payload"))
        with self.assertRaisesRegex(ValueError, "^finite advisory structured selection failed$"):
            self.advisory.finite_advisory_update(model, "trader", initial_state())
        self.assertEqual(model.calls, 1)
        model = FakeModel({"raw": "injected", "parsed": SELECTION, "parsing_error": "invalid"})
        with self.assertRaises(ValueError):
            self.advisory.finite_advisory_update(model, "bull", initial_state())
        self.assertEqual(model.calls, 1)

    def test_parsed_envelope_discards_raw_prose(self):
        model = FakeModel({"raw": "UNTRUSTED RAW", "parsed": SELECTION, "parsing_error": None})
        update = self.advisory.finite_advisory_update(model, "bull", initial_state())
        self.assertNotIn("UNTRUSTED RAW", str(update))
        self.assertIn("observed 7 CNY", str(update))

    def test_raw_reports_memory_and_messages_never_enter_prompt(self):
        state = initial_state()
        for key in (
            "market_report",
            "fundamentals_report",
            "sentiment_report",
            "news_report",
            "past_context",
            "messages",
        ):
            state[key] = "POISON-UNREGISTERED-CONCLUSION"
        model = FakeModel()
        self.advisory.finite_advisory_update(model, "bull", state)
        self.assertNotIn("POISON", model.prompts[0])

    def test_tampered_history_count_and_trace_rejected_before_model(self):
        state = initial_state()
        state.update(self.advisory.finite_advisory_update(FakeModel(), "bull", state))
        for mutate in (
            lambda s: s["investment_debate_state"].update(history="invented narrative"),
            lambda s: s["investment_debate_state"].update(count=True),
            lambda s: s["finite_advisory_trace"][0].update(narrative="invented"),
            lambda s: s["finite_advisory_trace"][0]["selection"].update(statement_ids=["unknown"]),
            lambda s: s.update(investment_plan="invented plan"),
        ):
            changed = copy.deepcopy(state)
            mutate(changed)
            model = FakeModel()
            with self.assertRaises(ValueError):
                self.advisory.finite_advisory_update(model, "bear", changed)
            self.assertEqual(model.calls, 0)

    def test_trace_is_bounded(self):
        state = initial_state()
        state["finite_advisory_trace"] = [{"role": "bull", "selection": SELECTION}] * 129
        model = FakeModel()
        with self.assertRaises(ValueError):
            self.advisory.finite_advisory_update(model, "bull", state)
        self.assertEqual(model.calls, 0)

    def test_actual_node_factories_take_finite_branch(self):
        # Imports are inert synthetic seams. Execute the real retained node code.
        utils = types.ModuleType("tradingagents.agents.utils.agent_utils")
        utils.get_instrument_context_from_state = lambda *a: self.fail("legacy prompt path")
        utils.get_language_instruction = lambda *a: self.fail("legacy prompt path")
        structured = types.ModuleType("tradingagents.agents.utils.structured")
        for name in ("bind_structured", "bind_required_structured"):
            setattr(structured, name, lambda *a: None)
        for name in ("invoke_required_structured", "invoke_structured_or_freetext"):
            setattr(structured, name, lambda *a: self.fail("legacy invocation path"))
        schemas = types.ModuleType("tradingagents.agents.schemas")
        for name in (
            "ResearchPlan",
            "TraderProposal",
            "PortfolioDecision",
            "PortfolioDecisionRevision",
            "PortfolioDecisionSelection",
        ):
            setattr(schemas, name, BaseModel)
        for name in ("render_research_plan", "render_trader_proposal", "render_pm_decision"):
            setattr(schemas, name, lambda *a: self.fail("legacy renderer path"))
        evidence = types.ModuleType("tradingagents.evidence")
        for name in (
            "AnalysisOutcome",
            "AnalysisOutcomeReason",
            "DecisionGateResult",
            "DraftThesis",
            "EvidenceReadiness",
            "EvidenceState",
            "analysis_diagnostic_codes",
            "analysis_outcome_publication",
            "decision_ready_material_claims",
            "evaluate_decision_gate",
        ):
            setattr(evidence, name, BaseModel)
        messages = types.ModuleType("langchain_core.messages")
        messages.AIMessage = lambda **kwargs: kwargs
        replacements = {
            "tradingagents.agents.finite_disclosure_advisory": self.advisory,
            utils.__name__: utils,
            structured.__name__: structured,
            schemas.__name__: schemas,
            evidence.__name__: evidence,
            messages.__name__: messages,
        }
        modules = (
            ("researchers/bull_researcher", "create_bull_researcher"),
            ("researchers/bear_researcher", "create_bear_researcher"),
            ("managers/research_manager", "create_research_manager"),
            ("trader/trader", "create_trader"),
            ("risk_mgmt/aggressive_debator", "create_aggressive_debator"),
            ("risk_mgmt/conservative_debator", "create_conservative_debator"),
            ("risk_mgmt/neutral_debator", "create_neutral_debator"),
            ("managers/portfolio_manager", "create_portfolio_manager"),
        )
        state = initial_state()
        with patch.dict(sys.modules, replacements):
            for path, factory in modules:
                module = load_module(
                    "isolated_" + factory, ROOT / "tradingagents/agents" / (path + ".py")
                )
                model = FakeModel()
                update = getattr(module, factory)(model)(state)
                self.assertEqual(model.calls, 1)
                state.update(update)
        self.assertEqual(len(state["finite_advisory_trace"]), 8)
        self.assertIn("observed 7 CNY", state["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
