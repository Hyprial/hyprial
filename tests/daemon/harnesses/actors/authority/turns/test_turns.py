from __future__ import annotations

import pytest

import threading
import ast
import inspect
import textwrap
from typing import Any, get_args
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessCommand, HarnessLaunchProjection, HarnessTimerElapsedCommand, RestoreEligibilityProjection, UpdateRestoreEligibilityCommand, harness_desired_generation, RemoveHarnessCommand
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

from ..support import (
    _AgentIdentity,
    _Launcher,
    _Process,
    _actor_dispatch_source,
    _envelope,
    _handled_frozen_commands,
    _spec,
    _steer_envelope,
    _wait_for,
)


def test_dispatch_waits_for_streaming_process_readiness() -> None:
    class _ReadinessStreaming(_Process):
        dispatch_ready = False

        def __init__(self) -> None:
            super().__init__()
            self.deliveries: list[HarnessDelivery] = []

        def enqueue(self, delivery: HarnessDelivery) -> bool:
            self.deliveries.append(delivery)
            return True

        def drain_results(self) -> tuple[object, ...]:
            return ()

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

    class _ReadinessLauncher:
        def __init__(self) -> None:
            self.process = _ReadinessStreaming()

        def start(self, _spec: HarnessLaunchSpec) -> _ReadinessStreaming:
            return self.process

    launcher = _ReadinessLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    try:
        assert supervisor.start(_spec())

        waiting = HarnessDelivery(
            "waiting", "conversation", "sender", "worker", "hello"
        )
        assert supervisor.dispatch(_envelope(waiting)) is False
        assert launcher.process.deliveries == []

        launcher.process.dispatch_ready = True
        supervisor.reconcile()
        _wait_for(
            lambda: supervisor._actor._records[
                "codex:worker"
            ].process.dispatch_ready
        )
        ready = HarnessDelivery(
            "ready", "conversation", "sender", "worker", "hello"
        )
        assert supervisor.dispatch(_envelope(ready)) is True
        assert launcher.process.deliveries == [ready]
    finally:
        supervisor.stop()


def test_restore_owner_rejects_queued_old_entity_after_recreate() -> None:
    launcher = _Launcher()
    identity = _AgentIdentity("entity-old")
    actor = HarnessRuntimeActor(
        launcher,
        agent_identity=identity,
        restart_backoff_seconds=0,
    )
    spec = HarnessLaunchProjection("codex", "worker", True)
    assert actor.submit(EnsureHarnessCommand("start-old", spec)) is PortAdmission.ACCEPTED
    _wait_for(lambda: len(launcher.processes) == 1)
    old = RestoreEligibilityProjection(
        "worker",
        "entity-old",
        harness_desired_generation(spec),
        True,
        1,
        1,
    )
    assert actor.submit(
        UpdateRestoreEligibilityCommand("queue-old", old)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: actor._restore_eligibility.get("worker") == old)

    assert actor.submit(
        RemoveHarnessCommand("remove-old", "codex", "worker")
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: actor.read_harness("codex:worker") is None)
    identity.token = "entity-new"
    assert actor.submit(
        EnsureHarnessCommand("start-new", spec)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: len(launcher.processes) == 2)

    delayed_old = RestoreEligibilityProjection(
        "worker",
        "entity-old",
        harness_desired_generation(spec),
        True,
        99,
        99,
    )
    assert actor.submit(
        UpdateRestoreEligibilityCommand("retry-old", delayed_old)
    ) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: (snapshot := actor._cell.snapshot()).queued
        == snapshot.in_flight == 0
    )
    assert actor._restore_eligibility.get("worker") is None

    launcher.processes[-1].running = False
    assert actor.submit(
        HarnessTimerElapsedCommand("tick-new", actor.generation, 1, 1)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: len(launcher.processes) == 3)
    assert actor.close_runtime(2.0)


def test_process_death_withdraws_actor_owned_liveness_binding() -> None:
    class _Binding:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()

    launcher = _Launcher()
    supervisor = ManagedHarnessRuntime(launcher, restart_backoff_seconds=30.0)
    supervisor.start(_spec())
    binding = _Binding()
    assert supervisor.bind_liveness("codex", "worker", binding) is True

    launcher.processes[0].running = False
    supervisor.reconcile()

    assert binding.closed.wait(1.0)
    supervisor.stop()


