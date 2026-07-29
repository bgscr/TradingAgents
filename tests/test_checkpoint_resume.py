"""Test checkpoint resume: crash mid-analysis, re-run resumes from last node."""

import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event
from typing import TypedDict

from langgraph.graph import END, StateGraph

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.acquisition import AcquisitionFailure, RetryPolicy
from tradingagents.dataflows.errors import VendorRateLimitError
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import AcquisitionUnavailableReason
from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)

# Mutable flag to simulate crash on first run
_should_crash = False


class _SimpleState(TypedDict):
    count: int


def _node_a(state: _SimpleState) -> dict:
    return {"count": state["count"] + 1}


def _node_b(state: _SimpleState) -> dict:
    if _should_crash:
        raise RuntimeError("simulated mid-analysis crash")
    return {"count": state["count"] + 10}


def _build_graph() -> StateGraph:
    builder = StateGraph(_SimpleState)
    builder.add_node("analyst", _node_a)
    builder.add_node("trader", _node_b)
    builder.set_entry_point("analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


def _round_trip_financial_ledger(
    data_dir: str,
    ticker: str,
    ledger: dict,
    *,
    checkpoint_thread_id: str,
) -> dict:
    workflow = StateGraph(AgentState)
    workflow.add_node(
        "financial_checkpoint_save",
        lambda _state: {"financial_dispatch_ledger": ledger},
    )
    workflow.set_entry_point("financial_checkpoint_save")
    workflow.add_edge("financial_checkpoint_save", END)
    config = {"configurable": {"thread_id": checkpoint_thread_id}}
    with get_checkpointer(data_dir, ticker) as saver:
        workflow.compile(checkpointer=saver).invoke({"messages": []}, config=config)
    with get_checkpointer(data_dir, ticker) as saver:
        saved = saver.get_tuple(config)
        return saved.checkpoint["channel_values"]["financial_dispatch_ledger"]


class TestCheckpointResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_crash_and_resume(self):
        """Crash at 'trader' node, then resume from checkpoint."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Run 1: crash at trader node
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        # Checkpoint should exist at step 1 (analyst completed)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))
        step = checkpoint_step(self.tmpdir, self.ticker, self.date)
        self.assertEqual(step, 1)

        # Run 2: resume — trader succeeds this time
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke(None, config=cfg)

        # analyst added 1, trader added 10 → 11
        self.assertEqual(result["count"], 11)

    def test_financial_success_resume_reuses_terminal_without_provider_io(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0
        should_crash = True
        resumed_results = []

        def provider(_request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            return (
                "# Balance Sheet data for 601328.SS (quarterly)\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        provider_chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(
                        variant_id="default",
                        invoke=provider,
                    ),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            material_arguments={"currency_mode": "reported"},
            tool_call_id="financial-success-1",
        )

        def dispatcher(checkpoint_ledger=None) -> FinancialToolDispatcher:
            return FinancialToolDispatcher(
                instrument_identity=asset_configuration.instrument_identity,
                run_asset_configuration=asset_configuration,
                provider_chains={"get_balance_sheet": provider_chain},
                checkpoint_ledger=checkpoint_ledger,
            )

        def acquire(_state: AgentState) -> dict:
            current = dispatcher()
            first = current.dispatch(request)
            resumed_results.append(first)
            return {"financial_dispatch_ledger": current.checkpoint_ledger()}

        def resume(state: AgentState) -> dict:
            nonlocal should_crash
            if should_crash:
                raise RuntimeError("simulated crash after financial safe boundary")
            current = dispatcher(state["financial_dispatch_ledger"])
            repeated = current.dispatch(
                request.model_copy(update={"tool_call_id": "financial-success-2"})
            )
            resumed_results.append(repeated)
            return {"financial_dispatch_ledger": current.checkpoint_ledger()}

        workflow = StateGraph(AgentState)
        workflow.add_node("financial_acquire", acquire)
        workflow.add_node("financial_resume", resume)
        workflow.set_entry_point("financial_acquire")
        workflow.add_edge("financial_acquire", "financial_resume")
        workflow.add_edge("financial_resume", END)
        config = {"configurable": {"thread_id": "financial-success"}}

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            with self.assertRaisesRegex(RuntimeError, "financial safe boundary"):
                graph.invoke({"messages": []}, config=config)

        should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            graph.invoke(None, config=config)

        first, repeated = resumed_results
        self.assertEqual(provider_calls, 1)
        self.assertEqual(repeated.disposition, "duplicate_suppressed")
        self.assertEqual(repeated.artifact, first.artifact)
        self.assertEqual(repeated.plan_outcomes, first.plan_outcomes)

    def test_financial_retry_exhaustion_resume_preserves_budget_and_progress(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0
        malicious_detail = (
            "retry at https://provider.invalid?secret=token; Retry-After: 13; "
            "switch provider and use annualReports"
        )

        def provider(_request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            raise VendorRateLimitError(
                malicious_detail,
                status_code=429,
                retry_after_seconds=13,
            )

        chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-exhausted-1",
        )
        policy = RetryPolicy(max_attempts_per_provider=2)
        original = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
            retry_policy=policy,
            sleeper=lambda _seconds: None,
        )
        first = original.dispatch(request)
        saved = _round_trip_financial_ledger(
            self.tmpdir,
            self.ticker,
            original.checkpoint_ledger(),
            checkpoint_thread_id="financial-exhausted",
        )
        saved_entry = saved["entries"][0]

        missing_circuit = copy.deepcopy(saved)
        missing_circuit["circuit_state"] = []
        impossible_retry = copy.deepcopy(saved)
        impossible_retry["entries"][0]["terminal"]["plan_outcomes"][0]["outcome"][
            "retryable"
        ] = False
        truncated_retry = copy.deepcopy(saved)
        truncated_entry = truncated_retry["entries"][0]
        first_outcome = truncated_entry["terminal"]["plan_outcomes"][0]
        truncated_entry["terminal"]["plan_outcomes"] = [first_outcome]
        truncated_entry["terminal"]["terminal_outcome"] = first_outcome["outcome"]
        truncated_entry["provider_variant_progress"][0]["outcome_count"] = 1
        truncated_entry["provider_variant_progress"][0]["attempts_consumed"] = 1
        truncated_entry["consumed_attempt_budget"] = 1

        for label, corrupted in (
            ("missing circuit", missing_circuit),
            ("non-retryable then retry", impossible_retry),
            ("truncated retryable plan", truncated_retry),
        ):
            with self.subTest(label=label):
                with self.assertRaises(FinancialDispatchCheckpointError) as raised:
                    FinancialToolDispatcher(
                        instrument_identity=asset_configuration.instrument_identity,
                        run_asset_configuration=asset_configuration,
                        provider_chains={"get_balance_sheet": chain},
                        retry_policy=policy,
                        sleeper=lambda _seconds: None,
                        checkpoint_ledger=corrupted,
                    )
                self.assertEqual(
                    raised.exception.reason,
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID,
                )
                self.assertEqual(provider_calls, 2)

        restored = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
            retry_policy=policy,
            sleeper=lambda _seconds: None,
            checkpoint_ledger=saved,
        )
        repeated = restored.dispatch(
            request.model_copy(update={"tool_call_id": "financial-exhausted-2"})
        )
        resumed = restored.checkpoint_ledger()
        circuit_result = restored.dispatch(
            request.model_copy(
                update={
                    "as_of_date": date(2026, 7, 29),
                    "tool_call_id": "financial-circuit-preserved",
                }
            )
        )

        self.assertEqual(provider_calls, 2)
        self.assertEqual(repeated.disposition, "duplicate_suppressed")
        self.assertEqual(repeated.plan_outcomes, first.plan_outcomes)
        self.assertEqual(saved_entry["consumed_attempt_budget"], 2)
        self.assertEqual(
            resumed["entries"][0]["consumed_attempt_budget"],
            saved_entry["consumed_attempt_budget"],
        )
        self.assertEqual(
            resumed["entries"][0]["provider_variant_progress"],
            saved_entry["provider_variant_progress"],
        )
        self.assertEqual(resumed["circuit_state"], saved["circuit_state"])
        self.assertEqual(
            circuit_result.plan_outcomes[-1].outcome.reason,
            AcquisitionUnavailableReason.CIRCUIT_OPEN,
        )
        self.assertEqual(provider_calls, 2)
        self.assertNotIn(malicious_detail, json.dumps(resumed, sort_keys=True))

    def test_financial_non_retryable_failure_resume_reuses_without_provider_io(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0

        def provider(_request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.AUTHENTICATION,
                error_code="credentials_rejected",
                retryable=False,
            )

        chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.ANNUAL,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-terminal-1",
        )
        original = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
            checkpoint_ledger=None,
        )
        first = original.dispatch(request)
        saved = _round_trip_financial_ledger(
            self.tmpdir,
            self.ticker,
            original.checkpoint_ledger(),
            checkpoint_thread_id="financial-non-retryable",
        )
        restored = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
            checkpoint_ledger=saved,
        )
        repeated = restored.dispatch(
            request.model_copy(update={"tool_call_id": "financial-terminal-2"})
        )

        self.assertEqual(provider_calls, 1)
        self.assertEqual(repeated.disposition, "duplicate_suppressed")
        self.assertEqual(repeated.plan_outcomes, first.plan_outcomes)
        self.assertEqual(
            repeated.plan_outcomes[-1].outcome.reason,
            AcquisitionUnavailableReason.AUTHENTICATION,
        )

    def test_financial_corrupt_checkpoint_state_fails_typed_before_provider_io(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0

        def provider(_request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            return (
                "# Balance Sheet data for 601328.SS (quarterly)\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-corrupt-source",
        )
        original = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
        )
        original.dispatch(request)
        saved = original.checkpoint_ledger()
        expected_calls = provider_calls

        corruptions = {}
        policy_mismatch = copy.deepcopy(saved)
        policy_mismatch["acquisition_policy_version"] = "financial-acquisition:v2"
        corruptions["policy"] = (
            policy_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.POLICY_MISMATCH,
        )
        asset_mismatch = asset_configuration.model_copy(
            update={"registry_digest": "b" * 64}
        )
        corruptions["asset"] = (
            copy.deepcopy(saved),
            asset_mismatch,
            chain,
            FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH,
        )
        canonical_mismatch = copy.deepcopy(saved)
        canonical_mismatch["entries"][0]["canonical_request_key"]["request_key"] = (
            "financial-request:v1:" + "f" * 64
        )
        corruptions["canonical request"] = (
            canonical_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.CANONICAL_REQUEST_MISMATCH,
        )
        artifact_mismatch = copy.deepcopy(saved)
        artifact_mismatch["entries"][0]["artifact_sha256"] = "b" * 64
        corruptions["artifact"] = (
            artifact_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.ARTIFACT_REFERENCE_INVALID,
        )
        budget_mismatch = copy.deepcopy(saved)
        budget_mismatch["entries"][0]["consumed_attempt_budget"] += 1
        corruptions["budget"] = (
            budget_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID,
        )
        progress_mismatch = copy.deepcopy(saved)
        progress_mismatch["entries"][0]["provider_variant_progress"][0][
            "attempts_consumed"
        ] += 1
        corruptions["progress"] = (
            progress_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID,
        )
        terminal_mismatch = copy.deepcopy(saved)
        terminal_mismatch["entries"][0]["terminal"]["provider"] = "alternate"
        corruptions["terminal"] = (
            terminal_mismatch,
            asset_configuration,
            chain,
            FinancialDispatchCheckpointFailureReason.TERMINAL_OUTCOME_INVALID,
        )
        alternate_chain = (
            FinancialProvider(
                name="alternate",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        corruptions["provider chain"] = (
            copy.deepcopy(saved),
            asset_configuration,
            alternate_chain,
            FinancialDispatchCheckpointFailureReason.PROVIDER_CHAIN_MISMATCH,
        )

        for label, (ledger, run_asset, configured_chain, expected_reason) in (
            corruptions.items()
        ):
            with self.subTest(label=label):
                with self.assertRaises(FinancialDispatchCheckpointError) as raised:
                    FinancialToolDispatcher(
                        instrument_identity=run_asset.instrument_identity,
                        run_asset_configuration=run_asset,
                        provider_chains={"get_balance_sheet": configured_chain},
                        checkpoint_ledger=ledger,
                    )
                self.assertEqual(raised.exception.reason, expected_reason)
                self.assertEqual(provider_calls, expected_calls)

    def test_financial_distinct_request_after_resume_executes_independently(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0

        def provider(request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            return (
                f"# Balance Sheet data for 601328.SS ({request.frequency.value})\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-original",
        )
        original = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
        )
        first = original.dispatch(request)
        restored = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={"get_balance_sheet": chain},
            checkpoint_ledger=original.checkpoint_ledger(),
        )

        duplicate = restored.dispatch(
            request.model_copy(update={"tool_call_id": "financial-duplicate"})
        )
        distinct = restored.dispatch(
            request.model_copy(
                update={
                    "as_of_date": date(2026, 7, 29),
                    "tool_call_id": "financial-distinct",
                }
            )
        )

        self.assertEqual(provider_calls, 2)
        self.assertEqual(duplicate.disposition, "duplicate_suppressed")
        self.assertEqual(distinct.disposition, "executed")
        self.assertNotEqual(first.request_key, distinct.request_key)
        self.assertEqual(len(restored.checkpoint_ledger()["entries"]), 2)

    def test_financial_resume_restores_provider_variant_progress_without_replay(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = []

        def unavailable(label):
            def invoke(_request: FinancialToolRequest) -> str:
                provider_calls.append(label)
                raise AcquisitionFailure(
                    reason=AcquisitionUnavailableReason.NO_DATA,
                    retryable=False,
                )

            return invoke

        def available(_request: FinancialToolRequest) -> str:
            provider_calls.append("secondary:default")
            return (
                "# Balance Sheet data for 601328.SS (quarterly)\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        chains = {
            "get_balance_sheet": (
                FinancialProvider(
                    name="primary",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="legacy",
                            invoke=unavailable("primary:legacy"),
                        ),
                        FinancialProviderVariant(
                            variant_id="modern",
                            invoke=unavailable("primary:modern"),
                        ),
                    ),
                ),
                FinancialProvider(
                    name="secondary",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="default",
                            invoke=available,
                        ),
                    ),
                ),
            )
        }
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-progress-1",
        )
        policy = RetryPolicy(max_attempts_per_provider=2)
        original = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains=chains,
            retry_policy=policy,
        )
        first = original.dispatch(request)
        saved = original.checkpoint_ledger()
        calls_before_resume = list(provider_calls)

        truncated = copy.deepcopy(saved)
        truncated_entry = truncated["entries"][0]
        first_plan_outcome = truncated_entry["terminal"]["plan_outcomes"][0]
        truncated_entry["terminal"].update(
            {
                "value": None,
                "artifact": None,
                "plan_outcomes": [first_plan_outcome],
                "terminal_outcome": first_plan_outcome["outcome"],
                "provider": None,
                "variant_id": None,
            }
        )
        truncated_entry["provider_variant_progress"] = [
            truncated_entry["provider_variant_progress"][0]
        ]
        truncated_entry["consumed_attempt_budget"] = 1
        truncated_entry["artifact_sha256"] = None
        with self.assertRaises(FinancialDispatchCheckpointError) as raised:
            FinancialToolDispatcher(
                instrument_identity=asset_configuration.instrument_identity,
                run_asset_configuration=asset_configuration,
                provider_chains=chains,
                retry_policy=policy,
                checkpoint_ledger=truncated,
            )
        self.assertEqual(
            raised.exception.reason,
            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID,
        )
        self.assertEqual(provider_calls, calls_before_resume)

        restored = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains=chains,
            retry_policy=policy,
            checkpoint_ledger=saved,
        )
        repeated = restored.dispatch(
            request.model_copy(update={"tool_call_id": "financial-progress-2"})
        )

        self.assertEqual(
            calls_before_resume,
            ["primary:legacy", "primary:modern", "secondary:default"],
        )
        self.assertEqual(provider_calls, calls_before_resume)
        self.assertEqual(repeated.plan_outcomes, first.plan_outcomes)
        self.assertEqual(
            [
                (item["provider"], item["variant_id"], item["attempts_consumed"])
                for item in saved["entries"][0]["provider_variant_progress"]
            ],
            [
                ("primary", "legacy", 1),
                ("primary", "modern", 1),
                ("secondary", "default", 1),
            ],
        )
        self.assertEqual(saved["entries"][0]["consumed_attempt_budget"], 3)

    def test_financial_ledger_rejects_an_in_flight_unsafe_checkpoint_boundary(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_started = Event()
        release_provider = Event()

        def provider(_request: FinancialToolRequest) -> str:
            provider_started.set()
            self.assertTrue(release_provider.wait(timeout=5))
            return (
                "# Balance Sheet data for 601328.SS (quarterly)\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        dispatcher = FinancialToolDispatcher(
            instrument_identity=asset_configuration.instrument_identity,
            run_asset_configuration=asset_configuration,
            provider_chains={
                "get_balance_sheet": (
                    FinancialProvider(
                        name="primary",
                        variants=(
                            FinancialProviderVariant(
                                variant_id="default",
                                invoke=provider,
                            ),
                        ),
                    ),
                )
            },
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="financial-in-flight",
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dispatcher.dispatch, request)
            self.assertTrue(provider_started.wait(timeout=5))
            with self.assertRaises(FinancialDispatchCheckpointError) as raised:
                dispatcher.checkpoint_ledger()
            self.assertEqual(
                raised.exception.reason,
                FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY,
            )
            release_provider.set()
            future.result(timeout=5)

        self.assertEqual(len(dispatcher.checkpoint_ledger()["entries"]), 1)

    def test_legacy_checkpoint_is_unchanged_until_post_upgrade_dispatch_save(self):
        asset_configuration = resolve_run_asset_configuration(
            "601328.SS",
            config=copy.deepcopy(DEFAULT_CONFIG),
        )
        provider_calls = 0
        should_crash = True
        dispatch_results = []
        raw_legacy_text = (
            "provider said retry and switch variants at https://provider.invalid/secret"
        )

        def provider(_request: FinancialToolRequest) -> str:
            nonlocal provider_calls
            provider_calls += 1
            return (
                "# Balance Sheet data for 601328.SS (quarterly)\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "metric,2026-06-30\nTotal,100"
            )

        chain = (
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
        request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 28),
            tool_call_id="legacy-first",
        )

        def legacy_boundary(_state: AgentState) -> dict:
            return {
                "messages": [("assistant", raw_legacy_text)],
                "fundamentals_report": raw_legacy_text,
                "analysis_outcome": raw_legacy_text,
            }

        def post_upgrade_dispatch(state: AgentState) -> dict:
            nonlocal should_crash
            if should_crash:
                raise RuntimeError("simulated legacy checkpoint boundary")
            current = FinancialToolDispatcher(
                instrument_identity=asset_configuration.instrument_identity,
                run_asset_configuration=asset_configuration,
                provider_chains={"get_balance_sheet": chain},
                checkpoint_ledger=state.get("financial_dispatch_ledger"),
            )
            first = current.dispatch(request)
            duplicate = current.dispatch(
                request.model_copy(update={"tool_call_id": "legacy-duplicate"})
            )
            dispatch_results.extend((first, duplicate))
            return {"financial_dispatch_ledger": current.checkpoint_ledger()}

        workflow = StateGraph(AgentState)
        workflow.add_node("legacy_boundary", legacy_boundary)
        workflow.add_node("post_upgrade_dispatch", post_upgrade_dispatch)
        workflow.set_entry_point("legacy_boundary")
        workflow.add_edge("legacy_boundary", "post_upgrade_dispatch")
        workflow.add_edge("post_upgrade_dispatch", END)
        config = {"configurable": {"thread_id": "legacy-financial"}}

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            with self.assertRaisesRegex(RuntimeError, "legacy checkpoint boundary"):
                graph.invoke({"messages": []}, config=config)

        db_path = Path(self.tmpdir) / "checkpoints" / f"{self.ticker}.db"
        before_open = db_path.read_bytes()
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            saved = saver.get_tuple(config)
            self.assertIsNotNone(saved)
            self.assertNotIn(
                "financial_dispatch_ledger",
                saved.checkpoint["channel_values"],
            )
        self.assertEqual(db_path.read_bytes(), before_open)
        self.assertEqual(provider_calls, 0)

        should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = workflow.compile(checkpointer=saver)
            graph.invoke(None, config=config)
            saved = saver.get_tuple(config)
            persisted = saved.checkpoint["channel_values"][
                "financial_dispatch_ledger"
            ]

        first, duplicate = dispatch_results
        self.assertEqual(provider_calls, 1)
        self.assertEqual(first.disposition, "executed")
        self.assertEqual(duplicate.disposition, "duplicate_suppressed")
        self.assertEqual(persisted["contract_version"], "1.0")
        self.assertEqual(persisted["entries"][0]["reuse_count"], 1)
        self.assertNotIn(raw_legacy_text, json.dumps(persisted, sort_keys=True))

    def test_clear_checkpoint_allows_fresh_start(self):
        """After clearing, the graph starts from scratch."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Create a checkpoint by crashing
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Clear it
        clear_checkpoint(self.tmpdir, self.ticker, self.date)
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Fresh run succeeds from scratch
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config=cfg)

        self.assertEqual(result["count"], 11)

    def test_clear_checkpoint_surfaces_storage_failure(self):
        """A failed cleanup must not masquerade as a cleared checkpoint."""
        checkpoint_dir = Path(self.tmpdir) / "checkpoints"
        checkpoint_dir.mkdir()
        db_path = checkpoint_dir / f"{self.ticker}.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE writes (unexpected_column TEXT)")
            conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")

        with self.assertRaisesRegex(sqlite3.OperationalError, "thread_id"):
            clear_checkpoint(self.tmpdir, self.ticker, self.date)


    def test_different_date_starts_fresh(self):
        """A different date must NOT resume from an existing checkpoint."""
        global _should_crash
        builder = _build_graph()
        date2 = "2026-04-21"

        # Run with date1 — crash to leave a checkpoint
        _should_crash = True
        tid1 = thread_id(self.ticker, self.date)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # date2 should have no checkpoint
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, date2))

        # Run with date2 — should start fresh and succeed
        _should_crash = False
        tid2 = thread_id(self.ticker, date2)
        self.assertNotEqual(tid1, tid2)

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})

        # Fresh run: analyst +1, trader +10 = 11
        self.assertEqual(result["count"], 11)

        # Original date checkpoint still exists (untouched)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

    def test_trading_graph_checkpoint_scope_resumes_and_clears_completed_run(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        events = []
        should_crash = True

        def analyst(state):
            events.append("analyst")
            return {"count": state["count"] + 1}

        def trader(state):
            events.append("trader")
            if should_crash:
                raise RuntimeError("simulated mid-analysis crash")
            return {"count": state["count"] + 10}

        workflow = StateGraph(_SimpleState)
        workflow.add_node("analyst", analyst)
        workflow.add_node("trader", trader)
        workflow.set_entry_point("analyst")
        workflow.add_edge("analyst", "trader")
        workflow.add_edge("trader", END)

        graph = object.__new__(TradingAgentsGraph)
        graph.config = {
            "checkpoint_enabled": True,
            "data_cache_dir": self.tmpdir,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
        }
        graph.selected_analysts = ("market",)
        graph.decision_policy = None
        graph.decision_horizon = None
        graph.workflow = workflow
        graph.graph = graph.workflow.compile()
        signature = graph._run_signature("stock")

        with graph.checkpoint_scope(self.ticker, self.date, "stock") as session:
            config = session.graph_config
            self.assertFalse(session.resume_from_checkpoint)
            self.assertEqual(
                config["configurable"]["thread_id"],
                thread_id(self.ticker, self.date, signature),
            )
            with self.assertRaises(RuntimeError):
                graph.graph.invoke({"count": 0}, config=config)

        self.assertTrue(
            has_checkpoint(self.tmpdir, self.ticker, self.date, signature)
        )
        self.assertEqual(events, ["analyst", "trader"])

        should_crash = False
        with graph.checkpoint_scope(self.ticker, self.date, "stock") as session:
            config = session.graph_config
            self.assertTrue(session.resume_from_checkpoint)
            graph_input = None if session.resume_from_checkpoint else {"count": 0}
            result = graph.graph.invoke(graph_input, config=config)
            graph.clear_run_checkpoint(self.ticker, self.date, "stock")

        self.assertEqual(result["count"], 11)
        self.assertEqual(events, ["analyst", "trader", "trader"])
        self.assertFalse(
            has_checkpoint(self.tmpdir, self.ticker, self.date, signature)
        )


class TestCheckpointSignature(unittest.TestCase):
    """A different graph shape (analyst selection / depth / asset mode) must not
    resume the previous run's checkpoint (#1089)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_empty_signature_is_legacy_id(self):
        self.assertEqual(
            thread_id(self.ticker, self.date),
            thread_id(self.ticker, self.date, ""),
        )

    def test_signature_changes_thread_id(self):
        legacy = thread_id(self.ticker, self.date)
        sig_a = thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        sig_b = thread_id(self.ticker, self.date, "analysts=market|asset=stock")
        self.assertNotEqual(sig_a, sig_b)          # different graph shapes differ
        self.assertNotEqual(legacy, sig_a)         # signature-keyed differs from legacy
        self.assertEqual(                          # same inputs are stable
            sig_a, thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        )

    def test_symbol_aliases_share_canonical_checkpoint_identity(self):
        alias = "BTCUSD"
        canonical = "BTC-USD"
        signature = "analysts=market|asset=crypto"
        builder = _build_graph()
        config = {
            "configurable": {
                "thread_id": thread_id(alias, self.date, signature),
            }
        }

        global _should_crash
        _should_crash = True
        with get_checkpointer(self.tmpdir, alias) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=config)

        self.assertEqual(
            thread_id(alias, self.date, signature),
            thread_id(canonical, self.date, signature),
        )
        self.assertTrue(
            has_checkpoint(self.tmpdir, canonical, self.date, signature)
        )

    def test_different_signature_starts_fresh(self):
        global _should_crash
        builder = _build_graph()
        sig1 = "analysts=market,news,fundamentals|asset=stock"
        sig2 = "analysts=market|asset=stock"       # dropped analysts -> different graph

        _should_crash = True
        tid1 = thread_id(self.ticker, self.date, sig1)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))
        # A different graph shape has no checkpoint to resume from.
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date, sig2))

        _should_crash = False
        tid2 = thread_id(self.ticker, self.date, sig2)
        self.assertNotEqual(tid1, tid2)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})
        self.assertEqual(result["count"], 11)
        # sig1's checkpoint remains untouched.
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))

    def test_run_signature_captures_graph_shape(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # Build a bare instance to exercise the pure helper without heavy __init__.
        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        base = g._run_signature("stock")
        self.assertIn("admission_binding=1", base)
        legacy = base.replace("|admission_binding=1", "")
        self.assertNotEqual(
            thread_id(self.ticker, self.date, legacy),
            thread_id(self.ticker, self.date, base),
        )

        self.assertNotEqual(base, g._run_signature("crypto"))     # asset mode
        g.selected_analysts = ("market",)
        self.assertNotEqual(base, g._run_signature("stock"))      # analyst selection
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 3, "max_risk_discuss_rounds": 1}
        self.assertNotEqual(base, g._run_signature("stock"))      # debate depth
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 5}
        self.assertNotEqual(base, g._run_signature("stock"))      # risk depth
        # Stable for identical inputs.
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        self.assertEqual(base, g._run_signature("stock"))

    def test_run_signature_captures_policy_and_horizon_contract(self):
        from tradingagents.decision_policy import DecisionHorizon, HorizonUnit
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market",)
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        g.decision_policy = type("Policy", (), {"registry_digest": "registry:a"})()
        g.decision_horizon = None
        without_horizon = g._run_signature("stock")

        g.decision_horizon = DecisionHorizon(count=5, unit=HorizonUnit.TRADING_DAYS)
        with_horizon = g._run_signature("stock")

        self.assertIn("evidence_schema=4", with_horizon)
        self.assertIn("decision_schema=1", with_horizon)
        self.assertIn("registry=registry:a", with_horizon)
        self.assertIn("horizon=5:trading_days", with_horizon)
        self.assertNotEqual(without_horizon, with_horizon)


if __name__ == "__main__":
    unittest.main()
