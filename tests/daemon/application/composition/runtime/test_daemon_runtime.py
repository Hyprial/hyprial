from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl import (
    DaemonEventBridge,
    DesiredStateStore,
    HarnessLaunchSpec,
    ManagedHarnessRuntime,
)
from hyprial.kernel import canonical_agent_uri
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxAuthorityUnavailable, InboxMessage
from tests.support.runtime._desired_state import write_v1


class FakeProcess:
    def __init__(self) -> None:
        self.running = True
        self.pid: int | None = None
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class FakeHarnessLauncher:
    def __init__(self) -> None:
        self.started: list[tuple[str, FakeProcess]] = []

    def start(self, spec):  # type: ignore[no-untyped-def]
        process = FakeProcess()
        # A real managed process owns these; the projection reads them from the
        # process, never from the launch spec.
        process.endpoint = "http://127.0.0.1:41234"
        process.dsh_home = Path("/tmp/worker/dsh")
        self.started.append((spec.name, process))
        return process


def _tick_until(runtime, done, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    """Tick reconcile until ``done(outcome)`` holds.

    Harness starts are asynchronous by design (a slow/unstartable harness
    must never block the accept loop), so a restart is submitted on one tick
    and harvested on a later one; tests that observe a restart must tick
    until the harvest lands.
    """

    deadline = time.monotonic() + timeout
    outcome = runtime.on_timer()
    while not done(outcome):
        assert time.monotonic() < deadline, "condition never harvested"
        time.sleep(0.01)
        outcome = runtime.on_timer()
    return outcome


def test_supervisor_status_reports_the_process_owned_dsh_endpoint() -> None:
    launcher = FakeHarnessLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.start(
        HarnessLaunchSpec(
            "dsh",
            "dsh-worker",
            True,
            # A persisted endpoint argument is no longer a source of truth:
            # the endpoint is an OUTPUT of the spawned generation.
            args=("--endpoint", "http://127.0.0.1:4080"),
        )
    )
    process = launcher.started[0][1]

    status = supervisor.status()[0]
    assert status["endpoint"] == "http://127.0.0.1:41234"
    assert status["dshHome"] == "/tmp/worker/dsh"
    assert process.endpoint == "http://127.0.0.1:41234"


def test_supervisor_status_surfaces_process_runtime_error() -> None:
    launcher = FakeHarnessLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    try:
        supervisor.start(HarnessLaunchSpec("claude", "worker", True))
        launcher.started[0][1].last_error = "SDK worker failed before ready"  # type: ignore[attr-defined]
        # Status is an immutable last-observed projection. A reconcile admits
        # the next process fact observation without reading native state here.
        assert "error" not in supervisor.status()[0]
        supervisor.reconcile()
        deadline = time.monotonic() + 1
        while "error" not in supervisor.status()[0] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert supervisor.status() == (
            {
                "id": "claude:worker",
                "runtime": "claude",
                "name": "worker",
                "running": True,
                "pid": None,
                "error": "SDK worker failed before ready",
            },
        )
    finally:
        supervisor.stop(2.0)


class FakeRegistration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self) -> None:
        self.liveliness: list[str] = []
        self.closed = False

    def declare_liveliness(self, key: str) -> FakeRegistration:
        self.liveliness.append(key)
        return FakeRegistration()

    def close(self) -> None:
        self.closed = True


class FakeInbox:
    def __init__(self) -> None:
        self.retries = 0
        self.closed = False

    def retry_due(self):  # type: ignore[no-untyped-def]
        self.retries += 1
        return []

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def pending_messages(self, actor):  # type: ignore[no-untyped-def]
        return []

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("unclean_marker", [False, True])
def test_event_bridge_start_never_restores_harness_processes(
    tmp_path: Path, unclean_marker: bool
) -> None:
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    if unclean_marker:
        (tmp_path / "daemon-run.json").write_text(
            '{"schemaVersion":1,"pid":101}', encoding="utf-8"
        )
    launcher = FakeHarnessLauncher()
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="mac-studio",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=FakeInbox(),
        harnesses=ManagedHarnessRuntime(launcher),
    )

    summary = runtime.start()

    assert summary.attempted == 0
    assert summary.restored == 0
    assert summary.failed == 0
    assert summary.previous_run_unclean is unclean_marker
    assert launcher.started == []
    runtime.stop()


