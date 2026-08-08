"""Ticket-04 lifecycle state for one managed qualified routing runtime."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_RESOURCE_KIND_PATTERN = r"^[a-z][a-z0-9._:=-]*$"


class ProductionQualifiedRoutingLifecycleFailureReason(str, Enum):
    """Bounded lifecycle failures that may cross the managed-composition seam."""

    BINDING_INVALID = "qualified_routing_runtime_binding_invalid"
    CLOSED = "qualified_routing_runtime_closed"
    DIFFERENT_RUN_ACTIVE = "qualified_routing_runtime_different_run_active"
    INTERRUPTED = "qualified_routing_runtime_interrupted"
    ELIGIBILITY_CONTEXT_UNBOUND = "qualified_routing_eligibility_context_unbound"
    ELIGIBILITY_CONTEXT_BINDING_INVALID = (
        "qualified_routing_eligibility_context_binding_invalid"
    )


class ProductionQualifiedRoutingLifecycleError(RuntimeError):
    """Typed, payload-free managed-runtime failure."""

    def __init__(
        self,
        reason: ProductionQualifiedRoutingLifecycleFailureReason,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class ProductionQualifiedRoutingLifecycleDiagnostic(BaseModel):
    """Sanitized operational detail retained when cleanup itself fails."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["interrupt", "close"]
    resource_kind: str = Field(pattern=_SAFE_RESOURCE_KIND_PATTERN, max_length=64)
    diagnostic_code: Literal[
        "qualified_routing_resource_interrupt_failed",
        "qualified_routing_resource_close_failed",
    ]


@dataclass(slots=True)
class _OwnedQualifiedRoutingResource:
    resource_kind: str
    close_callback: Callable[[], object]
    interrupt_callback: Callable[[], object] | None
    interrupted: bool = False
    closed: bool = False


class ProductionQualifiedRuntimeOwner:
    """One composition-local reverse-order ownership stack."""

    def __init__(self) -> None:
        self.stop_signal = threading.Event()
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._resources: list[_OwnedQualifiedRoutingResource] = []
        self._diagnostics: list[ProductionQualifiedRoutingLifecycleDiagnostic] = []
        self._interrupted = False
        self._closed = False

    @property
    def resource_kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(resource.resource_kind for resource in self._resources)

    @property
    def diagnostics(self) -> tuple[ProductionQualifiedRoutingLifecycleDiagnostic, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    @property
    def interrupted(self) -> bool:
        with self._lock:
            return self._interrupted

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def own(self, resource_kind: str, resource: object) -> None:
        if (
            not isinstance(resource_kind, str)
            or re.fullmatch(_SAFE_RESOURCE_KIND_PATTERN, resource_kind) is None
            or resource is None
        ):
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        close = getattr(resource, "close", None)
        interrupt = getattr(resource, "interrupt", None)
        close_callback = close if callable(close) else lambda: None
        interrupt_callback = interrupt if callable(interrupt) else None
        with self._lock:
            if self._closed:
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.CLOSED
                )
            self._resources.append(
                _OwnedQualifiedRoutingResource(
                    resource_kind=resource_kind,
                    close_callback=close_callback,
                    interrupt_callback=interrupt_callback,
                )
            )

    def _record_failure(
        self,
        *,
        operation: Literal["interrupt", "close"],
        resource_kind: str,
    ) -> None:
        diagnostic_code = (
            "qualified_routing_resource_interrupt_failed"
            if operation == "interrupt"
            else "qualified_routing_resource_close_failed"
        )
        with self._lock:
            self._diagnostics.append(
                ProductionQualifiedRoutingLifecycleDiagnostic(
                    operation=operation,
                    resource_kind=resource_kind,
                    diagnostic_code=diagnostic_code,
                )
            )

    def interrupt(self) -> None:
        self.stop_signal.set()
        with self._cleanup_lock:
            with self._lock:
                if self._interrupted:
                    return
                self._interrupted = True
                if self._closed:
                    return
                resources = tuple(reversed(self._resources))
                for resource in resources:
                    if resource.interrupt_callback is not None:
                        resource.interrupted = True
            for resource in resources:
                if resource.interrupt_callback is None:
                    continue
                try:
                    resource.interrupt_callback()
                except BaseException:
                    self._record_failure(
                        operation="interrupt",
                        resource_kind=resource.resource_kind,
                    )

    def close(self) -> None:
        with self._cleanup_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                resources = tuple(reversed(self._resources))
                for resource in resources:
                    resource.closed = True
            for resource in resources:
                try:
                    resource.close_callback()
                except BaseException:
                    self._record_failure(
                        operation="close",
                        resource_kind=resource.resource_kind,
                    )


__all__ = [
    "ProductionQualifiedRoutingLifecycleDiagnostic",
    "ProductionQualifiedRoutingLifecycleError",
    "ProductionQualifiedRoutingLifecycleFailureReason",
    "ProductionQualifiedRuntimeOwner",
]
