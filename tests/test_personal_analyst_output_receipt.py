"""Offline projection of actual analyst observation points into personal receipts."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from scripts import personal_graph
from tradingagents.agents.analysts.submission import process_analyst_response
from tradingagents.agents.utils.structured import invoke_required_structured
from tradingagents.evidence import AnalystEvidenceReport
from tradingagents.run_diagnostics import emit, record_output

PRIVATE = "fixture-token fixture-private-path fixture-prompt fixture-reasoning fixture-body"


def _length_error():
    return LengthFinishReasonError(
        completion=ChatCompletion(
            id=PRIVATE,
            object="chat.completion",
            created=0,
            model=PRIVATE,
            choices=[
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": PRIVATE,
                        "reasoning_content": PRIVATE,
                    },
                }
            ],
            usage={
                "prompt_tokens": 136,
                "completion_tokens": 122,
                "total_tokens": 258,
                "completion_tokens_details": {"reasoning_tokens": 100},
            },
        )
    )


def _run_receipt(tmp_path, monkeypatch, function):
    from tradingagents.analysis_budget import AnalysisBudgetStopped
    from tradingagents.graph import trading_graph

    class FixtureGraph:
        def __init__(self, **kwargs):
            self.graph = SimpleNamespace(with_config=lambda **kwargs: self.graph)

        def propagate(self, ticker, day):
            function()
            raise AnalysisBudgetStopped("deadline_exceeded")

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", FixtureGraph)
    receipt = personal_graph.run(
        {
            "analysis_timeout_seconds": 60,
            "analysis_budget_enabled": True,
            "personal_verified_inputs": {},
        },
        "600895.SS",
        "2026-11-26",
        tmp_path,
        (time.time(), time.monotonic()),
    )
    raw = (tmp_path / receipt["run_id"] / "status.json").read_text(encoding="utf-8")
    assert json.loads(raw) == receipt
    assert receipt["stop_reason"] == "deadline_exceeded"
    assert all(marker not in raw for marker in PRIVATE.split())
    return receipt["analyst_output_diagnostics"]


def test_personal_receipt_retains_only_safe_analyst_observations(tmp_path, monkeypatch):
    def exercise_observations():
        finalizer = Mock()
        partial = AIMessage(
            content=PRIVATE,
            response_metadata={"finish_reason": "length", "max_tokens": 4096},
            usage_metadata={
                "input_tokens": 500,
                "output_tokens": 96,
                "total_tokens": 596,
                "output_token_details": {"reasoning": 80},
            },
            tool_calls=[
                {
                    "name": "AnalystEvidenceReport",
                    "id": PRIVATE,
                    "type": "tool_call",
                    "args": {"report_markdown": PRIVATE, "material_claims": []},
                }
            ],
        )
        result = process_analyst_response(
            partial,
            finalizer=finalizer,
            analyst="market",
            ticker="600895.SS",
            trade_date="2026-11-26",
            messages=[],
        )
        assert result.submission_source.detail == "output_limit" and not result.claims
        assert result.message.tool_calls == []
        finalizer.invoke.assert_not_called()

        finalizer.invoke.return_value = {
            "raw": partial,
            "parsed": {"report_markdown": PRIVATE},
            "parsing_error": _length_error(),
        }
        result = invoke_required_structured(finalizer, PRIVATE, "News Analyst")
        assert result.value is None and result.reason == "output_limit"
        assert result.diagnostics["completion_tokens"] == 122  # Existing general projection.
        finalizer.invoke.assert_called_once()

        finalizer.reset_mock()
        finalizer.invoke.side_effect = _length_error()
        result = invoke_required_structured(finalizer, PRIVATE, "Fundamentals Analyst")
        assert result.value is None and result.reason == "output_limit"
        finalizer.invoke.assert_called_once()

        finalizer.invoke.side_effect = None
        for raw in (
            None,
            AIMessage(content=PRIVATE, response_metadata={"finish_reason": "length"}),
            AIMessage(
                content=PRIVATE,
                response_metadata={
                    "finish_reason": "length",
                    "token_usage": {
                        "completion_tokens": 1 << 1024,
                        "completion_tokens_details": {"reasoning_tokens": 18},
                    },
                },
            ),
        ):
            finalizer.reset_mock()
            finalizer.invoke.return_value = {
                "raw": raw,
                "parsed": None,
                "parsing_error": _length_error(),
            }
            result = invoke_required_structured(finalizer, PRIVATE, "Fundamentals Analyst")
            assert result.value is None and result.reason == "output_limit"
            finalizer.invoke.assert_called_once()

        with pytest.raises(json.JSONDecodeError) as bad_json:
            json.loads(PRIVATE)
        with pytest.raises(ValidationError) as bad_schema:
            AnalystEvidenceReport.model_validate({"report_markdown": {"private": PRIVATE}})
        finalizer.invoke.side_effect = None
        for error in (bad_json.value, bad_schema.value):
            finalizer.reset_mock()
            finalizer.invoke.return_value = {
                "raw": AIMessage(content=PRIVATE, response_metadata={"finish_reason": "stop"}),
                "parsed": None,
                "parsing_error": error,
            }
            result = invoke_required_structured(finalizer, PRIVATE, "News Analyst", max_attempts=1)
            assert result.value is None and result.reason == "validation_error"
            finalizer.invoke.assert_called_once()

        finalizer.invoke.return_value = {
            "raw": AIMessage(
                content=PRIVATE,
                usage_metadata={
                    "input_tokens": 500,
                    "output_tokens": 18,
                    "total_tokens": 518,
                },
            ),
            "parsed": {"report_markdown": {"private": PRIVATE}},
            "parsing_error": None,
        }
        result = invoke_required_structured(
            finalizer,
            PRIVATE,
            "News Analyst",
            validator=AnalystEvidenceReport.model_validate,
            max_attempts=1,
        )
        assert result.value is None and result.reason == "validation_error"
        assert "completion_tokens" not in result.diagnostics  # Existing general projection.

        finalizer.invoke.return_value = {
            "raw": AIMessage(content=PRIVATE),
            "parsed": "ok",
            "parsing_error": None,
        }
        assert invoke_required_structured(finalizer, PRIVATE, "Sentiment Analyst").value == "ok"
        invoke_required_structured(finalizer, PRIVATE, "Portfolio Manager")
        emit("news_exception", {"material": PRIVATE})
        emit("analyst_output", {"unexpected": PRIVATE})

    observations = _run_receipt(tmp_path, monkeypatch, exercise_observations)
    sdk_event = {
        "analyst": "fundamentals",
        "stage": "finalizer",
        "observation": "sdk_length_finish_reason_error",
        "completion_tokens": 122,
        "reasoning_tokens": 100,
    }
    assert observations == {
        "dropped_events": 0,
        "events": [
            {
                "analyst": "market",
                "stage": "initial_response",
                "observation": "raw_finish_reason_length",
                "completion_tokens": 96,
                "reasoning_tokens": 80,
            },
            {
                "analyst": "news",
                "stage": "finalizer",
                "observation": "raw_finish_reason_length",
                "completion_tokens": 96,
                "reasoning_tokens": 80,
            },
            sdk_event,
            sdk_event,
            sdk_event,
            sdk_event,
            {"analyst": "news", "stage": "finalizer", "observation": "json_syntax"},
            {"analyst": "news", "stage": "finalizer", "observation": "schema_validation"},
            {
                "analyst": "news",
                "stage": "finalizer",
                "observation": "schema_validation",
                "completion_tokens": 18,
            },
            {"analyst": "sentiment", "stage": "finalizer", "observation": "success"},
        ],
    }


def test_personal_receipt_bounds_events_and_omits_unverified_counts(tmp_path, monkeypatch):
    def exercise_observations():
        record_output(
            "market",
            "finalizer",
            1,
            {"completion_tokens": True, "reasoning_tokens": -1, "max_tokens": 4096},
            outcome="success",
        )
        for _ in range(70):
            record_output(
                "news", "initial_response", 1, {"completion_tokens": 1 << 1024}, outcome="response"
            )

    observations = _run_receipt(tmp_path, monkeypatch, exercise_observations)
    assert len(observations["events"]) == 60 and observations["dropped_events"] == 11
    assert all(
        set(event) == {"analyst", "stage", "observation"} for event in observations["events"]
    )
