from __future__ import annotations

import os
import time
import threading
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    ActorState,
    AdmissionResult,
    SupervisionPolicy,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.actor import (
    DispatchIoCompleted,
    DispatchIoFailed,
    ReassociateIoCompletion,
)
from hyprial.daemon.impl.inbox.contracts.ports import (
    BoolMutationCompleted,
)
from tests.support.links.gate import permissive_admission_slot


class _Events:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[object] = []

    def publish(self, event: object) -> None:
        with self._lock:
            self.items.append(event)

    def snapshot(self) -> tuple[object, ...]:
        with self._lock:
            return tuple(self.items)


class _CompletionClosedRuntime(ActorRuntime):
    def __init__(self) -> None:
        super().__init__(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=3,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
        self._completion_closed = True

    def tell(self, handle: object, command: object) -> AdmissionResult:
        if self._completion_closed and isinstance(
            command, (DispatchIoCompleted, DispatchIoFailed)
        ):
            return AdmissionResult.CLOSED
        return super().tell(handle, command)  # type: ignore[arg-type]

    def allow_completions(self) -> None:
        self._completion_closed = False


class _BlockingProgressTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        del node, message
        self.calls += 1
        self.entered.set()
        assert self.release.wait(1.0)
        return True


class _DoubleRestartReassociateRuntime(ActorRuntime):
    def __init__(self) -> None:
        super().__init__(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=4,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
        self._restarted_during_takeover = False

    def tell(self, handle: object, command: object) -> AdmissionResult:
        if (
            isinstance(command, ReassociateIoCompletion)
            and not self._restarted_during_takeover
        ):
            self._restarted_during_takeover = True
            assert super().tell(handle, object()) is AdmissionResult.ACCEPTED  # type: ignore[arg-type]
            deadline = time.monotonic() + 1.0
            # The generation advances before the replacement actor starts.
            # Wait for admission to reopen so the stale command reaches its
            # generation fence instead of being rejected as CLOSED upstream.
            while True:
                snapshot = self.snapshot(handle)  # type: ignore[arg-type]
                if snapshot.generation == 3 and snapshot.state is ActorState.RUNNING:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("second actor restart did not complete")
                time.sleep(0.005)
        return super().tell(handle, command)  # type: ignore[arg-type]


class _CountingCompletionRuntime(ActorRuntime):
    def __init__(self) -> None:
        super().__init__(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=3,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
        self.completion_admissions = 0

    def tell(self, handle: object, command: object) -> AdmissionResult:
        if isinstance(
            command,
            (DispatchIoCompleted, DispatchIoFailed, ReassociateIoCompletion),
        ):
            self.completion_admissions += 1
        return super().tell(handle, command)  # type: ignore[arg-type]


class _BlockingFinalEvents(_Events):
    def __init__(self, correlation_id: str) -> None:
        super().__init__()
        self._correlation_id = correlation_id
        self._blocked = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, event: object) -> None:
        if (
            isinstance(event, BoolMutationCompleted)
            and event.correlation_id == self._correlation_id
            and not self._blocked
        ):
            self._blocked = True
            self.entered.set()
            assert self.release.wait(1.0)
        super().publish(event)


def _actor_state(coordinator: DeliveryCustodyCoordinator) -> ActorState:
    return coordinator._runtime.snapshot(coordinator._handle).state


# CI flake fix (2026-10-09): 1.0s wall-clock deadlines are too tight when
# shards saturate a loaded host. Deadlines only bound the wait; the tests
# assert state, not speed, so scaling them is semantically safe.
WAIT_TIMEOUT_SECONDS = float(os.environ.get("HYPRIAL_TEST_WAIT_SECONDS", "5.0"))


def _wait_until(predicate: object, timeout: float | None = None) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + (WAIT_TIMEOUT_SECONDS if timeout is None else timeout)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _runtime() -> ActorRuntime:
    return ActorRuntime(
        policies={
            STATE_AUTHORITY: SupervisionPolicy(
                max_restarts=3,
                restart_window=10.0,
                base_backoff=0.0,
                max_backoff=0.0,
                jitter_ratio=0.0,
            )
        },
        random_source=lambda: 0.5,
    )


def _message(message_id: str, *, intent: str = "message") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender="agent:owner:node-a:alice",
        recipient="agent:owner:node-b:bob",
        payload=b'{"message":"hello"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=10,
        idempotency_key=f"idem-{message_id}",
    )


@pytest.fixture
def authority(tmp_path: Path):
    events = _Events()
    transport = MemoryDeliveryTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=_runtime(),
        node_id="node-b",
        # alice->bob is agent-to-agent; bind the explicit permissive premise
        # so this suite exercises actor mutation threading, not the link gate.
        service_options={"link_admission": permissive_admission_slot()},
    )
    facade = DeliveryCustodyFacade(
        coordinator,
        tmp_path / "inbox.sqlite3",
        timeout=1.0,
    )
    yield facade, coordinator, transport, events
    if coordinator.drain(1.0).complete is False:
        raise AssertionError("delivery authority did not drain")











































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_BlockingFinalEvents',
    '_BlockingProgressTransport',
    '_CompletionClosedRuntime',
    '_CountingCompletionRuntime',
    '_DoubleRestartReassociateRuntime',
    '_Events',
    '_actor_state',
    '_message',
    '_runtime',
    '_wait_until',
    'authority',
]
