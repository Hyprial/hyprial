"""Fail-loud routing fallbacks when no pending row names the sender.

PR #1463 finding 2: a FAILED result whose delivery has no pending inbox row
and no settlement tombstone used to produce only an error log line
(``availability_loud.route_missing``) -- nobody was told.  The sender notice
now falls back to the ``delivery_status`` ledger route, and a total miss
raises a human-audience alarm instead of silence.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.runtime import DaemonEventBridge

from tests.daemon.delivery.providers.test_provider_failure_settlement import (
    ACTOR,
    SENDER,
    _Harnesses,
    _desired,
    _Transport,
)

SENDER_NOTICE = "provider-unavailable"


def _inbox(tmp_path: Path) -> DeliveryCustodyFacade:
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        CorrelatedInboxEventRouter(),
        node_id="provider-failure-node",
    )
    return DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")


def _bridge(
    state: Path,
    inbox: DeliveryCustodyFacade,
    harnesses: _Harnesses,
    clock: dict[str, int],
    logs: list[tuple[str, str, str, dict[str, object]]],
) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=state,
        node_id="provider-failure-node",
        desired_state=_desired(state),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda _name: ACTOR,
        clock_ms=lambda: clock["now"],
        logger=lambda level, component, event, **fields: logs.append(
            (level, component, event, fields)
        ),
    )


def _seed_delivery_status(
    database: Path, message_id: str, *, sender: str, recipient: str = ACTOR
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """INSERT INTO delivery_status (
                   message_id, sender, recipient, state, holder, reason,
                   conversation_id, idempotency_key, recorded_at_ms,
                   purge_after_ms
               ) VALUES (?, ?, ?, 'fetched', 'provider-failure-node',
                         'dispatched', ?, NULL, 1, 99999999999999)""",
            (
                message_id,
                sender,
                recipient,
                f"conversation-{message_id}",
            ),
        )
        connection.commit()


def test_harness_failure_route_reads_the_terminal_ledger(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    try:
        _seed_delivery_status(
            tmp_path / "inbox.sqlite3",
            "ledger-route",
            sender=SENDER,
        )
        route = inbox.harness_failure_route("ledger-route")
        assert route is not None
        assert route.sender == SENDER
        assert route.recipient == ACTOR
        assert route.conversation_id == "conversation-ledger-route"
        assert route.payload == b"{}"
        assert inbox.harness_failure_route("never-booked") is None
    finally:
        inbox.close()


def test_failed_result_with_only_a_ledger_route_still_notifies_the_sender(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    logs: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = _bridge(tmp_path, inbox, harnesses, clock, logs)
    delivery_id = str(uuid.uuid4())
    # No inbox row and no settlement: only the terminal ledger names the
    # waiting sender.
    _seed_delivery_status(
        tmp_path / "inbox.sqlite3", delivery_id, sender=SENDER
    )
    runtime.start()
    try:
        harnesses.results.append(
            HarnessResult(
                delivery_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="provider blew up",
                failure_code="PROVIDER_AUTHENTICATION_FAILED",
            )
        )
        runtime.on_timer()
        runtime.on_timer()

        notices = [
            item
            for item in inbox.outbox_items()
            if item.message.recipient == SENDER
        ]
        assert len(notices) == 1
        missing = [
            entry for entry in logs if entry[2] == "availability_loud.route_missing"
        ]
        assert missing == []
    finally:
        runtime.stop()
        inbox.close()


def test_failed_result_with_no_route_at_all_raises_a_human_alarm(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    logs: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = _bridge(tmp_path, inbox, harnesses, clock, logs)
    alarms: list[Any] = []
    inbox.alarm_emitter = type(  # noqa: SLF001 - the spy is the point
        "SpyEmitter", (), {"emit": staticmethod(lambda alarm: alarms.append(alarm))}
    )()
    delivery_id = str(uuid.uuid4())
    runtime.start()
    try:
        harnesses.results.append(
            HarnessResult(
                delivery_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="provider blew up",
                failure_code="PROVIDER_AUTHENTICATION_FAILED",
            )
        )
        runtime.on_timer()
        runtime.on_timer()

        missing = [
            entry for entry in logs if entry[2] == "availability_loud.route_missing"
        ]
        assert len(missing) == 1
        assert len(alarms) == 1
        assert alarms[0].audience == "human"
        assert alarms[0].message_id == delivery_id
        assert alarms[0].reason == "PROVIDER_AUTHENTICATION_FAILED"
    finally:
        runtime.stop()
        inbox.close()
