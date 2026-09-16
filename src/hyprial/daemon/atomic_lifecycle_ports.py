"""Bounded adapters onto lifecycle mutations owned by real domain actors.

There is intentionally no lifecycle database here. Tokens and idempotency
receipts live in AgentRegistry or DesiredStateStore and are committed by the
domain actor that owns the corresponding resource.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from queue import Empty, Full, Queue
from typing import Any

from hyprial.agents.ports import (
    BindAgentCommand,
    CreateAgentCommand,
    DestroyAgentCommand,
    ReleaseAgentCommand,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected

from .correlation import CompletionReceipt, CorrelationEventRouter
from .harness_ports import EnsureHarnessCommand, RemoveHarnessCommand
from .lifecycle_receipts import (
    LifecycleMutationCompleted,
    LifecycleMutationFailed,
    LifecycleMutationRequest,
)
from .session_ports import RegisterSessionCommand, UnregisterSessionCommand


class AtomicLifecycleDomainPort:
    """One bounded handoff into an actor-owned atomic lifecycle transaction."""

    def __init__(
        self,
        *,
        domain: str,
        router: CorrelationEventRouter,
        generation: Callable[[], int],
        version: Callable[[], int],
        submit_domain: Callable[[LifecycleMutationRequest], PortAdmission],
        wait_domain: Callable[[str, object], object],
        retire_receipt: Callable[[str, str], bool],
        confirm_receipt_retired: Callable[[str, str], None],
        release_replay_claim: Callable[[str], None] | None = None,
        capacity: int = 32,
    ) -> None:
        self.domain = domain
        self._router = router
        self._generation = generation
        self._version = version
        self._submit_domain = submit_domain
        self._wait_domain = wait_domain
        self._retire_receipt = retire_receipt
        self._confirm_receipt_retired = confirm_receipt_retired
        self._release_replay_claim = release_replay_claim
        self._queue: Queue[LifecycleMutationRequest | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._pending: set[str] = set()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"hyprial-{domain}-lifecycle-port",
            daemon=True,
        )
        self._thread.start()

    @property
    def generation(self) -> int:
        return self._generation()

    @property
    def version(self) -> int:
        return self._version()

    def submit(self, request: LifecycleMutationRequest) -> PortAdmission:
        with self._condition:
            if self._closed:
                return PortAdmission.CLOSING
            if request.attempt_token in self._pending:
                return PortAdmission.ACCEPTED
            try:
                self._queue.put_nowait(request)
            except Full:
                return PortAdmission.OVERLOADED
            self._pending.add(request.attempt_token)
            return PortAdmission.ACCEPTED

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        return self._retire_receipt(attempt_token, resource_token)

    def confirm_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> None:
        self._confirm_receipt_retired(attempt_token, resource_token)
        if self._release_replay_claim is not None:
            self._release_replay_claim(attempt_token)

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            while self._pending or self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                request = self._queue.get(timeout=0.1)
            except Empty:
                continue
            if request is None:
                self._queue.task_done()
                return
            try:
                self._perform(request)
            finally:
                with self._condition:
                    self._pending.discard(request.attempt_token)
                    self._queue.task_done()
                    self._condition.notify_all()

    def _perform(self, request: LifecycleMutationRequest) -> None:
        admitted_generation = self.generation
        admitted_version = self.version
        admission = self._submit_domain(request)
        if admission is not PortAdmission.ACCEPTED:
            self._router.publish(
                PortCommandRejected(
                    correlation_id=request.correlation_id,
                    domain=self.domain,
                    generation=self.generation,
                    version=self.version,
                    code=f"PORT_{admission.value.upper()}",
                    detail=f"{self.domain} command admission is {admission.value}",
                    admission=admission,
                ),
                attempt_token=request.attempt_token,
            )
            return
        try:
            event = self._wait_domain(
                request.correlation_id,
                (LifecycleMutationCompleted, LifecycleMutationFailed),
            )
        except Exception as error:
            self._router.publish(
                PortCommandRejected(
                    correlation_id=request.correlation_id,
                    domain=self.domain,
                    generation=self.generation,
                    version=self.version,
                    code=str(getattr(error, "code", "LIFECYCLE_DOMAIN_FAILED")),
                    detail=str(error),
                ),
                attempt_token=request.attempt_token,
            )
            return
        if not isinstance(event, (LifecycleMutationCompleted, LifecycleMutationFailed)):
            raise TypeError("domain returned a non-lifecycle completion")
        event = replace(
            event,
            generation=admitted_generation,
            version=admitted_version,
        )
        receipt = self._router.publish(event, attempt_token=request.attempt_token)
        if (
            isinstance(event, LifecycleMutationCompleted)
            and event.replayed
            and receipt is not CompletionReceipt.COMMITTED
            and self._release_replay_claim is not None
        ):
            self._release_replay_claim(request.attempt_token)


class HarnessLifecycleDomainPort(AtomicLifecycleDomainPort):
    """A harness has a control lane that cannot wait behind its own stop."""

    def __init__(
        self, *, failure_control: Callable[
            [LifecycleMutationRequest, str, str, float],
            LifecycleMutationCompleted | LifecycleMutationFailed,
        ], **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._failure_control = failure_control

    def fail_lifecycle(
        self, request: LifecycleMutationRequest, *, code: str, detail: str,
        timeout: float,
    ) -> LifecycleMutationCompleted | LifecycleMutationFailed:
        return self._failure_control(request, code, detail, timeout)


def agent_resource_key(payload: object) -> str:
    if isinstance(payload, (CreateAgentCommand, DestroyAgentCommand)):
        return f"agent-record:{payload.name}"
    if isinstance(payload, (BindAgentCommand, ReleaseAgentCommand)):
        return f"binding:{payload.actor}"
    raise TypeError(f"invalid Agent lifecycle payload: {type(payload).__name__}")


def session_resource_key(payload: object) -> str:
    if isinstance(payload, (RegisterSessionCommand, UnregisterSessionCommand)):
        return f"session:{payload.actor}"
    raise TypeError(f"invalid Session lifecycle payload: {type(payload).__name__}")


def harness_resource_key(payload: object) -> str:
    if isinstance(payload, EnsureHarnessCommand):
        return f"harness:{payload.spec.harness}:{payload.spec.name}"
    if isinstance(payload, RemoveHarnessCommand):
        return f"harness:{payload.harness}:{payload.name}"
    raise TypeError(f"invalid Harness lifecycle payload: {type(payload).__name__}")


__all__ = [
    "AtomicLifecycleDomainPort",
    "agent_resource_key",
    "harness_resource_key",
    "session_resource_key",
]