def test_mailbox_role_is_advertised_and_provider_crashes_are_restarted(
    tmp_path: Path,
) -> None:
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path, as_mailbox=True)
    launcher = FakeHarnessLauncher()
    transport = FakeTransport()
    desired_state = DesiredStateStore(desired_path)
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.restore(desired_state.load().harnesses)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-mailbox",
        desired_state=desired_state,
        transport=transport,
        inbox=FakeInbox(),
        harnesses=supervisor,
    )
    runtime.start()
    assert transport.liveliness == ["hyprial/v1/liveliness/mailbox/node-mailbox"]

    launcher.started[0][1].running = False
    outcome = _tick_until(runtime, lambda o: o.harness_restarts == 1)

    assert outcome.harness_restarts == 1
    assert [name for name, _process in launcher.started] == ["squire", "squire"]
    runtime.stop()


def test_on_timer_retries_orphan_collection_every_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    supervisor = ManagedHarnessRuntime(FakeHarnessLauncher())
    monkeypatch.setattr(
        supervisor,
        "collect_orphans",
        lambda: calls.append(None) or 0,
    )
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-gc",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=FakeTransport(),
        inbox=FakeInbox(),
        harnesses=supervisor,
    )
    runtime.start()
    try:
        first = runtime.on_timer()
        second = runtime.on_timer()
    finally:
        runtime.stop()

    assert calls == [None, None]
    assert first.harness_orphans_retired == 0
    assert second.harness_orphans_retired == 0
    assert "harnesses.collect_orphans" in dict(first.phase_ms)


def test_on_timer_drains_readiness_reports(tmp_path: Path) -> None:
    """Phase ③ rides the tick: disposition reports land in ReconcileSummary.

    Restore settles one start, which buffers one report inside the actor;
    the next ``on_timer`` drains it into ``harness_readiness`` under its own
    ``harnesses.drain_readiness`` phase, and a tick with no new dispositions
    drains empty -- reports are events, not a per-tick heartbeat.
    """

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    launcher = FakeHarnessLauncher()
    desired_state = DesiredStateStore(desired_path)
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.restore(desired_state.load().harnesses)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-readiness",
        desired_state=desired_state,
        transport=FakeTransport(),
        inbox=FakeInbox(),
        harnesses=supervisor,
    )
    runtime.start()

    outcome = runtime.on_timer()

    assert [report.source for report in outcome.harness_readiness] == ["pi:squire"]
    assert outcome.harness_readiness[0].phase == "connector-up"
    assert outcome.harness_readiness[0].verdict == "ready"
    assert "harnesses.drain_readiness" in dict(outcome.phase_ms)
    # Drained once: the same disposition must not be reported twice.
    assert runtime.on_timer().harness_readiness == ()
    runtime.stop()


class FakeStreamingProcess(FakeProcess):
    """Satisfies the StreamingHarnessProcess protocol for the pump."""

    def __init__(self) -> None:
        super().__init__()
        self.enqueued: list[object] = []

    def enqueue(self, delivery) -> bool:  # type: ignore[no-untyped-def]
        self.enqueued.append(delivery)
        return True

    def drain_results(self):  # type: ignore[no-untyped-def]
        return ()

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        return False


class FakeStreamingLauncher:
    def __init__(self) -> None:
        self.started: list[tuple[str, FakeStreamingProcess]] = []
        self.attempts = 0
        self.fail_next = False

    def start(self, spec):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.fail_next:
            raise RuntimeError("provider start refused for the test")
        process = FakeStreamingProcess()
        self.started.append((spec.name, process))
        return process


