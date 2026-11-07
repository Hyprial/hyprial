from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.correlation.correlation import CompletionReceipt, CorrelationEventRouter
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports import DropRouteCommand, EnsureRouteCommand, RouteSpec
from hyprial.daemon.impl.route_registration import (
    RouteCommandError,
    RouteRegistrationClient,
    RouteRegistrationIo,
)


@dataclass
class _Registration:
    key: str
    closed: bool = False
    close_failures: int = 0
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError(f"injected close failure for {self.key}")
        self.closed = True


class _Transport:
    def __init__(
        self,
        gate: threading.Event | None = None,
        *,
        liveliness_close_failures: int = 0,
        inbox_close_failures: int = 0,
    ) -> None:
        self.gate = gate
        self.liveliness_close_failures = liveliness_close_failures
        self.inbox_close_failures = inbox_close_failures
        self.liveliness: list[_Registration] = []
        self.queryables: list[_Registration] = []

    def declare_liveliness(self, key: str) -> _Registration:
        if self.gate is not None:
            self.gate.wait()
        item = _Registration(key, close_failures=self.liveliness_close_failures)
        self.liveliness.append(item)
        return item

    def declare_queryable(self, key: str, handler: object) -> _Registration:
        item = _Registration(key, close_failures=self.inbox_close_failures)
        self.queryables.append(item)
        return item


class _SelectiveTransport(_Transport):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.entered = threading.Event()
        self.release = threading.Event()

    def declare_liveliness(self, key: str) -> _Registration:
        if self.phase == "liveliness" and "agent-a" in key:
            self.entered.set()
            assert self.release.wait(2)
        return super().declare_liveliness(key)

    def declare_queryable(self, key: str, handler: object) -> _Registration:
        if self.phase == "queryable" and "agent-a" in key:
            self.entered.set()
            assert self.release.wait(2)
        return super().declare_queryable(key, handler)


class _BlockingCloseRegistration(_Registration):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.entered = threading.Event()
        self.release = threading.Event()

    def close(self) -> None:
        self.entered.set()
        assert self.release.wait(2)
        super().close()


class _SelectiveCloseTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.blocked_queryable: _BlockingCloseRegistration | None = None

    def declare_queryable(self, key: str, handler: object) -> _Registration:
        del handler
        if "agent-a" in key and self.blocked_queryable is None:
            item = _BlockingCloseRegistration(key)
            self.blocked_queryable = item
        else:
            item = _Registration(key)
        self.queryables.append(item)
        return item


class _FailThenBlockRegistration(_Registration):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.retry_entered = threading.Event()
        self.retry_release = threading.Event()

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("injected first cleanup failure")
        self.retry_entered.set()
        assert self.retry_release.wait(2)
        self.closed = True


class _PartialPrepareTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.partial_liveliness: _FailThenBlockRegistration | None = None
        self.queryable_failures = 1

    def declare_liveliness(self, key: str) -> _Registration:
        if self.partial_liveliness is None:
            item = _FailThenBlockRegistration(key)
            self.partial_liveliness = item
        else:
            item = _Registration(key)
        self.liveliness.append(item)
        return item

    def declare_queryable(self, key: str, handler: object) -> _Registration:
        if self.queryable_failures:
            self.queryable_failures -= 1
            raise RuntimeError("injected queryable failure")
        return super().declare_queryable(key, handler)


class _OverloadThenRouter:
    def __init__(self, router: CorrelationEventRouter, overloads: int) -> None:
        self.router = router
        self.overloads = overloads
        self.calls = 0

    def publish(self, event: object) -> CompletionReceipt:
        self.calls += 1
        if self.calls <= self.overloads:
            return CompletionReceipt.OVERLOADED
        return self.router.publish(event)  # type: ignore[arg-type]


class _HeldRouteRouter:
    def __init__(self, router: CorrelationEventRouter, held_route: str) -> None:
        self.router = router
        self.held_route = held_route
        self.release = threading.Event()
        self.held = threading.Event()

    def publish(self, event: object) -> CompletionReceipt:
        if (
            getattr(event, "route_id", None) == self.held_route
            and not self.release.is_set()
        ):
            self.held.set()
            return CompletionReceipt.STALE
        return self.router.publish(event)  # type: ignore[arg-type]


