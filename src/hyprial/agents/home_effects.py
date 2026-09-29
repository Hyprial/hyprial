"""Bounded filesystem-only Agent home effects with retained custody."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, replace

from hyprial.actor_runtime import AdmissionResult
from hyprial.contracts import ipc_errors

from .home import AgentHomeError, AgentHomeProvisioner, HomeReceipt
from .runtime import (
    AgentRuntimeContext,
    AgentRuntimePreparation,
    materialize_agent_runtime_context,
)


@dataclass(frozen=True, slots=True)
class ProvisionHome:
    receipt: HomeReceipt


@dataclass(frozen=True, slots=True)
class ReplaceHome:
    revoked: HomeReceipt
    replacement: HomeReceipt


@dataclass(frozen=True, slots=True)
class CleanupHome:
    receipt: HomeReceipt


@dataclass(frozen=True, slots=True)
class PrepareWorkspaceHome:
    receipt: HomeReceipt


@dataclass(frozen=True, slots=True)
class PrepareRuntimeHome:
    preparation: AgentRuntimePreparation


HomeFilesystemPlan = (
    ProvisionHome
    | ReplaceHome
    | CleanupHome
    | PrepareWorkspaceHome
    | PrepareRuntimeHome
)


@dataclass(frozen=True, slots=True)
class AgentHomeEffect:
    correlation_id: str
    generation: int
    plan: HomeFilesystemPlan


@dataclass(frozen=True, slots=True)
class AgentHomeEffectCompleted:
    correlation_id: str
    generation: int
    receipt: HomeReceipt | None = None
    workspace: str | None = None
    runtime_context: AgentRuntimeContext | None = None
    settled: bool = True
    error_code: str | None = None
    error_detail: str | None = None


class AgentHomeEffectLane:
    """Execute frozen filesystem plans; this lane never receives a registry."""

    def __init__(
        self,
        provisioner: AgentHomeProvisioner,
        *,
        complete,
        capacity: int = 32,
        retry_seconds: float = 0.01,
    ) -> None:
        if capacity < 1 or retry_seconds <= 0:
            raise ValueError("home effect capacity and retry must be positive")
        self._provisioner = provisioner
        self._complete = complete
        self._capacity = capacity
        self._retry = retry_seconds
        self._queue: queue.Queue[AgentHomeEffect] = queue.Queue(capacity)
        self._condition = threading.Condition()
        self._pending: dict[tuple[str, int], threading.Event] = {}
        self._submitted: set[tuple[str, int]] = set()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="agent-home-effect", daemon=True
        )
        self._thread.start()

    def reserve(self, correlation_id: str, generation: int) -> AdmissionResult:
        token = (correlation_id, generation)
        with self._condition:
            if self._closed:
                return AdmissionResult.CLOSED
            if token in self._pending:
                return AdmissionResult.ACCEPTED
            if len(self._pending) >= self._capacity:
                return AdmissionResult.OVERLOADED
            self._pending[token] = threading.Event()
            return AdmissionResult.ACCEPTED

    def submit_reserved(self, effect: AgentHomeEffect) -> AdmissionResult:
        token = (effect.correlation_id, effect.generation)
        with self._condition:
            if token not in self._pending:
                return AdmissionResult.CLOSED
            if token in self._submitted:
                return AdmissionResult.ACCEPTED
            self._submitted.add(token)
            self._queue.put_nowait(effect)
            return AdmissionResult.ACCEPTED

    def cancel_reservation(self, correlation_id: str, generation: int) -> bool:
        token = (correlation_id, generation)
        with self._condition:
            if token in self._submitted:
                return False
            receipt = self._pending.pop(token, None)
            if receipt is None:
                return False
            receipt.set()
            self._condition.notify_all()
            return True

    def acknowledge(self, correlation_id: str, generation: int) -> bool:
        token = (correlation_id, generation)
        with self._condition:
            if token not in self._submitted:
                return False
            receipt = self._pending.pop(token, None)
            if receipt is None:
                return False
            self._submitted.discard(token)
            receipt.set()
            self._condition.notify_all()
            return True

    def owns(self, correlation_id: str, generation: int) -> bool:
        with self._condition:
            return (correlation_id, generation) in self._pending

    def _run(self) -> None:
        while True:
            try:
                effect = self._queue.get(timeout=self._retry)
            except queue.Empty:
                with self._condition:
                    if self._closed and not self._pending:
                        return
                continue
            token = (effect.correlation_id, effect.generation)
            with self._condition:
                receipt = self._pending[token]
            completed = self._execute(effect)
            while not receipt.is_set():
                try:
                    self._complete(completed)
                except BaseException:
                    pass
                if receipt.wait(self._retry):
                    break
            self._queue.task_done()

    def _execute(self, effect: AgentHomeEffect) -> AgentHomeEffectCompleted:
        try:
            plan = effect.plan
            if isinstance(plan, ProvisionHome):
                receipt = self._provisioner.provision_claimed(plan.receipt).receipt
            elif isinstance(plan, ReplaceHome):
                try:
                    receipt = self._provisioner.provision_claimed(
                        plan.replacement
                    ).receipt
                except AgentHomeError as error:
                    if error.category not in {
                        "receipt-mismatch",
                        "unowned-residue",
                    }:
                        raise
                    self._provisioner.cleanup(
                        plan.revoked,
                        expected_token=plan.revoked.resource_token,
                    )
                    receipt = self._provisioner.provision_claimed(
                        plan.replacement
                    ).receipt
            elif isinstance(plan, CleanupHome):
                receipt = self._provisioner.cleanup(
                    plan.receipt,
                    expected_token=plan.receipt.resource_token,
                )
            elif isinstance(plan, PrepareWorkspaceHome):
                workspace = self._provisioner.ensure_workspace(plan.receipt)
                return AgentHomeEffectCompleted(
                    effect.correlation_id,
                    effect.generation,
                    receipt=plan.receipt,
                    workspace=str(workspace),
                )
            elif isinstance(plan, PrepareRuntimeHome):
                self._provisioner.validate(plan.preparation.home_receipt)
                context = materialize_agent_runtime_context(plan.preparation)
                if context.harness == "claude":
                    # Lazy import keeps the shared Agent runtime independent at
                    # module import time while still giving this one FS owner
                    # custody of the harness-specific links.
                    from hyprial.harnesses.claude_runtime import (
                        prepare_claude_runtime_context,
                    )

                    prepare_claude_runtime_context(context)
                elif context.harness == "codex":
                    from hyprial.harnesses.codex import (
                        prepare_codex_runtime_context,
                    )

                    prepare_codex_runtime_context(context)
                return AgentHomeEffectCompleted(
                    effect.correlation_id,
                    effect.generation,
                    receipt=plan.preparation.home_receipt,
                    runtime_context=replace(context, authority_prepared=True),
                )
            else:
                raise TypeError("unsupported Agent home filesystem plan")
            return AgentHomeEffectCompleted(
                effect.correlation_id, effect.generation, receipt
            )
        except BaseException as error:
            expected = isinstance(error, (AgentHomeError, TypeError, ValueError))
            code = (
                str(getattr(error, "code", "AGENT_ERROR"))
                if isinstance(error, AgentHomeError)
                else ipc_errors.INVALID_ARGUMENT
                if isinstance(error, (TypeError, ValueError))
                else type(error).__name__
            )
            return AgentHomeEffectCompleted(
                effect.correlation_id,
                effect.generation,
                settled=expected,
                error_code=code,
                error_detail=str(error),
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()
