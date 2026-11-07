"""WP2 gate tests (spec §4.2): admit_send, sender_known, admit_dispatch.

Every case drives the real ``LinkService`` over the doubles in
``tests/support/links``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.composition.links import LocalIdentity
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.links.model import LinkEntry, LinkFile, link_file_path
from hyprial.daemon.impl.links.ports import (
    Allow,
    Hold,
    LinkAdmin,
    LinkChangeSource,
    LinkGate,
    LinkQueries,
    LinkSendObserver,
    Mint,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.runtime import DaemonEventBridge

from tests.support.links.ledger import FakeInboxPort
from hyprial.daemon.impl.links.service import RESERVATION_TTL_MS

from tests.support.links.org import (
    BlockPoint,
    FakeOrgFs,
    LogRecorder,
    establish,
    establish_many,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
X = "agent:p:n2:x"
XS = "agent:o:n2:s"
OWNER = "user:o"
U = "user:p"
COORD = "agent:o:n1:dispatch-coordinator"
RCOORD = "agent:p:n2:dispatch-coordinator"
RHOOK = "agent:p:n2:_turn-hooks"


def harness(*, spaces: tuple[str, ...] = ("s1",), **kwargs):
    fs = FakeOrgFs()
    for space in spaces:
        fs.add_space(space)
    return make_service(fs=fs, **kwargs)


def _view_of(h):

    with h.service._mem:
        return h.service._view(A, h.clock.now_ms())


# -- G1 ------------------------------------------------------------------


def test_g1_without_org_only_exempt_targets_are_allowed() -> None:
    h = make_service(fs=FakeOrgFs())
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(ipc_errors.LINK_SELF_FULL, B, "org-unavailable")
    assert h.service.admit_send(
        sender=A, target=OWNER, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.admit_send(
        sender=A, target=U, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.admit_send(
        sender=A, target="route:lark:x", intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.sender_known(recipient=B, sender=X) is False
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=X, message_id="m1", now_ms=now
        )
        is False
    )


# -- G2 ------------------------------------------------------------------

def test_g2_user_targets_never_mint_and_count_towards_w() -> None:
    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    before = sorted(h.store.index().files[A].entries)
    assert h.service.admit_send(
        sender=A, target=U, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.admit_send(
        sender=A, target=OWNER, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    h.tick()
    assert sorted(h.store.index().files[A].entries) == before
    assert h.service.top_fields(A)["outboundInFlight"] == 0
    h.service.submitted(
        sender=A, target=U, message_id="mu", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.top_fields(A)["outboundInFlight"] == 1
    h.service.submitted(
        sender=A, target=OWNER, message_id="mo", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.top_fields(A)["outboundInFlight"] == 2
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "W")


def test_g2_non_owner_user_target_is_refused_when_w_is_full() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=U, message_id="mu1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    h.service.submitted(
        sender=A, target=U, message_id="mu2", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    with pytest.raises(DaemonRequestError) as refusal:
        h.service.admit_send(
            sender=A, target=U, intent="request", reply_to=None, now_ms=now
        )
    assert refusal.value.code == ipc_errors.LINK_WINDOW_FULL
    assert refusal.value.data["detail"] == "W"


def test_g2_owner_target_is_never_blocked() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=U, message_id="mu1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    h.service.submitted(
        sender=A, target=U, message_id="mu2", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.admit_send(
        sender=A, target=OWNER, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)


# -- G3 ------------------------------------------------------------------


def test_g3_non_local_senders_are_refused_not_silently_allowed() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    with pytest.raises(DaemonRequestError) as remote:
        h.service.admit_send(
            sender=X, target=B, intent="request", reply_to=None, now_ms=now
        )
    assert remote.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    with pytest.raises(DaemonRequestError) as sibling:
        h.service.admit_send(
            sender=XS, target=B, intent="request", reply_to=None, now_ms=now
        )
    assert sibling.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert h.store.intents() == {}
    assert h.service.admit_send(
        sender=OWNER, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.admit_send(
        sender="adapter:lark:x", target=B, intent="request", reply_to=None,
        now_ms=now,
    ) == Allow(peer=None)
    # C is a local agent that is offline and unhosted; it is still managed.
    assert h.service.admit_send(
        sender=C, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)


# -- G4 ------------------------------------------------------------------


def test_g4_internal_actors_are_exempt_by_name_on_every_node() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    for target in (COORD, RCOORD, RHOOK):
        assert h.service.admit_send(
            sender=A, target=target, intent="request", reply_to=None, now_ms=now
        ) == Allow(peer=None)
    assert h.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=X)
    assert h.service.sender_known(recipient=A, sender=RCOORD) is True
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=RCOORD, message_id="m1", now_ms=now
        )
        is True
    )
    h.tick()
    assert h.service.top_fields(A)["outboundInFlight"] == 0


# -- G5 ------------------------------------------------------------------


def test_g5_reply_validation_precedes_exemption() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    inbox = h.inbox
    inbox.put_in(A, "m1", peer=U, state="open", opened_at_ms=now)
    inbox.put_in(A, "m2", peer=OWNER, state="open", opened_at_ms=now)
    inbox.put_in(A, "m3", peer=B, state="open", opened_at_ms=now)
    inbox.put_in(A, "m4", peer=COORD, state="drained", opened_at_ms=now)
    h.tick()
    with pytest.raises(DaemonRequestError) as unknown:
        h.service.admit_send(
            sender=A, target=U, intent="reply", reply_to="ghost", now_ms=now
        )
    assert unknown.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE
    with pytest.raises(DaemonRequestError) as wrong:
        h.service.admit_send(
            sender=A, target=OWNER, intent="reply", reply_to="m1", now_ms=now
        )
    assert wrong.value.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(DaemonRequestError) as drained:
        h.service.admit_send(
            sender=A, target=COORD, intent="reply", reply_to="m4", now_ms=now
        )
    assert drained.value.code == ipc_errors.LINK_RECLAIMED
    # A route target is not compared against the original requester.
    assert h.service.admit_send(
        sender=A, target="route:lark:x", intent="reply", reply_to="m3", now_ms=now
    ) == Allow(peer=None)
    # A request the memory has not reconciled yet is read from the inbox once.
    inbox.queue_inbound(A, "m5", sender=B, received_at_ms=now)
    before = inbox.inbound_row_calls
    assert h.service.admit_send(
        sender=A, target=B, intent="reply", reply_to="m5", now_ms=now
    ) in (Allow(peer=B), Hold(ipc_errors.LINK_MINTING, B, "awaiting-peer"))
    assert inbox.inbound_row_calls == before + 1
    assert h.service.admit_send(
        sender=A, target=OWNER, intent="reply", reply_to="m2", now_ms=now
    ) == Allow(peer=None)


# -- G6 ------------------------------------------------------------------


def test_g6_same_peer_fifo_returns_the_head_hold() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    establish(h.fs, A, B)
    h.tick()
    # The observer registers a held request for (A, B).  The link is already
    # effective and W has room, so without the FIFO rule the next request would
    # be admitted instead.
    h.service.submitted(
        sender=A, target=B, message_id="h1", reply_to=None,
        hold_reason=ipc_errors.LINK_MINTING, now_ms=now,
    )
    decision = h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    )
    assert isinstance(decision, Hold)
    assert decision.reason == ipc_errors.LINK_MINTING
    assert decision.peer == B


# -- G7 ------------------------------------------------------------------


def test_g7_reservations_expire_and_the_observer_is_idempotent() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    # Reservations to a user target count towards W and never hit cwnd.
    for _ in range(2):
        assert h.service.admit_send(
            sender=A, target=U, intent="request", reply_to=None, now_ms=now
        ) == Allow(peer=None)
    with pytest.raises(DaemonRequestError) as full:
        h.service.admit_send(
            sender=A, target=U, intent="request", reply_to=None, now_ms=now
        )
    assert full.value.code == ipc_errors.LINK_WINDOW_FULL
    # An unconfirmed reservation is dropped after RESERVATION_TTL_MS.
    h.clock.advance(RESERVATION_TTL_MS)
    later = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=U, intent="request", reply_to=None, now_ms=later
    ) == Allow(peer=None)
    # Replaying the observer never double-counts.
    for _ in range(2):
        h.service.submitted(
            sender=A, target=U, message_id="mu", reply_to=None,
            hold_reason=None, now_ms=later,
        )
        h.service.submitted(
            sender=A, target=U, message_id="mx", reply_to=None,
            hold_reason=ipc_errors.LINK_MINTING, now_ms=later,
        )
    with h.service._mem:
        assert len(h.service._states[A].outbound) == 1
        assert len(h.service._states[A].held_pending) == 1
    # One more open item fills W, so a managed target reports the W bound.
    h.service.submitted(
        sender=A, target=U, message_id="mu2", reply_to=None,
        hold_reason=None, now_ms=later,
    )
    establish(h.fs, A, B)
    h.tick()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=later
    ) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "W")


# -- G8 ------------------------------------------------------------------


def test_g8_sender_known_uses_committed_copies_only() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    # B is a peer here: a local agent always republishes its own empty file.
    h = make_service(fs=fs, local_names=("a", "c"))
    # C's file exists only as a pending intent until the first flush.
    h.store.write(LinkFile(actor=C, revision=0, entries={}))
    assert h.service.sender_known(recipient=A, sender=C) is False
    before = len(fs.calls)
    h.service.sender_known(recipient=A, sender=C)
    assert len(fs.calls) == before
    h.tick()
    assert h.service.sender_known(recipient=A, sender=C) is True
    establish(fs, A, B)
    h.tick()
    assert h.service.sender_known(recipient=A, sender=B) is True
    fs.remove("s1", "link/agent:o:n1:b.json")
    h.tick()
    assert B in h.store.index().absent
    assert h.service.sender_known(recipient=A, sender=B) is True
    assert h.service.sender_known(recipient=A, sender=X) is False


# -- G9 ------------------------------------------------------------------


def test_g9_dispatch_admission_is_idempotent_and_survives_a_rebuild() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    inbox = FakeInboxPort()
    h = make_service(fs=fs, inbox=inbox)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    inbox.inject(A, "seed", sender=B, received_at_ms=now)
    h.tick()
    assert (
        h.service.admit_dispatch(recipient=A, sender=B, message_id="m1", now_ms=now)
        is True
    )
    assert (
        h.service.admit_dispatch(recipient=A, sender=B, message_id="m2", now_ms=now)
        is False
    )
    assert (
        h.service.admit_dispatch(recipient=A, sender=B, message_id="m1", now_ms=now)
        is True
    )
    inbox.put_in(A, "m1", peer=B, state="open", opened_at_ms=now)
    # Drop the service without close(): a fresh one rebuilds from the ledger.
    rebuilt = make_service(fs=fs, inbox=inbox)
    rebuilt.tick()
    assert (
        rebuilt.service.admit_dispatch(
            recipient=A, sender=B, message_id="m1", now_ms=now
        )
        is True
    )


# -- G10 -----------------------------------------------------------------


def test_g10_reply_exemption_and_reply_intent_accounting() -> None:
    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    # One open outbound to B and one pending inbound from B fill W exactly.
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    h.service.injected(
        actor=A, message_id="seed", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    # W is full; a reply matching an outbound this node opened is exempt.
    h.service.reply_received(
        actor=A, reply_message_id="r1", reply_to="m1", sender=B,
        received_at_ms=now + 5,
    )
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="r1", now_ms=now,
            intent="reply", reply_to="m1",
        )
        is True
    )
    # A correlation naming another peer's outbound does not exempt, and it must
    # not close the other peer's request either.
    h.service.submitted(
        sender=A, target=C, message_id="mc", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="rk", now_ms=now,
            intent="reply", reply_to="mc",
        )
        is False
    )
    with h.service._mem:
        state = h.service._states[A]
        assert any(item.message_id == "mc" for item in state.outbound)
        # reply-intent without a correlation id still consumes W.
        state.outbound.clear()
        state.pending_inbound.clear()
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="p1", now_ms=now, intent="reply"
        )
        is True
    )
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="p2", now_ms=now, intent="reply"
        )
        is True
    )
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="p3", now_ms=now, intent="reply"
        )
        is False
    )
    with h.service._mem:
        assert len(h.service._states[A].pending_inbound) == 2


# -- G11 -----------------------------------------------------------------


def test_g11_settle_hint_keeps_unreconciled_requests() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.settle_hint(actor=A, message_id="unknown") == "keep"
    h.inbox.put_in(A, "m1", peer=B, state="drained", opened_at_ms=now)
    h.tick()
    assert h.service.settle_hint(actor=A, message_id="m1") == "ack"


# -- G12 -----------------------------------------------------------------


class _ThreadLocalPorts:
    """A ``LinkGate`` proxy that records the thread of every in-memory call.

    It also publishes the first observed thread so the doubles can raise if
    any ``FakeOrgFs``/``FakeInboxPort`` operation is attempted from it.
    """

    def __init__(self, service: object, holder: list[int | None]) -> None:
        self._service = service
        self._holder = holder
        self.threads: dict[str, set[int]] = {}

    def _record(self, name: str) -> None:
        ident = threading.get_ident()
        self.threads.setdefault(name, set()).add(ident)
        self._holder[0] = ident

    def admit_send(self, **kwargs: object) -> object:
        self._record("admit_send")
        return self._service.admit_send(**kwargs)  # type: ignore[attr-defined]

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        self._record("sender_known")
        return self._service.sender_known(  # type: ignore[attr-defined]
            recipient=recipient, sender=sender
        )

    def admit_dispatch(self, **kwargs: object) -> bool:
        self._record("admit_dispatch")
        return self._service.admit_dispatch(**kwargs)  # type: ignore[attr-defined]

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        self._record("settle_hint")
        return self._service.settle_hint(  # type: ignore[attr-defined]
            actor=actor, message_id=message_id
        )


class _ThreadTransport:
    def close(self) -> None:
        return None


class _ThreadHarnesses:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def drain_results(self) -> tuple[object, ...]:
        return ()

    def streaming_actors(self) -> tuple[str, ...]:
        return ("bob",)

    def dispatch(self, actor: str, delivery: object) -> bool:
        self.dispatched.append(delivery.delivery_id)  # type: ignore[attr-defined]
        return True


class _ThreadEvents:
    def publish(self, _event: object) -> None:
        pass


def test_g12_gate_calls_stay_on_the_inbox_actor_thread(tmp_path: Path) -> None:
    me = "agent:o:n1:bob"
    peer = "agent:o:n1:alice"
    tripped: list[str] = []
    holder: list[int | None] = [None]

    def guard(op: str) -> None:
        if holder[0] is not None and threading.get_ident() == holder[0]:
            tripped.append(op)

    fs = FakeOrgFs(guard=guard)
    fs.add_space("s1")
    inbox = FakeInboxPort(guard=guard)
    now = 1_000_000
    write_reference(
        fs, peer, {me: LinkEntry(peer=me, minted_at_ms=now, revision=1)}, revision=1
    )
    write_reference(
        fs, me, {peer: LinkEntry(peer=peer, minted_at_ms=now, revision=1)}, revision=1
    )
    h = make_service(fs=fs, inbox=inbox, local_names=("bob",))
    h.tick(now)
    proxy = _ThreadLocalPorts(h.service, holder)
    slot = LinkAdmission()
    slot.bind(proxy)  # type: ignore[arg-type]

    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _ThreadEvents(),
        node_id="n1",
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    harnesses = _ThreadHarnesses()
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="n1",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_ThreadTransport(),  # type: ignore[arg-type]
        inbox=facade,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: me,
    )
    message = InboxMessage(
        message_id="m-thread",
        conversation_id="c-thread",
        sender=peer,
        recipient=me,
        payload=json.dumps({"message": "hi"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    try:
        assert facade.receive(message).accepted
        bridge._dispatch_harness_deliveries()
    finally:
        facade.close()

    assert "sender_known" in proxy.threads
    assert "admit_dispatch" in proxy.threads
    idents = set().union(*proxy.threads.values())
    assert threading.get_ident() not in idents
    assert len(idents) == 1, idents
    assert tripped == [], tripped
    assert harnesses.dispatched == ["m-thread"]


# -- G13 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    ["orgfs_read", "orgfs_write", "ledger_snapshot", "apply"],
)
def test_g13_service_io_never_blocks_the_gate(blocked: str) -> None:
    read = BlockPoint()
    write = BlockPoint()
    ledger = BlockPoint()
    apply_block = BlockPoint()
    fs = FakeOrgFs(
        read_block=read if blocked == "orgfs_read" else None,
        write_block=write if blocked == "orgfs_write" else None,
    )
    fs.add_space("s1")
    inbox = FakeInboxPort(
        ledger_block=ledger if blocked == "ledger_snapshot" else None,
        apply_block=apply_block if blocked == "apply" else None,
    )
    h = make_service(fs=fs, inbox=inbox)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    if blocked == "orgfs_write":
        h.service.add(actor=A, peer=C, now_ms=now)
    block = {
        "orgfs_read": read,
        "orgfs_write": write,
        "ledger_snapshot": ledger,
        "apply": apply_block,
    }[blocked]
    block.reset()
    ticker = threading.Thread(target=lambda: h.service.tick(now), daemon=True)
    ticker.start()
    assert block.wait_entered(5.0), f"service never reached {blocked}"
    probe_done = threading.Event()

    def probe() -> None:
        h.service.sender_known(recipient=A, sender=B)
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="probe", now_ms=now
        )
        probe_done.set()

    threading.Thread(target=probe, daemon=True).start()
    assert probe_done.wait(2.0), f"the gate blocked behind {blocked} I/O"
    block.go()
    ticker.join(5.0)


# -- G14 -----------------------------------------------------------------


def test_g14_one_slot_is_shared_across_concurrent_admissions() -> None:
    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    h.service.injected(
        actor=A, message_id="seed", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    barrier = threading.Barrier(3)
    results: dict[str, bool] = {}

    def probe(message_id: str) -> None:
        barrier.wait()
        results[message_id] = h.service.admit_dispatch(
            recipient=A, sender=B, message_id=message_id, now_ms=now
        )

    threads = [
        threading.Thread(target=probe, args=("m1",)),
        threading.Thread(target=probe, args=("m2",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5.0)
    assert sorted(results.values()) == [False, True]


def test_g14_multi_target_send_uses_the_remaining_slot_once() -> None:
    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    h.service.injected(
        actor=A, message_id="seed", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    first = h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    )
    second = h.service.admit_send(
        sender=A, target=C, intent="request", reply_to=None, now_ms=now
    )
    assert first == Allow(peer=B)
    assert second == Hold(ipc_errors.LINK_WINDOW_FULL, C, "W")


# -- G15 -----------------------------------------------------------------


def test_g15_service_satisfies_every_seam_and_notifies_safely() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    service = h.service
    for protocol in (
        LinkGate,
        LinkSendObserver,
        LinkQueries,
        LinkAdmin,
        LinkChangeSource,
    ):
        assert isinstance(service, protocol)
    calls: list[int] = []
    unsubscribe = service.subscribe(lambda: calls.append(service.list_links(A)["k"]))
    service.drop(actor=A, peer=B, now_ms=now)
    h.tick(now + 1_000)
    assert len(calls) >= 1
    unsubscribe()
    frozen = len(calls)
    service.add(actor=A, peer=B, now_ms=now)
    h.tick(now + 2_000)
    service.drop(actor=A, peer=B, now_ms=now)
    h.tick(now + 3_000)
    assert len(calls) == frozen


# -- H1: the switch also governs the non-owner user: W refusal ------------


def _w_full_service(**kwargs):
    h = harness(**kwargs)
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    for message_id in ("i1", "i2"):
        h.service.injected(
            actor=A, message_id=message_id, sender=B, intent="request",
            reply_to=None, received_at_ms=now,
        )
    return h, now


def test_h1_observe_skips_the_user_window_refusal_and_counts_it() -> None:
    logs = LogRecorder()
    h, now = _w_full_service(window_mode="observe", logger=logs)
    assert h.service.admit_send(
        sender=A, target=U, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=None)
    assert h.service.stats(A)["windowObserved"] == 1
    fields = logs.fields_for("link.window_observed")[0]
    assert (fields["site"], fields["bound"], fields["peer"]) == ("user", "W", U)


def test_h1_enforce_still_refuses_the_user_window_new_request() -> None:
    h, now = _w_full_service(window_mode="enforce")
    with pytest.raises(DaemonRequestError) as refused:
        h.service.admit_send(
            sender=A, target=U, intent="request", reply_to=None, now_ms=now
        )
    assert refused.value.code == ipc_errors.LINK_WINDOW_FULL
    assert refused.value.data["detail"] == "W"
    assert h.service.stats(A)["windowObserved"] == 0


# -- hosted agents on this node -------------------------------------------


H = "agent:x:n1:hosted"


def hosted_service(**kwargs):
    """A service whose registry says this node runs both A and a hosted H.

    The callbacks come from the real ``LocalIdentity`` so these cases pin the
    composition decision, not a test-invented locality rule.
    """

    fs = FakeOrgFs()
    for space in kwargs.pop("spaces", ("s1",)):
        fs.add_space(space)
    identity = LocalIdentity("user:o", "n1", local=lambda: (A, H))
    return make_service(
        fs=fs,
        is_local_agent=identity.is_local,
        local_agents=identity.local_agents,
        author_of=identity.author_of,
        **kwargs,
    )


def test_hosted_agent_on_this_node_accepts_the_reference_and_releases_the_hold() -> None:
    h = hosted_service()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=H, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=H)
    h.inbox.hold(
        "m1",
        sender=A,
        target=H,
        reason=ipc_errors.LINK_MINTING,
        created_at_ms=now,
        expires_at_ms=now + 60_000,
    )
    h.tick()
    for _ in range(3):
        h.advance(1_000)
        h.tick()

    documents = {node.name for node in h.fs.listdir("s1", "link")}
    assert link_file_path(H).rsplit("/", 1)[-1] in documents
    assert h.inbox.held == {}
    # The released request is now in flight (one open out row), which is why a
    # *new* request to the same peer meets cwnd, not a mint hold.
    assert h.inbox.row_state(A, "m1") == "open"
    assert h.service.admit_send(
        sender=A, target=H, intent="request", reply_to=None,
        now_ms=h.clock.now_ms(),
    ) == Hold(ipc_errors.LINK_WINDOW_FULL, H, "cwnd")


def test_a_hosted_agent_on_this_node_can_reply_through_the_gate() -> None:
    h = hosted_service()
    h.tick()
    now = h.clock.now_ms()
    h.inbox.put_in(H, "m1", peer=A, state="open", opened_at_ms=now)
    establish(h.fs, A, H, author="user:o")
    h.tick()

    assert h.service.admit_send(
        sender=H, target=A, intent="reply", reply_to="m1",
        now_ms=h.clock.now_ms(),
    ) == Allow(peer=A)
