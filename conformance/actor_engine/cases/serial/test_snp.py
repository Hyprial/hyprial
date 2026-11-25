# Decision record (file header; no inline comments by repository rule):
# - SNP-01 walks one serial actor through every state of 5.1 and asserts the
#   whole 5-tuple (state, generation, queued, in_flight, failures_in_window)
#   at points where each field is pinned: queued counts the mailbox only,
#   in_flight the running handler, generation follows the single rule of 5.1
#   (+1 only on entering running), failures_in_window counts the restart
#   deque. restart_pending is held open by a custom 0.25 s backoff profile.
# - In `stopping`/`stopped` only (state, generation, queued, in_flight) are
#   asserted: whether failures_in_window still reports the deque after stop
#   is not pinned by the spec.
# - Pool meaning (9.3, 9.1, 9.5): state `running` and generation 1 for the
#   whole lifetime, no failure path (failures_in_window 0 even after worker
#   errors of every class), queued = submitted payloads not yet picked up,
#   in_flight = payloads executing. After stop the pool reads `stopped`, still
#   generation 1.
# - Pool completion delivery is at-least-once (9.4, EFF-06), so duplicates are
#   allowed and completions are compared as a set of operation ids.
# - Snapshot never mutates state (5.3): repeated reads are identical.
"""SNP: snapshot (spec 3.2, 5.1, 5.3, 9.3, E-6)."""

from __future__ import annotations

import threading

import pytest

from support.helpers import POLICIES, WAIT, Block, CustomBase, Raise, Rec, Recorder, case, wait_until

KINDS = pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
PENDING = {**POLICIES, "pending": (3, 60.0, 0.25, 0.25, 0.0)}


@case("SNP-01")
@KINDS
def test_snp_01_serial_fields_through_lifecycle(make_harness, kind: str) -> None:
    h = make_harness(kind=kind, policies=PENDING)
    log = Recorder()
    actor = h.spawn(profile="pending", capacity=8)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)

    release = threading.Event()
    blocker = Block(release)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "a")) == 0
        assert h.tell(actor, Rec(log, "b")) == 0
        assert h.snapshot(actor) == ("running", 1, 2, 1, 0)
    finally:
        release.set()
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)

    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(actor, "restart_pending")
    assert h.snapshot(actor) == ("restart_pending", 1, 0, 0, 1)
    assert h.tell(actor, Rec(log, "kept")) == 0
    assert h.snapshot(actor) == ("restart_pending", 1, 1, 0, 1)
    assert h.events.wait_kind(actor, "child_restarted")
    assert log.wait_len(3)
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 2, 0, 0, 1)

    release2 = threading.Event()
    blocker2 = Block(release2)
    try:
        assert h.tell(actor, blocker2) == 0
        assert blocker2.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "dead")) == 0
        assert h.stop(actor, 0) is False
        assert h.snapshot(actor)[:4] == ("stopping", 2, 1, 1)
    finally:
        release2.set()
    assert h.wait_state(actor, "stopped")
    assert h.snapshot(actor)[:4] == ("stopped", 2, 0, 0)
    assert log.items() == ["a", "b", "kept"]


@case("SNP-01")
def test_snp_01_serial_quarantined_fields(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(profile="never")
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
        assert h.tell(actor, Rec(log, "drained")) == 0
        assert h.snapshot(actor) == ("running", 1, 2, 1, 0)
    finally:
        release.set()
    assert h.wait_state(actor, "quarantined")
    first = h.snapshot(actor)
    assert first == ("quarantined", 1, 0, 0, 1)
    assert [h.snapshot(actor) for _ in range(50)] == [first] * 50
    assert log.items() == []


@case("SNP-01")
def test_snp_01_snapshot_reads_do_not_mutate(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(capacity=4)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "q")) == 0
        reads = [h.snapshot(actor) for _ in range(100)]
        assert reads == [("running", 1, 1, 1, 0)] * 100
    finally:
        release.set()
    assert log.wait_len(1)
    assert log.items() == ["q"]


@case("SNP-01")
def test_snp_01_pool_fields(h) -> None:
    completions = Recorder()
    release = threading.Event()
    blocker = Block(release)
    pool = h.spawn_pool(pool_size=1, capacity=8)
    owner = h.spawn_owner(pool, completions)
    assert h.snapshot(pool) == ("running", 1, 0, 0, 0)
    payloads = [blocker, Rec(Recorder(), "ok"), Raise(RuntimeError("worker error")),
                Raise(CustomBase("worker base")), "panic"]

    def submit(index: int, payload: object) -> int:
        return h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name,
                                    f"op-{index}", 1, payload)

    try:
        assert submit(0, payloads[0]) == 0
        assert blocker.entered.wait(WAIT)
        assert [submit(index, payload) for index, payload in enumerate(payloads[1:], 1)] == [0] * 4
        assert h.snapshot(pool) == ("running", 1, 4, 1, 0)
    finally:
        release.set()
    assert wait_until(lambda: {c.operation_id for c in completions.items()} == {f"op-{i}" for i in range(5)})
    assert h.wait_idle(pool)
    assert h.snapshot(pool) == ("running", 1, 0, 0, 0)
    assert sorted({c.operation_id for c in completions.items()}) == [f"op-{i}" for i in range(5)]
    assert h.stop(pool, WAIT) is True
    assert h.snapshot(pool)[:4] == ("stopped", 1, 0, 0)


@case("SNP-02")
def test_snp_02_unknown_and_forged_serial_handles_key_error(h) -> None:
    actor = h.spawn()
    other = h.spawn()
    for actor_id, name in (("no-such-actor-id", actor.name), ("no-such-actor-id", "no-such-name"),
                           (actor.actor_id, actor.name + "-forged"), (actor.actor_id, other.name),
                           (other.actor_id, actor.name), (actor.actor_id, "")):
        with pytest.raises(KeyError):
            h.engine.snapshot(actor_id, name)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)


@case("SNP-02")
def test_snp_02_unknown_and_forged_pool_handles_key_error(h) -> None:
    pool = h.spawn_pool()
    for actor_id, name in (("no-such-pool-id", pool.name), (pool.actor_id, pool.name + "-forged")):
        with pytest.raises(KeyError):
            h.engine.snapshot(actor_id, name)
    assert h.snapshot(pool) == ("running", 1, 0, 0, 0)
