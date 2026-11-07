"""T15/T16: ``message.settle_hint`` is read-only and defers to the LinkGate.

A carrier asks at turn end whether the daemon still needs the message kept.
The answer never mutates inbox state, and once a link gate is bound the gate
owns the reclaimed-residue verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from tests.support.links.gate import PermissiveLinkGate

from ..support import (
    FakeLinkGate,
    _close_application_domains as _close_application_domains,
    durable_application,
    register_actor,
)

READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"


def _seed(app, message_id: str = "x", *, sender: str = DRIVER, recipient: str = READER) -> None:  # type: ignore[no-untyped-def]
    assert app._inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id=f"c-{message_id}",
            sender=sender,
            recipient=recipient,
            payload=json.dumps({"message": "B"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            summary="S",
        )
    ).accepted


def _hint(app, message_id: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return app.handle(
        "message.settle_hint",
        {"actor": "reader", "sessionRef": "session-1", "messageId": message_id},
    )


def _snapshot(app):  # type: ignore[no-untyped-def]
    return tuple(
        (row["message_id"], row["fetched_at_ms"], row["consumed"])
        for row in app._inbox._db.execute(
            "SELECT message_id, fetched_at_ms, consumed FROM inbox ORDER BY message_id"
        ).fetchall()
    )


def test_settle_hint_is_keep_while_pending_and_ack_once_settled(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app)
    before = _snapshot(app)

    assert _hint(app, "x") == {"ok": True, "messageId": "x", "hint": "keep", "pending": True}
    assert _snapshot(app) == before

    assert app.handle(
        "message.ack", {"actor": "reader", "sessionRef": "session-1", "messageId": "x"}
    )["acknowledged"] is True
    assert _hint(app, "x") == {"ok": True, "messageId": "x", "hint": "ack", "pending": False}


def test_settle_hint_acks_after_a_successful_reply(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    # B2: an agent reply crosses the send-boundary link gate, and an unbound
    # slot holds it (ruling 54).  This scenario is not about links, so it binds
    # a permissive gate explicitly (wp2.md section 4.9 option a), once.
    app.link_admission.bind(PermissiveLinkGate())
    register_actor(app, "reader")
    _seed(app)

    reply = app.handle(
        "message.reply",
        {
            "actor": "reader",
            "sessionRef": "session-1",
            "messageId": "x",
            "message": "ok",
            "summary": "ok",
        },
    )

    assert reply["replied"] is True
    assert _hint(app, "x")["hint"] == "ack"


def test_settle_hint_acks_unknown_and_foreign_messages(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, "theirs", sender=READER, recipient=DRIVER)
    before = _snapshot(app)

    assert _hint(app, "no-such-message") == {
        "ok": True,
        "messageId": "no-such-message",
        "hint": "ack",
        "pending": False,
    }
    assert _hint(app, "theirs") == {
        "ok": True,
        "messageId": "theirs",
        "hint": "ack",
        "pending": False,
    }
    assert _snapshot(app) == before


def test_settle_hint_defers_a_pending_verdict_to_the_gate(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(hint="ack")
    app.link_admission.bind(gate)
    _seed(app)

    assert _hint(app, "x")["hint"] == "ack"
    assert gate.hints == ["x"]

    # A settled or unknown message never reaches the gate.
    assert _hint(app, "unknown")["hint"] == "ack"
    assert gate.hints == ["x"]


def test_settle_hint_treats_an_invalid_gate_answer_as_keep(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(hint="banana")
    app.link_admission.bind(gate)
    _seed(app)

    result = _hint(app, "x")

    assert result == {"ok": True, "messageId": "x", "hint": "keep", "pending": True}


# --- R4: owner onBehalfOf, and someone else's message ----------------------


def test_owner_on_behalf_of_asks_about_the_named_consumers_message(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app)
    before = _snapshot(app)

    result = app.handle(
        "message.settle_hint",
        {"actor": "user:op", "onBehalfOf": "reader", "messageId": "x"},
        _trusted_message_origin="ipc",
    )

    assert result == {"ok": True, "messageId": "x", "hint": "keep", "pending": True}
    assert _snapshot(app) == before


def test_owner_on_behalf_of_cannot_ask_about_someone_elses_message(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, "theirs", sender=READER, recipient=DRIVER)

    result = app.handle(
        "message.settle_hint",
        {"actor": "user:op", "onBehalfOf": "reader", "messageId": "theirs"},
        _trusted_message_origin="ipc",
    )

    assert result == {
        "ok": True,
        "messageId": "theirs",
        "hint": "ack",
        "pending": False,
    }
