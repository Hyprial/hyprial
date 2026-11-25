"""A stored origin never fails a batch read; the batch publishes the current shape.

Allen, 2026-11-19: 「同意，来源信息里有不认识的字段时，把那些字段丢掉」.  Rows
stored by an earlier version carry origin fields the current closed schema does
not declare.  The read keeps every field the schema declares with a valid value,
drops the rest, and never rewrites storage.  A row in the current shape is
published byte for byte.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from tests.daemon.delivery.origin.test_inbound_origin import _app, _round_trip  # type: ignore[import-not-found]

READER = {"from": "reader", "sessionRef": "session-reader"}
DRIVER = "agent:op:node-1:driver"
NEW = {
    "chatKind": "dm",
    "provider": "lark",
    "via": "session",
    "sender": {"kind": "user", "platformId": "ou_y", "displayName": "B"},
}
OLD_WITHOUT_SENDER = {"chatType": "p2p", "provider": "lark", "via": "session"}
OLD_WITH_SENDER = {
    "chatType": "group",
    "provider": "lark",
    "via": "session",
    "platformCreatedAt": "2026-10-26T07:33:20.000Z",
    "sender": {
        "kind": "user", "platformId": "ou_x", "owner": "alice",
        "standing": "verified", "source": "binding", "displayName": "A",
    },
}


def _put(app: DaemonApplication, message_id: str, origin: object, created_at_ms: int) -> bytes:
    payload = json.dumps(
        {"message": f"body-{message_id}", "summary": f"s-{message_id}", "topic": None, "origin": origin}
    ).encode()
    app._inbox.receive(InboxMessage(
        message_id=message_id, conversation_id=f"c-{message_id}", sender=DRIVER,
        recipient=app.agents.require("reader").uri, payload=payload, intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE, created_at_ms=created_at_ms,
    ))
    return payload


def _stored(app: DaemonApplication, message_id: str) -> bytes:
    row = app._inbox._db.execute(  # type: ignore[attr-defined]
        "SELECT payload FROM inbox WHERE message_id = ?", (message_id,)
    ).fetchone()
    return bytes(row[0])


def _batch(tmp_path: Path, *rows: tuple[str, object]) -> tuple[dict[str, dict], str, dict[str, bytes]]:
    app = _app(tmp_path)
    try:
        written = {
            message_id: _put(app, message_id, origin, index)
            for index, (message_id, origin) in enumerate(rows, start=1)
        }
        result = app.handle("message.pending.batch", READER)
        stored = {message_id: _stored(app, message_id) for message_id in written}
        assert stored == written
    finally:
        app._inbox.close()
        app.agents.close()
    origins = {item["messageId"]: item["origin"] for item in result["items"]}
    return origins, result["delivery"]["text"], stored


def test_old_and_new_rows_are_published_together(tmp_path: Path) -> None:
    origins, text, _stored_rows = _batch(
        tmp_path, ("m-old1", OLD_WITHOUT_SENDER), ("m-old2", OLD_WITH_SENDER), ("m-new", NEW),
    )

    assert origins["m-old1"] == {"provider": "lark", "via": "session"}
    assert origins["m-old2"] == {
        "provider": "lark",
        "via": "session",
        "platformCreatedAt": "2026-10-26T07:33:20.000Z",
        "sender": {"kind": "user", "platformId": "ou_x", "displayName": "A"},
    }
    assert json.dumps(origins["m-new"]) == json.dumps(NEW)
    assert "sender=A (via agent:op:node-1:driver, account ou_x)" in text
    assert "chatType" not in text and "standing" not in text and "owner=" not in text


def test_an_origin_that_is_not_an_object_reads_as_unrecorded(tmp_path: Path) -> None:
    origins, _text, _stored_rows = _batch(
        tmp_path, ("m-list", ["chatType", "p2p"]), ("m-text", "lark"), ("m-new", NEW),
    )

    assert origins["m-list"] == {"via": "daemon"}
    assert origins["m-text"] == {"via": "daemon"}
    assert origins["m-new"] == NEW


def test_unusable_known_fields_are_dropped_not_fatal(tmp_path: Path) -> None:
    origins, _text, _stored_rows = _batch(
        tmp_path,
        ("m-no-account", {"via": "session", "sender": {"displayName": "X", "kind": "user"}}),
        ("m-bad-types", {"via": "session", "provider": 7, "chatKind": "p2p",
                         "platformCreatedAt": 1793000000000, "sender": "ou_x"}),
        ("m-only-unknown", {"chatType": "p2p", "standing": "verified"}),
        ("m-list-kind", {"via": "session", "chatKind": ["dm"], "sender": {"platformId": ["ou"]}}),
    )

    assert origins["m-no-account"] == {"via": "session"}
    assert origins["m-bad-types"] == {"via": "session"}
    assert origins["m-only-unknown"] == {"via": "daemon"}
    assert origins["m-list-kind"] == {"via": "session"}


def test_a_mechanism_reply_origin_is_published(tmp_path: Path) -> None:
    mechanism = {"via": "daemon", "mechanism": "turn-hook", "onBehalfOf": DRIVER}
    origins, _text, _stored_rows = _batch(tmp_path, ("m-hook", mechanism))

    assert origins["m-hook"] == mechanism


def test_write_time_normalisation_is_unchanged(tmp_path: Path) -> None:
    row, _body = _round_trip(
        _app(tmp_path),
        chatKind="dm",
        chatType="p2p",
        messageId="om_1",
        unknownField=1,
        sender={"kind": "user", "platformId": "ou_x", "displayName": "A", "standing": "verified"},
    )

    assert json.dumps(row["origin"]) == json.dumps({
        "chatKind": "dm",
        "sender": {"kind": "user", "platformId": "ou_x", "displayName": "A"},
        "provider": "lark",
        "platformMessageId": "om_1",
        "via": "session",
    })


def test_write_time_ignores_an_unhashable_chat_kind(tmp_path: Path) -> None:
    row, _body = _round_trip(
        _app(tmp_path), chatKind=["dm"], sender={"platformId": "ou_x"},
    )

    assert row["origin"] == {
        "sender": {"kind": None, "platformId": "ou_x", "displayName": None},
        "provider": "lark",
        "via": "session",
    }
