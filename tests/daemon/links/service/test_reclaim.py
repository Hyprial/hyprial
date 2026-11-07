"""WP2 reclaim tests (spec §4.4): residue, drain codes, barriers, PAC, admin."""

from __future__ import annotations

from dataclasses import replace

from hyprial.daemon.impl.links import model, policy
from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon.impl.links.ports import (
    EVENT_LINK_RECLAIMED,
    Allow,
    Hold,
    Mint,
)
from hyprial.daemon.impl.links.store import CensoredRecord
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.support.links.ledger import FakeInboxPort
from tests.support.links.org import (
    FakeOrgFs,
    FakePacPort,
    RecordingPublisher,
    establish,
    establish_many,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
D = "agent:o:n1:d"
E = "agent:o:n1:e"
F = "agent:o:n1:f"
X = "agent:p:n2:x"

MINTING = ipc_errors.LINK_MINTING
SELF_FULL = ipc_errors.LINK_SELF_FULL


def harness(*, publisher=None, pac=None, local_names=("a",), **kwargs):
    fs = FakeOrgFs()
    fs.add_space("s1")
    return make_service(
        fs=fs, publisher=publisher, pac=pac, local_names=local_names, **kwargs
    )


# -- R1 ------------------------------------------------------------------


def test_r1_eviction_only_executes_the_policy_victim(monkeypatch) -> None:
    calls: list[int] = []
    original = policy.select_victim

    def counting(view, *, now_ms):
        calls.append(1)
        return original(view, now_ms=now_ms)

    monkeypatch.setattr(policy, "select_victim", counting)
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    assert h.service.add(actor=A, peer=C, now_ms=now)["state"] == "pending"
    assert calls == []


# -- R2 ------------------------------------------------------------------


def test_r2_drop_drains_every_residue_and_cancels_pending_retries() -> None:
    publisher = RecordingPublisher()
    h = harness(publisher=publisher)
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    # Two admitted, one censored, one waiting that only the inbox knows about.
    for message_id in ("i1", "i2"):
        h.service.injected(
            actor=A, message_id=message_id, sender=B, intent="request",
            reply_to=None, received_at_ms=now,
        )
    with h.service._mem:
        h.service._states[A].censored_inbound["c1"] = CensoredRecord(
            actor=A, message_id="c1", requester=B, received_at_ms=now,
            threshold_ms=1_000, expires_at_ms=now + 600_000,
        )
        # Never admitted and never fetched (§3.3's inbox residue).
        h.service._states[A].pending_inbound.pop("c1", None)
    h.inbox.queue_inbound(A, "w1", sender=B, received_at_ms=now)
    # One sent outbound, one reservation, one released-but-unsent retry, and
    # one never-released held request.
    h.inbox.put_out(A, "o1", peer=B, state="open", sent_at_ms=now)
    with h.service._mem:
        h.service._states[A].reservations.append((B, now))
    h.inbox.put_out(A, "o2", peer=B, state="open", sent_at_ms=None)
    h.inbox.hold(
        "held1", sender=A, target=B, reason=MINTING,
        created_at_ms=now, expires_at_ms=now + 600_000,
    )
    h.tick()
    outcome = h.service.drop(actor=A, peer=B, now_ms=now)
    assert outcome["state"] == "reclaiming" and outcome["written"] is False
    # The service thread performs the reclaim and the drain (P-3).
    h.tick(now + 1_000)
    applied = next(
        update for update in reversed(h.inbox.applies) if A in update.drain
    )
    assert sorted(applied.drain[A]) == ["c1", "i1", "i2", "w1"]
    assert applied.cancel_outbound == {"o2": ipc_errors.LINK_RECLAIMED}
    assert applied.release_outbound == ((A, "o1"),)
    with h.service._mem:
        state = h.service._states[A]
        assert state.reservations == []
        assert "held1" in state.held
    reclaimed = publisher.payloads(EVENT_LINK_RECLAIMED)
    assert [payload["reason"] for payload in reclaimed] == ["dropped"]
    assert reclaimed[0]["drained"] == 4
    assert reclaimed[0]["drainedMessageIds"] == ["c1", "i1", "i2", "w1"]
    # C23/P1: reclaimed has its own payload; it is not minted's plus extras
    # and carries neither revision nor side.
    assert set(reclaimed[0]) == {
        "actor", "atMs", "cancelled", "drained", "drainedMessageIds",
        "lifecycle", "peer", "reason", "released", "taskId",
    }
    assert reclaimed[0]["peer"] == B
    assert reclaimed[0]["taskId"] is None


# -- R3 ------------------------------------------------------------------


def test_r3_drain_codes_survive_a_remint_and_are_per_actor() -> None:
    h = harness(local_names=("a", "c"))
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.injected(
        actor=A, message_id="m1", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    h.service.drop(actor=A, peer=B, now_ms=now)
    h.tick(now + 1_000)
    for _ in range(2):
        with pytest_raises_code(ipc_errors.LINK_RECLAIMED):
            h.service.admit_send(
                sender=A, target=B, intent="reply", reply_to="m1", now_ms=now
            )
    assert h.service.add(actor=A, peer=B, now_ms=now)["state"] == "pending"
    h.tick(now + 2_000)
    with pytest_raises_code(ipc_errors.LINK_RECLAIMED):
        h.service.admit_send(
            sender=A, target=B, intent="reply", reply_to="m1", now_ms=now
        )
    with pytest_raises_code(ipc_errors.MESSAGE_REPLY_UNAVAILABLE):
        h.service.admit_send(
            sender=A, target=B, intent="reply", reply_to="ghost", now_ms=now
        )
    # The drain is per (actor, message id): another actor is unaffected.
    assert h.service.settle_hint(actor=C, message_id="m1") == "keep"


class pytest_raises_code:
    """Tiny context manager asserting a DaemonRequestError code."""

    def __init__(self, code: str) -> None:
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert exc_type is DaemonRequestError, exc
        assert exc.code == self.code, (exc.code, self.code)
        return True


# -- R4 ------------------------------------------------------------------


def test_r4_a_late_reply_to_a_released_outbound_is_ignored() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.inbox.put_out(A, "o1", peer=B, state="open", sent_at_ms=now)
    h.tick()
    h.service.drop(actor=A, peer=B, now_ms=now)
    h.tick(now + 1_000)
    with h.service._mem:
        assert "o1" in h.service._states[A].released_outbound
        before = list(h.service._states[A].rtt)
    h.service.reply_received(
        actor=A, reply_message_id="r1", reply_to="o1", sender=B,
        received_at_ms=now + 100,
    )
    with h.service._mem:
        assert list(h.service._states[A].rtt) == before
        assert h.service._states[A].outbound == []
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="r1", now_ms=now,
            intent="reply", reply_to="o1",
        )
        is False
    )