class FakeActorRegistration:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.close_attribution: list[tuple[str, str]] = []

    @property
    def healthy(self) -> bool:
        return not self.closed

    def close(
        self,
        *,
        reason: str = "unspecified",
        initiator: str = "external-caller",
    ) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_attribution.append((reason, initiator))


# The identity the daemon advertises for a managed provider is the
# canonical four-segment URI, never the supervisor-local short name.
SQUIRE_URI = canonical_agent_uri("op", "node-reg", "squire")


def _streaming_runtime(
    tmp_path: Path,
    *,
    clock=None,  # type: ignore[no-untyped-def]
    restart_backoff_seconds: float = 0.0,
):  # type: ignore[no-untyped-def]
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    launcher = FakeStreamingLauncher()
    registrations: list[FakeActorRegistration] = []

    def registrar(actor_uri: str) -> FakeActorRegistration:
        registration = FakeActorRegistration(actor_uri)
        registrations.append(registration)
        return registration

    desired_state = DesiredStateStore(desired_path)
    supervisor = ManagedHarnessRuntime(
        launcher,
        clock=clock,
        restart_backoff_seconds=restart_backoff_seconds,
        # Tests toggle launcher failures per attempt; never let the
        # failure-budget termination interfere with those scenarios.
        failure_budget=0,
    )
    supervisor.restore(desired_state.load().harnesses)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=desired_state,
        transport=FakeTransport(),
        inbox=FakeInbox(),
        harnesses=supervisor,
        harness_actor_registrar=registrar,
        harness_actor_uri=lambda name: canonical_agent_uri("op", "node-reg", name),
    )
    return runtime, launcher, registrations


def test_streaming_provider_actor_is_registered_on_start(tmp_path: Path) -> None:
    runtime, _launcher, registrations = _streaming_runtime(tmp_path)
    runtime.start()
    try:
        assert [item.name for item in registrations] == [SQUIRE_URI]
        assert not registrations[0].closed
    finally:
        runtime.stop()


def test_active_streaming_provider_redeclares_a_lost_actor_registration(
    tmp_path: Path,
) -> None:
    """A retained map key must not make a dead registration permanent.

    This fault injection does not claim what invalidated the production Pi
    registration.  It isolates the lifecycle gap reported by that incident:
    once the registration handle is no longer effective while the supervised
    worker remains active, periodic reconcile must recreate it without waiting
    for an operator to restart the connector.
    """

    runtime, _launcher, registrations = _streaming_runtime(tmp_path)
    runtime.start()
    try:
        assert len(registrations) == 1
        registrations[0].close()

        runtime.on_timer()

        assert len(registrations) == 2
        assert registrations[1].name == SQUIRE_URI
        assert registrations[1].closed is False
        assert registrations[0].close_attribution == [
            ("unspecified", "external-caller")
        ]

        runtime.on_timer()

        assert len(registrations) == 2
    finally:
        runtime.stop()


def test_streaming_provider_gets_system_notice_once_without_fifo_ack(
    tmp_path: Path,
) -> None:
    notice = InboxMessage(
        message_id="notice-1",
        conversation_id="conversation-1",
        sender="system:hyprial:remote-node",
        recipient=SQUIRE_URI,
        payload=b'{"message":"failed: original-1"}',
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=1,
    )

    class NoticeInbox(FakeInbox):
        def __init__(self) -> None:
            super().__init__()
            self.notices = [notice]
            self.dismissed: list[str] = []

        def system_notices(self, actor: str):  # type: ignore[no-untyped-def]
            return tuple(item for item in self.notices if item.recipient == actor)

        def dismiss_system_notice(self, message_id: str) -> bool:
            self.dismissed.append(message_id)
            self.notices = [
                item for item in self.notices if item.message_id != message_id
            ]
            return True

    runtime, launcher, _registrations = _streaming_runtime(tmp_path)
    inbox = NoticeInbox()
    runtime.inbox = inbox  # type: ignore[assignment]
    runtime.start()
    try:
        summary = runtime.on_timer()

        assert summary.harness_deliveries == 1
        assert inbox.dismissed == [notice.message_id]
        assert inbox.pending_messages(SQUIRE_URI) == []
        assert len(launcher.started[0][1].enqueued) == 1
        delivery = launcher.started[0][1].enqueued[0]
        assert delivery.delivery_id == notice.message_id  # type: ignore[attr-defined]
        assert (delivery.sender, delivery.notice) == (notice.sender, True)  # type: ignore[attr-defined]

        assert runtime.on_timer().harness_deliveries == 0
        assert len(launcher.started[0][1].enqueued) == 1
    finally:
        runtime.stop()


