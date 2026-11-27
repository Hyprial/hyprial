"""A real daemon application whose only stand-in is the sidecar process.

Decision (service grant exposing side, 2026-11-26): the wiring tests drive
``DaemonApplication`` through its public ``handle`` path, with the real
supervisor start, the real IPC router, the real service grant handler and a
real OrgFS runtime. The sidecar is the stdio fixture with a roster, started
through ``forwarding_environment`` exactly as the daemon starts a real one.
What is observed is what reached the sidecar's wire (the fixture's receipt)
and the application's log events. Whether a pushed set admits the right
device is the end-to-end file's question, answered there with the connect
probe; here a trigger is proven by the expose it caused.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import device_key_path
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.daemon.service_grants.enforcement._support import (
    FIXTURE,
    ALICE_NODE,
    MEMBERS,
    Harness,
    roster_document,
)

KEY_UNAVAILABLE = "service.grant.key_unavailable"


class Daemon:
    """``DaemonApplication`` over the fixture, plus what the tests observe."""

    def __init__(self, root: Path, harness: Harness, *, members: tuple = MEMBERS) -> None:
        self.harness = harness
        self.receipt = root / "receipt.jsonl"
        self.roster_path = root / "roster.json"
        self.write_roster(members=members)
        self.fault_file = root / "fault.json"
        (root / "state").mkdir()
        _device_key(harness.homes[ALICE_NODE.owner])
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        environment = {
            "HYPRIAL_FORWARDING_COMMAND": json.dumps([sys.executable, str(FIXTURE)]),
            "HYPRIAL_FORWARDING_UP": json.dumps(_up(root)),
            "FAKE_FORWARD_RECEIPT": str(self.receipt),
            "FAKE_FORWARD_ROSTER": str(self.roster_path),
            "FAKE_FORWARD_FAULT": str(self.fault_file),
        }

        self.app = DaemonApplication(
            state_dir=root / "state",
            socket_path=root / "state/daemon.sock",
            node_id=ALICE_NODE.device_id,
            hyprial_home=harness.homes[ALICE_NODE.owner],
            owner=ALICE_NODE.owner,
            forwarding_environment=environment,
            **daemon_dependencies(),
        )
        self.app._inbox = object()  # type: ignore[assignment]
        self.app._harnesses = object()  # type: ignore[assignment]
        self.app._presence = object()  # type: ignore[assignment]
        self.app._runtime = harness.runtime
        self.app._orgfs_runtime = harness.runtime
        self.app._log = self.log  # type: ignore[method-assign]
        self.app._exposure_store = harness.exposures
        self.started = threading.Event()
        start = self.app._start_forwarding_supervisor

        def started() -> None:
            start()
            self.started.set()

        self.app._start_forwarding_supervisor = started  # type: ignore[method-assign]

    def arm_fault(self, op: str, mode: str, *, port: int | None = None) -> None:
        """Make the sidecar's next ``op``, on ``port`` when one is given, end as ``mode`` says.

        The fault is armed once: the sidecar removes the file when it acts on it.
        """

        fault: dict[str, Any] = {"op": op, "mode": mode, "once": True}
        if port is not None:
            fault["port"] = port
        self.fault_file.write_text(json.dumps(fault), encoding="utf-8")

    def write_roster(self, *, members: tuple = MEMBERS, without_server_key: tuple = ()) -> None:
        document = roster_document(members, without_server_key=without_server_key)
        document["tailnetId"] = self.harness.space_id
        self.roster_path.write_text(json.dumps(document), encoding="utf-8")

    def log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, component, event, fields))

    @property
    def supervisor(self) -> Any:
        return self.app._forwarding_supervisor

    def start(self) -> None:
        """Start the sidecar the way the first exposure request does, and let it settle."""

        self.app._start_forwarding_supervisor()
        assert self.started.is_set()
        self.settle()

    def settle(self, timeout: float = 10.0) -> None:
        """Wait until the admission cadence has finished every tick it was offered.

        A tick that the cadence's actor has accepted but not yet started is
        not "active", so "not active" alone is not idle: the projection has
        to come to rest, with nothing active, and stay there for a moment.
        The count of offered ticks equal to the count of finished ones is not
        the test: ticks offered while one was running are coalesced into one,
        so after a coalescing the two counts never meet again, though nothing
        is owed. Without a cadence there is nothing to wait for, which is
        itself the finding a test then makes on its own assertion.
        """

        cadence = getattr(self.app, "_service_admission_cadence", None)
        if cadence is None:
            return
        deadline = time.monotonic() + timeout
        while not _at_rest(cadence, deadline) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _at_rest(cadence, deadline), "admission cadence did not settle"

    def exposes(self, *, port: int | None = None) -> list[dict[str, Any]]:
        if not self.receipt.exists():
            return []
        commands = [json.loads(line) for line in self.receipt.read_text().splitlines()]
        return [
            command
            for command in commands
            if command["op"] == "expose" and (port is None or command["port"] == port)
        ]

    def wait_for_wire(self, op: str, *, timeout: float = 10.0) -> None:
        """Wait until a command ``op`` reached the sidecar's wire."""

        deadline = time.monotonic() + timeout
        while not self.on_wire(op) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.on_wire(op), f"no {op} reached the sidecar's wire"

    def on_wire(self, op: str) -> bool:
        return bool(self.commands(op))

    def commands(self, op: str) -> list[dict[str, Any]]:
        if not self.receipt.exists():
            return []
        commands = [json.loads(line) for line in self.receipt.read_text().splitlines()]
        return [command for command in commands if command["op"] == op]

    def wait_for_pushes(self, count: int, *, port: int | None = None, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Wait until ``count`` exposes for ``port`` reached the sidecar's wire."""

        deadline = time.monotonic() + timeout
        while len(self.exposes(port=port)) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.exposes(port=port)

    def logged(self, event: str) -> list[tuple[str, dict[str, Any]]]:
        return [(level, fields) for level, _c, name, fields in self.events if name == event]

    def wire_ops(self, port: int) -> list[str]:
        """The ``expose``/``unexpose`` commands for ``port``, in the order the sidecar got them.

        The fixture applies its commands in that order, so the last entry is
        whether the port is exposed on the sidecar now.
        """

        if not self.receipt.exists():
            return []
        commands = [json.loads(line) for line in self.receipt.read_text().splitlines()]
        return [
            command["op"]
            for command in commands
            if command["op"] in {"expose", "unexpose"} and command.get("port") == port
        ]

    def close(self) -> None:
        """Close what starting the sidecar opened, then the application's domains.

        The maintenance scheduler and the sidecar are started by this test, not
        by ``DaemonApplication`` start-up, so they are closed here in the order
        the daemon's own shutdown uses: admission cadence, scheduler, sidecar.
        """

        cadence = getattr(self.app, "_service_admission_cadence", None)
        if cadence is not None:
            cadence.close(5.0)
            self.app._service_admission_cadence = None
        if self.app._forwarding_supervisor is not None:
            self.app._forwarding_supervisor.close()
            self.app._forwarding_supervisor = None
        self.app._maintenance_scheduler.shutdown(5.0)
        self.app._agent_session_domains.close()


QUIET = 0.1


def _counts(projection: Any) -> tuple[int, ...]:
    return (projection.accepted, projection.overloaded, projection.coalesced, projection.completed, projection.failed)


def _at_rest(cadence: Any, deadline: float) -> bool:
    """Whether the cadence is idle: nothing active, and the counts still for ``QUIET``.

    A tick accepted by the actor but not started yet changes the counts when
    it starts, so a projection that has not moved for that long and shows
    nothing active has no tick left to run.
    """

    first = cadence.projection()
    if first.active:
        return False
    until = min(deadline, time.monotonic() + QUIET)
    while time.monotonic() < until:
        time.sleep(0.01)
        now = cadence.projection()
        if now.active or _counts(now) != _counts(first):
            return False
    return True


def _device_key(home: Path) -> None:
    """The key file a provisioned home has, so starting forwarding runs no binary.

    The application adopts the device record as stable only when this file is
    next to it; without one it would run the tailcat binary's ``genkey``, and
    the sidecar here is the fixture.
    """

    key_file = device_key_path(home)
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file.write_text('{"v":1,"server":{},"client":{}}', encoding="utf-8")
    key_file.chmod(0o600)


def _up(root: Path) -> dict[str, Any]:
    return {
        "v": 7,
        "requestId": "up-fixture",
        "op": "up",
        "keyFile": str(root / "device-key.json"),
        "membershipStore": str(root / "membership.json"),
        "owner": "fixture",
        "deviceId": "fixture",
        "controlPort": 7448,
        "addressFile": str(root / "address"),
        "inboundTarget": "tcp:127.0.0.1:0",
        "peerPort": 7447,
        "slots": [],
    }