# -- R5 ------------------------------------------------------------------


def test_r5_the_reclaim_barrier_only_clears_on_two_explicit_withdrawals() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    # B withdraws with a higher revision.
    write_reference(h.fs, B, {}, revision=9, space="s1")
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert state.reclaimed.get(B) is not None
        barrier = state.barrier[B]
    # B's old reference reappears: it is not accepted while the barrier holds.
    write_reference(h.fs, B, {A: LinkEntry(peer=A, minted_at_ms=now, revision=1)},
                    revision=1, space="s1")
    h.tick()
    with h.service._mem:
        assert B in h.service._states[A].reclaimed
        assert h.service._states[A].barrier.get(B) == barrier
    # B's copy is merely absent: the barrier stays.
    h.fs.remove("s1", f"link/{B}.json")
    h.tick()
    with h.service._mem:
        assert h.service._states[A].barrier.get(B) == barrier
    # A higher revision on both sides that names nobody clears the barrier.
    h.fs.put_document(
        "s1", f"link/{B}.json", model.to_document(
            model.LinkFile(actor=B, revision=12, entries={})
        ), author="user:o",
    )
    h.tick()
    with h.service._mem:
        assert B not in h.service._states[A].reclaimed
    assert h.service.add(actor=A, peer=B, now_ms=now)["state"] == "pending"


# -- R6 ------------------------------------------------------------------


