"""Synthetic publication seams only; no real profile, model, cache or store."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(os.environ.get("FINITE_TEST_ROOT", Path(__file__).resolve().parents[1]))
PROFILE = "601328-2026H1-v2"
CONFIG = {"finite_disclosure_profile": PROFILE}
BINDING = {
    "contract": "finite-run-context-v2",
    "profile": PROFILE,
    "release_sha256": "a" * 64,
    "scope_sha256": "b" * 64,
}
SELECTION = {
    "evidence_ids": ["member-1"],
    "statement_ids": ["statement-1"],
    "relationship": "cites",
}
VIEW = {
    "rows": [
        {
            "table": "income",
            "period": "20260630",
            "field": "revenue",
            "raw_api_value": "7",
            "derived_disclosure_contribution": "7",
            "pdf_million": "7",
            "pdf_page": 1,
            "field_meaning": "synthetic observation",
            "missing": False,
        }
    ]
}
READ = {
    "contract": "finite-disclosure-cache-evidence-v2",
    "profile": PROFILE,
    "release_sha256": "a" * 64,
    "view_sha256": "c" * 64,
    "scope_sha256": "b" * 64,
    "source_ref": "finite-cache:v2:" + "c" * 64,
    "tool_call_id": "synthetic-call-1",
    "tool_name": "get_fundamentals",
    "capability": "company_financials",
    "read_at": "2026-10-22T12:00:00+00:00",
    "historical_observations": [
        {
            "table": x,
            "source_sha256": "d" * 64,
            "observed_at": "2026-10-21T18:00:00+00:00",
            "expires_at": "2026-10-22T18:00:00+00:00",
        }
        for x in ("balancesheet", "income", "cashflow")
    ],
    "new_physical_requests": 0,
    "full_announcement_window_coverage": False,
    "strict_pit_eligible": False,
    "selected_artifact": {"artifact_sha256": "e" * 64, "raw_text": "trusted synthetic artifact"},
}


def module(name, **attrs):
    result = ModuleType(name)
    result.__dict__.update(attrs)
    return result


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


class Envelope:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.__dict__.update(value)

    @classmethod
    def model_validate(cls, value):
        return value if isinstance(value, cls) else cls(value)

    def model_dump(self, **kwargs):
        return copy.deepcopy(self.value)


class FakeStore:
    def __init__(self, root="synthetic-store"):
        self.root = Path(root)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def new_directory(self, relative):
        self.calls.append(("mkdir", relative))

    def write_bytes(self, relative, value):
        self.calls.append(("write", relative, value))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules)
        self.modules.start()
        for name in (
            "tradingagents",
            "tradingagents.dataflows",
            "tradingagents.agents",
            "tradingagents.agents.utils",
            "tradingagents.graph",
            "cli",
        ):
            sys.modules[name] = module(name, __path__=[])
        admission = module(
            "tradingagents.dataflows.finite_disclosure_admission",
            Rejected=ValueError,
            PERIODS={},
            CORE={},
            SCOPE={"synthetic": True},
            LIMITS=("no valuation", "no strict PIT"),
        )
        cache = module(
            "tradingagents.dataflows.finite_disclosure_cache",
            PROFILE=PROFILE,
            enabled=lambda cfg: cfg.get("finite_disclosure_profile") is not None,
            load_view=lambda *a, **k: ({}, {}, VIEW),
            installed_profile=lambda: {"release_sha256": "a" * 64},
        )
        sys.modules[admission.__name__] = admission
        sys.modules[cache.__name__] = cache
        sys.modules["tradingagents.dataflows.config"] = module(
            "tradingagents.dataflows.config", get_config=lambda: {}
        )
        sys.modules["tradingagents.evidence"] = module(
            "tradingagents.evidence",
            EvidenceState=Envelope,
            AnalysisOutcome=Envelope,
            render_analysis_outcome=lambda outcome: "Bounded Analysis Outcome",
        )
        sys.modules["tradingagents.evidence_diagnostics"] = module(
            "tradingagents.evidence_diagnostics",
            DIAGNOSTIC_LABELS={},
            source_diagnostic=lambda *a: None,
            claim_diagnostic=lambda *a: None,
        )
        sys.modules["tradingagents.terminal_contract"] = module(
            "tradingagents.terminal_contract",
            validated_trading_decision_from_state=lambda state: None,
            authorized_trading_decision_from_state=lambda *a, **k: SimpleNamespace(
                rating=SimpleNamespace(value="Hold")
            ),
        )

        def require_context(state):
            if state.get("finite_disclosure_context") != BINDING:
                raise ValueError("synthetic untrusted context")
            return SimpleNamespace(view=VIEW, selection_catalog=lambda role: SELECTION)

        def render_selection(role, selection, state):
            require_context(state)
            if selection != SELECTION:
                raise ValueError("synthetic unregistered IDs")
            return role + ": bounded synthetic observation reference"

        def validate_envelope(raw):
            if raw != READ:
                raise ValueError("synthetic envelope integrity failure")
            return Envelope(raw)

        def require_report(role, report, state):
            require_context(state)
            expected = (
                self.output.render_report(VIEW)
                if role == "fundamentals"
                else render_selection(role, state["finite_analyst_selections"][role], state)
            )
            if report != expected:
                raise ValueError("synthetic unbound report")

        runtime = module(
            "tradingagents.finite_disclosure_runtime",
            require_context=require_context,
            render_selection=render_selection,
            require_analyst_report=require_report,
            validate_finite_envelope=validate_envelope,
            is_finite_state=lambda state: state.get("finite_disclosure_context") is not None,
        )
        sys.modules[runtime.__name__] = runtime
        advisory_path = ROOT / "tradingagents/agents/finite_disclosure_advisory.py"
        if not advisory_path.exists():
            advisory_path = (
                ROOT.parent / "downstream-stage/tradingagents/agents/finite_disclosure_advisory.py"
            )
        self.advisory = load("tradingagents.agents.finite_disclosure_advisory", advisory_path)
        self.output = load(
            "tradingagents.dataflows.finite_disclosure_output",
            ROOT / "tradingagents/dataflows/finite_disclosure_output.py",
        )
        self.presentation = load(
            "tradingagents.report_presentation", ROOT / "tradingagents/report_presentation.py"
        )
        sys.modules["tradingagents.report_storage"] = module(
            "tradingagents.report_storage", ReportStore=FakeStore
        )
        self.exports = load(
            "tradingagents.report_exports", ROOT / "tradingagents/report_exports.py"
        )

    def tearDown(self):
        self.modules.stop()

    def state(self):
        state = {
            "company_of_interest": "601328.SH",
            "trade_date": "2026-10-16",
            "finite_disclosure_context": copy.deepcopy(BINDING),
            "finite_analyst_selections": {
                role: copy.deepcopy(SELECTION) for role in self.output.ANALYST_REPORTS
            },
            "evidence_state": {"finite_cache_reads": [copy.deepcopy(READ)], "material_claims": []},
            "analysis_outcome_contract": {"synthetic": "blocked"},
            "analysis_outcome": "Bounded Analysis Outcome",
            "run_id": "run:synthetic",
            "decision_audit_sha256": "f" * 64,
        }
        for role, field in self.output.ANALYST_REPORTS.items():
            state[field] = (
                self.output.render_report(VIEW)
                if role == "fundamentals"
                else role + ": bounded synthetic observation reference"
            )
        entries = [
            self.advisory.FiniteAdvisoryEntry(role=role, selection=SELECTION)
            for role in (
                "bull",
                "bear",
                "research_manager",
                "trader",
                "aggressive",
                "conservative",
                "neutral",
                "portfolio_manager",
            )
        ]
        state.update(self.advisory._project_trace(entries, state))
        state["finite_advisory_trace"] = [entry.model_dump(mode="json") for entry in entries]
        return state

    def test_positive_all_stages_and_truthful_cache_projection(self):
        state = self.state()
        self.assertTrue(self.output.validate_publication(state, CONFIG, terminal=True))
        projection = self.output.publication_projection(state, CONFIG)
        self.assertEqual(projection["finite_disclosure_context"], BINDING)
        read = projection["finite_cache_reads"][0]
        self.assertEqual(read["new_physical_requests"], 0)
        self.assertFalse(read["strict_pit_eligible"])
        self.assertNotIn("selected_artifact", read)
        self.assertEqual(len(projection["finite_advisory_trace"]), 8)
        documents, stages = self.output.finite_export_documents(state, config=CONFIG)
        self.assertEqual(len(stages), 12)
        self.assertIn("synthetic observation", documents["complete_report.md"])
        self.assertIn("finite_publication.json", documents)

    def test_reports_trace_and_claim_injection_rejected(self):
        mutations = (
            lambda s: s.update(news_report="unsupported bank risk conclusion"),
            lambda s: s["investment_debate_state"].update(bull_history="valuation target 999"),
            lambda s: s.update(analysis_outcome="model free prose"),
            lambda s: s["evidence_state"]["material_claims"].append(
                {"analyst": "fundamentals", "statement": "TTM grew", "source_quote": "TTM grew"}
            ),
            lambda s: s["finite_advisory_trace"][0]["selection"].update(statement_ids=["unknown"]),
        )
        for mutate in mutations:
            state = self.state()
            mutate(state)
            with self.assertRaises(ValueError):
                self.output.validate_publication(state, CONFIG)

    def test_profile_context_and_ledger_cannot_be_dropped(self):
        for key in ("finite_disclosure_context", "finite_advisory_trace"):
            state = self.state()
            state.pop(key)
            with self.assertRaises(ValueError):
                self.output.validate_publication(state, CONFIG)
        state = self.state()
        state["evidence_state"]["finite_cache_reads"] = []
        with self.assertRaises(ValueError):
            self.output.validate_publication(state, CONFIG, terminal=True)
        with self.assertRaises(ValueError):
            self.output.validate_publication(self.state(), {})

    def test_cache_envelope_and_duplicate_read_fail_closed(self):
        state = self.state()
        state["evidence_state"]["finite_cache_reads"][0]["new_physical_requests"] = 1
        with self.assertRaises(ValueError):
            self.output.validate_publication(state, CONFIG)
        state = self.state()
        state["evidence_state"]["finite_cache_reads"].append(copy.deepcopy(READ))
        with self.assertRaises(ValueError):
            self.output.validate_publication(state, CONFIG)
        with self.assertRaises(ValueError):
            self.output.cache_read_projection(self.state()["evidence_state"], config=CONFIG)

    def test_export_revalidates_exact_document_map_before_writes(self):
        state = self.state()
        store = FakeStore()
        documents, stages = self.output.finite_export_documents(state, config=CONFIG)
        identity = SimpleNamespace(
            run_id=state["run_id"], audit_digest=state["decision_audit_sha256"]
        )
        poisoned = {**documents, "injected.md": "unsupported conclusion"}
        with self.assertRaises(ValueError):
            self.exports.publish_report_export(
                store, identity, "English", poisoned, stages, state=state, config=CONFIG
            )
        self.assertEqual(store.calls, [])
        self.exports.publish_report_export(
            store, identity, "English", documents, stages, state=state, config=CONFIG
        )
        manifests = [
            call[2]
            for call in store.calls
            if call[0] == "write" and call[1].endswith("manifest.json")
        ]
        self.assertEqual(
            json.loads(manifests[0])["finite_publication"]["finite_disclosure_context"], BINDING
        )

    def test_draft_rejects_prose_and_accepts_bound_stage(self):
        state = self.state()
        store = FakeStore()
        with patch.object(self.exports, "ReportStore", return_value=store):
            with self.assertRaises(ValueError):
                self.exports.write_advisory_draft(
                    "fake",
                    "news_report.md",
                    "unsupported conclusion",
                    state=state,
                    config=CONFIG,
                    section_name="news_report",
                )
            self.assertEqual(store.calls, [])
            text = self.presentation.advisory_markdown(
                "news_report", state["news_report"], "English"
            )
            self.exports.write_advisory_draft(
                "fake",
                "news_report.md",
                text,
                state=state,
                config=CONFIG,
                section_name="news_report",
            )
        self.assertTrue(store.calls)

    def test_raw_callback_payloads_never_reach_runtime_artifacts(self):
        sys.modules["tradingagents.run_telemetry"] = module(
            "tradingagents.run_telemetry",
            RunTelemetryProjection=Envelope,
            acquisition_summary_from_projection=lambda p: {},
        )
        runtime = load("cli.runtime_artifacts", ROOT / "cli/runtime_artifacts.py")
        writer = runtime.RuntimeArtifactWriter.__new__(runtime.RuntimeArtifactWriter)
        writer._finite_mode = True
        writer._publication_config = CONFIG
        events = []
        writer._enqueue = events.append
        writer.record_message("POISON", "POISON", "POISON")
        writer.record_tool_call("POISON", "POISON", {"POISON": "raw model"})
        writer.record_tool_result("POISON", {"POISON": "raw model"})
        self.assertNotIn("POISON", str(events))
        writer.record_validated_state(self.state())
        self.assertIn("bounded synthetic observation", str(events))
        with patch.object(runtime, "_persist_encoded_runtime_artifact") as persist:
            runtime.persist_runtime_artifact(Path("fake"), {"POISON": "raw model"}, config=CONFIG)
            self.assertNotIn(b"POISON", persist.call_args.args[1])

    def test_reflection_requires_binding_and_never_invokes_model(self):
        reflection = load(
            "tradingagents.graph.reflection", ROOT / "tradingagents/graph/reflection.py"
        )
        reflector = reflection.Reflector(
            SimpleNamespace(invoke=lambda *a: self.fail("real invocation")), config=CONFIG
        )
        with self.assertRaises(ValueError):
            reflector.reflect_on_final_decision("unbound decision", 2.0, 3.0)
        # Bind a synthetic positive decision seam without testing the strategy gate.
        state = self.state()
        state.pop("analysis_outcome")
        state.pop("analysis_outcome_contract")
        state["final_trade_decision"] = "bounded typed decision"
        state["trading_decision"] = {"synthetic": True}
        sys.modules["tradingagents.terminal_contract"].validated_trading_decision_from_state = (
            lambda s: object() if s["final_trade_decision"] == "bounded typed decision" else None
        )
        result = reflector.reflect_on_final_decision(
            "bounded typed decision", 2.0, 3.0, final_state=state
        )
        self.assertEqual(result, self.output.FINITE_REFLECTION)
        self.assertNotIn("200", result)

    def test_memory_namespace_never_reads_old_prose_log(self):
        memory = load(
            "tradingagents.agents.utils.memory", ROOT / "tradingagents/agents/utils/memory.py"
        )
        with (
            patch.object(Path, "mkdir"),
            patch.object(Path, "read_text", side_effect=AssertionError("old-store read")),
        ):
            log = memory.TradingMemoryLog({**CONFIG, "memory_log_path": "old/decisions.md"})
        self.assertIn("finite-disclosure-v2", str(log._log_path))
        self.assertIn("a" * 64, str(log._log_path))
        with self.assertRaises(ValueError):
            log.update_with_outcome("601328.SH", "2026-10-16", 1, 1, 3, "unbound reflection")

    def test_default_mode_remains_available(self):
        self.assertFalse(self.output.validate_publication({"news_report": "legacy report"}, {}))
        store = FakeStore()
        with patch.object(self.exports, "ReportStore", return_value=store):
            self.exports.write_advisory_draft("fake", "legacy.md", "legacy report", config={})
        self.assertTrue(store.calls)

    def test_cli_validates_before_stream_display_and_buffer_mutation(self):
        tree = ast.parse((ROOT / "cli/main.py").read_text(encoding="utf-8"))
        stream = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.For)
            and isinstance(n.iter, ast.Call)
            and isinstance(n.iter.func, ast.Attribute)
            and n.iter.func.attr == "stream"
        )
        validate = next(
            n
            for n in ast.walk(stream)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "validate_publication"
        )
        outlets = [
            n
            for n in ast.walk(stream)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr
            in {
                "add_message",
                "publish_event",
                "refresh",
                "update_report_section",
                "record_validated_state",
            }
        ]
        self.assertTrue(outlets)
        self.assertTrue(all(validate.lineno < n.lineno for n in outlets))


if __name__ == "__main__":
    unittest.main()
