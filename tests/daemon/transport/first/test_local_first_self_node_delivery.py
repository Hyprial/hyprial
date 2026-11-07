"""E2E-013 regression: minted self-node identities are local delivery objects.

#189's sender canonicalization (Allen A) mints bare CLI names into
``agent:<owner>:<this-node>:<name>`` — but ``LocalFirstDeliveryTransport``
only delivered to *registered* actors, so a reply addressed to a minted
identity was treated as remote traffic and parked in the outbox forever.
Registration governs wake/liveliness, never deliverability: a canonical URI
whose machine segment is this node is delivered into the local durable
inbox whether or not a connector claims it, and the read side canonicalizes
a bare ``--from`` to the same key.

These tests wire the REAL LocalFirstDeliveryTransport — the layer the
application-level fixtures bypass, which is exactly why the hole escaped
unit tests and only the end-to-end gate caught it.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.local_link import LocalFirstDeliveryTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, permissive_admission_slot

NODE = "node-1"
OWNER = "op"
OPS = f"agent:{OWNER}:{NODE}:ops"  # the bare CLI name "ops" resolves to this
WORKER = f"agent:{OWNER}:{NODE}:worker"


def _message(recipient: str, sender: str = "agent:o:m:actor") -> InboxMessage:
    return InboxMessage(
        message_id=f"m-{recipient.split(':')[-1]}",
        conversation_id="c-1",
        sender=sender,
        recipient=recipient,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


# -- the transport predicate, both directions ---------------------------------


def _wired() -> tuple[LocalFirstDeliveryTransport, MemoryDeliveryTransport, list[InboxMessage]]:
    inner = MemoryDeliveryTransport()
    transport = LocalFirstDeliveryTransport(
        inner, origin_node=NODE, local_node_id=NODE, local_owner=OWNER
    )
    received: list[InboxMessage] = []

    class _Result:
        acknowledged = True

    transport.bind_receiver(lambda message: received.append(message) or _Result())  # type: ignore[return-value]
    return transport, inner, received


def test_self_node_uri_delivers_locally_without_registration() -> None:
    """The mutation this kills: local delivery gated on registration."""

    transport, inner, received = _wired()

    assert transport.deliver(_message(OPS)) is True
    assert [m.recipient for m in received] == [OPS]
    assert inner.delivered == []  # nothing escaped to the network leg


def test_foreign_node_uri_still_uses_the_network_leg() -> None:
    transport, inner, received = _wired()

    transport.deliver(_message("agent:op:far-away:worker"))

    assert received == []
    assert inner.delivered != []  # went to the inner (network) transport


def test_malformed_and_old_node_uris_are_not_local() -> None:
    """Only a well-formed four-segment self-node URI qualifies: a renamed
    node's old URIs and malformed shapes keep remote semantics."""

    transport, inner, received = _wired()

    for recipient in (
        "agent:op:old-node:ops",  # renamed away: not this node anymore
        "agent:contract:node-1:ghost",  # foreign owner on this node: theirs, not ours
        "agent:op:node-1",  # two segments
        "ops",  # bare
    ):
        transport.deliver(_message(recipient))

    assert received == []
    assert len(inner.delivered) == 4


def test_is_online_mirrors_the_same_locality_rule() -> None:
    transport, _inner, _received = _wired()

    assert transport.is_online(OPS) is True  # durable inbox always accepts
    assert transport.is_online("agent:op:far-away:worker") is False
    assert transport.is_online("agent:contract:node-1:ghost") is False


def test_registered_actors_stay_local_as_before() -> None:
    transport, inner, received = _wired()
    registration = transport.register_actor(WORKER)
    try:
        assert transport.deliver(_message(WORKER)) is True
        assert [m.recipient for m in received] == [WORKER]
        assert inner.delivered == []
    finally:
        registration.close()


# -- the full roundtrip, wired like production ---------------------------------


def _application(tmp_path: Path) -> tuple[DaemonApplication, InboxService, MemoryDeliveryTransport]:
    """The production wiring the unit fixtures bypass: inbox submissions
    flow through a real LocalFirstDeliveryTransport."""

    inner = MemoryDeliveryTransport()
    local_first = LocalFirstDeliveryTransport(
        inner, origin_node=NODE, local_node_id=NODE, local_owner=OWNER
    )
    # Explicit permissive premise: ops→worker and worker→ops are
    # agent-to-agent rows through a slot with no real gate yet (item 3).
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        local_first,
        link_admission=permissive_admission_slot(),
    )
    local_first.bind_receiver(inbox.receive)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app.link_admission.bind(PermissiveLinkGate())
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    # resolve-or-reject (Allen b): the CLI sender persona is registered,
    # so its canonical URI is registry-backed, never minted.
    app.handle("agent.create", {"name": "ops"})
    app.handle("agent.create", {"name": "worker"})
    return app, inbox, inner


def test_bare_cli_sender_roundtrips_through_the_registered_identity(
    tmp_path: Path,
) -> None:
    """The E2E-013 shape: bare CLI send → worker reply → CLI read.

    No connector ever runs for the registered sender persona; the reply
    must still land in the LOCAL durable inbox (never the outbox) and be
    readable by the same bare --from spelling the operator typed.
    """

    app, inbox, inner = _application(tmp_path)
    try:
        sent = app.handle(
            "message.send", {"from": "ops", "to": [WORKER], "summary": "do it", "message": "do it"}
        )
        # The send itself also lands locally: WORKER names this node.
        assert sent["deliveries"][0]["queued"] is False
        assert inbox.outbox_count() == 0
        assert inner.delivered == []  # nothing touched the network leg

        # The worker (unfenced here — the fence is orthogonal) reads and replies.
        pending = app.handle("message.pending.list", {"from": WORKER})
        assert [m["messageId"] for m in pending["messages"]] == [sent["messageId"]]
        replied = app.handle(
            "message.reply",
            {"from": WORKER, "messageId": sent["messageId"], "summary": "done", "message": "done"},
        )
        assert replied["replied"] is True

        # The reply waits under the minted identity, readable via the bare name.
        assert inbox.outbox_count() == 0
        assert inner.delivered == []
        replies = app.handle("message.pending.list", {"from": "ops"})
        assert [m["messageId"] for m in replies["messages"]] == [
            replied["replyMessageId"]
        ]
        assert replies["messages"][0]["from"] == WORKER
    finally:
        inbox.close()


def test_reply_row_sender_passes_the_ingress_gate(tmp_path: Path) -> None:
    """B-layer consistency: the reply's sender is the worker's canonical
    URI — a four-segment identity the ingress gate admits, never rejects."""

    app, inbox, _inner = _application(tmp_path)
    try:
        sent = app.handle(
            "message.send", {"from": "ops", "to": [WORKER], "summary": "do it", "message": "do it"}
        )
        app.handle(
            "message.reply",
            {"from": WORKER, "messageId": sent["messageId"], "summary": "done", "message": "done"},
        )
        replies = app.handle("message.pending.list", {"from": "ops"})
        # If ingress had rejected the reply, no row would exist at all.
        assert len(replies["messages"]) == 1
    finally:
        inbox.close()