class _SlowStopRouteIo(RouteRegistrationIo):
    def __init__(self, *args, expected_stops: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.stop_release = threading.Event()
        self.all_stopping = threading.Event()
        self._expected_stops = expected_stops
        self._stopping = 0
        self._stopping_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _before_native_stop(self) -> None:
        with self._stopping_lock:
            self._stopping += 1
            if self._stopping == self._expected_stops:
                self.all_stopping.set()
        assert self.stop_release.wait(2)


def _command(generation: int = 1, correlation: str = "route-1") -> EnsureRouteCommand:
    return EnsureRouteCommand(
        correlation,
        "attempt-route-1",
        generation,
        1,
        RouteSpec("agent-a", "live/agent-a", "inbox/agent-a"),
    )


def _await_and_retire(
    io: RouteRegistrationIo,
    router: CorrelationEventRouter,
    command: object,
    correlation: str,
    attempt: str,
) -> object:
    waiter = router.register(
        correlation,
        attempt_token=attempt,
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(command) is PortAdmission.ACCEPTED
    event = waiter.wait(1)
    deadline = time.monotonic() + 1
    while not io.receipt_pending(attempt) and time.monotonic() < deadline:
        time.sleep(0.005)
    token = event.provenance.resource_token  # type: ignore[attr-defined]
    assert io.retire_receipt(attempt, token)
    io.confirm_receipt_retired(attempt, token)
    return event


def test_completion_overload_does_not_repeat_zenoh_registration() -> None:
    router = CorrelationEventRouter()
    waiter = router.register(
        "route-1",
        attempt_token="attempt-route-1",
        generation=1,
        versions=frozenset({1}),
    )
    transport = _Transport()
    sink = _OverloadThenRouter(router, overloads=3)
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", sink)

    assert io.submit(_command()) is PortAdmission.ACCEPTED
    event = waiter.wait(1)
    assert event.route_id == "agent-a"  # type: ignore[attr-defined]
    assert sink.calls == 4
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1
    deadline = time.monotonic() + 1
    while not io.receipt_pending("attempt-route-1") and time.monotonic() < deadline:
        time.sleep(0.005)
    io.retire_receipt(
        "attempt-route-1",
        event.provenance.resource_token,  # type: ignore[attr-defined]
    )
    io.confirm_receipt_retired(
        "attempt-route-1",
        event.provenance.resource_token,  # type: ignore[attr-defined]
    )
    assert io.drain(1)


def test_typed_client_concurrent_ensure_has_one_physical_registration() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router
    )
    client = RouteRegistrationClient(io, router)
    spec = RouteSpec("agent-a", "live/agent-a", "inbox/agent-a")
    entered = threading.Barrier(3)
    results: list[object] = []

    def ensure() -> None:
        entered.wait()
        results.append(client.ensure(spec))

    threads = [threading.Thread(target=ensure) for _ in range(2)]
    for thread in threads:
        thread.start()
    entered.wait()
    for thread in threads:
        thread.join(2.0)

    assert len(results) == 2
    assert sorted(event.changed for event in results) == [False, True]  # type: ignore[attr-defined]
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1
    assert client.registered() == ("agent-a",)
    assert client.drop("agent-a").changed is True
    assert client.registered() == ()
    assert io.drain(1)


@pytest.mark.parametrize("phase", ("factory", "liveliness", "queryable"))
def test_blocked_native_prepare_keeps_other_routes_and_controls_responsive(
    phase: str,
) -> None:
    router = CorrelationEventRouter()
    transport = _SelectiveTransport(phase)

    def registration_factory(spec: RouteSpec):  # type: ignore[no-untyped-def]
        if spec.route_id == "agent-a":
            transport.entered.set()
            assert transport.release.wait(2)
        liveliness = _Registration(spec.liveliness_key)
        queryable = _Registration(spec.inbox_key)
        transport.liveliness.append(liveliness)
        transport.queryables.append(queryable)
        return liveliness, queryable

    io = RouteRegistrationIo(
        transport,
        lambda _route: lambda _key: b"ok",
        router,
        registration_factory=registration_factory if phase == "factory" else None,
        native_workers=2,
    )
    client = RouteRegistrationClient(io, router)
    results: list[object] = []
    first = threading.Thread(
        target=lambda: results.append(
            client.ensure(
                RouteSpec("agent-a", "live/agent-a", "inbox/agent-a"),
                owner_lease="session:a",
            )
        ),
        daemon=True,
    )
    first.start()
    assert transport.entered.wait(1)

    other = client.ensure(
        RouteSpec("agent-b", "live/agent-b", "inbox/agent-b"),
        owner_lease="session:b",
    )
    assert other.changed
    assert not io.reassociate(
        "missing-attempt", correlation_id="missing", generation=2, version=2
    )
    assert first.is_alive()

    transport.release.set()
    first.join(2)
    assert not first.is_alive()
    assert len(results) == 1
    assert io.registered() == ("agent-a", "agent-b")
    assert len(transport.liveliness) == 2
    assert len(transport.queryables) == 2
    io.close_registrations()
    assert io.drain(1)


def test_partial_prepare_retains_acquired_handle_until_cleanup_finishes() -> None:
    router = CorrelationEventRouter()
    transport = _PartialPrepareTransport()
    io = RouteRegistrationIo(
        transport,
        lambda _route: lambda _key: b"ok",
        router,
        native_workers=2,
    )
    client = RouteRegistrationClient(io, router)
    spec = RouteSpec("agent-a", "live/agent-a", "inbox/agent-a")
    failures: list[RouteCommandError] = []
    replacement: list[object] = []

    def first_ensure() -> None:
        try:
            client.ensure(spec, owner_lease="session:first")
        except RouteCommandError as error:
            failures.append(error)

    first = threading.Thread(target=first_ensure, daemon=True)
    first.start()
    deadline = time.monotonic() + 1
    while transport.partial_liveliness is None and time.monotonic() < deadline:
        time.sleep(0.005)
    partial = transport.partial_liveliness
    assert partial is not None
    assert partial.retry_entered.wait(1)

    second = threading.Thread(
        target=lambda: replacement.append(
            client.ensure(spec, owner_lease="session:replacement")
        ),
        daemon=True,
    )
    second.start()
    assert len(transport.liveliness) == 1
    assert first.is_alive()
    assert second.is_alive()

    partial.retry_release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert len(failures) == 1
    assert failures[0].code == "ROUTE_IO_FAILED"
    assert len(replacement) == 1
    assert partial.close_calls == 2
    assert partial.closed
    assert len(transport.liveliness) == 2
    assert client.owners("agent-a") == ("session:replacement",)
    io.close_registrations()
    assert io.drain(1)


def test_blocked_last_close_keeps_other_route_progress_and_same_route_fifo() -> None:
    router = CorrelationEventRouter()
    transport = _SelectiveCloseTransport()
    io = RouteRegistrationIo(
        transport,
        lambda _route: lambda _key: b"ok",
        router,
        native_workers=2,
    )
    client = RouteRegistrationClient(io, router)
    spec_a = RouteSpec("agent-a", "live/agent-a", "inbox/agent-a")
    client.ensure(spec_a, owner_lease="session:a")
    blocked = transport.blocked_queryable
    assert blocked is not None
    dropped: list[object] = []
    dropper = threading.Thread(
        target=lambda: dropped.append(
            client.drop("agent-a", owner_lease="session:a")
        ),
        daemon=True,
    )
    dropper.start()
    assert blocked.entered.wait(1)

    replacement: list[object] = []
    replacer = threading.Thread(
        target=lambda: replacement.append(
            client.ensure(spec_a, owner_lease="session:replacement")
        ),
        daemon=True,
    )
    replacer.start()
    other = client.ensure(
        RouteSpec("agent-b", "live/agent-b", "inbox/agent-b"),
        owner_lease="session:b",
    )
    assert other.changed
    assert not io.reassociate(
        "missing-close-attempt",
        correlation_id="missing-close",
        generation=2,
        version=2,
    )
    assert dropper.is_alive()
    assert replacer.is_alive()
    assert len([item for item in transport.queryables if "agent-a" in item.key]) == 1

    blocked.release.set()
    dropper.join(2)
    replacer.join(2)
    assert not dropper.is_alive()
    assert not replacer.is_alive()
    assert len(dropped) == 1
    assert len(replacement) == 1
    assert client.owners("agent-a") == ("session:replacement",)
    assert len([item for item in transport.queryables if "agent-a" in item.key]) == 2
    io.close_registrations()
    assert io.drain(1)


def test_settled_client_rejoins_late_receipt_without_repeating_route_effect() -> None:
    router = CorrelationEventRouter()
    sink = _HeldRouteRouter(router, "agent-a")
    transport = _Transport()
    io = RouteRegistrationIo(
        transport,
        lambda _route: lambda _key: b"ok",
        sink,
        completion_deadline=0.01,
    )
    client = RouteRegistrationClient(io, router, timeout=0.01)
    command = EnsureRouteCommand(
        "settled-route-completion",
        "settled-route-attempt",
        1,
        1,
        RouteSpec("agent-a", "live/agent-a", "inbox/agent-a"),
        "session:s-1",
    )
    request = LifecycleMutationRequest(
        command.correlation_id,
        command.attempt_token,
        "session-route-operation",
        None,
        command,
    )
    results: list[object] = []
    worker = threading.Thread(target=lambda: results.append(client.call_settled(request)))
    worker.start()
    assert sink.held.wait(1)
    assert worker.is_alive()
    assert io.registered() == ("agent-a",)

    other_command = EnsureRouteCommand(
        "other-route-completion",
        "other-route-attempt",
        1,
        1,
        RouteSpec("agent-b", "live/agent-b", "inbox/agent-b"),
        "session:s-2",
    )
    other = client.call_settled(
        LifecycleMutationRequest(
            other_command.correlation_id,
            other_command.attempt_token,
            "other-session-route-operation",
            None,
            other_command,
        )
    )
    assert other.route_id == "agent-b"
    assert worker.is_alive()

    sink.release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].route_id == "agent-a"  # type: ignore[attr-defined]
    assert len(transport.liveliness) == 2
    assert len(transport.queryables) == 2
    assert not io.receipt_pending(command.attempt_token)
    io.close_registrations()
    assert io.drain(1)


