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
from hyprial.shell.impl.gui.adapter import (
    AppAdapterUnavailable, AppChatAdapter, SessionBinding,
)
from tests.daemon.delivery.message.test_message_history import (
    ACTOR, CONVERSATION, OTHER, OTHER_SESSION, READER, SESSION,
    _remote_delivery, harness as harness,
)

KEY = "browser-original-operation"
TEXT = "original frozen draft"


def _send(harness):
    return harness.app.handle("message.send", {
        "from": ACTOR, "sessionRef": SESSION, "to": [READER],
        "message": TEXT, "summary": TEXT, "conversationId": CONVERSATION,
        "idempotencyKey": KEY,
    }, _trusted_message_origin="ipc")


def _lookup(harness, **overrides):
    return harness.app.handle("message.send.lookup", {
        "from": ACTOR, "sessionRef": SESSION, "target": READER,
        "text": TEXT, "conversationId": CONVERSATION, "idempotencyKey": KEY,
        **overrides,
    }, _trusted_message_origin="ipc")


def test_lookup_is_registered_without_migrating_the_message_family():
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES

    binding = METHOD_REGISTRY.get("message.send.lookup")
    assert binding is not None
    assert binding.spec.mutates is False
    assert binding.spec.retry == "safe"
    assert binding.spec.http_exposed is False
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
    harness.inbox = InboxService(
        harness.database, transport, link_admission=harness.app.link_admission,
    )
    harness.app._inbox = harness.inbox

    for _ in range(2):
        assert _lookup(harness) == {
            "status": "accepted", "commandId": KEY,
            "conversationId": CONVERSATION, "messageId": original["messageId"],
        }
    assert transport.deliveries == 1


def test_missing_is_unknown_then_committed_send_becomes_accepted(harness):
    assert _lookup(harness) == {"status": "unknown", "reason": "not-found"}
    harness.transport.offline.add(READER)
    original = _send(harness)
    assert original["deliveries"][0]["queued"] is True
    assert _lookup(harness)["messageId"] == original["messageId"]
    assert harness.inbox.outbox_count() == 1


def test_adapter_recovers_after_daemon_accepts_but_response_is_lost(harness):
    sends = []

    def request(method, params):
        result = harness.app.handle(method, params, _trusted_message_origin="ipc")
        if method == "message.send":
            sends.append(result)
            raise OSError("connection closed after durable acceptance")
        return result

    adapter = AppChatAdapter(
        request=request, sender=ACTOR, session_ref=SESSION,
        sessions={"chat": SessionBinding((READER,), CONVERSATION)},
    )
    body = {"text": TEXT, "idempotencyKey": KEY}
    with pytest.raises(AppAdapterUnavailable):
        adapter.send("chat", body)
    recovered = adapter.send_lookup("chat", body)
    assert recovered["status"] == "accepted"
    assert recovered["messageId"] == sends[0]["messageId"]
    assert recovered["commandId"] == KEY
    assert len(sends) == 1


def test_unavailable_database_is_unknown(harness, monkeypatch):
    def unavailable(**selectors):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(harness.inbox, "lookup_send", unavailable)
    assert _lookup(harness) == {"status": "unknown", "reason": "unavailable"}


@pytest.mark.parametrize("override", [
    {"idempotencyKey": "other"}, {"conversationId": "other"},
    {"target": OTHER}, {"text": TEXT + " edited"},
    {"from": OTHER, "sessionRef": OTHER_SESSION},
])
def test_lookup_never_confuses_another_request_with_the_original(harness, override):
    _send(harness)
    result = _lookup(harness, **override)
    assert result["status"] == "unknown"
    assert "messageId" not in result


def test_lookup_requires_authenticated_identity_even_in_observe_mode(harness):
    _send(harness)
    harness.app._sender_auth_enforced = False
    with pytest.raises(DaemonRequestError) as caught:
        harness.app.handle("message.send.lookup", {
            "from": ACTOR, "target": READER, "text": TEXT,
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
        service_options={"link_admission": harness.app.link_admission},
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
    harness.transport.offline.add(READER)
    original = _send(harness)
    with closing(sqlite3.connect(harness.database)) as writer:
        writer.execute("UPDATE sender_copies SET payload=? WHERE message_id=?",
                       (json.dumps({"message": "uncommitted"}).encode(), original["messageId"]))
        assert _lookup(harness)["status"] == "accepted"
        assert _lookup(harness, text="uncommitted")["status"] == "unknown"
        writer.rollback()
