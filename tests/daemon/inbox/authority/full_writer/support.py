from __future__ import annotations

import os
import time
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.kernel import (
    ActorCell,
    ActorEngineConfig,
    ActorHost,
    ActorSpec,
    create_actor_host,
)
from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
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
from tests.support.actor_hosts import InterceptingHost


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


class _CompletionClosedHost(InterceptingHost):
    def __init__(self) -> None:
        super().__init__(_build_host())
        self._completion_closed = True
        self.intercept_tell(self._admit)

    def _admit(self, _cell, command: object) -> AdmissionResult | None:
        if self._completion_closed and isinstance(
            command, (DispatchIoCompleted, DispatchIoFailed)
        ):
            return AdmissionResult.CLOSED
        return None

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


class _DoubleRestartReassociateHost(InterceptingHost):
    def __init__(self) -> None:
        super().__init__(_build_host(max_restarts=4))
        self._restarted_during_takeover = False
        self.intercept_tell(self._admit)

    def _admit(self, cell, command: object) -> AdmissionResult | None:
        if (
            isinstance(command, ReassociateIoCompletion)
            and not self._restarted_during_takeover
        ):
            self._restarted_during_takeover = True
            assert cell.tell(object()) is AdmissionResult.ACCEPTED
            deadline = time.monotonic() + 1.0
            # The generation advances before the replacement actor starts.
            # Wait for admission to reopen so the stale command reaches its
            # generation fence instead of being rejected as CLOSED upstream.
            while True:
                snapshot = cell.snapshot()
                if snapshot.generation == 3 and snapshot.state is ActorState.RUNNING:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("second actor restart did not complete")
                time.sleep(0.005)
        return None


class _CountingCompletionHost(InterceptingHost):
    def __init__(self) -> None:
        super().__init__(_build_host())
        self.completion_admissions = 0
        self.intercept_tell(self._admit)

    def _admit(self, _cell, command: object) -> AdmissionResult | None:
        if isinstance(
            command,
            (DispatchIoCompleted, DispatchIoFailed, ReassociateIoCompletion),
        ):
            self.completion_admissions += 1
        return None


class _RestartObservedHost(InterceptingHost):
    def __init__(self) -> None:
        super().__init__(_build_host())
        self._factory_calls = 0
        self.restarted = threading.Event()

    def spawn(self, spec: ActorSpec) -> ActorCell:
        factory = spec.handler_factory

        def observed_factory():
            handler = factory()
            self._factory_calls += 1
            if self._factory_calls > 1:
                self.restarted.set()
            return handler

        return super().spawn(replace(spec, handler_factory=observed_factory))


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
    return coordinator._cell.snapshot().state


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


def _build_host(*, max_restarts: int = 3) -> ActorHost:
    return create_actor_host(
        ActorEngineConfig(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=max_restarts,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
    )


def _actor_host() -> InterceptingHost:
    return InterceptingHost(_build_host())


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
        actor_host=_actor_host(),
        node_id="node-b",
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
    '_CompletionClosedHost',
    '_CountingCompletionHost',
    '_DoubleRestartReassociateHost',
    '_Events',
    '_RestartObservedHost',
    '_actor_state',
    '_message',
    '_actor_host',
    '_wait_until',
    'authority',
]
