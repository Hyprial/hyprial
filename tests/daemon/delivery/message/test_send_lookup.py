"""Resolve lost send receipts using only the sender's durable message copy."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import DaemonRequestError
from tests.daemon.delivery.message.test_message_history import (
    CONVERSATION, OTHER_SESSION, SESSION,
    _remote_delivery, harness as harness,
)
from tests.support.actor_hosts import new_actor_host

KEY = "browser-original-operation"
TEXT = "original frozen draft"


def _send(harness):
    return harness.app.handle("message.send", {
        "from": harness.actor, "sessionRef": SESSION, "to": [harness.reader],
        "message": TEXT, "summary": TEXT, "conversationId": CONVERSATION,
        "idempotencyKey": KEY,
    }, _trusted_message_origin="ipc")


def _lookup(harness, **overrides):
    return harness.app.handle("message.send.lookup", {
        "from": harness.actor, "sessionRef": SESSION, "target": harness.reader,
        "text": TEXT, "conversationId": CONVERSATION, "idempotencyKey": KEY,
        **overrides,
    }, _trusted_message_origin="ipc")


def test_lookup_is_http_exposed_without_migrating_the_message_family():
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES

    binding = METHOD_REGISTRY.get("message.send.lookup")
    assert binding is not None
    assert binding.spec.mutates is False
    assert binding.spec.retry == "safe"
    assert binding.spec.http_exposed is True
    assert binding.spec.effective_http_binding.identity.mode == "local-owner-user-selector"
    assert "message" not in MIGRATED_FAMILIES


@pytest.mark.parametrize("override", [
    {"target": ""}, {"text": 1}, {"unexpected": "field"},
])
def test_lookup_schema_refuses_invalid_fields(harness, override):
    with pytest.raises(DaemonRequestError) as caught:
        _lookup(harness, **override)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_lost_response_resolves_after_delivery_and_store_restart(harness, tmp_path):
    transport = _remote_delivery(harness, tmp_path)
    original = _send(harness)
    assert harness.inbox.outbox_count() == 0
    harness.inbox.close()
    harness.inbox = InboxService(harness.database, transport)
    harness.app._inbox = harness.inbox

    for _ in range(2):
        assert _lookup(harness) == {
            "status": "accepted", "commandId": KEY,
            "conversationId": CONVERSATION, "messageId": original["messageId"],
        }
    assert transport.deliveries == 1


def test_missing_is_unknown_then_committed_send_becomes_accepted(harness):
    assert _lookup(harness) == {"status": "unknown", "reason": "not-found"}
    harness.transport.offline.add(harness.reader)
    original = _send(harness)
    assert original["deliveries"][0]["queued"] is True
    assert _lookup(harness)["messageId"] == original["messageId"]
    assert harness.inbox.outbox_count() == 1


def test_unavailable_database_is_unknown(harness, monkeypatch):
    def unavailable(**selectors):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(harness.inbox, "lookup_send", unavailable)
    assert _lookup(harness) == {"status": "unknown", "reason": "unavailable"}


@pytest.mark.parametrize("override", [
    lambda harness: {"idempotencyKey": "other"},
    lambda harness: {"conversationId": "other"},
    lambda harness: {"target": harness.other},
    lambda harness: {"text": TEXT + " edited"},
    lambda harness: {"from": harness.other, "sessionRef": OTHER_SESSION},
], ids=[f"override{index}" for index in range(5)])
def test_lookup_never_confuses_another_request_with_the_original(harness, override):
    _send(harness)
    result = _lookup(harness, **override(harness))
    assert result["status"] == "unknown"
    assert "messageId" not in result


def test_lookup_requires_authenticated_identity_even_in_observe_mode(harness):
    _send(harness)
    harness.app._sender_auth_enforced = False
    with pytest.raises(DaemonRequestError) as caught:
        harness.app.handle("message.send.lookup", {
            "from": harness.actor, "target": harness.reader, "text": TEXT,
            "conversationId": CONVERSATION, "idempotencyKey": KEY,
        }, _trusted_message_origin="ipc")
    assert caught.value.code == "SENDER_NOT_AUTHENTICATED"
    with pytest.raises(DaemonRequestError):
        _lookup(harness, sessionRef=OTHER_SESSION)


@pytest.mark.parametrize("payload", [b"broken", b"[]", b'{"message": 2}'])
def test_corrupt_durable_copy_stays_unknown(harness, payload):
    original = _send(harness)
    with closing(sqlite3.connect(harness.database)) as db, db:
        db.execute("UPDATE sender_copies SET payload=? WHERE message_id=?",
                   (payload, original["messageId"]))
    assert _lookup(harness)["status"] == "unknown"


def test_lookup_reads_committed_copy_while_delivery_and_receipt_are_pending(harness):
    entered, release = threading.Event(), threading.Event()
    original_service = harness.inbox
    transport = harness.transport

    def blocked_delivery(message):
        entered.set()
        assert release.wait(5)
        return False

    transport.deliver = blocked_delivery

    class Events:
        def publish(self, event):
            pass

    coordinator = DeliveryCustodyCoordinator(
        harness.database, transport, Events(),
        actor_host=new_actor_host(),
    )
    facade = DeliveryCustodyFacade(coordinator, harness.database, timeout=0.1)
    harness.app._inbox = facade
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            send = pool.submit(_send, harness)
            assert entered.wait(3)
            result = _lookup(harness)
            assert result["status"] == "accepted"
            with closing(sqlite3.connect(harness.database)) as db:
                assert db.execute("SELECT COUNT(*) FROM actor_submission_receipts").fetchone()[0] == 0
            with pytest.raises(InboxSubmissionOutcomeUnknown):
                send.result(timeout=3)
            assert _lookup(harness) == result
    finally:
        release.set()
        assert coordinator.drain(3).complete
        harness.app._inbox = original_service


def test_uncommitted_copy_is_not_accepted(harness):
    harness.transport.offline.add(harness.reader)
    original = _send(harness)
    with closing(sqlite3.connect(harness.database)) as writer:
        writer.execute("UPDATE sender_copies SET payload=? WHERE message_id=?",
                       (json.dumps({"message": "uncommitted"}).encode(), original["messageId"]))
        assert _lookup(harness)["status"] == "accepted"
        assert _lookup(harness, text="uncommitted")["status"] == "unknown"
        writer.rollback()


def test_key_lookup_accepts_unique_durable_copy_using_only_operation_key(harness):
    original = _send(harness)
    result = harness.app._ipc_message_send_key_lookup(
        "message.send.key_lookup",
        {"from": harness.actor, "sessionRef": SESSION, "operationKey": KEY},
        _trusted_message_origin="ipc",
    )
    assert result == {
        "status": "accepted", "commandId": KEY,
        "conversationId": CONVERSATION, "messageId": original["messageId"],
    }


@pytest.mark.parametrize("column,value", [
    ("intent", lambda harness: "event"), ("lifecycle", lambda harness: "transient"),
    ("recipient", lambda harness: harness.other),
    ("idempotency_key", lambda harness: f"{KEY}:{harness.reader}:1"),
    ("payload", lambda harness: b'{"message":""}'),
], ids=["intent", "lifecycle", "recipient", "idempotency_key", "payload"])
def test_key_lookup_rejects_invalid_single_candidate(harness, column, value):
    original = _send(harness)
    with closing(sqlite3.connect(harness.database)) as db, db:
        db.execute(f"UPDATE sender_copies SET {column}=? WHERE message_id=?",
                   (value(harness), original["messageId"]))
    assert harness.inbox.key_lookup_send(sender=harness.actor, operation_key=KEY) == {
        "status": "unknown", "reason": "invalid-record",
    }


def test_key_lookup_counts_bad_candidate_before_validating_lifecycle(harness):
    original = _send(harness)
    with closing(sqlite3.connect(harness.database)) as db, db:
        db.execute(
            """INSERT INTO sender_copies
               (message_id, conversation_id, sender, recipient, payload, intent,
                lifecycle, idempotency_key, created_at_ms, message_expires_at_ms)
               SELECT ?, conversation_id, sender, recipient, payload, intent,
                      'transient', idempotency_key, created_at_ms, message_expires_at_ms
                 FROM sender_copies WHERE message_id=?""",
            ("other-transient-copy", original["messageId"]),
        )
    assert harness.inbox.key_lookup_send(sender=harness.actor, operation_key=KEY) == {
        "status": "unknown", "reason": "invalid-record",
    }


def test_key_lookup_matches_underscore_literally(harness):
    original = _send(harness)
    operation_key = KEY + "_"
    similar_key = KEY + "X"
    with closing(sqlite3.connect(harness.database)) as db, db:
        db.execute("UPDATE sender_copies SET idempotency_key=? WHERE message_id=?",
                   (f"{operation_key}:{harness.reader}:0", original["messageId"]))
        db.execute(
            """INSERT INTO sender_copies
               (message_id, conversation_id, sender, recipient, payload, intent,
                lifecycle, idempotency_key, created_at_ms, message_expires_at_ms)
               SELECT ?, conversation_id, sender, recipient, payload, intent,
                      lifecycle, ?, created_at_ms, message_expires_at_ms
                 FROM sender_copies WHERE message_id=?""",
            ("other-operation-copy", f"{similar_key}:{harness.reader}:0", original["messageId"]),
        )
    assert harness.inbox.key_lookup_send(sender=harness.actor, operation_key=operation_key) == {
        "status": "accepted", "commandId": operation_key,
        "conversationId": CONVERSATION, "messageId": original["messageId"],
    }


def test_key_lookup_uses_authenticated_sender_and_rejects_extra_selectors(harness):
    _send(harness)
    lookup = harness.app._ipc_message_send_key_lookup
    params = {"from": harness.actor, "sessionRef": SESSION, "operationKey": KEY}
    assert lookup("message.send.key_lookup", {**params, "from": harness.other,
                  "sessionRef": OTHER_SESSION}, _trusted_message_origin="ipc") == {
        "status": "unknown", "reason": "not-found",
    }
    for extra in ("text", "target", "conversationId"):
        with pytest.raises(DaemonRequestError) as caught:
            lookup("message.send.key_lookup", {**params, extra: "spoof"},
                   _trusted_message_origin="ipc")
        assert caught.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DaemonRequestError) as caught:
        lookup("message.send.key_lookup", {"from": harness.actor, "operationKey": KEY},
               _trusted_message_origin="ipc")
    assert caught.value.code == "SENDER_NOT_AUTHENTICATED"


def test_key_lookup_after_lost_response_and_restart_does_not_resend(harness):
    harness.transport.offline.add(harness.reader)
    sends = []

    def dropped_response():
        sends.append(_send(harness))
        raise OSError("response lost after durable acceptance")

    with pytest.raises(OSError, match="response lost"):
        dropped_response()
    harness.inbox.close()
    harness.inbox = InboxService(harness.database, harness.transport)
    harness.app._inbox = harness.inbox
    result = harness.app._ipc_message_send_key_lookup(
        "message.send.key_lookup",
        {"from": harness.actor, "sessionRef": SESSION, "operationKey": KEY},
        _trusted_message_origin="ipc",
    )
    assert result == {
        "status": "accepted", "commandId": KEY,
        "conversationId": CONVERSATION, "messageId": sends[0]["messageId"],
    }
    assert len(sends) == 1
    assert harness.inbox.outbox_count() == 1


def test_key_lookup_reports_missing_and_unavailable_without_retry(harness, monkeypatch):
    params = {"from": harness.actor, "sessionRef": SESSION, "operationKey": KEY}
    lookup = harness.app._ipc_message_send_key_lookup
    assert lookup("message.send.key_lookup", params, _trusted_message_origin="ipc") == {
        "status": "unknown", "reason": "not-found",
    }

    def unavailable(**selectors):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(harness.inbox, "key_lookup_send", unavailable)
    assert lookup("message.send.key_lookup", params, _trusted_message_origin="ipc") == {
        "status": "unknown", "reason": "unavailable",
    }