def test_reverse_mutation_missing_frozen_handler_is_detected() -> None:
    source = textwrap.dedent(
        inspect.getsource(HarnessRuntimeActor._dispatch_command)
    )
    mutated = source.replace(
        "elif isinstance(command, RemoveHarnessCommand):",
        "elif False:",
        1,
    )
    assert "isinstance(command, RemoveHarnessCommand)" in source
    mutated_counts = _handled_frozen_commands(mutated)
    assert set(mutated_counts) != set(get_args(HarnessCommand))
    assert RemoveHarnessCommand not in mutated_counts


def test_actor_handler_has_no_blocking_proxy_or_process_io_calls() -> None:
    source = textwrap.dedent(_actor_dispatch_source())
    tree = ast.parse(source)
    forbidden = {"ask", "get", "wait", "start", "stop", "enqueue"}
    assert not [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]


def _streaming_process() -> object:
    class _ReadyStreaming(_Process):
        dispatch_ready = True

        def __init__(self) -> None:
            super().__init__()
            self.enqueued: list[HarnessDelivery] = []

        def enqueue(self, delivery: HarnessDelivery) -> bool:
            self.enqueued.append(delivery)
            return True

        def drain_results(self) -> tuple[object, ...]:
            return ()

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

        def steer(self, delivery_id: str, *, timeout: float = 5.0) -> bool:
            del delivery_id, timeout
            return True

    return _ReadyStreaming()


def test_two_members_offering_the_same_origin_reserve_once() -> None:
    class _TwoLauncher:
        def __init__(self) -> None:
            self.processes: list[object] = []

        def start(self, _spec: HarnessLaunchSpec) -> object:
            process = _streaming_process()
            self.processes.append(process)
            return process

    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )

    launcher = _TwoLauncher()
    supervisor = ManagedHarnessRuntime(launcher, restart_backoff_seconds=0)
    try:
        assert supervisor.start(_spec("member-a"))
        assert supervisor.start(_spec("member-b"))
        _wait_for(lambda: len(launcher.processes) == 2)
        delivery = HarnessDelivery("shared", "c", "s", "member-a", "work")
        first = CellDispatchEnvelope(
            stamp=CellDispatchStamp("c.test", "member-a", "ce-1", 1, ("shared",)),
            input=delivery,
        )
        second = CellDispatchEnvelope(
            stamp=CellDispatchStamp("c.test", "member-b", "ce-1", 1, ("shared",)),
            input=delivery,
        )
        assert supervisor.dispatch(first) is True
        assert supervisor.dispatch(second) is False
        enqueued = [
            process.enqueued
            for process in launcher.processes
            if process.enqueued
        ]
        assert len(enqueued) == 1 and enqueued[0] == [delivery]
    finally:
        supervisor.stop()


def test_two_independent_runtime_actors_both_accept_the_same_origin() -> None:
    """The M-F2 boundary pin: no shared reservation owner across actors yet.

    M′ M5 relies on ONE HarnessRuntimeActor per composition.  This test
    documents that two independent actors each hold their own reservation
    table, so the same origin admitted to both executes twice — the shared
    owner a multi-actor composition must add before M′ holds there.
    """

    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )

    launchers: list[object] = []

    class _OneLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> object:
            process = _streaming_process()
            launchers.append(process)
            return process

    supervisors = [
        ManagedHarnessRuntime(_OneLauncher(), restart_backoff_seconds=0)
        for _ in range(2)
    ]
    try:
        for supervisor in supervisors:
            assert supervisor.start(_spec("member-a"))
        _wait_for(lambda: len(launchers) == 2)
        delivery = HarnessDelivery("shared", "c", "s", "member-a", "work")
        envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp("c.test", "member-a", "ce-1", 1, ("shared",)),
            input=delivery,
        )
        assert supervisors[0].dispatch(envelope) is True
        assert supervisors[1].dispatch(envelope) is True
        assert sum(len(process.enqueued) for process in launchers) == 2
    finally:
        for supervisor in supervisors:
            supervisor.stop()


