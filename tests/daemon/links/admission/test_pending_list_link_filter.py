"""WP4 §2.9 / review B3, B5: interactive list/wait/fetch honour the gate."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from tests.daemon.delivery.origin.test_inbound_origin import _app
from tests.support.links.gate import (
    BudgetedLinkGate,
    PermissiveLinkGate,
    ScriptedLinkGate,
)
from tests.support.runtime._owned_applications import owned_applications as owned_applications

READER = "agent:op:node-1:reader"
SENDER = "agent:op:node-1:driver"
SESSION = {"from": "reader", "sessionRef": "session-reader"}


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(message_id: str, *, recipient: str = READER) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message": "hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _reply(message_id: str, *, reply_to: str | None) -> InboxMessage:
    body: dict[str, object] = {"message": "re"}
    if reply_to is not None:
        body["replyTo"] = reply_to
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=SENDER,
        recipient=READER,
        payload=json.dumps(body).encode(),
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _assembled_app(tmp_path: Path) -> object:
    app = _app(tmp_path)
    app._inbox.close()
    database = tmp_path / "custody.sqlite3"
    # A fresh slot for this test (the helper's own slot is already bound
    # permissive); assigning it to the app keeps list/wait and the actor on
    # the same object so a scripted gate controls both.
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


def test_list_hides_held_and_fetch_does_not_stamp_them(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, admit={"held": False}, default_admit=True)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("held")).accepted
    assert app._inbox.receive(_message("ok")).accepted

    listed = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in listed["messages"]] == ["ok"]

    fetched = app.handle(
        "message.pending.list", {**SESSION, "fetched": True}
    )
    assert [row["messageId"] for row in fetched["messages"]] == ["ok"]
    assert app._inbox.has_fetched("ok") is True
    assert app._inbox.has_fetched("held") is False

    gate.admit["held"] = True
    relisted = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in relisted["messages"]] == ["held", "ok"]


def test_wait_does_not_ring_for_held_rows(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=False)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("held")).accepted

    held = app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": [], "holdMs": 600},
    )
    assert held["changed"] is False and held["held"] is True

    # Only the link state changes; no inbox write.  The running wait must
    # still notice within its bounded rescans (review B5).
    def release() -> None:
        time.sleep(0.2)
        gate.admit["held"] = True

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    started = time.monotonic()
    result = app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": [], "holdMs": 3000},
    )
    thread.join()
    assert result["changed"] is True and result["held"] is True, result
    assert time.monotonic() - started < 2.5


def test_reply_boundary_on_list_and_wait(tmp_path: Path) -> None:
    """review B1.5: list/wait use the same reply association as headless."""

    app = _assembled_app(tmp_path)
    # The gate admits one valid correlation and denies everything else
    # (missing, wrong type, other peer, closed, reclaimed).
    gate = ScriptedLinkGate(known={SENDER}, default_admit=False)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_reply("m-valid", reply_to="m-open")).accepted
    for index, reply_to in enumerate((None, "m-other", "m-closed", "m-reclaimed")):
        assert app._inbox.receive(
            _reply(f"m-held-{index}", reply_to=reply_to)
        ).accepted
    gate.admit["m-valid"] = True

    listed = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in listed["messages"]] == ["m-valid"]
    valid_calls = [
        call for call in gate.admit_calls() if call["message_id"] == "m-valid"
    ]
    assert valid_calls and valid_calls[-1]["intent"] == "reply"
    assert valid_calls[-1]["reply_to"] == "m-open"

    # wait rings only for the admissible reply; the denied ones keep it held.
    result = app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": [], "holdMs": 300},
    )
    # The wait rings (whether it returned immediately or at the hold) because
    # only the admitted reply is visible to the doorbell.
    assert result["changed"] is True


def test_wait_does_not_ring_for_denied_replies(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=False)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_reply("m-held", reply_to=None)).accepted
    assert app._inbox.receive(_reply("m-held-2", reply_to="m-other")).accepted

    result = app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": [], "holdMs": 300},
    )
    assert result["changed"] is False and result["held"] is True


def test_fetch_pending_only_filter(tmp_path: Path) -> None:
    slot = LinkAdmission()
    slot.bind(PermissiveLinkGate())
    facade = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            tmp_path / "fetch.sqlite3",
            MemoryDeliveryTransport(),
            _Events(),
            service_options={"link_admission": slot},
        ),
        tmp_path / "fetch.sqlite3",
    )
    try:
        assert facade.receive(_message("a")).accepted
        assert facade.receive(_message("b")).accepted

        selected = facade.fetch_pending(READER, now_ms=10, only=frozenset({"a"}))
        assert [item.message_id for item in selected] == ["a"]
        assert facade.has_fetched("a") is True
        assert facade.has_fetched("b") is False

        assert facade.fetch_pending(READER, now_ms=11, only=frozenset()) == ()
        assert facade.has_fetched("b") is False
        rest = facade.fetch_pending(READER, now_ms=12, only=None)
        assert [item.message_id for item in rest] == ["a", "b"]
        assert facade.has_fetched("b") is True
    finally:
        facade.close()


def test_gate_calls_run_on_the_inbox_actor_thread(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=True)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("m-thread")).accepted
    app.handle("message.pending.list", dict(SESSION))

    threads = set(gate.call_threads("sender_known")) | set(
        gate.call_threads("admit_dispatch")
    )
    assert threads, "the gate was never asked"
    assert threading.get_ident() not in threads
    assert len(threads) == 1, threads


def test_wait_gate_calls_run_on_the_inbox_actor_thread(tmp_path: Path) -> None:
    """review B3.4: pending.wait must not call the gate on the caller thread."""

    app = _assembled_app(tmp_path)
    gate = ScriptedLinkGate(known={SENDER}, default_admit=True)
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("m-wait-thread")).accepted
    app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": [], "holdMs": 200},
    )

    threads = set(gate.call_threads("sender_known")) | set(
        gate.call_threads("admit_dispatch")
    )
    assert threads, "the gate was never asked"
    assert threading.get_ident() not in threads
    assert len(threads) == 1, threads


def test_budgeted_gate_shares_one_admission_slot(tmp_path: Path) -> None:
    app = _assembled_app(tmp_path)
    gate = BudgetedLinkGate(1, known={SENDER})
    app.link_admission.bind(gate)

    assert app._inbox.receive(_message("m-1")).accepted
    assert app._inbox.receive(_message("m-2")).accepted

    first = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in first["messages"]] == ["m-1"]
    assert gate.remaining == 0

    # A repeat ask for the admitted id never consumes another unit and a
    # second distinct id cannot also take the single slot.
    second = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in second["messages"]] == ["m-1"]
    assert gate.remaining == 0


def test_fetch_rereads_live_rows_after_candidate_query(tmp_path: Path) -> None:
    slot = LinkAdmission()
    slot.bind(PermissiveLinkGate())
    facade = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            tmp_path / "race.sqlite3",
            MemoryDeliveryTransport(),
            _Events(),
            service_options={"link_admission": slot},
        ),
        tmp_path / "race.sqlite3",
    )
    try:
        assert facade.receive(_message("m-gone")).accepted
        candidates = [m.message_id for m in facade.pending_messages(READER)]
        assert candidates == ["m-gone"]
        # The row is consumed between the candidate query and the fetch; the
        # actor re-reads live rows, so it must not be returned or stamped.
        facade.ack(READER, "m-gone")
        assert facade.fetch_pending(READER, only=frozenset(candidates)) == ()
    finally:
        facade.close()
