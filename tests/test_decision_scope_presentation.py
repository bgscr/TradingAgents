"""Synthetic display regressions; no live models, sources or native qualification."""
import copy
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from tests.report_export_helpers import export_manifest, export_root
from tests.test_reporting import _decision_state
from tradingagents.agents.managers import research_manager
from tradingagents.agents.managers.direction_selector import render_trading_decision
from tradingagents.agents.schemas import (
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
    render_research_plan,
    render_trader_proposal,
)
from tradingagents.agents.trader import trader
from tradingagents.decision_policy import TradingDecisionContract
from tradingagents.evidence import EvidenceState
from tradingagents.report_presentation import (
    advisory_direction_projection,
    decision_scope_markdown,
)
from tradingagents.reporting import write_report_tree
from tradingagents.terminal_contract import apply_terminal_contract


def state_with_suggestions(research="Underweight", action="Sell"):
    state = _decision_state()
    apply_terminal_contract(state)
    for stage, body in (
        ("research", render_research_plan(ResearchPlan(
            recommendation=research, rationale="纯合成研究理由", strategic_actions="纯合成建议"))),
        ("trader", render_trader_proposal(TraderProposal(action=action, reasoning="纯合成交易理由"))),
    ):
        rating = research if stage == "research" else action
        if stage == "research":
            state["investment_debate_state"]["judge_decision"] = body
            state["investment_plan"] = body
        else:
            state["trader_investment_plan"] = body
        state[f"{stage}_advisory_direction"] = advisory_direction_projection(state, stage, rating, body)
    return state


def render_scope(state, language="Chinese", *, missing_evidence=False):
    decision = TradingDecisionContract.model_validate(state["trading_decision"])
    evidence = None if missing_evidence else EvidenceState.model_validate(state["evidence_state"])
    return decision_scope_markdown(state, decision, evidence, language)


@pytest.mark.parametrize("research,action,expected", [
    ("Buy", "Buy", "标签与规则评级相同"),
    ("Underweight", "Sell", "标签与规则评级不同"),
    ("Overweight", "Buy", "增配；标签与规则评级不同"),
])
def test_structured_labels_compared_without_rating_or_scale_conversion(research, action, expected):
    state = state_with_suggestions(research, action)
    before = copy.deepcopy(state)
    text = render_scope(state)
    assert expected in text and "规则评级：买入" in text
    assert "不是所有分析员意见的综合投票" in text
    assert "不证明来源事实冲突" in text and state == before


@pytest.mark.parametrize("mutation", [
    "missing", "body", "symbol", "date", "run", "stage", "rating", "extra", "type",
])
def test_unbound_advice_is_unknown_and_never_echoes_metadata(mutation):
    state = state_with_suggestions()
    record = state["trader_advisory_direction"]
    if mutation == "missing":
        state.pop("trader_advisory_direction")
    elif mutation == "body":
        state["trader_investment_plan"] += " changed"
    elif mutation == "symbol":
        record["symbol"] = "OTHER"
    elif mutation == "date":
        record["as_of"] = "2099-01-01"
    elif mutation == "run":
        record["run_id"] = "run:" + "f" * 64
    elif mutation == "stage":
        record["stage"] = "research"
    elif mutation == "rating":
        record["rating"] = "Buy"  # Valid enum, but not the actual rendered header.
    elif mutation == "extra":
        record["private-token"] = "synthetic-secret"
    else:
        state["trader_advisory_direction"] = "synthetic-secret"
    text = render_scope(state)
    assert "交易建议：未保留可核对的结构化建议，无法自动比较" in text
    assert "synthetic-secret" not in text and "private-token" not in text


def test_research_projection_checks_the_selected_judge_body_not_an_old_plan():
    state = state_with_suggestions()
    state["investment_debate_state"]["judge_decision"] = "Unstructured BUY SELL HOLD"
    assert "研究建议：未保留可核对" in render_scope(state)
    state["investment_debate_state"]["judge_decision"] = ""
    assert "研究建议：减配" in render_scope(state)  # Same documented fallback as the exporter.


def test_no_evidence_or_no_decision_cannot_invent_comparison_or_hold():
    state = state_with_suggestions()
    text = render_scope(state, missing_evidence=True)
    assert "缺少可核对的决定事实" in text and "标签与规则评级不同" not in text
    text = decision_scope_markdown(state, None, EvidenceState(), "Chinese")
    assert "未形成经验证的交易决定" in text and "规则评级" not in text
    assert "持有" not in text


def test_price_rule_scope_is_limited_to_its_bound_fact():
    # Synthetic contract transformation exercises display only, not admission.
    state = state_with_suggestions()
    fact = state["trading_decision"]["facts"][0]
    fact.update(canonical_field="market.close_return_20d", normalized_value="0.08", unit="ratio")
    rule = state["trading_decision"]["assertions"][0]
    rule.update(rule_id="market.return_20d.buy", rule_version="1.0",
                comparator="greater_than_or_equal", threshold="0.05")
    source = state["evidence_state"]["source_facts"][0]
    source.update(canonical_field=fact["canonical_field"], normalized_value="0.08")
    text = render_scope(state)
    assert "8.00% ≥ 5.00%" in text and "只采用这项价格规则" in text
    assert "五年趋势、资金流、融资余额及模型建议未加入本次评级依据" in text
    source["normalized_value"] = Decimal("0.09")
    assert "8.00%" not in render_scope(state)


