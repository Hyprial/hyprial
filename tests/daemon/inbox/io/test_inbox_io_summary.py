"""WP5-D: PAC and routine delivery carry a bounded, deterministic summary.

Design: ``docs/design/send-links-impl/wp5.md`` §6/§7.7 and
``docs/design/design-send-links-2026-10-14.md`` §7.  Every path that bypasses
``message.send`` must still satisfy the summary contract: the row a carrier may
inject has a sender-written summary of at most ``SUMMARY_MAX_BYTES`` UTF-8
bytes, derived deterministically from the text actually delivered so that an
idempotent retry of the same ``(event_id, edge)`` reproduces the same summary.

The tests build the same durable pipeline production uses -- a custody
coordinator whose transport loops back into the inbox facade -- so the row
being asserted is a real committed inbox row, not a spy's argument list.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.routines import _RoutinesMixin
from hyprial.daemon.impl.dispatch.identity import dispatch_message_id
from hyprial.daemon.impl.dispatch.remote import RemoteWorkflow
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.contracts.summary import summary_from_text
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
)
from hyprial.daemon.impl.inbox.links.local_link import LocalFirstDeliveryTransport
from hyprial.daemon.impl.pac.actors.daemon import DaemonPacNotificationSender
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import (
    PacGraphStore,
    default_database_path,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.support.pac.builders import build_allen_graph, principal_uri

SENDER = "agent:test:node-a:pac"
TARGET = "agent:test:node-a:worker"
EFFECT = "pac:test-effect"
SUMMARY = "PAC S"
ROOT = Path(__file__).resolve().parents[4]


def _harness(
    tmp_path: Path,
    *,
    deliver_user: object = None,
) -> tuple[DeliveryCustodyCoordinator, DeliveryCustodyFacade, InboxDeliveryIoAdapter]:
    """The production shape: coordinator -> local transport -> inbox facade."""

    router = CorrelatedInboxEventRouter()
    database = tmp_path / "inbox.sqlite3"
    transport = LocalFirstDeliveryTransport(
        MemoryDeliveryTransport(),
        origin_node="node-a",
        local_owner="test",
        local_node_id="node-a",
    )
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        router,
        mailbox_capacity=8,
        io_capacity=2,
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    transport.bind_receiver(facade.receive)
    adapter = InboxDeliveryIoAdapter(
        coordinator,
        router,
        resolve_target=lambda target: target,
        clock_ms=lambda: 1_000,
        completion_timeout=2.0,
        deliver_user=deliver_user,  # type: ignore[arg-type]
    )
    return coordinator, facade, adapter


@pytest.fixture
def inbox(tmp_path: Path):
    """A harness factory that always drains the coordinator after the test."""

    built: list[DeliveryCustodyCoordinator] = []

    def build(**kwargs: object):
        coordinator, facade, adapter = _harness(tmp_path, **kwargs)
        built.append(coordinator)
        return coordinator, facade, adapter

    yield build
    for coordinator in built:
        coordinator.drain(timeout=2.0)


def _rows(facade: DeliveryCustodyFacade) -> tuple[object, ...]:
    return facade.pending_all()


def _stored_summary(database: Path, message_id: str) -> object:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT summary FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def test_deliver_requires_summary(inbox) -> None:
    _coordinator, _facade, adapter = inbox()

    with pytest.raises(TypeError):
        adapter.deliver(
            effect_id=EFFECT,
            sender=SENDER,
            target=TARGET,
            conversation_id="c-1",
            text="body",
        )

    with pytest.raises(DaemonRequestError) as empty:
        adapter.deliver(
            effect_id=EFFECT,
            sender=SENDER,
            target=TARGET,
            conversation_id="c-1",
            text="body",
            summary="",
        )
    assert empty.value.code == ipc_errors.SUMMARY_REQUIRED

    with pytest.raises(DaemonRequestError) as oversized:
        adapter.deliver(
            effect_id=EFFECT,
            sender=SENDER,
            target=TARGET,
            conversation_id="c-1",
            text="body",
            summary="x" * 513,
        )
    assert oversized.value.code == ipc_errors.SUMMARY_REQUIRED
    assert oversized.value.data == {"summaryBytes": 513, "maxBytes": 512}


def test_delivered_row_carries_summary(tmp_path: Path, inbox) -> None:
    _coordinator, facade, adapter = inbox()
    message_id = adapter.message_id(EFFECT)

    delivered = adapter.deliver(
        effect_id=EFFECT,
        sender=SENDER,
        target=TARGET,
        conversation_id="c-1",
        text="body",
        summary=SUMMARY,
    )

    assert delivered.message_id == message_id
    assert _stored_summary(tmp_path / "inbox.sqlite3", message_id) == SUMMARY
    listed = _rows(facade)
    row = next(item for item in listed if item.message_id == message_id)
    assert row.summary == SUMMARY


class _RecordingFailingSender:
    """A sender that records the whole call and then fails transiently."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
        summary: str,
        expires_at_ms: int | None = None,
    ) -> str:
        self.calls.append(
            {
                "recipient": recipient,
                "text": text,
                "sender": sender,
                "conversation_id": conversation_id,
                "idempotency_key": idempotency_key,
                "summary": summary,
            }
        )
        raise RuntimeError("simulated transport failure")


