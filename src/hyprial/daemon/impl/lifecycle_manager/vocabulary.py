"""Lifecycle kinds/states/specs/operations and the domain port protocol."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Literal
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    HarnessLaunchProjection,
)
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    AgentLifecycleMutationPort,
    AtomicLifecycleMutationPort,
    HarnessLifecycleMutationPort,
    RouteLifecycleMutationPort,
    SessionLifecycleMutationPort,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports  import (
    RouteSpec,
)


class LifecycleKind(StrEnum):
    CREATE = "create"
    DEACTIVATE = "deactivate"
    REMOVE = "remove"
    TRANSFER = "transfer"


class LifecycleState(StrEnum):
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionLifecycleSpec:
    cwd: str
    command: tuple[str, ...]
    source: str
    session_ref: str
    runtime: str = "claude_interactive"


@dataclass(frozen=True, slots=True)
class LifecycleSpec:
    agent_name: str
    actor: str
    harness: HarnessLaunchProjection
    route: RouteSpec
    session: SessionLifecycleSpec | None = None
    interruption_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleOperation:
    operation_id: str
    kind: LifecycleKind
    target: LifecycleSpec
    source: LifecycleSpec | None = None

    @classmethod
    def create(cls, operation_id: str, target: LifecycleSpec) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.CREATE, target)

    @classmethod
    def remove(cls, operation_id: str, target: LifecycleSpec) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.REMOVE, target)

    @classmethod
    def deactivate(
        cls, operation_id: str, target: LifecycleSpec
    ) -> LifecycleOperation:
        """Stop one runtime while preserving the durable Agent entity."""

        return cls(operation_id, LifecycleKind.DEACTIVATE, target)

    @classmethod
    def transfer(
        cls,
        operation_id: str,
        *,
        source: LifecycleSpec,
        target: LifecycleSpec,
    ) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.TRANSFER, target, source)


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    operation_id: str
    state: LifecycleState
    completed_effects: tuple[str, ...]
    compensated_effects: tuple[str, ...]
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LifecyclePorts:
    agent: AgentLifecycleMutationPort
    session: SessionLifecycleMutationPort
    harness: HarnessLifecycleMutationPort
    route: RouteLifecycleMutationPort


class LifecycleDomainPort:
    """Composition adapter for a receipt-producing typed domain port.

    The wrapped domain, not this adapter, must commit
    :class:`LifecycleMutationCompleted` provenance in the same transaction as
    its mutation.  This class intentionally has no projection/pre-read hook.
    """

    def __init__(
        self,
        domain: Literal["agent", "session", "harness"],
        commands: AtomicLifecycleMutationPort,
        *,
        generation: Callable[[], int],
        version: Callable[[], int],
        retire_receipt: Callable[[str, str], bool],
        confirm_receipt_retired: Callable[[str, str], None],
    ) -> None:
        self.domain = domain
        self._commands = commands
        self._generation = generation
        self._version = version
        self._retire_receipt = retire_receipt
        self._confirm_receipt_retired = confirm_receipt_retired

    @property
    def generation(self) -> int:
        return self._generation()

    @property
    def version(self) -> int:
        return self._version()

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        return self._retire_receipt(attempt_token, resource_token)

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        self._confirm_receipt_retired(attempt_token, resource_token)

    def submit(self, command: LifecycleMutationRequest) -> PortAdmission:
        return self._commands.submit(command)


@dataclass(frozen=True, slots=True)
class _Step:
    name: str
    domain: str
    spec: LifecycleSpec
    forward: str
    inverse: str
    route_owner: str | None = None


class LifecycleOperationConflict(RuntimeError):
    pass


class LifecycleStepFailed(RuntimeError):
    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.code = code


class LifecycleStepUnresolved(RuntimeError):
    """An admitted effect may have run but has no committed completion yet."""