def test_restart_withdraws_then_redeclares_the_registration(tmp_path: Path) -> None:
    """Starts are asynchronous, so a death always costs one registration gap:
    the token is withdrawn when the corpse is reaped and redeclared on the
    tick that harvests the replacement.  In-flight sends queue durably in
    between instead of targeting a dead connector."""

    runtime, launcher, registrations = _streaming_runtime(tmp_path)
    runtime.start()
    try:
        launcher.started[0][1].running = False
        runtime.on_timer()  # corpse reaped, replacement submitted async
        _tick_until(runtime, lambda _o: registrations[0].closed)
        assert registrations[0].closed is True

        _tick_until(runtime, lambda _o: len(registrations) == 2)
        assert [item.name for item in registrations] == [SQUIRE_URI, SQUIRE_URI]
        assert registrations[1].closed is False
    finally:
        runtime.stop()


def test_dead_child_is_offline_before_bounded_supervisor_restart(
    tmp_path: Path,
) -> None:
    """The daemon must expose a real stopped/error interval and withdraw the
    target before desired-state recovery advertises a replacement process."""

    now = {"value": 10.0}
    runtime, launcher, registrations = _streaming_runtime(
        tmp_path,
        clock=lambda: now["value"],
        restart_backoff_seconds=1.0,
    )
    runtime.start()
    try:
        crashed = launcher.started[0][1]
        crashed.last_error = "pi RPC process exited with status 23"  # type: ignore[attr-defined]
        crashed.running = False

        # Reconciliation and process stop run in bounded effect workers. Wait
        # for the crashed generation's stop without advancing the fake clock:
        # the backoff still forbids replacement during the offline interval.
        first = _tick_until(
            runtime,
            lambda _outcome: crashed.stops == 1
            and runtime.harnesses.status()[0]["running"] is False,
        )

        assert first.harness_restarts == 0
        assert crashed.stops == 1
        _tick_until(runtime, lambda _o: registrations[0].closed)
        assert registrations[0].closed is True
        assert registrations[0].close_attribution == [
            ("actor-inactive", "daemon-runtime")
        ]
        assert runtime.harnesses.status() == (
            {
                "id": "pi:squire",
                "runtime": "pi",
                "name": "squire",
                "running": False,
                "pid": None,
                "state": "retrying",
                "error": "pi RPC process exited with status 23",
            },
        )

        now["value"] += 1.01
        second = _tick_until(runtime, lambda o: o.harness_restarts == 1)
        assert second.harness_restarts == 1
        assert [item.name for item in registrations] == [SQUIRE_URI, SQUIRE_URI]
        assert registrations[1].closed is False
    finally:
        runtime.stop()


def test_unrestartable_provider_loses_its_registration_until_recovery(
    tmp_path: Path,
) -> None:
    """A connector that dies and cannot restart must disappear from the
    network so later sends queue durably instead of targeting a corpse;
    recovery registers anew."""

    runtime, launcher, registrations = _streaming_runtime(tmp_path)
    runtime.start()
    try:
        launcher.started[0][1].running = False
        launcher.fail_next = True
        runtime.on_timer()  # stops the corpse, submits a start that fails
        _tick_until(runtime, lambda _o: registrations[0].closed)
        assert registrations[0].closed is True
        assert registrations[0].close_attribution == [
            ("actor-inactive", "daemon-runtime")
        ]

        # Wait for the failed attempt to be consumed, then allow starts again;
        # the next ticks submit a replacement and harvest it.
        deadline = time.monotonic() + 5.0
        while launcher.attempts < 2:
            assert time.monotonic() < deadline, "failing start never attempted"
            time.sleep(0.01)
        launcher.fail_next = False
        _tick_until(runtime, lambda o: len(registrations) == 2)
        assert [item.name for item in registrations] == [SQUIRE_URI, SQUIRE_URI]
        assert registrations[1].closed is False
    finally:
        runtime.stop()