def test_r6_k_shrink_reclaims_regular_links_and_never_pac_ones() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    publisher = RecordingPublisher()
    old = 1_000_000 - 60_000
    establish_many(fs, [(A, B), (A, C), (A, D), (A, E)], now_ms=old)
    write_reference(
        fs, A,
        {
            B: LinkEntry(peer=B, minted_at_ms=old, revision=5),
            C: LinkEntry(peer=C, minted_at_ms=old, revision=5),
            D: LinkEntry(peer=D, minted_at_ms=old, revision=5),
            E: LinkEntry(peer=E, minted_at_ms=old, revision=5),
            F: LinkEntry(peer=F, minted_at_ms=old, revision=5, lifecycle="pac_task",
                         task_id="pac-1"),
        },
        revision=5,
    )
    h = make_service(fs=fs, publisher=publisher, local_names=("a",))
    h.presence.set([f"agent:o:n1:p{index}" for index in range(55)])
    h.tick()
    assert h.service.stats(A)["k"] == 5
    h.presence.set([f"agent:o:n1:p{index}" for index in range(7)])
    h.clock.advance(25 * 60 * 60 * 1000)
    h.tick()
    assert h.service.stats(A)["k"] == 2
    reclaimed = publisher.payloads(EVENT_LINK_RECLAIMED)
    assert [payload["reason"] for payload in reclaimed] == ["k_shrink", "k_shrink"]
    assert F not in [payload["peer"] for payload in reclaimed]
    entries = h.store.index().files[A].entries
    assert F in entries
    assert len([peer for peer in entries if peer != F]) == 2


# -- R7 ------------------------------------------------------------------


def test_r7_bind_task_mints_a_pac_reference_and_records_the_binding() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    callback = h.service.bind_task(principal=A, worker=X, task_id="pac-g1", now_ms=now)
    assert policy.degree(_view(h, A)) == 0
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=X, message_id="m1", now_ms=now
        )
        is False
    )
    h.tick()
    entry = h.store.index().files[A].entries[X]
    assert entry.lifecycle == "pac_task"
    assert entry.task_id == "pac-g1"
    assert h.inbox.bindings[(A, X, "pac-g1")] == now
    assert X in h.store.index().files[A].entries
    callback("pac-message-1")
    h.tick()
    with h.service._mem:
        assert [
            item.message_id for item in h.service._states[A].outbound
        ] == ["pac-message-1"]


def _view(h, actor):
    with h.service._mem:
        return h.service._view(actor, h.clock.now_ms())


# -- R8 ------------------------------------------------------------------


def test_r8_pac_delivery_counting_is_idempotent_and_bounded() -> None:
    h = harness()
    establish(h.fs, A, X)
    h.tick()
    now = h.clock.now_ms()
    callback = h.service.bind_task(principal=A, worker=X, task_id="pac-g1", now_ms=now)
    h.tick()
    callback("p1")
    callback("p1")
    h.tick()
    with h.service._mem:
        assert [item.message_id for item in h.service._states[A].outbound] == ["p1"]
    # The next delivery exceeds the peer's cwnd of 1.
    assert h.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now
    ) == Hold(ipc_errors.LINK_WINDOW_FULL, X, "cwnd")
    # A delivery callback that never fires is never counted.
    callback2 = h.service.bind_task(
        principal=A, worker=X, task_id="pac-g2", now_ms=now
    )
    h.tick()
    with h.service._mem:
        assert sum(
            1 for item in h.service._states[A].outbound if item.message_id == "p2"
        ) == 0
    del callback2


# -- R9 ------------------------------------------------------------------


def test_r9_pac_release_uses_the_durable_state_not_the_bus() -> None:
    pac = FakePacPort()
    h = harness(pac=pac)
    h.tick()
    now = h.clock.now_ms()
    h.service.bind_task(principal=A, worker=X, task_id="pac-g1", now_ms=now)
    h.tick()
    # A second task on the same peer keeps the link alive.
    h.service.bind_task(principal=A, worker=X, task_id="pac-g2", now_ms=now)
    h.tick()
    pac.states["pac-g1"] = "ended"
    h.tick()
    with h.service._mem:
        assert h.service._states[A].task_bindings[X] == {"pac-g2"}
        assert X not in h.service._states[A].reclaimed
    # An unknown state never reclaims.
    pac.states["pac-g2"] = "unknown"
    h.tick()
    with h.service._mem:
        assert X not in h.service._states[A].reclaimed
    # The last binding ending reclaims the PAC link.
    pac.states["pac-g2"] = "ended"
    h.tick()
    with h.service._mem:
        assert X in h.service._states[A].reclaimed
    # A restarted service recovers the ended task from the durable table.
    fs2 = FakeOrgFs()
    fs2.add_space("s1")
    write_reference(
        fs2, A,
        {
            X: LinkEntry(
                peer=X, minted_at_ms=now, revision=1, lifecycle="pac_task",
                task_id="pac-g3",
            )
        },
        revision=1,
    )
    inbox2 = FakeInboxPort()
    inbox2.bindings[(A, X, "pac-g3")] = now
    pac.states["pac-g3"] = "ended"
    restarted = make_service(fs=fs2, inbox=inbox2, pac=pac, local_names=("a",))
    restarted.tick()
    with restarted.service._mem:
        assert X in restarted.service._states[A].reclaimed