def test_a_member_that_left_before_the_offer_is_refused_and_retried() -> None:
    class _StoppedLauncher:
        def __init__(self) -> None:
            self.process = _streaming_process()

        def start(self, _spec: HarnessLaunchSpec) -> object:
            return self.process

    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )

    launcher = _StoppedLauncher()
    supervisor = ManagedHarnessRuntime(launcher, restart_backoff_seconds=30.0)
    try:
        assert supervisor.start(_spec("worker"))
        _wait_for(
            lambda: supervisor._actor._records["codex:worker"].process is not None
        )
        launcher.process.running = False
        supervisor.reconcile()
        _wait_for(
            lambda: supervisor._actor._records["codex:worker"].process is None
        )
        delivery = HarnessDelivery("gone", "c", "s", "worker", "work")
        envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp("c.test", "worker", "ce-1", 1, ("gone",)),
            input=delivery,
        )
        assert supervisor.dispatch(envelope) is False
        assert launcher.process.enqueued == []
        assert "gone" not in supervisor._actor._delivery_reservations
    finally:
        supervisor.stop()


def test_a_uri_member_resolves_through_the_agent_identity_projection() -> None:
    class _ResolvingIdentity(_AgentIdentity):
        def __init__(self) -> None:
            super().__init__("entity-1")
            self.asked: list[str] = []

        def actor_name(self, actor: str) -> str | None:
            self.asked.append(actor)
            return "worker" if actor == "agent:op:node:worker" else None

    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )
    from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
    from hyprial.daemon.impl.harnesses.runtime.ports import (
        EnsureHarnessCommand,
        HarnessLaunchProjection,
    )

    class _ReadyLauncher:
        def __init__(self) -> None:
            self.process = _streaming_process()

        def start(self, _spec: HarnessLaunchSpec) -> object:
            return self.process

    launcher = _ReadyLauncher()
    identity = _ResolvingIdentity()
    actor = HarnessRuntimeActor(
        launcher, agent_identity=identity, restart_backoff_seconds=0,
    )
    facade = HarnessRuntimeFacade(actor, reply_timeout=5.0)
    try:
        assert actor.submit(
            EnsureHarnessCommand(
                "start", HarnessLaunchProjection("codex", "worker", True)
            )
        ) is PortAdmission.ACCEPTED
        _wait_for(
            lambda: actor._records.get("codex:worker") is not None
            and actor._records["codex:worker"].process is not None
        )
        delivery = HarnessDelivery("uri-1", "c", "s", "agent:op:node:worker", "work")
        envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp(
                "c.test", "agent:op:node:worker", "ce-1", 1, ("uri-1",)
            ),
            input=delivery,
        )
        assert facade.dispatch(envelope) is True
        assert identity.asked == ["agent:op:node:worker"]
        assert launcher.process.enqueued == [delivery]
    finally:
        facade.stop(2.0)


def _ready_supervisor() -> tuple[Any, Any, Any]:
    from ..support import _Launcher

    class _ReadyLauncher(_Launcher):
        def __init__(self) -> None:
            super().__init__()
            self.processes: list[object] = [None]

        def start(self, _spec: HarnessLaunchSpec) -> object:
            process = _streaming_process()
            self.processes.append(process)
            return process

    launcher = _ReadyLauncher()
    supervisor = ManagedHarnessRuntime(launcher, restart_backoff_seconds=0)
    return supervisor, launcher, None


def test_the_dispatch_entry_refuses_a_steer_envelope() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery
    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )

    supervisor, launcher, _ = _ready_supervisor()
    try:
        assert supervisor.start(_spec())
        _wait_for(lambda: len(launcher.processes) > 1 and launcher.processes[1] is not None)
        stamp = CellDispatchStamp("c.test", "worker", "ce-1", 1, ("steer-as-turn",))
        delivery = HarnessDelivery("steer-as-turn", "c", "s", "worker", "work")
        with pytest.raises(TypeError):
            CellDispatchEnvelope(stamp=stamp, input=delivery, dispatch_kind="steer")
        steer_only = CellDispatchEnvelope(
            stamp=stamp, input=None, dispatch_kind="steer"
        )
        assert supervisor.dispatch(steer_only) is False
        assert supervisor.dispatch(_envelope(delivery)) is True
        enqueued = launcher.processes[1].enqueued
        assert [d.delivery_id for d in enqueued] == ["steer-as-turn"]
    finally:
        supervisor.stop()


