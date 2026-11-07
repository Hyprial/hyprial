"""No-change commands complete without publishing a durable projection change."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from hyprial.daemon.impl.inbox.batch.contract import BatchCursor, BatchMember
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import ActorState

from ..support import _message, _wait_until, authority as authority


def _stored(coordinator) -> tuple[str, ...]:
    connection = sqlite3.connect(
        f"{coordinator._database.as_uri()}?mode=ro", uri=True
    )
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def _wait_for_restart(coordinator) -> None:
    def restarted() -> bool:
        snapshot = coordinator._runtime.snapshot(coordinator._handle)
        return snapshot.generation > 1 and snapshot.state is ActorState.RUNNING

    _wait_until(restarted)


def _observe_global_reads(monkeypatch, coordinator) -> list[None]:
    calls: list[None] = []
    service = coordinator._active_handler._service
    original = service.pending_all

    def pending_all():
        calls.append(None)
        return original()

    monkeypatch.setattr(service, "pending_all", pending_all)
    return calls


@pytest.mark.parametrize("operation", ["admitted_pending", "admitted_dispatchable"])
@pytest.mark.parametrize("populated", [False, True])
def test_admission_replies_do_not_publish_persistent_changes(
    authority, monkeypatch, operation: str, populated: bool
) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("admitted")
    if populated:
        assert facade.receive(message, now_ms=10).accepted
    before = _stored(coordinator)
    version = facade.version
    global_reads = _observe_global_reads(monkeypatch, coordinator)

    for _ in range(3):
        result = getattr(facade, operation)(message.recipient, now_ms=20)
        assert result == ((message,) if populated else ())

    assert _stored(coordinator) == before
    assert (facade.version, len(global_reads)) == (version, 0)


@pytest.mark.parametrize("operation", ["fetch_pending", "drain_system_notices"])
def test_empty_mutation_reply_keeps_version(authority, monkeypatch, operation) -> None:
    facade, coordinator, _transport, _events = authority
    before = _stored(coordinator)
    version = facade.version
    global_reads = _observe_global_reads(monkeypatch, coordinator)

    assert getattr(facade, operation)(_message("empty").recipient) == ()

    assert _stored(coordinator) == before
    assert (facade.version, len(global_reads)) == (version, 0)


def test_repeat_fetch_returns_messages_without_a_new_projection(
    authority, monkeypatch
) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("repeat-fetch")
    assert facade.receive(message, now_ms=10).accepted
    assert facade.fetch_pending(message.recipient, now_ms=20) == (message,)
    before = _stored(coordinator)
    version = facade.version
    global_reads = _observe_global_reads(monkeypatch, coordinator)

    assert facade.fetch_pending(message.recipient, now_ms=30) == (message,)

    assert _stored(coordinator) == before
    assert (facade.version, len(global_reads)) == (version, 0)


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("replay", [False, True])
def test_stable_batch_selection_does_not_publish_a_change(
    authority, monkeypatch, populated: bool, replay: bool
) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("batch-message")
    if populated:
        assert facade.receive(message, now_ms=10).accepted
        assert facade.receive_system_notice(
            replace(_message("batch-notice", intent="system"), sender="system:local")
        )
    selection = facade.batch_snapshot(
        message.recipient, keys=(message.recipient,), now_ms=20
    )
    cursor = BatchCursor(
        actor=selection.actor,
        bounds=selection.bounds,
        members=tuple(
            BatchMember(row.source, row.message.message_id, row.ordinal)
            for row in selection.rows
        ),
        deferred=selection.deferred,
        predecessor_digest=selection.predecessor_digest,
    )
    before = _stored(coordinator)
    version = facade.version
    global_reads = _observe_global_reads(monkeypatch, coordinator)

    repeated = facade.batch_snapshot(
        message.recipient,
        keys=(message.recipient,),
        now_ms=30,
        cursor=cursor if replay else None,
    )

    assert repeated.rows == selection.rows
    assert _stored(coordinator) == before
    assert (facade.version, len(global_reads)) == (version, 0)


@pytest.mark.parametrize("operation", ["receive", "fetch", "ack", "drain", "batch"])
def test_actual_mutations_wake_projection_waiters(authority, operation) -> None:
    facade, _coordinator, _transport, _events = authority
    message = _message(f"real-{operation}")
    if operation in {"fetch", "ack", "batch"}:
        assert facade.receive(message, now_ms=10).accepted
    if operation == "drain":
        assert facade.receive_system_notice(replace(message, intent="system"))
    version = facade.version
    started = threading.Event()
    observed: list[int] = []

    def wait() -> None:
        started.set()
        observed.append(facade.wait_for_version(version, 3.0))

    thread = threading.Thread(target=wait)
    thread.start()
    assert started.wait(1.0)
    if operation == "receive":
        assert facade.receive(message, now_ms=10).accepted
    elif operation == "fetch":
        assert facade.fetch_pending(message.recipient, now_ms=20) == (message,)
    elif operation == "ack":
        assert facade.ack(message.recipient, message.message_id).acknowledged
    elif operation == "drain":
        assert facade.drain_system_notices(message.recipient)
    else:
        selection = facade.batch_snapshot(
            message.recipient, keys=(message.recipient,), now_ms=20
        )
        assert selection.rows and facade.has_fetched(message.message_id)
    thread.join(1.0)
    assert not thread.is_alive()
    assert observed == [version + 1]


def test_fetch_publishes_receipt_retirement_with_an_existing_stamp(authority) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("late-local-outbox")
    assert facade.receive(message, now_ms=10).accepted
    facade.fetch_pending(message.recipient, now_ms=20)
    assert facade.submit(message, now_ms=30, defer_direct=True).accepted
    assert facade.outbox_count() == 1
    before = _stored(coordinator)
    version = facade.version

    assert facade.fetch_pending(message.recipient, now_ms=40) == (message,)

    assert facade.outbox_count() == 0
    assert _stored(coordinator) != before
    assert facade.version == version + 1


def test_completed_fetch_publishes_retry_cleanup_with_an_existing_stamp(authority) -> None:
    facade, _coordinator, _transport, _events = authority
    message = _message("completed-retry")
    assert facade.receive(message, now_ms=10).accepted
    facade.fetch_pending(message.recipient, now_ms=20)
    failed = facade.settle_harness_failure(
        message.recipient, message.message_id, "temporary",
        permanent=False, max_attempts=3, backoff_ms=(1,), now_ms=30,
    )
    assert not failed.terminal
    version = facade.version

    assert facade.fetch_pending(
        message.recipient, now_ms=40,
        only=frozenset({message.message_id}), completed_turn=True,
    ) == (message,)

    assert facade.harness_failure_settlement(message.message_id) is None
    assert facade.harness_failure_attempts(message.message_id)
    assert facade.version == version + 1


def test_committed_fetch_is_published_even_if_a_later_phase_fails(
    authority, monkeypatch
) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("commit-before-error")
    assert facade.receive(message, now_ms=10).accepted
    version = facade.version

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("injected post-commit receipt failure")

    monkeypatch.setattr(InboxService, "retire_outbox_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.fetch_pending(message.recipient, now_ms=20)

    _wait_for_restart(coordinator)
    assert facade.has_fetched(message.message_id)
    assert facade.version == version + 1


def test_rolled_back_fetch_does_not_publish_a_change(authority, monkeypatch) -> None:
    facade, coordinator, _transport, _events = authority
    message = _message("rollback")
    assert facade.receive(message, now_ms=10).accepted
    before = _stored(coordinator)
    version = facade.version

    def fail_ledger(*args, **kwargs):
        raise RuntimeError("injected ledger failure before commit")

    monkeypatch.setattr(InboxService, "_write_inbound_ledger_locked", fail_ledger)
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.fetch_pending(message.recipient, now_ms=20)

    _wait_for_restart(coordinator)
    assert not facade.has_fetched(message.message_id)
    assert _stored(coordinator) == before
    assert facade.version == version
