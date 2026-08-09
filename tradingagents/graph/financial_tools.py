"""Compiled-graph boundary for deterministic company-financial tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import date, datetime
from typing import Any

from tradingagents.asset_configuration import (
    RunAssetConfiguration,
    RunAssetConfigurationProjection,
)
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointCompatibility,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingRunProjection,
    MainlandCapabilityRoutingShadowFailure,
    TushareCapability,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.acquisition import RetryPolicy
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_dispatch import (
    FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialDispatchCheckpointLedger,
    FinancialIndicatorRequestArguments,
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_selection_evidence,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestCache
from tradingagents.evidence import EvidenceState
from tradingagents.qualified_routing_eligibility_lifecycle import (
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleError,
)

_FINANCIAL_TOOL_CONTRACTS = {
    "get_fundamentals": (
        FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        FinancialReportingFrequency.NOT_APPLICABLE,
    ),
    "get_balance_sheet": (
        FinancialStatementType.BALANCE_SHEET,
        FinancialReportingFrequency.QUARTERLY,
    ),
    "get_cashflow": (
        FinancialStatementType.CASH_FLOW,
        FinancialReportingFrequency.QUARTERLY,
    ),
    "get_income_statement": (
        FinancialStatementType.INCOME_STATEMENT,
        FinancialReportingFrequency.QUARTERLY,
    ),
}

class FinancialDispatchToolNode:
    """Execute one fundamentals tool turn through a checkpoint-aware dispatcher."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        vendor_methods: Mapping[str, Mapping[str, object]] | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        qualified_statement_router: MainlandFinancialCapabilityRouter | None = None,
        qualified_statement_request_factory: (
            Callable[[FinancialToolRequest], FinancialStatementRoutingRequest]
            | None
        ) = None,
        qualified_indicator_request_factory: (
            Callable[[FinancialToolRequest], FinancialIndicatorRoutingRequest]
            | None
        ) = None,
        qualified_provider_subrequest_cache: ProviderSubrequestCache | None = None,
        restored_shadow_dispatcher: FinancialToolDispatcher | None = None,
        qualified_context_owner: (
            FinancialEligibilityAcquisitionContextOwner | None
        ) = None,
    ) -> None:
        if qualified_context_owner is not None:
            if (
                qualified_statement_request_factory is not None
                or qualified_indicator_request_factory is not None
            ):
                raise ValueError(
                    "context-owned qualified routing cannot expose direct factories"
                )
            has_owner_wiring = any(
                value is not None
                for value in (
                    qualified_statement_router,
                    qualified_provider_subrequest_cache,
                    restored_shadow_dispatcher,
                )
            )
            if has_owner_wiring:
                try:
                    owner_routing = qualified_context_owner.financial_routing
                except ProductionQualifiedRoutingLifecycleError as exc:
                    raise ValueError(
                        "context-owned qualified routing is not completely restored"
                    ) from exc
                if (
                    qualified_statement_router is not owner_routing.router
                    or qualified_provider_subrequest_cache
                    is not owner_routing.provider_subrequest_cache
                    or restored_shadow_dispatcher
                    is not owner_routing.restored_shadow_dispatcher
                ):
                    raise ValueError(
                        "context-owned qualified routing wiring is not owner-owned"
                    )
        has_factory = (
            qualified_statement_request_factory is not None
            or qualified_indicator_request_factory is not None
        )
        if qualified_context_owner is None and (
            (qualified_statement_router is None) == has_factory
        ):
            raise ValueError(
                "qualified router requires at least one matching request factory"
            )
        if qualified_statement_router is not None and config is not None:
            if str(
                config.get("mainland_capability_routing_mode", "legacy")
            ).strip().casefold() not in {"qualified_v1", "qualified_v1_shadow"}:
                raise ValueError(
                    "qualified financial tool node requires explicit qualified_v1 mode"
                )
            configured_signature = config.get(
                "mainland_capability_routing_plan_signature"
            )
            if (
                configured_signature is not None
                and str(configured_signature)
                != qualified_statement_router.routing_plan.plan_signature
            ):
                raise ValueError(
                    "financial tool node Capability Routing Plan is immutable"
                )
        if qualified_context_owner is not None and config is not None:
            if str(
                config.get("mainland_capability_routing_mode", "legacy")
            ).strip().casefold() != "qualified_v1_shadow":
                raise ValueError(
                    "eligibility context owner requires qualified_v1_shadow mode"
                )
            configured_signature = config.get(
                "mainland_capability_routing_plan_signature"
            )
            if (
                configured_signature is not None
                and str(configured_signature)
                != qualified_context_owner.routing_plan.plan_signature
            ):
                raise ValueError(
                    "financial tool node Capability Routing Plan is immutable"
                )
        self._config = dict(config) if config is not None else None
        self._vendor_methods = vendor_methods
        self._retry_policy = retry_policy
        self._clock = clock
        self._sleeper = sleeper
        self._qualified_statement_router = qualified_statement_router
        self._qualified_statement_request_factory = (
            qualified_statement_request_factory
        )
        self._qualified_indicator_request_factory = (
            qualified_indicator_request_factory
        )
        self._qualified_provider_subrequest_cache = (
            qualified_provider_subrequest_cache
        )
        self._restored_shadow_dispatcher = restored_shadow_dispatcher
        self._qualified_context_owner = qualified_context_owner
        if restored_shadow_dispatcher is not None and (
            qualified_statement_router is None
            or qualified_provider_subrequest_cache is None
        ):
            raise ValueError(
                "restored shadow dispatcher requires qualified run wiring"
            )

    def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        messages = state.get("messages") or ()
        if not messages:
            raise ValueError("financial tool node requires a model tool-call message")
        model_message = messages[-1]
        tool_calls = getattr(model_message, "tool_calls", None) or ()
        if not tool_calls:
            raise ValueError("financial tool node requires at least one tool call")

        rollout_mode = self._routing_mode()
        configured_mode = str(
            (self._config or {}).get(
                "mainland_capability_routing_mode",
                "legacy",
            )
        ).strip().casefold()
        if rollout_mode == "qualified_v1" and configured_mode != rollout_mode:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
            )
        dispatcher, evidence = self._dispatcher_from_state(state)
        graph_message_id = getattr(model_message, "id", None)
        requests = [
            _financial_request_from_tool_call(
                tool_call,
                canonical_symbol=evidence.instrument_identity.symbol,
                trade_date=str(state.get("trade_date") or ""),
                graph_message_id=(
                    str(graph_message_id) if graph_message_id is not None else None
                ),
            )
            for tool_call in tool_calls
        ]
        qualified_routing: QualifiedFinancialRoutingComposition | None = None
        shadow_dispatcher = None
        shadow_checkpoint_dispatcher = None
        tool_messages = []
        shadow_executed = False
        shadow_failure_count = 0
        if rollout_mode == "qualified_v1_shadow":
            context_owner = self._qualified_context_owner
            qualified_request = next(
                (request for request in requests if self._qualified_route_enabled(request)),
                None,
            )
            if context_owner is not None and qualified_request is not None:
                try:
                    acquisition = context_owner.acquire_context(
                        as_of_date=qualified_request.as_of_date
                    )
                    if acquisition.context is None:
                        shadow_failure_count += 1
                    else:
                        qualified_routing = context_owner.financial_routing
                        shadow_dispatcher = self._dispatcher_from_state(
                            state,
                            shadow=True,
                            qualified_router_override=qualified_routing.router,
                            provider_subrequest_cache_override=(
                                qualified_routing.provider_subrequest_cache
                            ),
                        )[0]
                        shadow_checkpoint_dispatcher = shadow_dispatcher
                except Exception:  # noqa: BLE001 - shadow cannot affect authority
                    shadow_failure_count += 1
            elif context_owner is None:
                shadow_dispatcher = self._dispatcher_from_state(
                    state,
                    shadow=True,
                )[0]
                shadow_checkpoint_dispatcher = shadow_dispatcher
        active_statement_factory = (
            qualified_routing.statement_request_factory
            if qualified_routing is not None
            else self._qualified_statement_request_factory
        )
        active_indicator_factory = (
            qualified_routing.indicator_request_factory
            if qualified_routing is not None
            else self._qualified_indicator_request_factory
        )
        for request in requests:
            if rollout_mode == "qualified_v1_shadow":
                tool_messages.append(dispatcher.dispatch_tool_message(request))
                if self._qualified_route_enabled(request) and shadow_dispatcher is not None:
                    shadow_executed = True
                    try:
                        if (
                            request.statement_type
                            is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
                            and active_indicator_factory is not None
                        ):
                            selection_request = (
                                shadow_dispatcher.restored_indicator_selection_request(
                                    request
                                )
                                or active_indicator_factory(request)
                            )
                            shadow_dispatcher.dispatch_indicator_selection(
                                request,
                                selection_request,
                            )
                        elif active_statement_factory is not None:
                            selection_request = (
                                shadow_dispatcher.restored_statement_selection_request(
                                    request
                                )
                                or active_statement_factory(request)
                            )
                            shadow_dispatcher.dispatch_statement_selection(
                                request,
                                selection_request,
                            )
                    except Exception:  # noqa: BLE001 - shadow cannot affect authority
                        shadow_failure_count += 1
                        with suppress(Exception):
                            shadow_dispatcher.discard_failed_qualified_selection(request)
            elif (
                self._qualified_route_enabled(request)
                and
                request.statement_type
                is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
                and self._qualified_indicator_request_factory is not None
            ):
                dispatch_result = dispatcher.dispatch_indicator_selection(
                    request,
                    self._qualified_indicator_request_factory(request),
                )
                evidence = project_financial_selection_evidence(
                    evidence,
                    dispatch_result,
                )
                tool_messages.append(dispatch_result.to_tool_message())
            elif (
                self._qualified_route_enabled(request)
                and
                request.statement_type
                is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
                and self._qualified_statement_request_factory is not None
            ):
                dispatch_result = dispatcher.dispatch_statement_selection(
                    request,
                    self._qualified_statement_request_factory(request),
                )
                evidence = project_financial_selection_evidence(
                    evidence,
                    dispatch_result,
                )
                tool_messages.append(dispatch_result.to_tool_message())
            else:
                tool_messages.append(dispatcher.dispatch_tool_message(request))
        from tradingagents.dataflows.market_snapshot import (
            refresh_active_evidence_physical_attempts,
        )

        evidence = refresh_active_evidence_physical_attempts(evidence)
        result = {
            "messages": tool_messages,
            "evidence_state": evidence.model_dump(mode="json"),
            "financial_dispatch_ledger": dispatcher.checkpoint_ledger(),
        }
        if shadow_checkpoint_dispatcher is not None:
            try:
                shadow_ledger = shadow_checkpoint_dispatcher.checkpoint_ledger()
            except Exception:  # noqa: BLE001 - shadow cannot affect authority
                shadow_failure_count += 1
            else:
                if shadow_executed or state.get("financial_dispatch_shadow_ledger"):
                    result["financial_dispatch_shadow_ledger"] = shadow_ledger
        if shadow_failure_count:
            result["financial_dispatch_shadow_failure"] = {
                "contract_version": "1.0",
                "diagnostic_code": "shadow_dispatch_failed",
                "failure_count": shadow_failure_count,
            }
        return result

    def _qualified_route_enabled(self, request: FinancialToolRequest) -> bool:
        router = self._qualified_statement_router
        context_owner = self._qualified_context_owner
        if router is None and context_owner is None:
            return False
        required = (
            TushareCapability.FINANCIAL_INDICATORS
            if request.statement_type
            is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
            else TushareCapability.STATEMENTS
        )
        plan = router.routing_plan if router is not None else context_owner.routing_plan
        return required in plan.enabled_tushare_capabilities

    def _routing_mode(self) -> str:
        router = self._qualified_statement_router
        if router is not None:
            return router.routing_plan.mode.value
        context_owner = self._qualified_context_owner
        if context_owner is not None:
            return context_owner.routing_plan.mode.value
        config = self._config or {}
        configured_mode = str(
            config.get("mainland_capability_routing_mode", "legacy")
        )
        if configured_mode == "qualified_v1_shadow":
            return "legacy"
        return configured_mode

    def validate_checkpoint_state(
        self,
        state: Mapping[str, Any],
        *,
        expected_asset_configuration: (
            RunAssetConfiguration | RunAssetConfigurationProjection | None
        ),
        expected_capability_routing_plan: MainlandCapabilityRoutingPlan | None = None,
    ) -> MainlandCapabilityRoutingCheckpointCompatibility | None:
        """Fail closed on contradictory restored state before graph work begins."""

        compatibility = None
        if expected_capability_routing_plan is not None:
            compatibility = validate_mainland_capability_routing_checkpoint(
                active_plan=expected_capability_routing_plan,
                checkpoint_plan=state.get("capability_routing_plan"),
            )
            expected_checkpoint_rollout = compatibility.checkpoint
            raw_rollout = state.get("capability_routing_rollout")
            try:
                restored_rollout = (
                    None
                    if raw_rollout is None
                    else MainlandCapabilityRoutingRunProjection.model_validate(
                        raw_rollout
                    )
                )
                if (
                    restored_rollout is not None
                    and restored_rollout != expected_checkpoint_rollout
                ):
                    raise ValueError("checkpoint rollout contradicts its plan")
                raw_stored_compatibility = state.get(
                    "capability_routing_checkpoint_compatibility"
                )
                if raw_stored_compatibility is not None:
                    stored_compatibility = (
                        MainlandCapabilityRoutingCheckpointCompatibility.model_validate(
                            raw_stored_compatibility
                        )
                    )
                    if stored_compatibility.active != compatibility.active:
                        raise ValueError(
                            "checkpoint compatibility contradicts active rollout"
                        )
                raw_shadow_failure = state.get("financial_dispatch_shadow_failure")
                if raw_shadow_failure is not None:
                    MainlandCapabilityRoutingShadowFailure.model_validate(
                        raw_shadow_failure
                    )
                    if expected_capability_routing_plan.mode.value != (
                        "qualified_v1_shadow"
                    ):
                        raise ValueError(
                            "shadow failure contradicts active routing mode"
                        )
            except (TypeError, ValueError) as exc:
                raise MainlandCapabilityRoutingCheckpointError(
                    MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
                ) from exc
            if (
                compatibility.action
                is MainlandCapabilityRoutingCheckpointAction.START_NEW
            ):
                return compatibility
        authoritative_ledger = state.get("financial_dispatch_ledger")
        shadow_ledger = state.get("financial_dispatch_shadow_ledger")
        if authoritative_ledger is None and shadow_ledger is None:
            return compatibility
        try:
            if expected_asset_configuration is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
                )
            expected = RunAssetConfigurationProjection.model_validate(
                expected_asset_configuration.model_dump(mode="json")
            )
            restored = RunAssetConfigurationProjection.model_validate(
                state.get("asset_configuration")
            )
            if restored != expected:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
                )
            if authoritative_ledger is not None:
                self._dispatcher_from_state(state)
            if shadow_ledger is not None:
                if self._qualified_statement_router is None:
                    ledger = FinancialDispatchCheckpointLedger.model_validate(
                        shadow_ledger
                    )
                    expected_binding = (
                        expected_capability_routing_plan.financial_eligibility_binding
                        if expected_capability_routing_plan is not None
                        else None
                    )
                    if (
                        expected_capability_routing_plan is None
                        or ledger.contract_version
                        != FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION
                        or ledger.capability_routing_plan_signature
                        != expected_capability_routing_plan.plan_signature
                        or ledger.financial_eligibility_binding != expected_binding
                        or ledger.run_scope_id != state.get("run_id")
                        or ledger.provider_subrequest_cache is None
                    ):
                        raise FinancialDispatchCheckpointError(
                            FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
                        )
                else:
                    self._dispatcher_from_state(state, shadow=True)
        except FinancialDispatchCheckpointError:
            raise
        except (TypeError, ValueError) as exc:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.MALFORMED
            ) from exc
        return compatibility

    def _dispatcher_from_state(
        self,
        state: Mapping[str, Any],
        *,
        shadow: bool = False,
        qualified_router_override: MainlandFinancialCapabilityRouter | None = None,
        provider_subrequest_cache_override: ProviderSubrequestCache | None = None,
    ) -> tuple[FinancialToolDispatcher, EvidenceState]:
        evidence = EvidenceState.model_validate(state.get("evidence_state") or {})
        identity = evidence.instrument_identity
        if identity is None or not identity.is_authoritative:
            raise ValueError("financial tool node requires authoritative Instrument Identity")
        asset_payload = state.get("asset_configuration")
        if not isinstance(asset_payload, Mapping):
            raise ValueError("financial tool node requires RunAssetConfiguration")
        asset_configuration = RunAssetConfigurationProjection.model_validate(asset_payload)
        effective_config = self._config
        if effective_config is None:
            from tradingagents.dataflows.config import get_config

            effective_config = get_config()
        effective_config = dict(effective_config)
        rollout_mode = (
            qualified_router_override.routing_plan.mode.value
            if qualified_router_override is not None
            else self._routing_mode()
        )
        qualified_router = (
            qualified_router_override or self._qualified_statement_router
        )
        checkpoint_key = "financial_dispatch_ledger"
        if rollout_mode == "qualified_v1_shadow":
            if shadow:
                checkpoint_key = "financial_dispatch_shadow_ledger"
            else:
                effective_config["mainland_capability_routing_mode"] = "legacy"
                effective_config.pop(
                    "mainland_capability_routing_plan_signature",
                    None,
                )
                qualified_router = None
        elif shadow:
            raise ValueError("shadow dispatcher requires qualified_v1_shadow mode")
        if qualified_router is not None:
            expected_binding = (
                qualified_router.routing_plan.financial_eligibility_binding
            )
            if expected_binding is None:
                if (
                    state.get("financial_eligibility_binding") is not None
                    or evidence.financial_eligibility_binding is not None
                ):
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                    )
            elif (
                state.get("financial_eligibility_binding")
                != expected_binding.model_dump(mode="json")
                or evidence.financial_eligibility_binding != expected_binding
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                )
        retry_policy = self._retry_policy
        if retry_policy is None:
            configured_retry_policy = effective_config.get("financial_dispatch_retry_policy")
            if configured_retry_policy is not None:
                retry_policy = RetryPolicy.model_validate(configured_retry_policy)
        restored_dispatcher = self._restored_shadow_dispatcher
        if shadow and restored_dispatcher is not None:
            if restored_dispatcher.checkpoint_ledger() != state.get(checkpoint_key):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                )
            return restored_dispatcher, evidence
        checkpoint_ledger = state.get(checkpoint_key)
        if (
            rollout_mode == "qualified_v1_shadow"
            and not shadow
            and isinstance(checkpoint_ledger, Mapping)
            and checkpoint_ledger.get("provider_subrequest_cache") is not None
        ):
            authoritative_cache = checkpoint_ledger.get(
                "provider_subrequest_cache"
            )
            shadow_ledger = state.get("financial_dispatch_shadow_ledger")
            mirrored_shadow_cache = (
                isinstance(shadow_ledger, Mapping)
                and shadow_ledger.get("provider_subrequest_cache")
                == authoritative_cache
            )
            empty_historical_cache = (
                isinstance(authoritative_cache, Mapping)
                and set(authoritative_cache)
                == {"contract_version", "run_scope_id", "entries"}
                and authoritative_cache.get("contract_version") == "1.0"
                and authoritative_cache.get("run_scope_id") == state.get("run_id")
                and authoritative_cache.get("entries") in ([], ())
            )
            if mirrored_shadow_cache or empty_historical_cache:
                checkpoint_ledger = dict(checkpoint_ledger)
                checkpoint_ledger.pop("provider_subrequest_cache", None)
        qualified_cache = (
            provider_subrequest_cache_override
            or self._qualified_provider_subrequest_cache
        )
        context_owner = self._qualified_context_owner
        dispatcher_context = (
            context_owner.context
            if (
                context_owner is not None
                and context_owner.checkpoint_thread_id is not None
                and qualified_cache is not None
            )
            else None
        )
        dispatcher = FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=identity,
            run_asset_configuration=asset_configuration,
            config=effective_config,
            vendor_methods=self._vendor_methods,
            retry_policy=retry_policy,
            clock=self._clock,
            sleeper=self._sleeper,
            checkpoint_ledger=checkpoint_ledger,
            provider_subrequest_cache=(
                qualified_cache
                if qualified_router is not None
                else None
            ),
            qualified_statement_router=qualified_router,
            run_scope_id=(
                str(state["run_id"])
                if state.get("run_id") is not None
                else None
            ),
            financial_eligibility_context=(
                dispatcher_context if qualified_router is not None else None
            ),
            checkpoint_thread_id=(
                context_owner.checkpoint_thread_id
                if qualified_router is not None and dispatcher_context is not None
                else None
            ),
        )
        return dispatcher, evidence

    def restore_shadow_dispatcher(
        self,
        state: Mapping[str, Any],
        *,
        provider_subrequest_cache: ProviderSubrequestCache,
        routing_plan: MainlandCapabilityRoutingPlan,
    ) -> FinancialToolDispatcher:
        """Restore the qualified ledger against a non-callable validation router."""

        validation_router = MainlandFinancialCapabilityRouter(
            routing_plan=routing_plan,
            statement_sources={},
            indicator_sources={},
            capability_sources={},
        )
        return self._dispatcher_from_state(
            state,
            shadow=True,
            qualified_router_override=validation_router,
            provider_subrequest_cache_override=provider_subrequest_cache,
        )[0]


