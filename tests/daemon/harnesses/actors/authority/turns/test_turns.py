from __future__ import annotations

import threading
import ast
import inspect
import textwrap
from typing import get_args
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
    _handled_frozen_commands,
    _spec,
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
        assert supervisor.dispatch("worker", waiting) is False
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
        assert supervisor.dispatch("worker", ready) is True
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
        lambda: (snapshot := actor._runtime.snapshot(actor._handle)).queued
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
