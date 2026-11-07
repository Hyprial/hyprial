"""WP4 §2.4 / review B4: ingress LINK_UNKNOWN refusal and its deferred notice."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.dispatch.identity import (
    DISPATCH_SERVICE_ACTOR_NAME,
    dispatch_service_actor_uri,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.pull import TerminalState
from hyprial.daemon.impl.inbox.service import ConsumptionState
from hyprial.kernel import canonical_agent_uri, ipc_errors
from tests.support.links.gate import ScriptedLinkGate, permissive_admission_slot

NODE = "node-b"
OWNER = "op"
PEER = canonical_agent_uri("op", "peer-node", "alice")
ME = canonical_agent_uri(OWNER, NODE, "bob")
# README rulings 45/51: the mechanism exemption is keyed on the reserved ACTOR
# NAME, never on the owner or machine segment.
TURN_HOOKS_ACTOR_NAME = "_turn-hooks"
RESERVED_ACTORS = frozenset({DISPATCH_SERVICE_ACTOR_NAME, TURN_HOOKS_ACTOR_NAME})
INTERNAL_URIS = tuple(
    canonical_agent_uri(owner, machine, name)
    for owner, machine in ((OWNER, NODE), ("other", "elsewhere"))
    for name in sorted(RESERVED_ACTORS)
)


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(message_id: str, *, sender: str = PEER, recipient: str = ME,
             intent: str = "request") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"message": "hi"}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


@pytest.fixture
def assembled(tmp_path: Path) -> Iterator[
    tuple[DeliveryCustodyFacade, ScriptedLinkGate, MemoryDeliveryTransport]
]:
    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    gate = ScriptedLinkGate(known=set())
    slot.bind(gate)
    transport = MemoryDeliveryTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        _Events(),
        node_id=NODE,
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    try:
        yield facade, gate, transport
    finally:
        facade.close()


def test_unknown_sender_is_refused_and_recorded(assembled) -> None:
    facade, gate, transport = assembled

    result = facade.receive(_message("m-refused"), now_ms=10)

    assert result.accepted is False
    assert result.acknowledged is False
    assert result.code == ipc_errors.LINK_UNKNOWN
    assert facade.has_received("m-refused") is False
    record = facade.delivery_status_for_recipient(ME, "m-refused")
    assert record is not None
    assert record.state.value == "refused"
    assert record.reason == ipc_errors.LINK_UNKNOWN
    assert record.holder == NODE
    assert facade.consumption_state("m-refused") is ConsumptionState.REFUSED

    # The remote notice is handed to the I/O worker, never sent from the actor
    # thread, and carries a bounded summary plus originalMessageId/reason.
    assert len(transport.notices) == 1
    target_node, notice = transport.notices[0]
    assert target_node == "peer-node"
    body = json.loads(notice.payload)
    assert body["reason"] == ipc_errors.LINK_UNKNOWN
    assert body["originalMessageId"] == "m-refused"
    assert body["systemNotice"] is True
    assert notice.summary and len(notice.summary.encode("utf-8")) <= 512
    assert gate.sender_known_calls() == [(ME, PEER)]


def test_refusal_is_idempotent_and_heals(assembled) -> None:
    facade, gate, transport = assembled

    facade.receive(_message("m-heal"), now_ms=10)
    first = facade.delivery_status_for_recipient(ME, "m-heal")
    facade.receive(_message("m-heal"), now_ms=99)
    second = facade.delivery_status_for_recipient(ME, "m-heal")
    assert first is not None and second is not None
    assert second.recorded_at_ms == first.recorded_at_ms
    # Two refusals emit two notices with the SAME deterministic id; the
    # receiving node dedups them by id (asserted on the emitted ids).
    assert len(transport.notices) == 2
    assert (
        transport.notices[0][1].message_id == transport.notices[1][1].message_id
    )

    gate.known.add(PEER)
    accepted = facade.receive(_message("m-heal"), now_ms=200)
    assert accepted.accepted is True
    healed = facade.delivery_status_for_recipient(ME, "m-heal")
    assert healed is not None and healed.state.value == "fetched"

    # The two refusal retries reuse one deterministic notice id, so the
    # receiving node dedups them (asserted on the emitted notice, not just on
    # the construction).
    assert transport.notices[0][1].idempotency_key == "link-unknown:m-heal"


@pytest.mark.parametrize(
    "sender",
    [
        f"user:{OWNER}",
        "adapter:lark:main",
        dispatch_service_actor_uri(OWNER, NODE),
        canonical_agent_uri(OWNER, NODE, "_turn-hooks"),
        # README rulings 45/51: a reserved name on a REMOTE node has no link
        # file either, so ingress must not ask ``sender_known``.
        canonical_agent_uri("other", "elsewhere", "dispatch-coordinator"),
        canonical_agent_uri("other", "elsewhere", "_turn-hooks"),
    ],
)
def test_owner_route_system_and_mechanism_bypass_ingress(assembled, sender: str) -> None:
    facade, gate, _transport = assembled

    result = facade.receive(_message(f"m-{sender}", sender=sender), now_ms=10)

    assert result.accepted is True
    assert gate.sender_known_calls() == []


def test_internal_recipient_bypasses_ingress(assembled) -> None:
    """README ruling 51: mail TO a mechanism actor is not link-governed."""

    facade, gate, _transport = assembled

    for index, internal in enumerate(INTERNAL_URIS):
        result = facade.receive(
            _message(f"m-to-internal-{index}", sender=PEER, recipient=internal),
            now_ms=10,
        )
        assert result.accepted is True, internal

    assert gate.sender_known_calls() == []


def test_known_sender_and_duplicates_pass(assembled) -> None:
    facade, gate, _transport = assembled
    gate.known.add(PEER)

    assert facade.receive(_message("m-known"), now_ms=10).accepted is True
    assert gate.sender_known_calls() == [(ME, PEER)]

    gate.known.clear()
    duplicate = facade.receive(_message("m-known"), now_ms=20)
    assert duplicate.duplicate is True
    assert duplicate.acknowledged is True
    # The dedup check runs before the link gate, so the retry is idempotent.
    assert gate.sender_known_calls() == [(ME, PEER)]


class _FailingNoticeTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        del node, message
        self.attempts += 1
        raise RuntimeError("notice transport down")


def test_rolled_back_refusal_emits_no_notice(tmp_path: Path) -> None:
    """review B4.4: the notice follows a COMMITTED refusal, never a rollback."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    slot.bind(ScriptedLinkGate(known=set()))
    service = InboxService(
        tmp_path / "rollback.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        link_admission=slot,
    )
    original = service._record_terminal

    def failing_terminal(message, **fields: object):
        if fields.get("state") is TerminalState.REFUSED:
            raise RuntimeError("terminal write failed")
        return original(message, **fields)

    service._record_terminal = failing_terminal  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            service.receive(_message("m-rollback"), now_ms=10)
        # The transaction rolled back, so neither the record nor the notice
        # may exist.
        assert service.take_pending_link_notices() == ()
        assert service.delivery_status_for_recipient(ME, "m-rollback") is None
    finally:
        service.close()