def test_r9_worker_inactive_only_drops_that_worker_binding() -> None:
    pac = FakePacPort()
    h = harness(pac=pac)
    establish(h.fs, A, X)
    h.tick()
    now = h.clock.now_ms()
    h.service.bind_task(principal=A, worker=X, task_id="pac-w1", now_ms=now)
    h.tick()
    pac.states["pac-w1"] = "active"
    pac.workers[("pac-w1", A)] = False
    h.tick()
    with h.service._mem:
        assert h.service._states[A].task_bindings.get(X, set()) == set()


def test_r9_a_regular_link_survives_its_task_ending() -> None:
    pac = FakePacPort()
    h = harness(pac=pac)
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.bind_task(principal=A, worker=B, task_id="pac-r1", now_ms=now)
    h.tick()
    pac.states["pac-r1"] = "ended"
    h.tick()
    with h.service._mem:
        assert B not in h.service._states[A].reclaimed
        assert h.service._states[A].task_bindings.get(B, set()) == set()
    assert B in h.store.index().files[A].entries


# -- R10 -----------------------------------------------------------------


def test_r10_add_is_idempotent_and_never_queues_when_full() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.add(actor=A, peer=B, now_ms=now) == {
        "actor": A,
        "peer": B,
        "state": "pending",
        "committed": False,
        "revision": 1,
        "written": False,
    }
    assert h.service.add(actor=A, peer=B, now_ms=now)["state"] == "pending"
    h.tick()
    replay = h.service.add(actor=A, peer=B, now_ms=now)
    assert replay["committed"] is True
    for bad in ("user:p", "user:o", A):
        with pytest_raises_code(ipc_errors.INVALID_ARGUMENT):
            h.service.add(actor=A, peer=bad, now_ms=now)
    with pytest_raises_code(ipc_errors.INVALID_ARGUMENT):
        h.service.add(actor=A, peer="agent:o:n1:dispatch-coordinator", now_ms=now)


def test_r10_add_reports_absent_when_the_self_budget_has_no_victim() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    old = 1_000_000 - 60_000
    establish_many(fs, [(A, B), (A, C)], now_ms=old)
    h = make_service(fs=fs, local_names=("a",))
    h.tick()
    now = h.clock.now_ms()
    for index, peer in ((1, B), (2, C)):
        h.service.submitted(
            sender=A, target=peer, message_id=f"o{index}", reply_to=None,
            hold_reason=None, now_ms=now,
        )
    outcome = h.service.add(actor=A, peer=D, now_ms=now)
    assert outcome["written"] is False
    assert outcome["queued"] is False
    assert outcome["holdReason"] == SELF_FULL
    assert A not in h.store.intents()


# -- R11 -----------------------------------------------------------------


def test_r11_a_failed_drain_is_retried_without_restoring_reply_rights() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.inbox.inject(A, "m1", sender=B, received_at_ms=now)
    h.inbox.fail_apply = True
    outcome = h.service.drop(actor=A, peer=B, now_ms=now)
    assert outcome["state"] == "reclaiming"
    h.tick(now + 1_000)
    with pytest_raises_code(ipc_errors.LINK_RECLAIMED):
        h.service.admit_send(
            sender=A, target=B, intent="reply", reply_to="m1", now_ms=now
        )
    h.inbox.fail_apply = False
    h.tick(now + 2_000)
    assert "m1" in h.inbox.applies[-1].drain[A]
    assert h.inbox.rows[(A, "m1")].state == "drained"


# -- R12: workflow-owned workers publish no empty roster file (E2E-021) --

RESIDENT = "agent:o:n1:resident"
W = "agent:o:n1:w"


def _link_documents(fs: FakeOrgFs) -> list[str]:
    return sorted(node.name for node in fs.listdir("s1", "link"))


