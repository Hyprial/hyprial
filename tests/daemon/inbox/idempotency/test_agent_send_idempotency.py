"""Regression: an agent-target message.send must be idempotent under retry.

The MCP StatelessDaemonProxy replays a mutation with the same request id after
an "accepted, then the socket closed" ambiguity, expecting the daemon to
deduplicate.  User and route targets already derive an idempotent message id
from the operation; agent targets used to mint a fresh uuid4 with no
idempotency key, so a replay enqueued a *second distinct probe*.  The delivery
pump dispatched both and the auto-reply path answered each — the field-observed
"pi replies twice to the same probe" (two replies with distinct idempotency
keys the requester's inbox could not collapse, surfacing as ps deliveryIds
ending :0: and :1:).
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

from hyprial.daemon.impl import (
    DaemonEventBridge,
    DesiredStateStore,
    HarnessResult,
    HarnessResultStatus,
    ManagedHarnessRuntime,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import canonical_agent_uri
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, permissive_admission_slot

NODE = "resident-probe-node"
PI_NAME = "resident-pi-worker"
PI_URI = canonical_agent_uri("allen", NODE, PI_NAME)
# Registered CLI sender persona (Allen b: resolve-or-reject — no minting).
DRIVER = "probe-driver"
DRIVER_URI = canonical_agent_uri("allen", NODE, DRIVER)


class LoopbackTransport:
    """Single-node loopback so submit() -> deliver() -> receive() dedups."""

    def __init__(self) -> None:
        self.service: InboxService | None = None
        self.online: set[str] = set()

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return recipient in self.online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _inbox(tmp_path: Path) -> InboxService:
    transport = LoopbackTransport()
    # Explicit permissive premise: this test drives an agent→agent probe
    # through a slot with no real gate yet (review item 3).
    service = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,
        link_admission=permissive_admission_slot(),
    )
    transport.bind(service)
    # The bare CLI-style sender canonicalizes at the daemon send boundary
    # (Allen A), so the node's own minted four-segment identity is the
    # address replies land on — it must be "online" for the loopback.
    transport.online.update({NODE, PI_URI, DRIVER_URI})
    return service


def _application(tmp_path: Path, inbox: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner="allen",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox
    # The application surfaces read their own slot for pending.list/wait; bind
    # the same explicit permissive premise so no test relies on the fail-closed
    # default (review item 3).
    app.link_admission.bind(PermissiveLinkGate())
    app._presence = object()
    app._harnesses = object()
    app._adapters = None
    return app


def _send(app: DaemonApplication, *, idempotency_key: str) -> dict:
    if app.agents.get(DRIVER) is None:
        app.handle("agent.create", {"name": DRIVER})
    return app.handle(
        "message.send",
        {
            "actor": DRIVER,
            "to": [PI_URI],
            "message": "probe: reply RESIDENT_PI_OK",
            "summary": "probe: reply RESIDENT_PI_OK",
            "conversationId": "conv-fixed",
            "idempotencyKey": idempotency_key,
        },
    )


def test_retried_agent_send_lands_a_single_probe(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _application(tmp_path, inbox)

    first = _send(app, idempotency_key="req-abc")
    replay = _send(app, idempotency_key="req-abc")

    probes = inbox.pending_messages(PI_URI)
    assert len(probes) == 1, "a replayed send must not enqueue a second probe"
    # Both calls resolve to the same durable identity so the caller sees one send.
    assert first["messageId"] == replay["messageId"] == probes[0].message_id


def test_distinct_operations_are_not_collapsed(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _application(tmp_path, inbox)

    _send(app, idempotency_key="req-1")
    _send(app, idempotency_key="req-2")

    assert len(inbox.pending_messages(PI_URI)) == 2


class _FakePi:
    """Minimal streaming provider that completes each delivery exactly once."""

    def __init__(self) -> None:
        self.running = True
        self.pid: int | None = None
        self._known: set[str] = set()
        self._results: list[HarnessResult] = []

    def enqueue(self, delivery) -> bool:  # type: ignore[no-untyped-def]
        if delivery.delivery_id in self._known:
            return False
        self._known.add(delivery.delivery_id)
        self._results.append(
            HarnessResult(
                delivery.delivery_id,
                delivery.recipient,
                HarnessResultStatus.COMPLETED,
                output="RESIDENT_PI_OK",
            )
        )
        return True

    def drain_results(self) -> tuple[HarnessResult, ...]:
        drained = tuple(self._results)
        self._results.clear()
        self._known.difference_update(item.delivery_id for item in drained)
        return drained

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        return delivery_id in self._known

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def __init__(self, process: _FakePi) -> None:
        self.process = process

    def start(self, spec):  # type: ignore[no-untyped-def]
        return self.process


class _FakeTransport:
    def close(self) -> None:
        pass


def _write_desired_state(path: Path) -> None:
    Path(f"{path}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "pi",
                        "name": PI_NAME,
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )


def test_retried_send_lands_one_probe_and_never_auto_replies(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    app = _application(tmp_path, inbox)

    # A single logical send that the proxy replayed once (same operation id).
    _send(app, idempotency_key="req-xyz")
    _send(app, idempotency_key="req-xyz")

    desired = tmp_path / "desired-state.json"
    _write_desired_state(desired)
    pi = _FakePi()
    desired_state = DesiredStateStore(desired)
    harnesses = ManagedHarnessRuntime(_Launcher(pi))
    harnesses.restore(desired_state.load().harnesses)
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "rt",
        node_id=NODE,
        desired_state=desired_state,
        transport=_FakeTransport(),
        inbox=inbox,
        harnesses=harnesses,
        harness_actor_uri=lambda name: canonical_agent_uri("allen", NODE, name),
    )
    runtime.start()
    try:
        runtime.on_timer()  # dispatch pending probe(s) to pi
        runtime.on_timer()  # drain pi result(s) -> settle per hint, never reply
        # WP5-A: the headless carrier no longer auto-replies (design §8.1).
        # The replay landed one probe; its completed turn keeps the row (no
        # gate is bound) and no reply row ever appears for the driver.
        probes = inbox.pending_messages(PI_URI)
        driver_rows = inbox.pending_messages(DRIVER_URI)
        acknowledged = inbox.is_acknowledged(probes[0].message_id) if probes else True
    finally:
        runtime.stop()

    assert len(probes) == 1, "one replayed send must land exactly one probe"
    assert acknowledged is False, "the kept request must stay pending"
    assert driver_rows == (), "a completed headless turn must not auto-reply"
