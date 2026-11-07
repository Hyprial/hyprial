"""WP2 §2.4 / §9.6 WP2-B2: the send boundary asks the one bound gate.

These drive the real ``DaemonApplication`` message IPC against a real inbox
authority (the production custody facade, so the durable receipt and the
``link_hold`` outbox column are observable) and a scripted, recording gate.
They deliberately do not need composition wiring (WP2-B3): the gate is bound
straight into ``app.link_admission``, which is the one slot ruling 67 names.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.contracts.ports import UpdateLinkLedgerCommand
from hyprial.daemon.impl.links.ports import (
    HOLD_REASON_KEY,
    HOLD_REASONS,
    Allow,
    Hold,
    Mint,
)
from hyprial.daemon.impl.squire import UserDeliveryRequest, UserDeliveryResult
from hyprial.kernel import DaemonRequestError, canonical_agent_uri, ipc_errors
from hyprial.kernel import reply_message_id
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)

OWNER = "op"
NODE = "wp2b-node"
ALPHA = canonical_agent_uri(OWNER, NODE, "alpha")
BETA = canonical_agent_uri(OWNER, NODE, "beta")
OTHER_AGENT = canonical_agent_uri(OWNER, "other-node", "driver")
OWNER_USER = f"user:{OWNER}"
PEER_USER = "user:reviewer"


class _Events:
    def publish(self, _event: object) -> None:
        pass


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _FakeUserDelivery:
    """Per-key durable result ledger, like the real ``user_delivery`` port.

    A key whose delivery succeeded replays as ``duplicate`` with the original
    native id; a failed or interrupted delivery records nothing; deliveries
    for one key are serialised (the real actor claims the key).
    """

    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.requests: list[UserDeliveryRequest] = []
        self.sent: list[UserDeliveryRequest] = []
        self.results: dict[str, UserDeliveryResult] = {}
        self.before_send: Any = None
        self._lock = threading.Lock()

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        with self._lock:
            previous = self.results.get(request.idempotency_key)
            if previous is not None:
                return dataclasses.replace(previous, duplicate=True)
            if self.before_send is not None:
                self.before_send(request)
            result = UserDeliveryResult(
                request.message_id,
                self.accepted,
                native_message_id=f"om-user-{len(self.requests)}",
                code=None if self.accepted else ipc_errors.USER_DELIVERY_FAILED,
            )
            if self.accepted:
                self.sent.append(request)
                self.results[request.idempotency_key] = result
            return result


class _RecordingGate:
    """A scripted ``LinkGate`` + ``LinkSendObserver`` that records every call."""

    def __init__(
        self,
        *,
        answers: list[Any] | None = None,
        error: DaemonRequestError | None = None,
    ) -> None:
        self.answers = list(answers or ())
        self.error = error
        self.admit_calls: list[dict[str, Any]] = []
        self.submitted_calls: list[dict[str, Any]] = []
        self.sender_known_calls: list[tuple[str, str]] = []

    def admit_send(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        now_ms: int,
    ) -> Any:
        self.admit_calls.append(
            {
                "sender": sender,
                "target": target,
                "intent": intent,
                "reply_to": reply_to,
                "now_ms": now_ms,
            }
        )
        if self.error is not None:
            raise self.error
        if self.answers:
            return self.answers.pop(0)
        return Allow()

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        self.sender_known_calls.append((recipient, sender))
        return True

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        del recipient, sender, message_id, now_ms, intent, reply_to
        return True

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        del actor, message_id
        return "keep"

    def submitted(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        reply_to: str | None,
        hold_reason: str | None,
        now_ms: int,
    ) -> None:
        del now_ms
        self.submitted_calls.append(
            {
                "sender": sender,
                "target": target,
                "message_id": message_id,
                "reply_to": reply_to,
                "hold_reason": hold_reason,
            }
        )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _app(tmp_path: Path, gate: _RecordingGate) -> DaemonApplication:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_deep_module_daemon_dependencies(),
    )
    database = tmp_path / "inbox.sqlite3"
    # One slot for both the application and the inbox custody actor, so the
    # send boundary and the ingress/dispatch boundaries cannot disagree.
    slot = app.link_admission
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        service_options={"link_admission": slot},
    )
    app._inbox = DeliveryCustodyFacade(coordinator, database)
    app._inbox_coordinator = coordinator
    app._presence = _Presence()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._adapters = None
    app._user_delivery = _FakeUserDelivery()
    slot.bind(gate)
    app.handle("agent.create", {"name": "alpha"})
    app.handle("agent.create", {"name": "beta"})
    return app


def _pending(message_id: str, *, sender: str = BETA) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-request",
        sender=sender,
        recipient=ALPHA,
        payload=b'{"message":"please answer"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _send_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "actor": "alpha",
        "to": [BETA],
        "summary": "probe",
        "message": "probe",
        "conversationId": "c-send",
        "idempotencyKey": "op-1",
    }
    params.update(overrides)
    return params


# -- W3: message.send wiring ------------------------------------------------


def test_w3_unlinked_agent_send_is_held_with_hold_reason(tmp_path: Path) -> None:
    gate = _RecordingGate(answers=[Mint(BETA)])
    app = _app(tmp_path, gate)

    result = app.handle("message.send", _send_params())

    delivery = result["deliveries"][0]
    assert result["ok"] is True
    assert delivery["accepted"] is True
    assert delivery["queued"] is True
    assert delivery[HOLD_REASON_KEY] == ipc_errors.LINK_MINTING
    assert delivery["detail"] == "minting"
    # A hold code never rides the legacy free-text ``reason`` key (ruling 15).
    assert delivery.get("reason") not in HOLD_REASONS
    # Single target: the hold is lifted to the top level for the carrier.
    assert result["queued"] is True
    assert result[HOLD_REASON_KEY] == ipc_errors.LINK_MINTING

    held = app._inbox.link_held_outbox()
    assert [item.link_hold for item in held] == [ipc_errors.LINK_MINTING]
    assert held[0].message.message_id == delivery["messageId"]

    ledger = app._inbox.link_ledger_snapshot(_now_ms())
    out_rows = [
        row
        for row in ledger
        if row.direction == "out" and row.message_id == delivery["messageId"]
    ]
    assert [row.state for row in out_rows] == ["held"]
    assert out_rows[0].peer == BETA

    assert gate.submitted_calls[-1]["hold_reason"] == ipc_errors.LINK_MINTING
    assert gate.admit_calls[-1]["sender"] == ALPHA
    assert gate.admit_calls[-1]["target"] == BETA


def test_w3_exempt_agent_target_is_not_gated(tmp_path: Path) -> None:
    """A self-send carries no link bookkeeping (§2.2 step 2)."""

    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    result = app.handle("message.send", _send_params(to=[ALPHA]))

    assert result["ok"] is True
    assert gate.admit_calls == []
    assert app._inbox.link_ledger_snapshot(_now_ms()) == ()
    assert app._inbox.link_held_outbox() == ()


def test_w3_user_target_delivers_and_writes_an_open_ledger_row(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    result = app.handle(
        "message.send",
        _send_params(to=[PEER_USER], conversationId="c-user", idempotencyKey="op-u"),
    )

    assert result["ok"] is True
    assert result["deliveries"][0]["accepted"] is True
    assert result["deliveries"][0]["queued"] is False
    # user: targets are never link-governed, but ruling 53 still asks the
    # gate so W can refuse a non-owner request synchronously.
    assert gate.admit_calls[-1]["target"] == PEER_USER

    rows = [
        row
        for row in app._inbox.link_ledger_snapshot(_now_ms())
        if row.direction == "out"
    ]
    assert [row.state for row in rows] == ["open"]
    assert rows[0].peer == PEER_USER
    assert gate.submitted_calls[-1]["hold_reason"] is None
    assert app._inbox.link_held_outbox() == ()


def test_w3_user_target_window_full_is_refused_synchronously(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate(
        error=DaemonRequestError(
            ipc_errors.LINK_WINDOW_FULL,
            "window W is full",
            {"target": PEER_USER, "detail": "W"},
        )
    )
    app = _app(tmp_path, gate)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "message.send",
            _send_params(to=[PEER_USER], idempotencyKey="op-full"),
        )

    assert refused.value.code == ipc_errors.LINK_WINDOW_FULL
    assert app._inbox.outbox_items() == ()
    assert not any(
        row.direction == "out"
        for row in app._inbox.link_ledger_snapshot(_now_ms())
    )
    assert app._inbox.link_held_outbox() == ()


def test_w3_unbound_gate_holds_a_governed_send(tmp_path: Path) -> None:
    """Ruling 54: no bound gate is fail-closed on the sender, not a bypass."""

    app = _app(tmp_path, _RecordingGate())
    app._link_admission = _UnboundSlot(app.link_admission)

    result = app.handle("message.send", _send_params(idempotencyKey="op-unbound"))

    delivery = result["deliveries"][0]
    assert delivery["queued"] is True
    assert delivery[HOLD_REASON_KEY] == ipc_errors.LINK_SELF_FULL
    assert delivery["detail"] == "gate-unavailable"
    assert [item.link_hold for item in app._inbox.link_held_outbox()] == [
        ipc_errors.LINK_SELF_FULL
    ]


class _UnboundSlot:
    """A slot that governs but has no gate, so the boundary holds (§2.4 C2)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def gate(self) -> None:
        return None

    def governs(self, sender: str, recipient: str) -> bool:
        return bool(self._inner.governs(sender, recipient))