def test_refusal_notice_dedups_at_the_receiving_node(
    assembled, tmp_path: Path
) -> None:
    """review B4.5: dedup is proven on the RECEIVER's result, not the id."""

    facade, _gate, transport = assembled
    facade.receive(_message("m-notice-dedup"), now_ms=10)
    facade.receive(_message("m-notice-dedup"), now_ms=11)
    notices = [notice for _node, notice in transport.notices]
    assert len(notices) == 2
    assert notices[0].message_id == notices[1].message_id

    # Deliver both refusals' notices to the sender's node: the deterministic
    # id makes the receiver answer duplicate=True, so retries do not pile up.
    receiving = InboxService(
        tmp_path / "receiving.sqlite3",
        MemoryDeliveryTransport(),
        node_id="peer-node",
        link_admission=permissive_admission_slot(),
    )
    try:
        first = receiving.receive(notices[0], now_ms=20)
        second = receiving.receive(notices[1], now_ms=21)
        assert first.accepted is True
        assert second.duplicate is True and second.acknowledged is True
    finally:
        receiving.close()


def test_many_refusals_never_drop_a_notice(tmp_path: Path) -> None:
    """review B4.5: an unbounded stage cannot silently lose a refusal notice."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    slot.bind(ScriptedLinkGate(known=set()))
    service = InboxService(
        tmp_path / "many.sqlite3",
        MemoryDeliveryTransport(),
        node_id=NODE,
        link_admission=slot,
    )
    try:
        for index in range(300):
            assert service.receive(
                _message(f"m-many-{index}"), now_ms=index + 1
            ).accepted is False
        staged = service.take_pending_link_notices()
        assert len(staged) == 300
    finally:
        service.close()


def test_notice_io_failure_keeps_the_refused_record(tmp_path: Path) -> None:
    """review B4.4: a failed notice I/O never loses the committed refusal."""

    slot = LinkAdmission(internal_actors=RESERVED_ACTORS)
    slot.bind(ScriptedLinkGate(known=set()))
    transport = _FailingNoticeTransport()
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        _Events(),
        node_id=NODE,
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        result = facade.receive(_message("m-notice-fail"), now_ms=10)
        assert result.code == ipc_errors.LINK_UNKNOWN
        # The refusal committed before the notice was staged; the transport
        # failure cannot roll it back.
        record = facade.delivery_status_for_recipient(ME, "m-notice-fail")
        assert record is not None and record.state.value == "refused"
        assert facade.consumption_state("m-notice-fail") is ConsumptionState.REFUSED
        assert transport.attempts >= 1
    finally:
        facade.close()