def test_r12_workflow_owned_workers_publish_no_empty_files() -> None:
    """Only resident agents publish the empty B13 roster file (E2E-021).

    A workflow-owned temporary worker never needs an empty committed file:
    it talks to its owner and through the PAC dispatch channel, which do not
    read the link graph.  Publishing one per worker cost the E2E-021 org
    directory space a durable commit per worker (about 320 workers, 969
    commits, 71.7 s join).  A worker that actually mints still publishes
    lazily, pinned by the next test.
    """

    fs = FakeOrgFs()
    fs.add_space("s1")
    pac = FakePacPort()
    pac.workflow_owned = {f"agent:o:n1:w{index}" for index in range(3)}
    h = make_service(
        fs=fs,
        pac=pac,
        local_names=("resident", "w0", "w1", "w2"),
    )

    h.tick(1)
    assert _link_documents(fs) == [f"{RESIDENT}.json"]
    assert h.service.sender_known(recipient=A, sender=RESIDENT) is True
    for worker in ("w0", "w1", "w2"):
        assert h.service.sender_known(
            recipient=A, sender=f"agent:o:n1:{worker}"
        ) is False
    committed = len(fs.write_versions)
    h.tick(2)
    h.tick(3)
    assert len(fs.write_versions) == committed, "repeated ticks must not recommit"


def test_r12b_a_workflow_worker_still_publishes_when_it_mints() -> None:
    """Skipping the empty file must not cost a minting worker its B13 proof."""

    fs = FakeOrgFs()
    fs.add_space("s1")
    pac = FakePacPort()
    pac.workflow_owned = {W}
    h = make_service(fs=fs, pac=pac, local_names=("resident", "w"))

    h.tick(1)
    assert f"{W}.json" not in _link_documents(fs)
    assert h.service.sender_known(recipient=A, sender=W) is False
    decision = h.service.admit_send(
        sender=W,
        target=X,
        intent="request",
        reply_to=None,
        now_ms=h.clock.now_ms(),
    )
    assert isinstance(decision, Mint)
    h.tick(2)
    assert f"{W}.json" in _link_documents(fs)
    assert h.service.sender_known(recipient=A, sender=W) is True


def test_r12c_a_skipped_worker_keeps_its_owner_and_pac_channels() -> None:
    """Not publishing must not cost a workflow worker its actual channels.

    A temporary worker talks to its owner as a ``user:`` target (every
    ``user:`` target bypasses the link gate) and through the daemon-internal
    PAC dispatch actor (exempt on both sides by reserved name), so neither
    channel reads the worker's link file.
    """

    fs = FakeOrgFs()
    fs.add_space("s1")
    pac = FakePacPort()
    pac.workflow_owned = {W}
    h = make_service(fs=fs, pac=pac, local_names=("resident", "w"))

    h.tick(1)
    assert f"{W}.json" not in _link_documents(fs)
    now = h.clock.now_ms()
    assert isinstance(
        h.service.admit_send(
            sender=W,
            target="user:o",
            intent="request",
            reply_to=None,
            now_ms=now,
        ),
        Allow,
    )
    assert h.service.admit_dispatch(
        recipient=W,
        sender="agent:o:n1:dispatch-coordinator",
        message_id="pac-notice-1",
        now_ms=now,
    ) is True
    # The worker replies to that internal sender: exempt as recipient too.
    assert isinstance(
        h.service.admit_send(
            sender=W,
            target="agent:o:n1:dispatch-coordinator",
            intent="reply",
            reply_to=None,
            now_ms=now,
        ),
        Allow,
    )


def test_r12d_a_worker_that_appears_after_a_tick_is_skipped_at_once() -> None:
    """E2E-021/3.2: a new worker is recognised on its first tick.

    The judgement reads the desired-state marker once per local actor, so no
    5 s cache window can publish an empty file for a worker that is registered
    between two ticks.
    """

    fs = FakeOrgFs()
    fs.add_space("s1")
    pac = FakePacPort()
    local: set[str] = {"resident"}

    def local_agents() -> tuple[str, ...]:
        return tuple(f"agent:o:n1:{name}" for name in sorted(local))

    h = make_service(
        fs=fs,
        pac=pac,
        local_names=("resident",),
        local_agents=local_agents,
        is_local_agent=lambda uri: uri in local_agents(),
    )

    h.tick(1)
    assert _link_documents(fs) == [f"{RESIDENT}.json"]

    # The worker becomes local and PAC-owned between the two ticks.
    local.add("w")
    pac.workflow_owned = {W}
    h.tick(2)

    assert f"{W}.json" not in _link_documents(fs)


def _replace(row, **changes):
    return replace(row, **changes)