# -- W4: message.reply wiring -----------------------------------------------


def test_w4_held_reply_keeps_the_request_pending(tmp_path: Path) -> None:
    gate = _RecordingGate(
        answers=[Hold(ipc_errors.LINK_MINTING, BETA, "awaiting-peer")]
    )
    app = _app(tmp_path, gate)
    assert app._inbox.receive(_pending("req-1")).accepted

    result = app.handle(
        "message.reply",
        {"actor": "alpha", "messageId": "req-1", "summary": "answer", "message": "answer"},
    )

    assert result["ok"] is True
    assert result["queued"] is True
    assert result[HOLD_REASON_KEY] == ipc_errors.LINK_MINTING
    assert result["detail"] == "awaiting-peer"
    assert result["replied"] is False
    assert result["acknowledged"] is False
    # The question stays pending and the reply is parked in the outbox.
    assert [item.message_id for item in app._inbox.pending_messages(ALPHA)] == [
        "req-1"
    ]
    reply_id = reply_message_id("req-1")
    assert [item.link_hold for item in app._inbox.link_held_outbox()] == [
        ipc_errors.LINK_MINTING
    ]
    reply_body = json.loads(
        app._inbox.outbox_item_for_message(reply_id).message.payload
    )
    assert reply_body["replyTo"] == "req-1"
    assert gate.admit_calls[-1]["intent"] == "reply"
    assert gate.admit_calls[-1]["reply_to"] == "req-1"
    assert gate.submitted_calls[-1]["hold_reason"] == ipc_errors.LINK_MINTING
    assert gate.submitted_calls[-1]["reply_to"] == "req-1"