def test_stop_closes_every_actor_registration(tmp_path: Path) -> None:
    runtime, _launcher, registrations = _streaming_runtime(tmp_path)
    runtime.start()
    runtime.stop()
    assert registrations and all(item.closed for item in registrations)
    assert registrations[0].close_attribution == [
        ("daemon-stop", "daemon-runtime")
    ]


def test_non_streaming_processes_are_not_registered(tmp_path: Path) -> None:
    """PTY-style providers have no delivery channel yet; registering them
    would advertise an actor whose messages nobody could ever consume."""

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    registrations: list[FakeActorRegistration] = []
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-pty",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=FakeInbox(),
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
        harness_actor_registrar=lambda name: registrations.append(
            FakeActorRegistration(name)
        )
        or registrations[-1],
    )
    runtime.start()
    try:
        assert registrations == []
    finally:
        runtime.stop()


def test_stuck_outbound_retry_does_not_hold_the_reconcile_tick(
    tmp_path: Path,
) -> None:
    """2026-09-14: ``retry_due`` waits on outbound sends up to the facade's
    authority timeout; 62 of 99 reconcile-tick overruns were this one call,
    and the steps queued behind it (inbox.prune, adapters, workflow) paid the
    wait too.  The tick now only KICKS the retry pump: a stuck send occupies
    the pump thread, never the tick, and the steps behind it still run.

    Mutation contract: calling ``self.inbox.retry_due`` synchronously from
    ``on_timer`` again makes this test go red (the tick then takes the full
    blocked time instead of returning inside its budget).
    """

    class _StuckInbox(FakeInbox):
        def __init__(self) -> None:
            super().__init__()
            self.prunes = 0
            self.entered = threading.Event()
            self.release = threading.Event()

        def retry_due(self):  # type: ignore[no-untyped-def]
            self.entered.set()
            # Bounded stand-in for "forever": long enough that a synchronous
            # call would blow the 1s tick budget several times over, short
            # enough that the mutated shape fails instead of hanging.
            self.release.wait(3.0)
            self.retries += 1
            return []

        def prune_inbox(self):  # type: ignore[no-untyped-def]
            self.prunes += 1
            return []

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    inbox = _StuckInbox()
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-retry-pump",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
    )
    runtime.start()
    try:
        started = time.monotonic()
        runtime.on_timer()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"reconcile tick held by outbound retry: {elapsed:.2f}s"
        # The step queued behind retry_due in the tick still ran.
        assert inbox.prunes == 1
        # The kick did reach the pump, which now owns the stuck call.
        assert inbox.entered.wait(2.0), "retry pump never ran retry_due"
    finally:
        inbox.release.set()
        runtime.stop()

    # stop() halted and joined the pump.
    assert runtime._retry_pump_thread is None


def test_retry_pump_logs_completion_counts_off_the_tick(tmp_path: Path) -> None:
    """Settled retry results are counted on the pump's own event, so the
    tick's inbox_results no longer claims work that is still in flight."""

    class _OneResultInbox(FakeInbox):
        def retry_due(self):  # type: ignore[no-untyped-def]
            self.retries += 1
            return [object()]

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    inbox = _OneResultInbox()
    logged: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-retry-pump",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
        logger=lambda level, component, event, **fields: logged.append(
            (level, component, event, fields)
        ),
    )
    runtime.start()
    try:
        outcome = runtime.on_timer()
        assert outcome.inbox_results == 0
        deadline = time.monotonic() + 2.0
        while inbox.retries == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inbox.retries >= 1
        deadline = time.monotonic() + 2.0
        while not any(event == "inbox.retry_pump.completed" for _, _, event, _ in logged) and time.monotonic() < deadline:
            time.sleep(0.01)
        completed = [
            fields for _, _, event, fields in logged
            if event == "inbox.retry_pump.completed"
        ]
        assert completed and completed[0]["inboxResults"] == 1
    finally:
        runtime.stop()


