"""Actual graph -> actual report export with explicitly modeled Windows handles.

The native boundary is the existing report safety fixture. These tests do not
qualify native Windows I/O or the interactive CLI lifecycle.
"""

import gzip
import json
from contextlib import nullcontext
from hashlib import sha256

import pytest
from pydantic_core import to_jsonable_python

from tests.report_export_helpers import export_manifest
from tests.test_analysis_budget_e2e import (
    AS_OF,
    SYMBOL,
    _assert_cache_unchanged,
    _close,
    prepare,
)
from tests.test_report_export_safety import model_windows_handles  # noqa: F401
from tradingagents.analysis_budget import active_budget, analysis_budget_scope
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.report_presentation import advisory_sections


@pytest.mark.parametrize("during_budget", [False, True], ids=["after-propagate", "active-publication-scope"])
def test_budget_graph_publishes_with_modeled_handles(tmp_path, monkeypatch, during_budget):
    settings, wire, cache, before = prepare(tmp_path, monkeypatch)
    graph = TradingAgentsGraph(config=settings, instrument_symbol=SYMBOL)
    try:
        state, _ = graph.propagate(SYMBOL, AS_OF.isoformat())
        assert state["terminal_outcome_kind"] == "trading_decision"
        assert state["evidence_preflight"]["passed"] and state["admission_gate"]["admitted"]
        analysis_receipt = graph.last_analysis_budget
        assert analysis_receipt["model_attempts"] == len(wire.model_calls) == 18
        assert analysis_receipt["data_attempts"] == len(wire.data_calls) == 6
        assert len(analysis_receipt["financial_cache_hits"]) == 3
        assert len(wire.financial_calls) == 3 and not wire.seeding

        # Preserve this exact synthetic graph result as a reviewable input for a
        # separately authorized native report-only probe. Messages are unused by
        # report publication. Serialization retains typed dates/decimals via the
        # normal Pydantic JSON conversion, never an arbitrary repr/str fallback.
        encoded = json.dumps({key: value for key, value in state.items() if key != "messages"},
                             default=to_jsonable_python, ensure_ascii=False, indent=2).encode("utf-8")
        (tmp_path / "publication-input.json").write_bytes(encoded)
        publication_state = json.loads(encoded)
        expected_stages = {relative for relative, _, _, _ in advisory_sections(publication_state)}
        assert len(expected_stages) == 12

        # propagate owns and closes its analysis scope. This second case tests
        # report publication under a separate active scope; it is not a claim
        # that the interactive CLI or one continuous graph+publish scope ran.
        scope = analysis_budget_scope(settings) if during_budget else nullcontext(None)
        root = tmp_path / "modeled-publication"
        with scope as publication_budget:
            assert active_budget() is publication_budget
            report = graph.save_reports(publication_state, SYMBOL, root)
            manifest = export_manifest(report)
            audit = json.loads((root / "decision-audit.json").read_text(encoding="utf-8"))
            assert set(manifest["advisory_stages"]) == expected_stages
            assert set(manifest["files"]) == expected_stages | {"complete_report.md", "5_portfolio/decision.md"}
            assert manifest["run_id"] == state["run_id"] == audit["run"]["run_id"]
            assert manifest["audit_sha256"] == audit["audit_sha256"] == state["decision_audit_sha256"]
            assert audit["terminal"]["terminal_outcome_kind"] == "trading_decision"
            assert report.is_relative_to(root / "report_exports")
            for relative, digest in manifest["files"].items():
                assert sha256((report.parent / relative).read_bytes()).hexdigest() == digest
            text = report.read_text(encoding="utf-8")
            assert state["trading_decision"]["decision_id"] in text
            for _, _, _, content in advisory_sections(publication_state):
                assert content in text
            artifacts = list((root / "evidence_artifacts" / "sha256").rglob("*.utf8.gz"))
            assert artifacts
            for artifact in artifacts:
                assert sha256(gzip.decompress(artifact.read_bytes())).hexdigest() == artifact.name.removesuffix(".utf8.gz")
            if publication_budget is not None:
                assert publication_budget.receipt()["admitted_attempts"] == 0
                assert publication_budget.receipt()["stop_reason"] is None
        assert active_budget() is None
        assert graph.last_analysis_budget == analysis_receipt
        assert len(wire.model_calls) == 18 and len(wire.data_calls) == 6
        assert len(wire.financial_calls) == 3
        _assert_cache_unchanged(cache, before)
        (tmp_path / "publication-observation.json").write_text(json.dumps({
            "native_handles": "modeled-existing-fixture", "during_separate_budget_scope": during_budget,
            "analysis_budget": analysis_receipt,
            "publication_budget": publication_budget.receipt() if publication_budget else None,
            "input_sha256": sha256(encoded).hexdigest(), "report": str(report),
            "manifest_files": len(manifest["files"]), "advisory_stages": len(expected_stages),
            "source_artifacts": len(artifacts),
        }, indent=2), encoding="utf-8")
    finally:
        _close(graph)
