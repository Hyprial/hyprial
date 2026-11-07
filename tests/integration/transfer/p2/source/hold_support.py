"""Deterministic harness-actor barriers; no model or network resources."""

from __future__ import annotations

import threading

from hyprial.daemon.impl.harnesses.runtime import ports
from hyprial.kernel import HarnessLaunchSpec


class Identity:
    token = "source-incarnation"

    def entity_token(self, actor):
        return self.token


class Process:
    pid = None
    running = True

    def stop(self):
        self.running = False


class Launcher:
    def __init__(self, *, blocked=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.processes = []

    def start(self, spec):
        self.entered.set()
        assert self.release.wait(5), "test did not release launch barrier"
        process = Process()
        self.processes.append(process)
        return process


class Events:
    def __init__(self, actor):
        self.condition = threading.Condition()
        self.items = []
        actor.subscribe_events(self.append)

    def append(self, event):
        with self.condition:
            self.items.append(event)
            self.condition.notify_all()

    def wait(self, correlation):
        with self.condition:
            assert self.condition.wait_for(
                lambda: any(e.correlation_id == correlation for e in self.items), 5
            ), f"no actor result for {correlation}"
            return next(e for e in self.items if e.correlation_id == correlation)

    def barrier(self, actor):
        actor.submit(ports.DrainHarnessReadinessCommand("barrier"))
        self.wait("barrier")


def hold(actor, *, correlation="hold", token="source-incarnation", operation="move"):
    command = getattr(ports, "HoldStartsForTransfer", None)
    assert command is not None, "harness actor has no transfer start-admission fence"
    actor.submit(command(correlation, actor="source", expected_entity_token=token,
                         operation_id=operation, generation=1))


def ensure(actor, *, correlation="start", name="source"):
    actor.submit(ports.EnsureHarnessCommand(
        correlation, ports.HarnessLaunchProjection("fake", name, True)
    ))


def spec(name="source"):
    return HarnessLaunchSpec("fake", name, True)