def test_retry_pump_survives_retry_due_exceptions(tmp_path: Path) -> None:
    """The pump is a thread whose whole job is calling into failure-prone
    I/O: an exception from retry_due must be logged and the next kick still
    processed -- never the silent-thread-death shape this change set exists
    to kill (2026-09-14).

    Mutation contract: letting the exception propagate out of the pump loop
    makes this test go red (the pump thread dies, the second kick is never
    consumed).
    """

    class _FlakyInbox(FakeInbox):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1
        # Due now, as production's contract reader reports (no reader: idle ceiling).
        next_retry_due_ms = staticmethod(lambda: 0)

        def retry_due(self):  # type: ignore[no-untyped-def]
            self.retries += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("authority timeout stand-in")
            return []

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    inbox = _FlakyInbox()
    logged: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-retry-pump",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
        logger=lambda level, component, event, **fields: logged.append(
            (level, component, event, fields)
        ),
    )
    runtime.start()
    try:
        runtime.on_timer()  # kick 1: retry_due raises inside the pump
        deadline = time.monotonic() + 2.0
        while inbox.retries < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inbox.retries == 1
        runtime.on_timer()  # kick 2: must still be processed
        deadline = time.monotonic() + 2.0
        while inbox.retries < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inbox.retries >= 2, "kick after a pump exception was never processed"
        failed = [
            fields for _, _, event, fields in logged
            if event == "inbox.retry_pump.failed"
        ]
        assert failed and failed[0]["errorType"] == "RuntimeError"
        assert "durationMs" in failed[0]
        pump = runtime._retry_pump_thread
        assert pump is not None and pump.is_alive()
    finally:
        runtime.stop()


def test_retry_pump_yields_on_stale_command_without_error(tmp_path: Path) -> None:
    """retry_due left the tick thread, so the pump now races the tick's own
    inbox commands; a generation/version fence that goes stale mid-flight is
    the expected loser of that race, not a fault.  A rejected round yields
    at info -- an error line on every such kick would read as a persistent
    pump failure, which it is not (the next kick re-reads the fence and
    runs).
    """

    class _StaleOnceInbox(FakeInbox):
        def __init__(self) -> None:
            super().__init__()
            self.stale_rounds = 1
        next_retry_due_ms = staticmethod(lambda: 0)  # as _FlakyInbox above

        def retry_due(self):  # type: ignore[no-untyped-def]
            self.retries += 1
            if self.stale_rounds:
                self.stale_rounds -= 1
                raise InboxAuthorityUnavailable(
                    "STALE_COMMAND: delivery command generation/version "
                    "fence is stale"
                )
            return []

    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    inbox = _StaleOnceInbox()
    logged: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-retry-pump",
        desired_state=DesiredStateStore(desired_path),
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
        logger=lambda level, component, event, **fields: logged.append(
            (level, component, event, fields)
        ),
    )
    runtime.start()
    try:
        runtime.on_timer()  # kick 1: the fence went stale, round yielded
        deadline = time.monotonic() + 2.0
        while inbox.retries < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inbox.retries == 1
        runtime.on_timer()  # kick 2: fresh fence, round runs
        deadline = time.monotonic() + 2.0
        while inbox.retries < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inbox.retries >= 2, "kick after a stale yield was never processed"

        # The yield is visible, at info, with the rejection detail -- not an
        # error: only real pump failures reach inbox.retry_pump.failed.
        yielded = [
            (level, fields)
            for level, _component, event, fields in logged
            if event == "inbox.retry_pump.yielded"
        ]
        assert yielded and yielded[0][0] == "info"
        assert str(yielded[0][1]["detail"]).startswith("STALE_COMMAND")
        assert "durationMs" in yielded[0][1]
        assert not [
            (level, event)
            for level, _component, event, _fields in logged
            if event == "inbox.retry_pump.failed"
        ]
        pump = runtime._retry_pump_thread
        assert pump is not None and pump.is_alive()
    finally:
        runtime.stop()


