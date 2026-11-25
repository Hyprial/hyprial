"""Wire option B, sender stamping site: the network transport decorator.

The envelope is written where a message leaves this node, never into the
stored row: the outbox payload, the sender copy and the submission digest
(plan §1.1) stay as they are.  Direct delivery and custody transfer are
stamped; notices and progress events keep their own payload schemas and pass
through untouched.  The seam guard pins that every frame-① network transport
the daemon composes is wrapped.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
    ZenohDeliveryTransport,
    stamped_network_transport,
)
from hyprial.daemon.impl.inbox.envelope import EnvelopeStampingTransport, stamp_payload
from hyprial.kernel import mint_thread_id

THREAD = mint_thread_id(b"payload-transport-thread")
SOURCE_ROOT = Path(__file__).resolve().parents[5] / "src" / "hyprial"
NETWORK_TRANSPORT = "ZenohDeliveryTransport"
DECORATOR = "EnvelopeStampingTransport"
FACTORY = "stamped_network_transport"
WIRING = SOURCE_ROOT / "daemon" / "impl" / "application" / "wiring" / "transport.py"


def _message(**overrides: object) -> InboxMessage:
    fields: dict[str, object] = {
        "message_id": "m-transport-1",
        "conversation_id": "c-transport",
        "sender": "agent:op:node-1:driver",
        "recipient": "agent:op:node-2:reader",
        "payload": b'{"message":"hi"}',
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1_700_000_000_000,
    }
    fields.update(overrides)
    return InboxMessage(**fields).with_envelope(thread_id=THREAD)  # type: ignore[arg-type]


class _Recording(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[InboxMessage] = []
        self.acks: list[str] = []

    def deliver(self, message: InboxMessage) -> bool:
        self.messages.append(message)
        return True

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        self.messages.append(message)
        return True

    def ack_received(self, message: InboxMessage) -> bool:
        self.acks.append(message.message_id)
        return True


def test_direct_delivery_and_custody_transfer_carry_the_envelope() -> None:
    inner = _Recording()
    transport = EnvelopeStampingTransport(inner)
    message = _message()
    assert transport.deliver(message)
    assert transport.transfer_custody("node-3", message)
    assert [sent.payload for sent in inner.messages] == [stamp_payload(message).payload] * 2
    assert json.loads(inner.messages[0].payload)["envelope"]["threadId"] == THREAD


def test_notices_and_progress_events_pass_through_untouched() -> None:
    inner = _Recording()
    transport = EnvelopeStampingTransport(inner)
    notice = _message(sender="system:hyprial:node-1", intent="system")
    progress = _message(intent="progress")
    assert transport.deliver_notice("node-2", notice)
    assert transport.deliver_progress("node-2", progress)
    assert inner.notices == [("node-2", notice)]
    assert inner.progress_events == [("node-2", progress)]


def test_every_other_question_is_answered_by_the_inner_transport() -> None:
    inner = _Recording()
    inner.set_online("agent:op:node-2:reader", True)
    inner.advertise_mailbox("node-3")
    transport = EnvelopeStampingTransport(inner)
    assert transport.is_online("agent:op:node-2:reader")
    assert not transport.is_online("agent:op:node-9:nobody")
    assert transport.online_mailboxes() == ("node-3",)
    assert transport.ack_received(_message())
    assert inner.acks == ["m-transport-1"]
    assert not EnvelopeStampingTransport(MemoryDeliveryTransport()).ack_received(_message())


def test_the_network_transport_factory_wraps_the_frame_transport() -> None:
    transport = stamped_network_transport(object(), object(), origin_node="node-1")  # type: ignore[arg-type]
    assert isinstance(transport, EnvelopeStampingTransport)
    assert isinstance(transport._inner, ZenohDeliveryTransport)
    assert transport._inner._origin_node == "node-1"


def test_the_daemon_wiring_builds_its_network_transport_through_the_factory() -> None:
    tree = ast.parse(WIRING.read_text(encoding="utf-8"))
    called = {_called(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert FACTORY in called
    assert NETWORK_TRANSPORT not in called


def _unwrapped_network_transports(source: str) -> list[int]:
    tree = ast.parse(source)
    wrapped: set[int] = set()
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called(node) == DECORATOR:
            wrapped.update(id(argument) for argument in node.args)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _called(node) == NETWORK_TRANSPORT
            and id(node) not in wrapped
        ):
            found.append(node.lineno)
    return found


def _called(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_every_composed_network_transport_is_wrapped() -> None:
    constructions: dict[str, list[int]] = {}
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.name == "zenoh_link.py":
            continue
        source = path.read_text(encoding="utf-8")
        if NETWORK_TRANSPORT + "(" not in source:
            continue
        constructions[str(path.relative_to(SOURCE_ROOT))] = _unwrapped_network_transports(
            source
        )
    assert constructions, "the daemon composes no network delivery transport"
    assert {path: lines for path, lines in constructions.items() if lines} == {}


def test_the_seam_guard_rejects_a_bare_network_transport() -> None:
    bare = "network = ZenohDeliveryTransport(transport, presence)\n"
    wrapped = "network = EnvelopeStampingTransport(ZenohDeliveryTransport(t, p))\n"
    assert _unwrapped_network_transports(bare) == [1]
    assert _unwrapped_network_transports(wrapped) == []
