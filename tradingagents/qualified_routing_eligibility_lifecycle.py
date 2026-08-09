"""Run-host ownership and first-use lifecycle for qualified eligibility context."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.capability_routing import MainlandCapabilityRoutingPlan, TushareCapability
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialConsolidationScope,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    FinancialEligibilityRequestBinding,
    validate_financial_eligibility_acquisition_context_binding,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquisitionRequest,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestCache
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
)


@dataclass(frozen=True)
class QualifiedFinancialRoutingComposition:
    """Immutable graph-owned wiring for qualified mainland financial routing."""

    router: MainlandFinancialCapabilityRouter
    statement_request_factory: (
        Callable[[FinancialToolRequest], FinancialStatementRoutingRequest] | None
    ) = None
    indicator_request_factory: (
        Callable[[FinancialToolRequest], FinancialIndicatorRoutingRequest] | None
    ) = None
    provider_subrequest_cache: ProviderSubrequestCache | None = None
    restored_shadow_dispatcher: FinancialToolDispatcher | None = None

    def __post_init__(self) -> None:
        financial_capabilities = {
            TushareCapability.STATEMENTS,
            TushareCapability.FINANCIAL_INDICATORS,
        }
        if (
            self.statement_request_factory is None
            and self.indicator_request_factory is None
            and financial_capabilities.intersection(
                self.router.routing_plan.enabled_tushare_capabilities
            )
        ):
            raise ValueError(
                "qualified financial routing requires at least one request factory"
            )
        if self.router.routing_plan.mode.value not in {
            "qualified_v1",
            "qualified_v1_shadow",
        }:
            raise ValueError(
                "qualified financial routing requires a qualified_v1 plan"
            )
        if self.restored_shadow_dispatcher is not None and (
            self.provider_subrequest_cache is None
            or not isinstance(
                self.restored_shadow_dispatcher,
                FinancialToolDispatcher,
            )
        ):
            raise ValueError(
                "restored shadow dispatcher requires its run-owned cache"
            )


class FinancialEligibilityAcquisitionContextOwner:
    """One process-local, single-assignment context slot for a qualified run."""

    def __init__(
        self,
        *,
        run_id: str,
        asset_configuration: RunAssetConfiguration,
        routing_plan: MainlandCapabilityRoutingPlan,
        metadata_acquirer: object,
        unbound_financial_routing: QualifiedFinancialRoutingComposition,
        expected_as_of_date: date | None,
        availability_scope: FinancialAvailabilityScope,
        consolidation_scope: FinancialConsolidationScope,
        retained_company_type_evidence: Sequence[object] = (),
        retained_listing_metadata: Sequence[object] = (),
        checkpoint_thread_id: str | None = None,
        interrupt_runtime: Callable[[], object],
        close_runtime: Callable[[], object],
    ) -> None:
        acquire = getattr(metadata_acquirer, "acquire", None)
        if (
            not isinstance(run_id, str)
            or not isinstance(asset_configuration, RunAssetConfiguration)
            or not isinstance(routing_plan, MainlandCapabilityRoutingPlan)
            or not callable(acquire)
            or not isinstance(
                unbound_financial_routing,
                QualifiedFinancialRoutingComposition,
            )
            or unbound_financial_routing.router.routing_plan != routing_plan
            or routing_plan.financial_eligibility_binding is None
            or (
                expected_as_of_date is not None
                and (
                    not isinstance(expected_as_of_date, date)
                    or isinstance(expected_as_of_date, datetime)
                )
            )
            or not isinstance(availability_scope, FinancialAvailabilityScope)
            or not isinstance(consolidation_scope, FinancialConsolidationScope)
            or not callable(interrupt_runtime)
            or not callable(close_runtime)
            or (
                checkpoint_thread_id is not None
                and (
                    len(checkpoint_thread_id) != 16
                    or any(
                        character not in "0123456789abcdef"
                        for character in checkpoint_thread_id
                    )
                    or run_id
                    != "run:"
                    + sha256(
                        f"checkpoint:{checkpoint_thread_id}".encode()
                    ).hexdigest()
                )
            )
        ):
            self._fail_binding()
        self._run_id = run_id
        self._asset_configuration = asset_configuration
        self._routing_plan = routing_plan
        self._metadata_acquirer = metadata_acquirer
        self._unbound_financial_routing = unbound_financial_routing
        self._expected_as_of_date = expected_as_of_date
        self._availability_scope = availability_scope
        self._consolidation_scope = consolidation_scope
        self._retained_company_type_evidence = tuple(
            retained_company_type_evidence
        )
        self._retained_listing_metadata = tuple(retained_listing_metadata)
        self._checkpoint_thread_id = checkpoint_thread_id
        self._interrupt_runtime = interrupt_runtime
        self._close_runtime = close_runtime
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._context: FinancialEligibilityAcquisitionContext | None = None
        self._bound_financial_routing: QualifiedFinancialRoutingComposition | None = None
        self._acquisition_result: FinancialEligibilityAcquisitionResult | None = None
        self._acquiring = False
        self._cleanup_started = False
        self._interruption_requested = False

    @classmethod
    def from_restored_context(
        cls,
        *,
        run_id: str,
        checkpoint_thread_id: str,
        asset_configuration: RunAssetConfiguration,
        routing_plan: MainlandCapabilityRoutingPlan,
        context: FinancialEligibilityAcquisitionContext,
        expected_as_of_date: date | None,
        availability_scope: FinancialAvailabilityScope,
        consolidation_scope: FinancialConsolidationScope,
        interrupt_runtime: Callable[[], object],
        close_runtime: Callable[[], object],
    ) -> FinancialEligibilityAcquisitionContextOwner:
        """Create a restoration-only owner without a metadata acquisition seam."""

        if (
            not isinstance(run_id, str)
            or len(checkpoint_thread_id) != 16
            or any(
                character not in "0123456789abcdef"
                for character in checkpoint_thread_id
            )
            or run_id
            != "run:"
            + sha256(f"checkpoint:{checkpoint_thread_id}".encode()).hexdigest()
            or not isinstance(asset_configuration, RunAssetConfiguration)
            or not isinstance(routing_plan, MainlandCapabilityRoutingPlan)
            or routing_plan.financial_eligibility_binding is None
            or not isinstance(availability_scope, FinancialAvailabilityScope)
            or not isinstance(consolidation_scope, FinancialConsolidationScope)
            or not callable(interrupt_runtime)
            or not callable(close_runtime)
        ):
            cls._fail_binding()
        owner = cls.__new__(cls)
        owner._run_id = run_id
        owner._asset_configuration = asset_configuration
        owner._routing_plan = routing_plan
        owner._metadata_acquirer = None
        owner._unbound_financial_routing = None
        owner._expected_as_of_date = expected_as_of_date
        owner._availability_scope = availability_scope
        owner._consolidation_scope = consolidation_scope
        owner._retained_company_type_evidence = ()
        owner._retained_listing_metadata = ()
        owner._checkpoint_thread_id = checkpoint_thread_id
        owner._interrupt_runtime = interrupt_runtime
        owner._close_runtime = close_runtime
        owner._lock = threading.RLock()
        owner._condition = threading.Condition(owner._lock)
        owner._context = None
        owner._bound_financial_routing = None
        owner._acquisition_result = None
        owner._acquiring = False
        owner._cleanup_started = False
        owner._interruption_requested = False
        owner._validate_context(context)
        owner._context = context
        owner._acquisition_result = FinancialEligibilityAcquisitionResult.succeeded(
            context
        )
        return owner

    @staticmethod
    def _fail_binding() -> None:
        raise ProductionQualifiedRoutingLifecycleError(
            ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
        )

    @property
    def context(self) -> FinancialEligibilityAcquisitionContext | None:
        with self._lock:
            return self._context

    @property
    def context_identity(self) -> str | None:
        context = self.context
        return context.context_identity if context is not None else None

    @property
    def routing_plan(self) -> MainlandCapabilityRoutingPlan:
        return self._routing_plan

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def checkpoint_thread_id(self) -> str | None:
        return self._checkpoint_thread_id

    def validate_host_binding(
        self,
        *,
        run_id: str,
        asset_configuration: RunAssetConfiguration,
        routing_plan: MainlandCapabilityRoutingPlan,
        expected_as_of_date: date | None,
        availability_scope: FinancialAvailabilityScope,
        consolidation_scope: FinancialConsolidationScope,
    ) -> None:
        """Reject any attempt to reuse the slot for a different run binding."""

        with self._lock:
            if (
                run_id != self._run_id
                or asset_configuration != self._asset_configuration
                or routing_plan != self._routing_plan
                or routing_plan.financial_eligibility_binding
                != self._routing_plan.financial_eligibility_binding
                or availability_scope is not self._availability_scope
                or consolidation_scope is not self._consolidation_scope
                or (
                    expected_as_of_date is not None
                    and (
                        not isinstance(expected_as_of_date, date)
                        or isinstance(expected_as_of_date, datetime)
                        or (
                            self._expected_as_of_date is not None
                            and expected_as_of_date != self._expected_as_of_date
                        )
                    )
                )
            ):
                self._fail_binding()
            if self._expected_as_of_date is None:
                self._expected_as_of_date = expected_as_of_date

    def _require_bound_routing(self) -> QualifiedFinancialRoutingComposition:
        with self._lock:
            if self._interruption_requested:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
                )
            routing = self._bound_financial_routing
        if routing is None:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
            )
        return routing

    @property
    def financial_routing(self) -> QualifiedFinancialRoutingComposition:
        return self._require_bound_routing()

    @property
    def statement_request_factory(
        self,
    ) -> Callable[[FinancialToolRequest], FinancialStatementRoutingRequest]:
        factory = self._require_bound_routing().statement_request_factory
        if factory is None:
            self._fail_binding()
        return factory

    @property
    def indicator_request_factory(
        self,
    ) -> Callable[[FinancialToolRequest], FinancialIndicatorRoutingRequest]:
        factory = self._require_bound_routing().indicator_request_factory
        if factory is None:
            self._fail_binding()
        return factory

    def _validate_context(
        self,
        context: FinancialEligibilityAcquisitionContext,
    ) -> None:
        try:
            validate_financial_eligibility_acquisition_context_binding(
                context,
                canonical_run_id=self._run_id,
                asset_configuration_signature=(
                    self._asset_configuration.asset_configuration_signature
                ),
                instrument_identity=self._asset_configuration.instrument_identity,
                as_of_date=self._expected_as_of_date,
                availability_scope=self._availability_scope,
                financial_eligibility_binding=(
                    self._routing_plan.financial_eligibility_binding
                ),
                routing_plan_signature=self._routing_plan.plan_signature,
                consolidation_scope=self._consolidation_scope,
            )
        except (TypeError, ValueError):
            self._fail_binding()

    def _bound_factory(
        self,
        factory: object,
        context: FinancialEligibilityAcquisitionContext,
    ) -> Callable[[FinancialToolRequest], object] | None:
        if factory is None:
            return None
        if not callable(factory):
            FinancialEligibilityAcquisitionContextOwner._fail_binding()
        binding = FinancialEligibilityRequestBinding.from_context(context)

        def invoke(request: FinancialToolRequest) -> object:
            self._require_bound_routing()
            if not isinstance(request, FinancialToolRequest):
                FinancialEligibilityAcquisitionContextOwner._fail_binding()
            existing = request.financial_eligibility_context_binding
            if existing is not None and existing != binding:
                FinancialEligibilityAcquisitionContextOwner._fail_binding()
            bound_request = request.model_copy(
                update={"financial_eligibility_context_binding": binding}
            )
            return factory(bound_request, context)

        return invoke

    def _cleanup_runtime(self) -> None:
        with self._lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True
        with suppress(BaseException):
            self._interrupt_runtime()
        with suppress(BaseException):
            self._close_runtime()

    def _publish_interrupted(
        self,
    ) -> FinancialEligibilityAcquisitionResult:
        interrupted = FinancialEligibilityAcquisitionResult.failed(
            FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
        )
        with self._condition:
            if (
                self._context is None
                and self._acquisition_result is None
            ):
                self._acquisition_result = interrupted
            self._acquiring = False
            self._condition.notify_all()
            return self._acquisition_result or interrupted

    def interrupt(self) -> None:
        with self._condition:
            self._interruption_requested = True
            self._bound_financial_routing = None
            self._condition.notify_all()
        self._cleanup_runtime()
        if self.context is None:
            self._publish_interrupted()

    def close(self) -> None:
        self.interrupt()

    def bind_context(
        self,
        context: FinancialEligibilityAcquisitionContext,
    ) -> FinancialEligibilityAcquisitionContext:
        self._validate_context(context)
        with self._lock:
            if self._interruption_requested or (
                self._acquisition_result is not None
                and self._acquisition_result.context is None
            ):
                self._fail_binding()
            if self._context is not None:
                if self._context == context:
                    return self._context
                self._fail_binding()
            if self._expected_as_of_date is None:
                self._expected_as_of_date = context.as_of_date
            raw = self._unbound_financial_routing
            if raw is None:
                self._fail_binding()
            try:
                cache = raw.provider_subrequest_cache
                if cache is not None:
                    cache.bind_financial_eligibility_context(
                        context_contract_version=context.contract_version,
                        context_identity=context.context_identity,
                    )
                bound = QualifiedFinancialRoutingComposition(
                    router=raw.router,
                    statement_request_factory=self._bound_factory(
                        raw.statement_request_factory,
                        context,
                    ),
                    indicator_request_factory=self._bound_factory(
                        raw.indicator_request_factory,
                        context,
                    ),
                    provider_subrequest_cache=raw.provider_subrequest_cache,
                    restored_shadow_dispatcher=raw.restored_shadow_dispatcher,
                )
            except ProductionQualifiedRoutingLifecycleError:
                raise
            except (AttributeError, TypeError, ValueError):
                self._fail_binding()
            self._bound_financial_routing = bound
            self._context = context
            return context

    def install_restored_financial_routing(
        self,
        routing: QualifiedFinancialRoutingComposition,
    ) -> QualifiedFinancialRoutingComposition:
        """Expose restored routing only after cache and dispatcher restoration."""

        if not isinstance(routing, QualifiedFinancialRoutingComposition):
            self._fail_binding()
        with self._lock:
            if self._interruption_requested or self._context is None:
                self._fail_binding()
            if self._bound_financial_routing is not None:
                if self._bound_financial_routing == routing:
                    return self._bound_financial_routing
                self._fail_binding()
            context = self._context
            if routing.router.routing_plan != self._routing_plan:
                self._fail_binding()
            cache = routing.provider_subrequest_cache
            if cache is not None:
                try:
                    cache.bind_financial_eligibility_context(
                        context_contract_version=context.contract_version,
                        context_identity=context.context_identity,
                    )
                except Exception:
                    self._fail_binding()
            try:
                bound = QualifiedFinancialRoutingComposition(
                    router=routing.router,
                    statement_request_factory=self._bound_factory(
                        routing.statement_request_factory,
                        context,
                    ),
                    indicator_request_factory=self._bound_factory(
                        routing.indicator_request_factory,
                        context,
                    ),
                    provider_subrequest_cache=cache,
                    restored_shadow_dispatcher=routing.restored_shadow_dispatcher,
                )
            except ProductionQualifiedRoutingLifecycleError:
                raise
            except (AttributeError, TypeError, ValueError):
                self._fail_binding()
            self._bound_financial_routing = bound
            return bound

    def acquire_context(
        self,
        *,
        as_of_date: date,
    ) -> FinancialEligibilityAcquisitionResult:
        if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
            self._fail_binding()
        with self._condition:
            if self._expected_as_of_date is None:
                self._expected_as_of_date = as_of_date
            elif as_of_date != self._expected_as_of_date:
                self._fail_binding()
            if self._acquisition_result is not None:
                return self._acquisition_result
            if self._acquiring:
                while self._acquisition_result is None:
                    self._condition.wait()
                assert self._acquisition_result is not None
                return self._acquisition_result
            self._acquiring = True
        try:
            plan_binding = self._routing_plan.financial_eligibility_binding
            if plan_binding is None:
                self._fail_binding()
            request = FinancialEligibilityMetadataAcquisitionRequest(
                canonical_run_id=self._run_id,
                run_id=self._run_id,
                asset_configuration=self._asset_configuration,
                asset_configuration_signature=(
                    self._asset_configuration.asset_configuration_signature
                ),
                instrument_identity=self._asset_configuration.instrument_identity,
                authoritative_as_of_date=as_of_date,
                as_of_date=as_of_date,
                authoritative_availability_scope=self._availability_scope,
                availability_scope=self._availability_scope,
                acquisition_purpose="foreground_current_analysis",
                financial_eligibility_binding=plan_binding,
                routing_plan_signature=self._routing_plan.plan_signature,
                currency=self._asset_configuration.instrument_identity.currency,
                consolidation_scope=self._consolidation_scope,
            )
            result = self._metadata_acquirer.acquire(
                request,
                retained_company_type_evidence=(
                    self._retained_company_type_evidence
                ),
                retained_listing_metadata=self._retained_listing_metadata,
            )
            if not isinstance(result, FinancialEligibilityAcquisitionResult):
                self._fail_binding()
            with self._condition:
                while (
                    self._interruption_requested
                    and self._acquisition_result is None
                ):
                    self._condition.wait()
                if self._acquisition_result is not None:
                    self._acquiring = False
                    self._condition.notify_all()
                    return self._acquisition_result
                if result.context is not None:
                    try:
                        self.bind_context(result.context)
                    except ProductionQualifiedRoutingLifecycleError as exc:
                        if exc.reason is not (
                            ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
                        ):
                            raise
                        result = FinancialEligibilityAcquisitionResult.failed(
                            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
                        )
                if result.context is not None:
                    self._acquiring = False
                    self._acquisition_result = result
                    self._condition.notify_all()
                    return result
        except BaseException as exc:
            self._cleanup_runtime()
            follower_reason = (
                FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
                if isinstance(exc, Exception)
                else FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
            )
            follower_result = FinancialEligibilityAcquisitionResult.failed(
                follower_reason
            )
            with self._condition:
                self._acquiring = False
                if self._acquisition_result is None:
                    self._acquisition_result = follower_result
                self._condition.notify_all()
            raise
        if result.failure is not None:
            self._cleanup_runtime()
        with self._condition:
            self._acquiring = False
            if self._acquisition_result is None:
                self._acquisition_result = result
            self._condition.notify_all()
            return self._acquisition_result

    def __repr__(self) -> str:
        return (
            "FinancialEligibilityAcquisitionContextOwner("
            f"context_bound={self.context is not None})"
        )


__all__ = [
    "FinancialEligibilityAcquisitionContextOwner",
    "QualifiedFinancialRoutingComposition",
]