def test_prune_yields_on_fence_race_and_retries_next_tick(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.api import InboxPruneItem

    item = InboxPruneItem("expired", "agent:o:n:worker", "TTL_EXPIRED", 1, 2)

    class RacingInbox(FakeInbox):
        calls = 0

        def prune_inbox(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise InboxAuthorityUnavailable("STALE_COMMAND: fence advanced")
            return (item,)

    desired = tmp_path / "desired-state.json"
    write_v1(desired)
    inbox = RacingInbox()
    logged: list[tuple[str, str]] = []
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="prune-fence-race",
        desired_state=DesiredStateStore(desired),
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
        logger=lambda level, _component, event, **_fields: logged.append(
            (level, event)
        ),
    )
    runtime.start()
    try:
        # Inject the actor's rejection at the boundary, without a scheduling race.
        # The next tick must perform the sweep and report its actual result.
        assert runtime.on_timer().inbox_pruned_items == ()
        assert inbox.calls == 1
        assert logged == [("info", "inbox.prune.yielded")]
        assert runtime.on_timer().inbox_pruned_items == (item,)
        assert inbox.calls == 2
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "error",
    [
        InboxAuthorityUnavailable("inbox command admission failed: closing"),
        OSError("database unavailable"),
    ],
)
def test_prune_preserves_non_fence_failures(tmp_path: Path, error: Exception) -> None:
    class BrokenInbox(FakeInbox):
        def prune_inbox(self):  # type: ignore[no-untyped-def]
            raise error

    desired = tmp_path / "desired-state.json"
    write_v1(desired)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="prune-failure",
        desired_state=DesiredStateStore(desired),
        transport=FakeTransport(),
        inbox=BrokenInbox(),
        harnesses=ManagedHarnessRuntime(FakeHarnessLauncher()),
    )
    runtime.start()
    try:
        with pytest.raises(type(error)) as caught:
            runtime.on_timer()
        assert caught.value is error
    finally:
        runtime.stop()


def test_dead_child_stop_effect_can_be_held_after_offline_projection(
    tmp_path: Path,
) -> None:
    """A timer return is not the old process-stop acknowledgement."""
    now = {"value": 10.0}
    runtime, launcher, registrations = _streaming_runtime(
        tmp_path,
        clock=lambda: now["value"],
        restart_backoff_seconds=1.0,
    )
    entered = threading.Event()
    release = threading.Event()
    runtime.start()
    try:
        crashed = launcher.started[0][1]
        original_stop = crashed.stop

        def held_stop() -> None:
            entered.set()
            assert release.wait(5), "held stop was never released"
            original_stop()

        crashed.stop = held_stop  # type: ignore[method-assign]
        crashed.last_error = "pi RPC process exited with status 23"  # type: ignore[attr-defined]
        crashed.running = False

        first = runtime.on_timer()
        assert first.harness_restarts == 0
        assert entered.wait(5), "reconcile did not submit the old-process stop"
        assert crashed.stops == 0
        assert len(launcher.started) == 1
        assert runtime.harnesses.status()[0]["running"] is False
        assert runtime.harnesses.status()[0]["error"] == crashed.last_error

        release.set()
        _tick_until(runtime, lambda _outcome: crashed.stops == 1)
        _tick_until(runtime, lambda _outcome: registrations[0].closed)
        assert len(launcher.started) == 1
        now["value"] += 1.01
        _tick_until(runtime, lambda outcome: outcome.harness_restarts == 1)
        assert len(launcher.started) == 2
    finally:
        release.set()
        runtime.stop()