def test_distinct_owner_leases_share_one_physical_route_until_last_release() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router
    )
    client = RouteRegistrationClient(io, router)
    spec = RouteSpec("agent-a", "live/agent-a", "inbox/agent-a")

    persona = client.ensure(spec, owner_lease="persona:agent-a")
    managed = client.ensure(spec, owner_lease="managed:agent-a")

    assert persona.changed is True
    assert managed.changed is True
    assert client.owners("agent-a") == (
        "managed:agent-a",
        "persona:agent-a",
    )
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1

    assert client.drop("agent-a", owner_lease="managed:agent-a").changed
    assert client.registered() == ("agent-a",)
    assert not transport.liveliness[0].closed
    assert not transport.queryables[0].closed

    assert client.drop("agent-a", owner_lease="persona:agent-a").changed
    assert client.registered() == ()
    assert transport.liveliness[0].closed
    assert transport.queryables[0].closed
    assert io.drain(1)


def test_concurrent_distinct_owner_acquire_declares_physical_route_once() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router
    )
    client = RouteRegistrationClient(io, router)
    spec = RouteSpec("agent-a", "live/agent-a", "inbox/agent-a")
    barrier = threading.Barrier(3)

    def acquire(owner: str) -> None:
        barrier.wait()
        client.ensure(spec, owner_lease=owner)

    threads = [
        threading.Thread(target=acquire, args=(owner,))
        for owner in ("persona:agent-a", "session:s-1")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert client.owners("agent-a") == (
        "persona:agent-a",
        "session:s-1",
    )
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1
    io.close_registrations()
    assert io.drain(1)


def test_receive_only_route_never_advertises_liveliness() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router
    )
    client = RouteRegistrationClient(io, router)

    client.ensure(
        RouteSpec("legacy", "live/legacy", "inbox/legacy", advertise=False),
        owner_lease="session:s-legacy",
    )

    assert transport.liveliness == []
    assert len(transport.queryables) == 1
    client.drop("legacy", owner_lease="session:s-legacy")
    assert transport.queryables[0].closed
    assert io.drain(1)


