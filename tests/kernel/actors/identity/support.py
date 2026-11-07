"""Canonical registered identity and symmetric entry-point alias resolution.

Providers register on the network as ``agent:<owner>:<machine>:<actor>``;
a short actor name resolves to that identity only when the alias is known.
These tests pin both halves: send and consumption resolve a registered alias
to the same URI, while an unknown name keeps the historic bare durable key.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
from pathlib import Path


from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, permissive_admission_slot


def seed_legacy_row(inbox: InboxService, message: InboxMessage) -> None:
    """Insert an inbox row directly, as if it predated the ingress gate.

    ``receive()`` now rejects bare-sender messages (Allen B); rows like this
    one still exist in upgraded databases, so tests verifying their
    continued consumption seed through SQL, not through the gated path.
    The terminal record is written too: a real pre-gate receive committed
    both in one transaction.
    """

    from hyprial.daemon.impl.inbox import HoldReason, TerminalState

    with inbox._db:  # type: ignore[attr-defined]
        inbox._db.execute(  # type: ignore[attr-defined]
            """INSERT INTO inbox (
                   message_id, conversation_id, sender, recipient, payload, intent,
                   lifecycle, idempotency_key, created_at_ms, received_at_ms,
                   origin_node
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                message.conversation_id,
                message.sender,
                message.recipient,
                message.payload,
                message.intent,
                message.lifecycle.value,
                message.idempotency_key,
                message.created_at_ms,
                0,
                message.origin_node,
            ),
        )
        inbox._record_terminal(  # type: ignore[attr-defined]
            message,
            state=TerminalState.FETCHED,
            reason=HoldReason.ACK_RECEIVED,
            now_ms=0,
        )


class _Presence:
    def __init__(self, actors: tuple[str, ...]) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _LoopbackTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).acknowledged


def application(
    tmp_path: Path,
    *,
    actors: tuple[str, ...] = (),
    providers: tuple[dict[str, object], ...] = (),
    loopback: bool = False,
) -> DaemonApplication:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": list(providers),
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    transport = _LoopbackTransport() if loopback else MemoryDeliveryTransport()
    # Explicit permissive premise: these identity tests read agent-to-agent
    # rows through a slot with no real gate yet (review item 3).
    app._inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,
        link_admission=permissive_admission_slot(),
    )
    app.link_admission.bind(PermissiveLinkGate())
    if isinstance(transport, _LoopbackTransport):
        transport.service = app._inbox
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence(actors)
    return app


PI_WORKER = {
    "provider": "pi",
    "name": "kanban-sim",
    "headless": True,
    "args": [],
    "ownership": "managed",
}

PI_WORKER_WITH_NICKNAME = {
    "provider": "pi",
    "name": "kanban-sim",
    "nickname": "kanban",
    "headless": True,
    "args": [],
    "ownership": "managed",
}


def _registered(app: DaemonApplication, name: str) -> str:
    """Lazily register a CLI sender persona.

    resolve-or-reject (Allen b): an unregistered sender is SENDER_UNRESOLVED
    at the send boundary, so tests that exercise target semantics register
    their sender first.
    """

    if app.agents.get(name) is None:
        app.handle("agent.create", {"name": name})
    return name


def _sent_recipient(app: DaemonApplication, target: str) -> str:
    result = app.handle(
        "message.send",
        {
            "from": _registered(app, "driver"),
            "to": [target],
            "message": "hi",
            "summary": "hi",
        },
    )
    (delivery,) = result["deliveries"]
    return delivery["target"]


def _restart_application(
    app: DaemonApplication,
    tmp_path: Path,
    *,
    providers: tuple[dict[str, object], ...] = (),
) -> DaemonApplication:
    assert app._inbox is not None
    close_application(app)
    return application(tmp_path, providers=providers)


class _AutoConfirmTransport(MemoryDeliveryTransport):
    """Simulates a reachable peer and records the full delivered frames."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[InboxMessage] = []

    def deliver(self, message: InboxMessage) -> bool:
        self.frames.append(message)
        self.confirm_direct(message.message_id)
        return super().deliver(message)
