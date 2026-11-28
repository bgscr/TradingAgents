"""Verify the prompts sent to both analyst stages of the personal entry."""

import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from scripts import personal_graph
from tests.test_structured_agents import _tool_exchange
from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.analysis_budget import AnalysisBudgetStopped
from tradingagents.evidence import AnalystEvidenceReport

SYMBOL = "600895.SS"
DAY = "2026-11-26"
DRAFT = "UNVALIDATED DRAFT: " + "Repeated advisory prose. " * 80
EVIDENCE = (
    "Dated observation: 2026-11-26.\nReliability: incomplete coverage; conflicting row retained."
)
ANALYSTS = (
    ("market", create_market_analyst, "get_indicators"),
    ("news", create_news_analyst, "get_news"),
    ("fundamentals", create_fundamentals_analyst, "get_income_statement"),
)


def _prompts_for_mode(tmp_path, monkeypatch, mode, *, evidence=EVIDENCE, draft=DRAFT):
    from tradingagents.graph import trading_graph

    observed = {}

    class Client:
        def __init__(self, analyst):
            self.analyst = analyst

        def bind_tools(self, tools):
            def capture(prompt):
                observed[self.analyst]["tools"] = prompt.messages[0].content
                observed[self.analyst]["tool_names"] = tuple(
                    getattr(tool, "name", tool.__name__ if isinstance(tool, type) else "")
                    for tool in tools
                )
                return AIMessage(content=draft)

            return RunnableLambda(capture)

        def get_required_structured_method(self):
            return "json_mode"

        def with_structured_output(self, schema, *, include_raw):
            assert schema is AnalystEvidenceReport and include_raw

            def capture(prompt):
                observed[self.analyst]["finalizer"] = prompt
                return AnalystEvidenceReport(report_markdown="Dated and qualified result.")

            return SimpleNamespace(invoke=capture)

    class FixtureGraph:
        def __init__(self, **kwargs):
            self.graph = SimpleNamespace(with_config=lambda **kwargs: self.graph)

        def propagate(self, ticker, day):
            for analyst, create, tool in ANALYSTS:
                observed[analyst] = {}
                messages = _tool_exchange(
                    tool, {"ticker": ticker, "curr_date": day}, evidence, analyst
                )
                node = create(Client(analyst))
                node({"company_of_interest": ticker, "trade_date": day, "messages": messages})
            raise AnalysisBudgetStopped("deadline_exceeded")

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", FixtureGraph)
    receipt = personal_graph.run(
        {
            "analysis_timeout_seconds": 60,
            "analysis_budget_enabled": True,
            "personal_verified_inputs": {},
            "personal_mode": mode,
        },
        SYMBOL,
        DAY,
        tmp_path,
        (time.time(), time.monotonic()),
    )
    assert receipt["stop_reason"] == "deadline_exceeded"
    return observed


def test_personal_full_shortens_both_prompts_without_cutting_source_context(tmp_path, monkeypatch):
    basic = _prompts_for_mode(tmp_path, monkeypatch, "basic")
    full = _prompts_for_mode(tmp_path, monkeypatch, "full")
    assert _prompts_for_mode(tmp_path, monkeypatch, "basic") == basic
    for analyst, _, tool in ANALYSTS:
        old, new = basic[analyst], full[analyst]
        assert new["tool_names"] == old["tool_names"]
        assert len(new["tools"]) < len(old["tools"])
        assert len(new["finalizer"]) < len(old["finalizer"])
        assert DRAFT.strip() in old["finalizer"] and DRAFT.strip() not in new["finalizer"]
        assert f"{tool}:{SYMBOL}:{DAY}" in new["tools"] and EVIDENCE in new["finalizer"]
        start = "Allowed source refs and collected evidence:\n"
        old_catalog = old["finalizer"].split(start, 1)[1].split("Unvalidated prose draft", 1)[0]
        new_catalog = (
            new["finalizer"].split(start, 1)[1].split("Return exactly one valid JSON", 1)[0]
        )
        assert old_catalog.strip() == new_catalog.strip()
        for field in (
            "report_markdown",
            "material_claims",
            "evidence_notes",
            "source_ref",
            "source_quote",
        ):
            assert field in new["tools"] and field in new["finalizer"]
        for stage in ("tools", "finalizer"):
            assert "report_markdown is a string" in new[stage] and "arrays of objects" in new[stage]
            if analyst == "market":
                assert "〈周期〉日均线为〈读数〉。" in new[stage]
        print(
            f"{analyst} chars tools {len(old['tools'])}->{len(new['tools'])}, "
            f"finalizer {len(old['finalizer'])}->{len(new['finalizer'])}"
        )


def test_compact_finalizer_retains_draft_pointer_when_a_preview_omits_evidence(
    tmp_path, monkeypatch
):
    middle = "2026-09-30: unaudited counterevidence, incomplete coverage."
    evidence = "A" * 2_010 + f"\n{middle}\n" + "B" * 2_010
    full = _prompts_for_mode(tmp_path, monkeypatch, "full", evidence=evidence, draft=middle)
    for analyst, _, _ in ANALYSTS:
        catalog, draft = full[analyst]["finalizer"].split("Unvalidated prose draft", 1)
        assert "[middle omitted]" in catalog and middle not in catalog
        assert middle in draft