def test_pac_senders_pass_a_deterministic_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store, graph_id = build_allen_graph(state)
    sender = _RecordingFailingSender()
    reactor = PacReactor(store, clock=lambda: 1_000, sender=sender)
    try:
        reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
        assert sender.calls
        planned = sender.calls[-1]
        assert planned["summary"] == summary_from_text(str(planned["text"]))

        reactor.resend_undelivered(graph_id)
        assert len(sender.calls) > 1
        resent = sender.calls[-1]
        assert resent["summary"] == planned["summary"]
        assert resent["summary"] == summary_from_text(str(resent["text"]))
    finally:
        store.close()

    class _TransparentIo:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def deliver(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=kwargs["effect_id"])

    io = _TransparentIo()
    app = SimpleNamespace(_pac_notification_io=io)
    assert (
        DaemonPacNotificationSender(app).send(
            recipient=TARGET,
            text="body",
            sender=SENDER,
            conversation_id="pac-graph",
            idempotency_key="pac-notify:e:edge",
            summary=SUMMARY,
        )
        is not None
    )
    assert io.calls[0]["summary"] == SUMMARY


class _RoutineHost(_RoutinesMixin):
    """The one attribute ``_deliver_routine_task`` needs from its owner."""

    def __init__(self, io: object) -> None:
        self._pac_notification_io = io
        self.logs: list[object] = []

    def _log(self, *args: object, **kwargs: object) -> None:
        self.logs.append((args, kwargs))


class _GrantAuthority:
    """Remote-offer staging without the graph authority's process boundary."""

    def __init__(self) -> None:
        self.staged: list[str] = []

    def ensure_remote_key(self) -> bytes:
        return b"k" * 32

    def stage_remote_offer(self, payload: str) -> None:
        self.staged.append(payload)


class _InertTransport:
    """The subscribe/put surface ``RemoteWire`` needs, with no peers."""

    def subscribe(self, key: str, callback: object) -> object:
        return SimpleNamespace(close=lambda: None)

    def put(self, key: str, payload: bytes) -> None:
        return None


def test_routine_and_remote_paths_pass_summary(tmp_path: Path, inbox) -> None:
    _coordinator, facade, adapter = inbox()

    host = _RoutineHost(adapter)
    assert host._deliver_routine_task(  # noqa: SLF001 - the delivery seam under test
        target=TARGET,
        conversation_id="routine-c1",
        text="do the routine check",
        sender=SENDER,
    )
    routine_row = next(
        item for item in _rows(facade) if item.conversation_id == "routine-c1"
    )
    assert routine_row.summary
    assert routine_row.summary == summary_from_text("do the routine check")

    state_dir = tmp_path / "remote-state"
    state_dir.mkdir()
    PacGraphStore(default_database_path(state_dir)).close()
    app = SimpleNamespace(
        owner="test",
        node_id="node-a",
        state_dir=state_dir,
        _dispatch_service_actor="agent:test:node-a:dispatch-coordinator",
        _pac_graph_authority=_GrantAuthority(),
        _log=lambda *a, **k: None,
        _pac_notification_io=adapter,
        agents=SimpleNamespace(
            get=lambda uri: SimpleNamespace(capabilities={"interactive": False})
        ),
        _resolve_send_sender=lambda uri: uri,
    )
    remote = RemoteWorkflow(app, _InertTransport())
    try:
        remote.wire.call = lambda target, method, data, **kw: {"current": True}
        text = "remote offer body"
        grant = {
            "graphId": "g-1",
            "nodeId": "work",
            "requestId": "r-1",
            "owner": "agent:test:node-a:borrowed",
            "origin": remote.authority,
            "deadlineMs": 1_000_000,
            "effectId": "remote-effect-1",
            "textDigest": hashlib.sha256(text.encode()).hexdigest(),
        }
        grant["messageId"] = dispatch_message_id(grant["effectId"])
        result = remote.handle("offer", {"grant": grant, "text": text})
        assert result["ok"] is True
    finally:
        remote.close()

    remote_row = next(
        item
        for item in _rows(facade)
        if item.message_id == dispatch_message_id("remote-effect-1")
    )
    assert remote_row.summary
    assert remote_row.summary == summary_from_text(text)


def test_user_target_keeps_the_dm_path(inbox) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def deliver_user(recipient: str, text: str, message_id: str, sender: str) -> bool:
        calls.append((recipient, text, message_id, sender))
        return True

    _coordinator, facade, adapter = inbox(deliver_user=deliver_user)

    delivered = adapter.deliver(
        effect_id=EFFECT,
        sender=SENDER,
        target="user:test",
        conversation_id="c-1",
        text="body",
        summary=SUMMARY,
    )

    assert delivered.recipient == "user:test"
    assert calls and calls[0][0] == "user:test"
    assert _rows(facade) == ()

    with pytest.raises(DaemonRequestError):
        adapter.deliver(
            effect_id="pac:other-effect",
            sender=SENDER,
            target="user:test",
            conversation_id="c-1",
            text="body",
            summary="",
        )
    assert len(calls) == 1


def test_the_shell_workflow_sender_uses_only_the_public_face() -> None:
    control = (
        ROOT
        / "src/hyprial/shell/impl/cli/commands/workflow/control.py"
    )
    tree = ast.parse(control.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [
        module for module in imported if module.startswith("hyprial.daemon.impl")
    ], imported
