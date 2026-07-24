# TradingAgents/graph/propagation.py

from typing import Any

from tradingagents.agents.utils.agent_states import (
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.evidence import EvidenceState


class Propagator:
    """Handles state initialization and propagation through the graph."""

    def __init__(self, max_recur_limit=100):
        """Initialize with configuration parameters."""
        self.max_recur_limit = max_recur_limit

    def create_initial_state(
        self,
        company_name: str,
        trade_date: str,
        asset_type: str = "stock",
        past_context: str = "",
        instrument_context: str = "",
        evidence_state: EvidenceState | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Create the initial state for the agent graph.

        ``instrument_context`` is the deterministic ticker-identity string
        resolved once at run start (see
        ``TradingAgentsGraph.resolve_instrument_context``). When empty, agents
        fall back to ticker-only context via
        ``get_instrument_context_from_state``.
        """
        return {
            "messages": [("human", company_name)],
            "company_of_interest": company_name,
            "asset_type": asset_type,
            "instrument_context": instrument_context,
            "trade_date": str(trade_date),
            "run_id": run_id,
            "past_context": past_context,
            "evidence_state": (evidence_state or EvidenceState()).model_dump(mode="json"),
            # Gate and selector payloads stay JSON-compatible because LangGraph
            # checkpoints serialize state independently of the in-memory models.
            "evidence_preflight": {},
            "admission_gate": {},
            "admitted_evidence_binding": None,
            "analysis_outcome": "",
            "analysis_outcome_contract": None,
            "strategy_rule_applications": [],
            "validated_decision_context": None,
            "direction_selector_diagnostics": None,
            "direction_selection_diagnostics": None,
            "direction_selection": None,
            "decision_gate": {},
            "decision_gate_v2": {},
            "trading_decision": None,
            "final_trade_decision": None,
            "investment_debate_state": InvestDebateState(
                {
                    "bull_history": "",
                    "bear_history": "",
                    "history": "",
                    "current_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "risk_debate_state": RiskDebateState(
                {
                    "aggressive_history": "",
                    "conservative_history": "",
                    "neutral_history": "",
                    "history": "",
                    "latest_speaker": "",
                    "current_aggressive_response": "",
                    "current_conservative_response": "",
                    "current_neutral_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "market_report": "",
            "fundamentals_report": "",
            "sentiment_report": "",
            "news_report": "",
        }

    def get_graph_args(
        self,
        callbacks: list | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Get arguments for the graph invocation.

        Args:
            callbacks: Optional list of callback handlers for tool execution tracking.
                       Note: LLM callbacks are handled separately via LLM constructor.
        """
        config = {"recursion_limit": self.max_recur_limit}
        if callbacks:
            config["callbacks"] = callbacks
        if run_id is not None:
            config["configurable"] = {"run_id": run_id}
        return {
            "stream_mode": "values",
            "config": config,
        }
