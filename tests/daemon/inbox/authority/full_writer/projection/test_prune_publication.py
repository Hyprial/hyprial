"""Expiry commits publish once; empty maintenance keeps the projection stable."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from hyprial.daemon.impl.inbox.actor.custody.notices import DeliveryCustodyNoticesMixin
from hyprial.daemon.impl.inbox.service import InboxService

from ..support import _message, authority as authority
from .test_no_change import _observe_global_reads, _stored, _wait_for_restart


@pytest.mark.parametrize("populated", [False, True])
def test_prune_without_expiry_keeps_version_and_projection(authority, monkeypatch, populated):
    facade, coordinator, _, _ = authority
    if populated:
        assert facade.receive(_message("not-expired"), now_ms=10).accepted
    before, version = _stored(coordinator), facade.version
    reads = _observe_global_reads(monkeypatch, coordinator)

    for _ in range(3):
        assert facade.prune_inbox(now_ms=20) == ()

    assert _stored(coordinator) == before
    assert (facade.version, len(reads)) == (version, 0)


@pytest.mark.parametrize("terminal", [False, True])
def test_expiry_still_commits_ledger_and_wakes_once(authority, terminal):
    facade, coordinator, _, _ = authority
    message = _message("expired")
    assert facade.receive(message, now_ms=10).accepted
    assert facade.fetch_pending(message.recipient, now_ms=20) == (message,)
    if terminal:
        result = facade.settle_harness_failure(
            message.recipient, message.message_id, "permanent", permanent=True,
            max_attempts=1, backoff_ms=(1,), now_ms=30,
        )
        assert result.terminal
    expiry = 31 + coordinator._active_handler._service.durable_ttl_ms
    version = facade.version
    started = threading.Event()
    seen = []

    def wait():
        started.set()
        seen.append(facade.wait_for_version(version, 2))

    thread = threading.Thread(target=wait)
    thread.start()
    assert started.wait(1)
    pruned = facade.prune_inbox(now_ms=expiry)
    thread.join(1)
    assert not thread.is_alive() and seen == [version + 1]
    assert [(item.message_id, item.reason) for item in pruned] == [
        (message.message_id, "TERMINAL_SETTLED" if terminal else "TTL_EXPIRED")
    ]
    assert facade.pending_messages(message.recipient) == ()
    assert facade.system_notices(message.sender) == ()
    rows = facade.link_ledger_snapshot(0)
    assert len(rows) == 1 and rows[0].state == "expired"
    assert facade.prune_inbox(now_ms=expiry + 1) == ()
    assert facade.version == version + 1


def test_expiry_keeps_local_sender_notice(authority):
    facade, coordinator, _, _ = authority
    message = replace(_message("unread-expired"), sender="agent:owner:node-b:alice")
    assert facade.receive(message, now_ms=10).accepted
    expiry = 11 + coordinator._active_handler._service.durable_ttl_ms
    version = facade.version
    assert len(facade.prune_inbox(now_ms=expiry)) == 1
    notices = facade.system_notices(message.sender)
    assert len(notices) == 1 and b"TTL_EXPIRED" in notices[0].payload
    assert facade.version == version + 1


@pytest.mark.parametrize("phase", ["forget", "notice"])
def test_expiry_commit_is_published_before_later_failure(authority, monkeypatch, phase):
    facade, coordinator, _, _ = authority
    message = _message("post-commit-expiry")
    assert facade.receive(message, now_ms=10).accepted
    facade.fetch_pending(message.recipient, now_ms=20)
    expiry = 21 + coordinator._active_handler._service.durable_ttl_ms
    version = facade.version

    def fail(*args, **kwargs):
        raise RuntimeError("injected after expiry commit")

    if phase == "forget":
        monkeypatch.setattr(InboxService, "_forget_admission", fail)
    else:
        monkeypatch.setattr(DeliveryCustodyNoticesMixin, "_stage_expired_sender_notices", fail)
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.prune_inbox(now_ms=expiry)
    _wait_for_restart(coordinator)
    assert facade.pending_messages(message.recipient) == ()
    assert facade.version == version + 1


def test_failed_expiry_transaction_does_not_publish(authority, monkeypatch):
    facade, coordinator, _, _ = authority
    message = _message("rolled-back-expiry")
    assert facade.receive(message, now_ms=10).accepted
    before, version = _stored(coordinator), facade.version
    expiry = 11 + coordinator._active_handler._service.durable_ttl_ms

    def fail(*args, **kwargs):
        raise RuntimeError("injected before expiry commit")

    monkeypatch.setattr(InboxService, "_ledger_expire_inbound_locked", fail)
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.prune_inbox(now_ms=expiry)
    _wait_for_restart(coordinator)
    assert _stored(coordinator) == before and facade.version == version