def test_committed_domain_receipt_replays_without_reexecuting_route_effect() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router, capacity=1
    )
    command = _command()

    first = router.register(
        "route-1",
        attempt_token="attempt-route-1",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(command) is PortAdmission.ACCEPTED
    event = first.wait(1)
    deadline = time.monotonic() + 1
    while not io.receipt_pending("attempt-route-1") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert io.receipt_pending("attempt-route-1")
    second = router.register(
        "route-1",
        attempt_token="attempt-route-1",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(command) is PortAdmission.ACCEPTED
    replay = second.wait(1)
    assert replay.provenance == event.provenance  # type: ignore[attr-defined]
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1
    other = EnsureRouteCommand(
        "route-other",
        "attempt-other",
        1,
        1,
        RouteSpec("agent-b", "live/agent-b", "inbox/agent-b"),
    )
    assert io.submit(other) is PortAdmission.OVERLOADED
    io.retire_receipt(
        "attempt-route-1",
        replay.provenance.resource_token,  # type: ignore[attr-defined]
    )
    io.confirm_receipt_retired(
        "attempt-route-1",
        replay.provenance.resource_token,  # type: ignore[attr-defined]
    )
    assert io.drain(1)


def test_stale_completion_requires_receipt_reassociation_without_reexecuting_io() -> (
    None
):
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", router)

    assert io.submit(_command(generation=1)) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + 1
    while not io.registered() and time.monotonic() < deadline:
        time.sleep(0.005)
    current = router.register(
        "route-2",
        attempt_token="attempt-route-1",
        generation=2,
        versions=frozenset({1}),
    )
    assert io.reassociate(
        "attempt-route-1", correlation_id="route-2", generation=2, version=1
    )
    event = current.wait(1)
    assert event.generation == 2
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1
    deadline = time.monotonic() + 1
    while not io.receipt_pending("attempt-route-1") and time.monotonic() < deadline:
        time.sleep(0.005)
    io.retire_receipt(
        "attempt-route-1",
        event.provenance.resource_token,  # type: ignore[attr-defined]
    )
    io.confirm_receipt_retired(
        "attempt-route-1",
        event.provenance.resource_token,  # type: ignore[attr-defined]
    )
    assert io.drain(1)


def test_compensation_drop_is_fenced_by_original_route_resource_token() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", router)

    def execute(command: object, correlation: str, attempt: str) -> object:
        waiter = router.register(
            correlation,
            attempt_token=attempt,
            generation=1,
            versions=frozenset({1}),
        )
        assert io.submit(command) is PortAdmission.ACCEPTED
        event = waiter.wait(1)
        deadline = time.monotonic() + 1
        while not io.receipt_pending(attempt) and time.monotonic() < deadline:
            time.sleep(0.005)
        token = event.provenance.resource_token  # type: ignore[attr-defined]
        io.retire_receipt(attempt, token)
        io.confirm_receipt_retired(attempt, token)
        return event

    original = execute(_command(), "route-1", "attempt-route-1")
    original_token = original.provenance.resource_token  # type: ignore[attr-defined]
    io.close_registrations()

    replacement_command = EnsureRouteCommand(
        "route-replacement",
        "attempt-replacement",
        1,
        1,
        RouteSpec("agent-a", "live/agent-a", "inbox/agent-a"),
    )
    replacement = execute(
        replacement_command, "route-replacement", "attempt-replacement"
    )
    replacement_token = replacement.provenance.resource_token  # type: ignore[attr-defined]
    assert replacement_token != original_token

    drop = DropRouteCommand("route-compensate", "attempt-compensate", 1, 1, "agent-a")
    request = LifecycleMutationRequest(
        "route-compensate",
        "attempt-compensate",
        "operation-original",
        original_token,
        drop,
    )
    completion = execute(request, "route-compensate", "attempt-compensate")
    assert completion.changed is False  # type: ignore[attr-defined]
    assert completion.provenance.resource_token == replacement_token  # type: ignore[attr-defined]
    assert io.registered() == ("agent-a",)
    assert io.drain(1)


@pytest.mark.parametrize(("inbox_failures", "liveliness_failures"), ((1, 0), (0, 1)))
def test_partial_drop_keeps_authority_and_retries_only_unclosed_handle(
    inbox_failures: int, liveliness_failures: int
) -> None:
    router = CorrelationEventRouter()
    transport = _Transport(
        inbox_close_failures=inbox_failures,
        liveliness_close_failures=liveliness_failures,
    )
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", router)
    ensured = _await_and_retire(io, router, _command(), "route-1", "attempt-route-1")
    token = ensured.provenance.resource_token  # type: ignore[attr-defined]
    drop_command = DropRouteCommand("route-drop", "attempt-drop", 1, 1, "agent-a")
    request = LifecycleMutationRequest(
        "route-drop", "attempt-drop", "drop-op", token, drop_command
    )

    first = router.register(
        "route-drop",
        attempt_token="attempt-drop",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(request) is PortAdmission.ACCEPTED
    failed = first.wait(1)
    assert failed.code == "ROUTE_PARTIAL_CLEANUP"  # type: ignore[attr-defined]
    assert io.registered() == ("agent-a",)
    client = RouteRegistrationClient(io, router)
    with pytest.raises(RouteCommandError) as acquisition:
        client.ensure(
            RouteSpec("agent-a", "live/agent-a", "inbox/agent-a"),
            owner_lease="session:late",
        )
    assert acquisition.value.code == "ROUTE_PARTIAL_CLEANUP"
    assert len(transport.liveliness) == 1
    assert len(transport.queryables) == 1

    second = router.register(
        "route-drop",
        attempt_token="attempt-drop",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(request) is PortAdmission.ACCEPTED
    completed = second.wait(1)
    assert completed.changed is True  # type: ignore[attr-defined]
    deadline = time.monotonic() + 1
    while not io.receipt_pending("attempt-drop") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert io.retire_receipt("attempt-drop", token)
    io.confirm_receipt_retired("attempt-drop", token)
    assert io.registered() == ()
    assert transport.queryables[0].close_calls == (2 if inbox_failures else 1)
    assert transport.liveliness[0].close_calls == (2 if liveliness_failures else 1)
    assert io.drain(1)


def test_close_registrations_finishes_partial_cleanup_idempotently() -> None:
    router = CorrelationEventRouter()
    transport = _Transport(inbox_close_failures=1)
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", router)
    ensured = _await_and_retire(io, router, _command(), "route-1", "attempt-route-1")
    token = ensured.provenance.resource_token  # type: ignore[attr-defined]
    drop = DropRouteCommand("drop", "drop-attempt", 1, 1, "agent-a")
    request = LifecycleMutationRequest("drop", "drop-attempt", "drop-op", token, drop)
    waiter = router.register(
        "drop",
        attempt_token="drop-attempt",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(request) is PortAdmission.ACCEPTED
    assert waiter.wait(1).code == "ROUTE_PARTIAL_CLEANUP"  # type: ignore[attr-defined]
    assert not io.drain(0.01)

    io.close_registrations()
    first_counts = (
        transport.queryables[0].close_calls,
        transport.liveliness[0].close_calls,
    )
    io.close_registrations()
    assert io.registered() == ()
    assert first_counts == (2, 1)
    assert (
        transport.queryables[0].close_calls,
        transport.liveliness[0].close_calls,
    ) == first_counts
    assert io.drain(1)


def test_blocked_route_io_keeps_drain_incomplete_and_admission_bounded() -> None:
    gate = threading.Event()
    router = CorrelationEventRouter()
    router.register(
        "route-1",
        attempt_token="attempt-route-1",
        generation=1,
        versions=frozenset({1}),
    )
    io = RouteRegistrationIo(
        _Transport(gate),
        lambda _route: lambda _key: b"ok",
        router,
        capacity=1,
    )
    assert io.submit(_command()) is PortAdmission.ACCEPTED
    assert io.submit(
        EnsureRouteCommand(
            "route-2",
            "attempt-route-2",
            1,
            1,
            RouteSpec("agent-b", "live/agent-b", "inbox/agent-b"),
        )
    ) is PortAdmission.OVERLOADED
    assert not io.drain(0.02)
    gate.set()


def test_drain_retry_does_not_resend_native_stop_tokens() -> None:
    router = CorrelationEventRouter()
    io = _SlowStopRouteIo(
        _Transport(),
        lambda _route: lambda _key: b"ok",
        router,
        capacity=2,
        native_workers=2,
        expected_stops=2,
    )

    assert not io.drain(0.01)
    assert io.all_stopping.wait(1)
    assert io._native_stops_sent == 2
    io.stop_release.set()

    assert io.drain(1)
    assert io.drain(0)
    assert io._native_stops_sent == 2
    assert io._native_queue.unfinished_tasks == 0
    assert io._queue.unfinished_tasks == 0


def test_close_timeout_keeps_teardown_behind_admitted_declaration() -> None:
    gate = threading.Event()
    router = CorrelationEventRouter()
    transport = _Transport(gate)
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router, capacity=2
    )
    waiter = router.register(
        "route-1", attempt_token="attempt-route-1", generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(_command()) is PortAdmission.ACCEPTED
    assert not io.drain(0.01)
    with pytest.raises(TimeoutError, match="did not finish"):
        io.close_registrations(timeout=0.02)
    assert transport.liveliness == []
    gate.set()
    event = waiter.wait(1)
    deadline = time.monotonic() + 1
    while not io.receipt_pending("attempt-route-1") and time.monotonic() < deadline:
        time.sleep(0.005)
    token = event.provenance.resource_token  # type: ignore[attr-defined]
    io.retire_receipt("attempt-route-1", token)
    io.confirm_receipt_retired("attempt-route-1", token)
    io.close_registrations()
    assert transport.liveliness[0].closed
    assert transport.queryables[0].closed
    assert io.drain(1)


def test_repeated_close_timeouts_coalesce_one_shutdown_custody() -> None:
    gate = threading.Event()
    router = CorrelationEventRouter()
    transport = _Transport(gate)
    io = RouteRegistrationIo(
        transport, lambda _route: lambda _key: b"ok", router, capacity=4
    )
    waiter = router.register(
        "route-1",
        attempt_token="attempt-route-1",
        generation=1,
        versions=frozenset({1}),
    )
    assert io.submit(_command()) is PortAdmission.ACCEPTED
    for _ in range(8):
        with pytest.raises(TimeoutError, match="did not finish"):
            io.close_registrations(0.005)

    deadline = time.monotonic() + 1
    while not io._shutdown_tokens and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(io._shutdown_tokens) == 1
    assert sum(len(items) for items in io._route_waiting.values()) == 1
    close_command = io._close_command
    assert close_command is not None
    assert close_command.pending == 1

    gate.set()
    event = waiter.wait(1)
    io.close_registrations(1)
    assert close_command.completed.is_set()
    assert io._shutdown_tokens == {}
    assert io._route_waiting == {}
    token = event.provenance.resource_token  # type: ignore[attr-defined]
    assert io.retire_receipt("attempt-route-1", token)
    io.confirm_receipt_retired("attempt-route-1", token)
    assert io.drain(1)


def test_late_reassociation_after_close_reports_fenced_failure() -> None:
    router = CorrelationEventRouter()
    transport = _Transport()
    io = RouteRegistrationIo(transport, lambda _route: lambda _key: b"ok", router)
    assert io.submit(_command()) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + 1
    while not io.registered() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert io.registered() == ("agent-a",)
    io.close_registrations()
    waiter = router.register(
        "late-generation", attempt_token="attempt-route-1",
        generation=2, versions=frozenset({1}),
    )
    assert io.reassociate(
        "attempt-route-1", correlation_id="late-generation",
        generation=2, version=1,
    )
    event = waiter.wait(1)
    assert event.code == "ROUTE_CLOSED_BEFORE_COMPLETION"  # type: ignore[attr-defined]
    assert io.registered() == ()
    assert transport.queryables[0].closed
    assert io.drain(1)