def test_the_steer_entry_refuses_a_turn_envelope() -> None:
    supervisor, launcher, _ = _ready_supervisor()
    try:
        assert supervisor.start(_spec())
        _wait_for(lambda: len(launcher.processes) > 1 and launcher.processes[1] is not None)
        turn = _envelope(HarnessDelivery("turn-1", "c", "s", "worker", "work"))
        assert supervisor.dispatch(turn) is True
        assert supervisor.steer(turn) is False
        assert supervisor.steer(_steer_envelope("worker", "turn-1")) is True
    finally:
        supervisor.stop()


def test_a_non_headless_stamp_is_refused_at_the_execution_edge() -> None:
    supervisor, launcher, _ = _ready_supervisor()
    try:
        assert supervisor.start(_spec())
        _wait_for(lambda: len(launcher.processes) > 1 and launcher.processes[1] is not None)
        envelope = _envelope(HarnessDelivery("forged", "c", "s", "worker", "work"))
        object.__setattr__(envelope.stamp, "source", "interactive-session")
        assert supervisor.dispatch(envelope) is False
        assert launcher.processes[1].enqueued == []
        forged_steer = _steer_envelope("worker", "forged")
        object.__setattr__(forged_steer.stamp, "source", "interactive-session")
        assert supervisor.steer(forged_steer) is False
    finally:
        supervisor.stop()


def test_an_unresolvable_uri_member_is_refused_while_a_bare_name_matches() -> None:
    class _SelectiveIdentity(_AgentIdentity):
        def __init__(self) -> None:
            super().__init__("entity-1")
            self.asked: list[str] = []

        def actor_name(self, actor: str) -> str | None:
            self.asked.append(actor)
            return "worker" if actor == "agent:op:node:worker" else None

    from hyprial.daemon.impl.api import HarnessDelivery
    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )
    from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
    from hyprial.daemon.impl.harnesses.runtime.ports import (
        EnsureHarnessCommand,
        HarnessLaunchProjection,
    )

    class _ReadyLauncher:
        def __init__(self) -> None:
            self.process = _streaming_process()

        def start(self, _spec: HarnessLaunchSpec) -> object:
            return self.process

    launcher = _ReadyLauncher()
    identity = _SelectiveIdentity()
    actor = HarnessRuntimeActor(
        launcher, agent_identity=identity, restart_backoff_seconds=0,
    )
    facade = HarnessRuntimeFacade(actor, reply_timeout=5.0)
    try:
        assert actor.submit(
            EnsureHarnessCommand(
                "start", HarnessLaunchProjection("codex", "worker", True)
            )
        ) is PortAdmission.ACCEPTED
        _wait_for(
            lambda: actor._records.get("codex:worker") is not None
            and actor._records["codex:worker"].process is not None
        )
        uri_envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp(
                "c.test", "agent:op:node:ghost", "ce-1", 1, ("uri-refused",)
            ),
            input=HarnessDelivery("uri-refused", "c", "s", "worker", "work"),
        )
        assert facade.dispatch(uri_envelope) is False
        assert launcher.process.enqueued == []
        bare_envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp("c.test", "worker", "ce-1", 1, ("bare-ok",)),
            input=HarnessDelivery("bare-ok", "c", "s", "worker", "work"),
        )
        assert facade.dispatch(bare_envelope) is True
        assert [d.delivery_id for d in launcher.process.enqueued] == ["bare-ok"]
    finally:
        facade.stop(2.0)


def test_diagnostics_never_gate_execution() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery
    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )

    supervisor, launcher, _ = _ready_supervisor()
    try:
        assert supervisor.start(_spec())
        _wait_for(lambda: len(launcher.processes) > 1 and launcher.processes[1] is not None)
        delivery = HarnessDelivery("diagnostics", "c", "s", "worker", "work")
        envelope = CellDispatchEnvelope(
            stamp=CellDispatchStamp(
                "c.someone-else", "worker", "ce-00000000000000000009-deadbeef",
                1234, ("diagnostics",),
            ),
            input=delivery,
        )
        assert supervisor.dispatch(envelope) is True
        assert [d.delivery_id for d in launcher.processes[1].enqueued] == [
            "diagnostics",
        ]
    finally:
        supervisor.stop()