def test_w4_allowed_agent_reply_closes_and_writes_reply_to(tmp_path: Path) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    assert app._inbox.receive(_pending("req-2")).accepted

    result = app.handle(
        "message.reply",
        {"actor": "alpha", "messageId": "req-2", "summary": "answer", "message": "answer"},
    )

    assert result["ok"] is True
    assert result["replied"] is True
    assert result["acknowledged"] is True
    assert app._inbox.pending_messages(ALPHA) == ()
    reply_body = json.loads(
        app._inbox.outbox_item_for_message(reply_message_id("req-2")).message.payload
    )
    assert reply_body["replyTo"] == "req-2"
    assert gate.admit_calls[-1]["intent"] == "reply"
    assert gate.submitted_calls[-1]["reply_to"] == "req-2"
    assert gate.submitted_calls[-1]["hold_reason"] is None


def test_w4_drained_request_answers_link_reclaimed(tmp_path: Path) -> None:
    gate = _RecordingGate(
        error=DaemonRequestError(ipc_errors.LINK_RECLAIMED, "link reclaimed")
    )
    app = _app(tmp_path, gate)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "message.reply",
            {"actor": "alpha", "messageId": "req-gone", "summary": "answer", "message": "answer"},
        )

    assert refused.value.code == ipc_errors.LINK_RECLAIMED
    assert gate.admit_calls[-1]["reply_to"] == "req-gone"