def _financial_request_from_tool_call(
    tool_call: Mapping[str, Any],
    *,
    canonical_symbol: str,
    trade_date: str,
    graph_message_id: str | None,
) -> FinancialToolRequest:
    tool_name = str(tool_call.get("name") or "")
    try:
        statement_type, default_frequency = _FINANCIAL_TOOL_CONTRACTS[tool_name]
    except KeyError as exc:
        raise ValueError("unsupported financial tool call") from exc
    raw_arguments = tool_call.get("args") or {}
    if not isinstance(raw_arguments, Mapping):
        raise ValueError("financial tool arguments must be a mapping")
    material_arguments = dict(raw_arguments)
    raw_indicator_arguments = material_arguments.pop(
        "indicator_request_arguments",
        None,
    )
    requested_symbol = str(
        material_arguments.pop("ticker", material_arguments.pop("symbol", ""))
    ).strip()
    if requested_symbol.upper() != canonical_symbol.strip().upper():
        raise ValueError("financial tool symbol contradicts Instrument Identity")
    raw_frequency = material_arguments.pop(
        "freq",
        material_arguments.pop("frequency", default_frequency.value),
    )
    frequency = (
        FinancialReportingFrequency.NOT_APPLICABLE
        if statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
        else FinancialReportingFrequency(str(raw_frequency).strip().casefold())
    )
    if raw_indicator_arguments is not None and (
        statement_type is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
        or not isinstance(raw_indicator_arguments, Mapping)
    ):
        raise ValueError("financial indicator request arguments are incompatible")
    indicator_request_arguments = (
        FinancialIndicatorRequestArguments.model_validate(raw_indicator_arguments)
        if raw_indicator_arguments is not None
        else None
    )
    raw_as_of_date = material_arguments.pop(
        "curr_date",
        material_arguments.pop("as_of_date", trade_date),
    )
    return FinancialToolRequest(
        tool_name=tool_name,
        statement_type=statement_type,
        frequency=frequency,
        as_of_date=date.fromisoformat(str(raw_as_of_date)),
        material_arguments=material_arguments,
        tool_call_id=str(tool_call.get("id") or ""),
        graph_message_id=graph_message_id,
        indicator_request_arguments=indicator_request_arguments,
    )


__all__ = [
    "FinancialEligibilityAcquisitionContextOwner",
    "FinancialDispatchToolNode",
    "QualifiedFinancialRoutingComposition",
]
