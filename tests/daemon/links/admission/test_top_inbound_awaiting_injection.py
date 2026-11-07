"""WP4 / review B6: ``inboundAwaitingInjection`` is a read-only top field."""

from __future__ import annotations

import time
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox.service import ConsumptionState
from tests.daemon.delivery.origin.test_inbound_origin import _app
from tests.support.links.gate import ScriptedLinkGate
from tests.support.runtime._owned_applications import owned_applications as owned_applications

READER = "agent:op:node-1:reader"
SENDER = "agent:op:node-1:driver"
SESSION = {"from": "reader", "sessionRef": "session-reader"}
FIELD = "inboundAwaitingInjection"


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(
    message_id: str, *, recipient: str = READER, expires_at_ms: int | None = None
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message": "hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        expires_at_ms=expires_at_ms,
    )


def _assembled_app(tmp_path: Path) -> object:
    app = _app(tmp_path)
    app._inbox.close()
    database = tmp_path / "custody.sqlite3"
    # Fresh slot (the helper's own is already bound permissive) shared by the
    # app surfaces and the custody actor.
    slot = LinkAdmission()
    app._link_admission = slot
    facade = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            database,
            MemoryDeliveryTransport(),
            _Events(),
            service_options={"link_admission": slot},
        ),
        database,
    )
    app._inbox = facade
    app._inbox_coordinator = facade._coordinator
    return app


def _awaiting(app: object) -> int:
    result = app.handle("top.snapshot", {})
    for row in result["actors"]:
        if row["actor"] == READER:
            return int(row[FIELD])
    raise AssertionError("reader actor row missing from top.snapshot")


def test_awaiting_injection_counts_only_held_alive_rows(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=False)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("m1")).accepted
    assert app._inbox.receive(_message("m2")).accepted
    # An expired row is never alive and must not be counted.
    assert app._inbox.receive(_message("m3", expires_at_ms=1)).accepted

    # The first list evaluates the gate; the count then reflects held rows.
    assert app.handle("message.pending.list", dict(SESSION))["messages"] == []
    assert _awaiting(app) == 2

    # Repeated top queries are inert: no new gate asks, no consumption change.
    calls_before = len(gate.admit_calls())
    consumption_before = app._inbox.consumption_state("m1")
    for _ in range(2):
        assert _awaiting(app) == 2
    assert len(gate.admit_calls()) == calls_before
    assert app._inbox.consumption_state("m1") is consumption_before
    assert consumption_before is ConsumptionState.PENDING

    # Release one: it may be injected, so it leaves the waiting count.
    gate.admit["m1"] = True
    listed = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in listed["messages"]] == ["m1"]
    assert _awaiting(app) == 1
    app.handle("message.pending.list", {**SESSION, "fetched": True})

    # Release the last: nothing is waiting.
    gate.admit["m2"] = True
    app.handle("message.pending.list", {**SESSION, "fetched": True})
    assert _awaiting(app) == 0


def test_unbound_gate_rows_are_still_visible(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    assert app.link_admission.bound is False
    assert app._inbox.receive(_message("m-unbound")).accepted
    assert _awaiting(app) == 1


def test_bound_rows_count_before_any_list_or_tick(tmp_path: Path) -> None:
    """review B6: the count comes from rows, not the gate's evaluated set."""

    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=False)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("m1")).accepted
    assert app._inbox.receive(_message("m2")).accepted
    # No list, no tick, no gate call yet: both governed rows must already be
    # visible as awaiting injection.
    assert gate.admit_calls() == []
    assert _awaiting(app) == 2
    assert gate.admit_calls() == []


def test_ack_removes_a_held_row_from_the_count(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    app.link_admission.bind(ScriptedLinkGate(known={SENDER}, default_admit=False))

    assert app._inbox.receive(_message("m-ack")).accepted
    assert app._inbox.receive(_message("m-stay")).accepted
    assert _awaiting(app) == 2

    assert app._inbox.ack(READER, "m-ack").acknowledged is True
    assert _awaiting(app) == 1


def test_ttl_prune_removes_a_held_row_from_the_count(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    app.link_admission.bind(ScriptedLinkGate(known={SENDER}, default_admit=False))

    now = time.time_ns() // 1_000_000
    assert app._inbox.receive(
        _message("m-ttl", expires_at_ms=now + 60_000), now_ms=now
    ).accepted
    assert _awaiting(app) == 1

    app._inbox.prune_inbox(now_ms=now + 120_000)
    assert _awaiting(app) == 0
