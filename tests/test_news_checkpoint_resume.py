"""Offline news identity regression across real SQLite/process boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.analysts.submission import AnalystSubmissionResult, build_analyst_update
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.news_data_tools import acquire_news, get_news
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.evidence import (
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MaterialClaim,
    merge_source_acquisition_outcomes,
    stable_acquisition_source_ref,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph


def run_phase(root: Path, phase: str, legacy: bool = True):
    """Exercise production checkpoint scope, tool generation and analyst ingestion."""
    assert phase in {"fail", "resume"}
    events_path = root / "events.jsonl"
    source_ref = stable_acquisition_source_ref("news", "600895.SS", "2026-10-05", "2026-10-12")

    def record(event):
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    def provider(*_args, **_kwargs):
        record("provider")
        if phase == "fail":
            raise RuntimeError("synthetic provider unavailable")
        return "Synthetic headline after restart"

    def request(call_id):
        return AIMessage(
            content="",
            id=f"request-{call_id}",
            tool_calls=[
                {
                    "name": "get_news",
                    "id": call_id,
                    "type": "tool_call",
                    "args": {
                        "ticker": "600895.SS",
                        "start_date": "2026-10-05",
                        "end_date": "2026-10-12",
                    },
                }
            ],
        )

    def warmup(_state):
        record("warmup")
        acquired = acquire_news(
            "600895.SS",
            "2026-10-05",
            "2026-10-12",
            tool_call_id="sentiment-1",
            source_ref="sentiment:synthetic",
            capability="sentiment_news",
        )
        return {
            "messages": [request("news-1")],
            "evidence_state": merge_source_acquisition_outcomes(None, acquired.outcomes).model_dump(
                mode="json"
            ),
            "market_report": "Retained market draft",
            "sentiment_report": "Retained sentiment draft",
        }

    def progress(state):
        tool_messages = [item for item in state["messages"] if isinstance(item, ToolMessage)]
        if len(tool_messages) == 1:
            record("second-request")
            return {"messages": [request("news-2")]}
        if legacy:
            # Simulate the serialized format at the user's failed pre-fix run.
            restored = []
            for item in tool_messages:
                payload = json.loads(json.dumps(item.artifact))
                for outcome in payload["acquisition_outcomes"]:
                    outcome.pop("acquisition_id", None)
                restored.append(item.model_copy(update={"artifact": payload}))
            return {"messages": restored}
        return {}

    def submit(state, claim_id, quote):
        return build_analyst_update(
            state,
            AnalystSubmissionResult(
                message=AIMessage(content="Synthetic news report"),
                report="Synthetic news report",
                claims=(
                    MaterialClaim(
                        claim_id=claim_id,
                        analyst="news",
                        statement=quote,
                        source_refs=(source_ref,),
                        source_quote=quote,
                    ),
                ),
                submission_source=EvidenceSource(
                    source_id="analyst.news.submission",
                    status=EvidenceStatus.AVAILABLE,
                    required=True,
                ),
            ),
            "news_report",
        )

    def finalize(state):
        record("finalize")
        if phase == "fail":
            raise RuntimeError("synthetic exit before news submission")
        assert state["market_report"] == "Retained market draft"
        assert state["sentiment_report"] == "Retained sentiment draft"
        result = submit(state, "news.unavailable", "No verified news")
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert len(evidence.acquisition_outcomes) == 3
        assert evidence.source_facts == ()
        result["messages"].append(request("news-3"))
        return result

    def finish(state):
        record("finish")
        result = submit(state, "news.recovered", "Synthetic headline after restart")
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert len(evidence.acquisition_outcomes) == 4
        assert len(evidence.source_facts) == 1
        assert (
            merge_source_acquisition_outcomes(evidence, evidence.acquisition_outcomes) == evidence
        )
        return result

    workflow = StateGraph(AgentState)
    for name, node in (
        ("warmup", warmup),
        ("tools_news", ToolNode([get_news])),
        ("progress", progress),
        ("News Analyst", finalize),
        ("tools_after_restart", ToolNode([get_news])),
        ("finish", finish),
    ):
        workflow.add_node(name, node)
    workflow.set_entry_point("warmup")
    workflow.add_edge("warmup", "tools_news")
    workflow.add_edge("tools_news", "progress")
    workflow.add_conditional_edges(
        "progress",
        lambda state: (
            "tools_news"
            if len([item for item in state["messages"] if isinstance(item, ToolMessage)]) < 2
            else "News Analyst"
        ),
    )
    workflow.add_edge("News Analyst", "tools_after_restart")
    workflow.add_edge("tools_after_restart", "finish")
    workflow.add_edge("finish", END)
    owner = object.__new__(TradingAgentsGraph)
    owner.config = {"checkpoint_enabled": True, "data_cache_dir": str(root / "cache")}
    owner.selected_analysts = ("social", "news")
    owner.decision_policy = owner.decision_horizon = None
    owner.workflow = workflow
    owner.graph = workflow.compile()
    set_config({"tool_vendors": {"get_news": "akshare"}})
    with (
        mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": provider}),
        authoritative_snapshot_run(),
        owner.checkpoint_scope("600895.SS", "2026-10-12") as session,
    ):
        assert session.resume_from_checkpoint == (phase == "resume")
        if phase == "fail":
            with pytest.raises(RuntimeError, match="synthetic exit before news submission"):
                owner.graph.invoke(
                    {
                        "messages": [],
                        "company_of_interest": "600895.SS",
                        "trade_date": "2026-10-12",
                    },
                    session.graph_config,
                )
            saved = owner.graph.get_state(session.graph_config)
            assert saved.next == ("News Analyst",)
            assert (
                len([item for item in saved.values["messages"] if isinstance(item, ToolMessage)])
                == 2
            )
        else:
            assert json.loads((root / "fail.json").read_text())["pid"] != os.getpid()
            result = owner.graph.invoke(None, session.graph_config)
            assert len(result["evidence_state"]["acquisition_outcomes"]) == 4
            assert result["market_report"] == "Retained market draft"
            assert result["sentiment_report"] == "Retained sentiment draft"
    events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()]
    expected = ["warmup", "provider", "second-request", "finalize"]
    if phase == "resume":
        expected += ["finalize", "provider", "finish"]
    assert events == expected
    receipt = {
        "phase": phase,
        "pid": os.getpid(),
        "legacy": legacy,
        "passed": True,
        "events": events,
    }
    (root / f"{phase}.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


@pytest.mark.parametrize("legacy", [False, True])
def test_news_checkpoint_restarts_in_new_process(tmp_path, legacy):
    for phase in ("fail", "resume"):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_news_checkpoint_resume",
                str(tmp_path),
                phase,
                str(int(legacy)),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads((tmp_path / "resume.json").read_text())["passed"]


if __name__ == "__main__":
    # This helper is launched only by the isolated test runner. Block network
    # even if an unexpected provider escapes its fixture.
    def no_network(event, _args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
            raise RuntimeError("news restart regression forbids live network")

    sys.addaudithook(no_network)
    run_phase(Path(sys.argv[1]), sys.argv[2], bool(int(sys.argv[3])))
