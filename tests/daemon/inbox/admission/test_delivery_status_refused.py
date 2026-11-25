"""WP4 §2.5-2.6 / review B4: the ``refused`` terminal state and its consumers.

T1b: the ingress LINK_UNKNOWN refusal is gone; the live reason for a
``refused`` record is ``TARGET_UNRESOLVED`` (an unclaimed recipient).  Old
rows written with the LINK_UNKNOWN reason string stay readable.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hyprial.daemon.impl.inbox import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection
from hyprial.daemon.impl.inbox.links.pull import (
    DeliveryStatus,
    DeliveryStatusStore,
    HoldReason,
    TerminalState,
    merge_delivery_status,
)
from hyprial.daemon.impl.inbox.service import ConsumptionState, InboxService
from hyprial.kernel import ipc_errors


def _store() -> tuple[DeliveryStatusStore, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = DeliveryStatusStore(connection, lock=threading.RLock())
    return store, connection


def _record(
    store: DeliveryStatusStore,
    state: TerminalState,
    *,
    now_ms: int,
    reason: str = HoldReason.ACK_RECEIVED.value,
) -> DeliveryStatus:
    return store.record(
        message_id="m1",
        sender="agent:op:nb:alice",
        recipient="agent:op:nb:bob",
        state=state,
        holder="nb",
        reason=reason,
        now_ms=now_ms,
    )


def test_refused_is_a_terminal_state() -> None:
    assert TerminalState("refused") is TerminalState.REFUSED
    assert ipc_errors.TARGET_UNRESOLVED == "TARGET_UNRESOLVED"


def test_record_upgrade_matrix() -> None:
    store, _connection = _store()

    _record(store, TerminalState.EXPIRED, now_ms=1, reason=HoldReason.TTL_EXPIRED.value)
    _record(store, TerminalState.REFUSED, now_ms=2, reason=ipc_errors.TARGET_UNRESOLVED)
    refused = store.lookup("m1")
    assert refused is not None and refused.state is TerminalState.REFUSED

    # refusal is a no-op: the first refusal's timestamp is kept.
    _record(store, TerminalState.REFUSED, now_ms=99, reason=ipc_errors.TARGET_UNRESOLVED)
    unchanged = store.lookup("m1")
    assert unchanged is not None and unchanged.recorded_at_ms == 2

    # refused -> fetched upgrades (the sender joined and the retry landed).
    _record(store, TerminalState.FETCHED, now_ms=3)
    fetched = store.lookup("m1")
    assert fetched is not None and fetched.state is TerminalState.FETCHED

    # fetched -> refused must not downgrade.
    _record(store, TerminalState.REFUSED, now_ms=4, reason=ipc_errors.TARGET_UNRESOLVED)
    still = store.lookup("m1")
    assert still is not None and still.state is TerminalState.FETCHED


def _status(message_id: str, state: TerminalState, reason: str, holder: str) -> DeliveryStatus:
    return DeliveryStatus(
        message_id=message_id,
        sender="agent:op:nb:alice",
        recipient="agent:op:nb:bob",
        state=state,
        holder=holder,
        reason=reason,
        recorded_at_ms=1,
    )


def test_merge_precedence() -> None:
    merged = merge_delivery_status(
        (
            _status("a", TerminalState.EXPIRED, HoldReason.TTL_EXPIRED.value, "nb"),
            _status("a", TerminalState.REFUSED, ipc_errors.TARGET_UNRESOLVED, "nb"),
        )
    )
    assert merged[0].state is TerminalState.REFUSED

    merged = merge_delivery_status(
        (
            _status("b", TerminalState.REFUSED, ipc_errors.TARGET_UNRESOLVED, "nb"),
            _status("b", TerminalState.FETCHED, HoldReason.ACK_RECEIVED.value, "nb"),
        )
    )
    assert merged[0].state is TerminalState.FETCHED


def test_consumption_state_reports_refused(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service = InboxService(database, MemoryDeliveryTransport())
    try:
        with service._db:
            service._status.record(
                message_id="m-refused",
                sender="agent:op:nb:alice",
                recipient="agent:op:nb:bob",
                state=TerminalState.REFUSED,
                holder="nb",
                reason=ipc_errors.TARGET_UNRESOLVED,
                now_ms=1,
            )
        assert service.consumption_state("m-refused") is ConsumptionState.REFUSED
    finally:
        service.close()

    projection = InboxReadProjection(database)
    assert projection.consumption_state("m-refused") is ConsumptionState.REFUSED


def test_cell_work_refused_is_not_added_to_frozen_ipc_codes_and_pull_rule_holds() -> None:
    assert "CELL_WORK_REFUSED" not in ipc_errors.ALL_CODES
    assert not hasattr(ipc_errors, "CELL_WORK_REFUSED")
    assert not hasattr(ipc_errors, "CELL_RETIRED")
    store, _connection = _store()
    _record(store, TerminalState.FETCHED, now_ms=10)
    _record(store, TerminalState.REFUSED, now_ms=20, reason="CELL_WORK_REFUSED")
    still = store.lookup("m1")
    assert still is not None and still.state is TerminalState.FETCHED