# -- W5: replay --------------------------------------------------------------


def test_w5_held_send_replay_returns_the_receipt_without_re_gating(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate(answers=[Mint(BETA)])
    app = _app(tmp_path, gate)
    params = _send_params(idempotencyKey="op-replay")

    first = app.handle("message.send", params)
    asks_after_first = len(gate.admit_calls)

    replay = app.handle("message.send", params)

    assert replay["messageId"] == first["messageId"]
    assert replay["queued"] is True
    assert replay[HOLD_REASON_KEY] == ipc_errors.LINK_MINTING
    assert len(gate.admit_calls) == asks_after_first
    assert len(app._inbox.link_held_outbox()) == 1


# -- W6: send sources --------------------------------------------------------


def test_w6_cli_sources_ask_the_gate_for_the_resolved_agent(tmp_path: Path) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    bare = app.handle(
        "message.send", _send_params(idempotencyKey="op-bare")
    )
    canonical = app.handle(
        "message.send",
        _send_params(actor=ALPHA, idempotencyKey="op-canonical"),
    )

    assert bare["ok"] is True and canonical["ok"] is True
    assert [call["sender"] for call in gate.admit_calls] == [ALPHA, ALPHA]


def test_w6_unbacked_foreign_sender_is_refused_before_the_gate(
    tmp_path: Path,
) -> None:
    """A foreign canonical sender never reaches the gate or the outbox."""

    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "message.send",
            _send_params(actor=OTHER_AGENT, idempotencyKey="op-foreign"),
        )

    assert refused.value.code == ipc_errors.SENDER_UNRESOLVED
    assert gate.admit_calls == []
    assert app._inbox.outbox_items() == ()
    assert app._inbox.link_held_outbox() == ()


# -- Rework 1 (wp2b-acceptance-1): held replies settle their original -------


def test_w4b_held_reply_release_acks_the_original_end_to_end(
    tmp_path: Path,
) -> None:
    """reply(Hold) -> update_link_ledger(release) -> the request is settled.

    P1 flagged the seam: the held reply books no ledger ``out`` row, so the
    release used to un-park the reply while the question stayed pending
    forever.  The release must ack the original from the outbox row's own
    association.
    """

    gate = _RecordingGate(
        answers=[Hold(ipc_errors.LINK_PEER_FULL, BETA, "peer-full")]
    )
    app = _app(tmp_path, gate)
    assert app._inbox.receive(_pending("req-h")).accepted

    held = app.handle(
        "message.reply",
        {"actor": "alpha", "messageId": "req-h", "summary": "answer", "message": "answer"},
    )
    assert held["ok"] is True
    assert held["queued"] is True
    assert held["acknowledged"] is False
    reply_id = held["replyMessageId"]

    result = app._inbox.update_link_ledger(
        UpdateLinkLedgerCommand(
            correlation_id="test:release-held-reply", release=(reply_id,)
        )
    )

    assert result.released == (reply_id,)
    # The original question is settled with the release, in one command.
    assert app._inbox.pending_messages(ALPHA) == ()
    released_row = app._inbox.outbox_item_for_message(reply_id)
    assert released_row is not None
    assert released_row.link_hold is None


# -- Rework 2: a user: replay never asks the gate a second time (B6) --------


