from typing import Annotated, Any

from langgraph.graph import MessagesState
from typing_extensions import TypedDict


# Researcher team state
class InvestDebateState(TypedDict):
    bull_history: Annotated[
        str, "Bullish Conversation history"
    ]  # Bullish Conversation history
    bear_history: Annotated[
        str, "Bearish Conversation history"
    ]  # Bullish Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    current_response: Annotated[str, "Latest response"]  # Last response
    judge_decision: Annotated[str, "Final judge decision"]  # Last response
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


# Risk management team state
class RiskDebateState(TypedDict):
    aggressive_history: Annotated[
        str, "Aggressive Agent's Conversation history"
    ]  # Conversation history
    conservative_history: Annotated[
        str, "Conservative Agent's Conversation history"
    ]  # Conversation history
    neutral_history: Annotated[
        str, "Neutral Agent's Conversation history"
    ]  # Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[
        str, "Latest response by the aggressive analyst"
    ]  # Last response
    current_conservative_response: Annotated[
        str, "Latest response by the conservative analyst"
    ]  # Last response
    current_neutral_response: Annotated[
        str, "Latest response by the neutral analyst"
    ]  # Last response
    judge_decision: Annotated[str, "Judge's decision"]
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


class AgentState(MessagesState):
    company_of_interest: Annotated[str, "Company that we are interested in trading"]
    asset_type: Annotated[str, "Asset type under analysis such as stock or crypto"]
    instrument_context: Annotated[str, "Deterministic ticker identity resolved at run start"]
    evidence_state: Annotated[dict[str, Any], "Validated shared evidence for gates"]
    evidence_preflight: Annotated[
        dict[str, Any], "Deterministic baseline-evidence preflight result"
    ]
    admission_gate: Annotated[dict[str, Any], "Deterministic admission-gate result"]
    analysis_outcome: Annotated[str, "Non-directional outcome when evidence blocks"]
    analysis_outcome_contract: Annotated[
        dict[str, Any] | None, "Typed non-directional terminal outcome"
    ]
    draft_thesis: Annotated[dict[str, Any], "Structured directional thesis draft"]
    decision_gate: Annotated[dict[str, Any], "Deterministic decision-gate result"]
    original_draft_thesis: Annotated[dict[str, Any], "Original PM thesis draft"]
    original_decision_gate: Annotated[dict[str, Any], "Original PM gate result"]
    revised_draft_thesis: Annotated[dict[str, Any], "Removal-only revised PM draft"]
    revised_decision_gate: Annotated[dict[str, Any], "Revised PM gate result"]
    pm_original_selection: Annotated[dict[str, Any] | None, "Raw structured PM selection"]
    pm_revision: Annotated[dict[str, Any] | None, "Raw removal-only PM revision"]
    evidence_gate_mode: Annotated[str, "Enforced or explicit legacy gate mode"]
    strategy_rule_applications: Annotated[
        list[dict[str, Any]], "Rule applications proposed for deterministic validation"
    ]
    validated_decision_context: Annotated[
        dict[str, Any] | None, "Closed, rule-backed decision context"
    ]
    direction_selector_diagnostics: Annotated[
        dict[str, Any] | None, "Legacy selector failure diagnostics"
    ]
    direction_selection_diagnostics: Annotated[
        dict[str, Any] | None, "Payload-free deterministic selection diagnostics"
    ]
    direction_selection: Annotated[
        dict[str, Any] | None, "Deterministic closed-context direction proposal"
    ]
    decision_gate_v2: Annotated[
        dict[str, Any], "Versioned deterministic decision-gate result"
    ]
    trading_decision: Annotated[
        dict[str, Any] | None, "Published structured trading decision when permitted"
    ]
    trade_date: Annotated[str, "What date we are trading at"]
    run_id: Annotated[str, "Stable identity for this resumable analysis run"]
    admitted_evidence_binding: Annotated[
        dict[str, Any] | None,
        "Checkpoint-safe binding for evidence accepted at Evidence Admission",
    ]

    sender: Annotated[str, "Agent that sent this message"]

    # research step
    market_report: Annotated[str, "Report from the Market Analyst"]
    sentiment_report: Annotated[str, "Report from the Sentiment Analyst"]
    news_report: Annotated[
        str, "Report from the News Researcher of current world affairs"
    ]
    fundamentals_report: Annotated[str, "Report from the Fundamentals Researcher"]

    # researcher team discussion step
    investment_debate_state: Annotated[
        InvestDebateState, "Current state of the debate on if to invest or not"
    ]
    investment_plan: Annotated[str, "Plan generated by the Analyst"]

    trader_investment_plan: Annotated[str, "Plan generated by the Trader"]

    # risk management team discussion step
    risk_debate_state: Annotated[
        RiskDebateState, "Current state of the debate on evaluating risk"
    ]
    final_trade_decision: Annotated[
        str | None, "Final decision made by the Risk Analysts"
    ]
    past_context: Annotated[str, "Memory log context injected at run start (same-ticker decisions + cross-ticker lessons)"]