@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_hold_opening_identifies_the_only_fact_and_personal_source_limits(language):
    # Reproduce the report presentation seam with a synthetic Hold boundary value.
    state = state_with_suggestions(research="Overweight", action="Buy")
    state["personal_report_sources"] = ("news", "margin_balance", "eastmoney_fundflow_ratio")
    decision = state["trading_decision"]
    decision["rating"] = "Hold"
    fact = decision["facts"][0]
    fact.update(canonical_field="market.close_return_20d", normalized_value="-0.04999999", unit="ratio")
    fact["calculation_lineage"]["adjustment_basis"] = "qfq"
    decision["assertions"][0].update(
        rule_id="market.return_20d.hold", rule_version="1.0", target_rating="Hold",
        predicate_id="market_return_threshold", comparator="absolute_value_less_than", threshold="0.05",
    )
    source = state["evidence_state"]["source_facts"][0]
    source.update(canonical_field=fact["canonical_field"], normalized_value=fact["normalized_value"])
    source["calculation_lineage"]["adjustment_basis"] = "qfq"
    before = copy.deepcopy(state)
    text = render_scope(state, language)
    assert text.index("|-4.999999%| < 5%") < text.index("status.json")
    expected = (
        ("只依据这一项市场事实", "财报与融资余额仅供参考", "发行人新闻正文未核验",
         "东财资金流占比在报告发布时尚未核验", "增配；标签与规则评级不同", "买入；标签与规则评级不同")
        if language == "Chinese" else
        ("only market fact used for the rating", "financial statements and financing balance are advisory",
         "Issuer news article bodies were unverified", "Eastmoney flow ratio was unverified at publication",
         "Overweight; its label differs", "Buy; its label differs")
    )
    assert all(marker in text for marker in expected)
    assert state == before
    source["normalized_value"] = "0.08"
    assert "|-4.999999%|" not in render_scope(state, language)
    source["normalized_value"] = fact["normalized_value"] = "-0.05"
    assert "|-5%| < 5%" not in render_scope(state, language)


@pytest.mark.parametrize("stage", ["research", "trader"])
@pytest.mark.parametrize("invalid_binding", [False, True])
def test_real_nodes_capture_only_structured_success_and_clear_fallback(monkeypatch, stage, invalid_binding):
    state = state_with_suggestions()
    state["investment_debate_state"]["count"] = 1
    if invalid_binding:
        state["trade_date"] = "not-a-date"
    module = research_manager if stage == "research" else trader
    monkeypatch.setattr(module, "get_instrument_context_from_state", lambda _: "SYNTHETIC")
    monkeypatch.setattr(module, "get_language_instruction", lambda: "")
    value = (ResearchPlan(recommendation=PortfolioRating.UNDERWEIGHT, rationale="synthetic", strategic_actions="synthetic")
             if stage == "research" else TraderProposal(action=TraderAction.SELL, reasoning="synthetic"))
    original = render_research_plan(value) if stage == "research" else render_trader_proposal(value)
    llm = MagicMock()
    structured = llm.with_structured_output.return_value
    structured.invoke.side_effect = [value, None]
    llm.invoke.return_value.content = original  # Identical free text still has no typed provenance.
    node = module.create_research_manager(llm) if stage == "research" else module.create_trader(llm)
    key = "investment_plan" if stage == "research" else "trader_investment_plan"
    first = node(state)
    assert first[key] == original
    assert (first[f"{stage}_advisory_direction"] is None) is invalid_binding
    assert structured.invoke.call_count == 1 and llm.invoke.call_count == 0
    state.update(first)
    second = node(state)
    assert second[key] == original and second[f"{stage}_advisory_direction"] is None
    assert structured.invoke.call_count == 2 and llm.invoke.call_count == 1


@pytest.fixture
def modeled_handles(monkeypatch):
    from tests.test_report_export_safety import model_windows_handles

    model_windows_handles.__wrapped__(monkeypatch)


@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_export_separates_system_summary_preserves_advice_and_decision(tmp_path, modeled_handles, language):
    state = state_with_suggestions()
    decision = TradingDecisionContract.model_validate(state["trading_decision"])
    state["risk_debate_state"]["judge_decision"] = render_trading_decision(decision)
    before = copy.deepcopy(state["trading_decision"])
    body = write_report_tree(state, "AAPL", tmp_path, output_language=language).read_text(encoding="utf-8")
    root = export_root(tmp_path)
    title = "先读：评级依据与辅助建议" if language == "Chinese" else "Read first: rule scope"
    assert body.index(title) < body.index("## Coverage" if language == "English" else "## 覆盖情况")
    system = (root / "4_risk/manager.md").read_text(encoding="utf-8")
    assert ("系统生成" if language == "Chinese" else "system-generated") in system
    assert ("未经事实验证" if language == "Chinese" else "not validated facts") not in system
    assert "4_risk/manager.md" not in export_manifest(tmp_path)["advisory_stages"]
    assert state["investment_plan"] in body and state["trader_investment_plan"] in body
    assert state["trading_decision"] == before
    audit = json.loads((tmp_path / "decision-audit.json").read_bytes())
    for key in ("rating", "decision_id", "context_id"):
        assert audit["trading_decision"][key] == before[key]
    assert {x["fact_id"] for x in audit["trading_decision"]["facts"]} == {x["fact_id"] for x in before["facts"]}
    assert [x["rule_id"] for x in audit["trading_decision"]["assertions"]] == [x["rule_id"] for x in before["assertions"]]
    assert "advisory-direction-v1" not in json.dumps(audit)


def test_unknown_risk_text_remains_unvalidated_not_system_generated(tmp_path, modeled_handles):
    state = state_with_suggestions()
    risk = "**Rating**: Buy\n\nUnverified free-text risk suggestion."
    state["risk_debate_state"]["judge_decision"] = risk
    write_report_tree(state, "AAPL", tmp_path, output_language="Chinese")
    text = (export_root(tmp_path) / "4_risk/manager.md").read_text(encoding="utf-8")
    assert risk in text and "未经事实验证" in text and "系统生成" not in text