def test_w5_user_target_replay_returns_the_original_result_without_re_gating(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    params = _send_params(
        to=[PEER_USER], conversationId="c-user", idempotencyKey="op-u-replay"
    )

    first = app.handle("message.send", params)
    asks_after_first = len(gate.admit_calls)
    gate.error = DaemonRequestError(
        ipc_errors.LINK_WINDOW_FULL,
        "window W is full",
        {"target": PEER_USER, "detail": "W"},
    )

    replay = app.handle("message.send", params)

    # Same key after W went full: the original result comes back, not the
    # gate's new refusal; no second ask, no second submitted notification.
    assert replay["ok"] is True
    assert (
        replay["deliveries"][0]["messageId"]
        == first["deliveries"][0]["messageId"]
    )
    assert replay["deliveries"][0]["accepted"] is True
    assert replay["deliveries"][0]["duplicate"] is True
    assert len(gate.admit_calls) == asks_after_first
    assert len(gate.submitted_calls) == 1
    rows = [
        row
        for row in app._inbox.link_ledger_snapshot(_now_ms())
        if row.direction == "out"
    ]
    assert len(rows) == 1
    assert rows[0].state == "open"


# -- Rework 3: user: ledger rows never outlive their request (§2.4 step 6) --


class _ExplodingUserDelivery:
    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        raise RuntimeError("transport exploded mid-delivery")


def test_w3_user_delivery_failure_releases_the_ledger_row(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    app._user_delivery = _ExplodingUserDelivery()

    with pytest.raises(RuntimeError, match="transport exploded"):
        app.handle(
            "message.send",
            _send_params(to=[PEER_USER], idempotencyKey="op-u-boom"),
        )

    # A failed delivery releases its ``out`` row in the same request
    # instead of leaking an open row that only the TTL purge would drop.
    assert not any(
        row.direction == "out"
        for row in app._inbox.link_ledger_snapshot(_now_ms())
    )


def test_w3_user_target_parse_failure_releases_the_ledger_row(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "message.send",
            _send_params(to=["user: "], idempotencyKey="op-u-parse"),
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert not any(
        row.direction == "out"
        for row in app._inbox.link_ledger_snapshot(_now_ms())
    )


def test_w3_user_ledger_rows_are_booked_only_for_local_agent_senders(
    tmp_path: Path,
) -> None:
    """Acceptance §六: adapter and foreign senders leave no ledger rows."""

    gate = _RecordingGate()
    app = _app(tmp_path, gate)

    local = app._link_user_plan(
        sender=ALPHA,
        target=PEER_USER,
        intent="request",
        reply_to=None,
        message_id="m-local",
    )
    foreign = app._link_user_plan(
        sender=OTHER_AGENT,
        target=PEER_USER,
        intent="request",
        reply_to=None,
        message_id="m-foreign",
    )
    adapter = app._link_user_plan(
        sender="adapter:lark:bot",
        target=PEER_USER,
        intent="request",
        reply_to=None,
        message_id="m-adapter",
    )

    assert local.ledger_open is not None
    assert foreign.ledger_open is None
    assert adapter.ledger_open is None
    # All three still measured W through the gate (ruling 53).
    assert [call["sender"] for call in gate.admit_calls] == [
        ALPHA,
        OTHER_AGENT,
        "adapter:lark:bot",
    ]


# -- Rework 4: a user: replay is recognised by user_delivery's duplicate ----
# result (§2.4 step 2), never by "an out row exists" (that row is written
# before delivery).


class _Crash(BaseException):
    """A process-death stand-in: not an ``Exception``, so nothing releases."""


def _user_params(key: str, **overrides: Any) -> dict[str, Any]:
    return _send_params(to=[PEER_USER], conversationId="c-user", idempotencyKey=key, **overrides)


def _window_full() -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.LINK_WINDOW_FULL, "window W is full", {"detail": "W"}
    )


def test_w5_replay_during_a_still_running_first_send_does_not_claim_success(
    tmp_path: Path,
) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    delivery: _FakeUserDelivery = app._user_delivery  # type: ignore[assignment]
    entered = threading.Event()
    proceed = threading.Event()

    def stall_then_fail(_request: UserDeliveryRequest) -> None:
        if not entered.is_set():
            entered.set()
            assert proceed.wait(10)
            raise RuntimeError("first delivery failed")

    delivery.before_send = stall_then_fail
    params = _user_params("op-u-race")
    first_error: list[BaseException] = []

    def first() -> None:
        try:
            app.handle("message.send", params)
        except BaseException as error:  # noqa: BLE001
            first_error.append(error)

    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert entered.wait(10)
    retry_result: list[Any] = []
    retry_thread = threading.Thread(
        target=lambda: retry_result.append(app.handle("message.send", params))
    )
    retry_thread.start()
    retry_thread.join(0.5)

    # The first send has delivered nothing: the retry must not have reported
    # success while it is still in flight.
    assert not retry_result
    assert delivery.sent == []
    proceed.set()
    first_thread.join(10)
    retry_thread.join(10)
    assert first_error and "first delivery failed" in str(first_error[0])
    # The retry then performs the real delivery itself.
    assert len(delivery.sent) == 1
    assert retry_result[0]["deliveries"][0]["accepted"] is True
    assert "duplicate" not in retry_result[0]["deliveries"][0]


def test_w5_replay_of_an_interrupted_first_send_delivers(tmp_path: Path) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    delivery: _FakeUserDelivery = app._user_delivery  # type: ignore[assignment]

    def crash(_request: UserDeliveryRequest) -> None:
        raise _Crash

    delivery.before_send = crash
    params = _user_params("op-u-crash")
    with pytest.raises(_Crash):
        app.handle("message.send", params)
    # The row was written before delivery and is still there.
    assert any(
        row.direction == "out" for row in app._inbox.link_ledger_snapshot(_now_ms())
    )
    delivery.before_send = None
    asks = len(gate.admit_calls)

    replay = app.handle("message.send", params)

    assert replay["deliveries"][0]["accepted"] is True
    assert len(delivery.sent) == 1
    assert len(gate.admit_calls) == asks
    assert len(gate.submitted_calls) == 1


def test_w5_replay_carries_the_original_native_message_id(tmp_path: Path) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    params = _user_params("op-u-native")
    first = app.handle("message.send", params)["deliveries"][0]
    asks = len(gate.admit_calls)
    gate.error = _window_full()

    replay = app.handle("message.send", params)["deliveries"][0]

    assert first["nativeMessageId"]
    assert replay["nativeMessageId"] == first["nativeMessageId"]
    assert replay["receiverAdapter"] is True
    assert replay["duplicate"] is True
    assert len(gate.admit_calls) == asks
    assert len(app._user_delivery.sent) == 1  # type: ignore[attr-defined]


def test_w5_replayed_user_reply_asks_the_gate_once(tmp_path: Path) -> None:
    gate = _RecordingGate()
    app = _app(tmp_path, gate)
    assert app._inbox.receive(_pending("req-u", sender=BETA)).accepted
    # The original's inbound ledger row (written by the link service on
    # injection in production; this gate double is not a ledger sink).
    with contextlib.closing(sqlite3.connect(tmp_path / "inbox.sqlite3")) as db:
        db.execute(
            "INSERT INTO link_ledger (actor, message_id, direction, peer, "
            "intent, reply_to, state, opened_at_ms, expires_at_ms) "
            "VALUES (?, 'req-u', 'in', ?, 'request', NULL, 'open', ?, ?)",
            (ALPHA, BETA, _now_ms(), _now_ms() + 600_000),
        )
        db.commit()
    params = _user_params("op-u-reply", replyTo="req-u")

    app.handle("message.send", params)
    asks = len(gate.admit_calls)
    gate.error = _window_full()
    replay = app.handle("message.send", params)

    assert replay["ok"] is True
    assert replay["deliveries"][0]["duplicate"] is True
    assert len(gate.admit_calls) == asks
    assert len(gate.submitted_calls) == 1
    assert len(app._user_delivery.sent) == 1  # type: ignore[attr-defined]
