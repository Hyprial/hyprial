# Decision record (file header; no inline comments by repository rule):
# - Custody-token admission cases of spec 9.1/9.2/9.4 (EFF-01, 03, 04, 05, 08,
#   10, 11, 13). Workers run the shared trampoline through the `h` fixture, so
#   every case runs with a Python and with a native worker handler; owners are
#   Python actors by nature (they must call `acknowledge`).
# - Token liveness is observed only through ABI results: a live token holds a
#   capacity slot (`pool_reserve` of a fresh key -> 1 on a full pool) and a
#   missing token makes `pool_cancel_reservation` / `acknowledge` -> False.
#   Pool snapshot queued/in_flight numbers are not asserted (9.3 fixes only
#   state `running` and generation 1).
# - The spec lets workers either pin on un-acked completions or keep running
#   (courier); no case depends on either choice. Redelivery count is never
#   asserted: at-least-once allows duplicates, so owners are compared by the
#   set of DISTINCT operation ids.
# - Every reservation a case makes is cancelled or submitted before the case
#   ends, otherwise pool stop at harness shutdown would (correctly) wait for it.
# - EFF-03 bounds the slowest of 50 reserves by TOL through
#   support.helpers.assert_timing; the pool has room for every retried batch.
"""EFF custody admission: capacity, reservation protocol, target kinds, owner validation."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import WAIT, Block, DeadLetters, Rec, Recorder, assert_timing, case, wait_until


class Owner:
    """Python owner actor: records completions in order, optionally acks and records ack results."""

    def __init__(self, harness, pool, ack: bool = True, capacity: int = 64, profile: str = "fast",
                 trace: Recorder | None = None, dead_letters=None) -> None:
        self.harness = harness
        self.pool = pool
        self.ack = ack
        self.log = Recorder()
        self.acks = Recorder()
        self.trace = trace
        completed = harness.module.EffectCompleted
        engine = harness.engine

        def handle(message: object) -> object:
            if isinstance(message, completed):
                self.log.append(message)
                if self.trace is not None:
                    self.trace.append(message)
                if self.ack:
                    self.acks.append(engine.acknowledge(pool.actor_id, pool.name,
                                                        message.operation_id, message.generation))
                return None
            return message() if callable(message) else message

        self.actor = harness.spawn(capacity=capacity, profile=profile, dead_letters=dead_letters,
                                   factory=lambda: handle)

    def ops(self) -> set[str]:
        return {message.operation_id for message in self.log.items()}

    def wait_ops(self, expected: set[str], timeout: float = WAIT) -> bool:
        return wait_until(lambda: expected <= self.ops(), timeout)


def submit(h, pool, owner, op: str, payload: object, generation: int = 1) -> int:
    return h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name, op, generation, payload)


def reserve(h, pool, op: str, generation: int = 1) -> int:
    return h.engine.pool_reserve(pool.actor_id, pool.name, op, generation)


def submit_reserved(h, pool, owner, op: str, payload: object, generation: int = 1) -> int:
    return h.engine.pool_submit_reserved(pool.actor_id, pool.name, owner.actor_id, owner.name,
                                         op, generation, payload)


def cancel(h, pool, op: str, generation: int = 1) -> bool:
    return h.engine.pool_cancel_reservation(pool.actor_id, pool.name, op, generation)


def ack(h, pool, op: str, generation: int = 1) -> bool:
    return h.engine.acknowledge(pool.actor_id, pool.name, op, generation)


@case("EFF-01")
def test_eff_01_capacity_counts_queued_running_and_awaiting_ack(h) -> None:
    """Capacity bounds live tokens: running + queued + completed-awaiting-ACK; overload creates nothing."""
    pool = h.spawn_pool(pool_size=1, capacity=3)
    owner = Owner(h, pool, ack=False)
    executed = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner.actor, "op1", Block(release, entered, "v1")) == 0
        assert entered.wait(WAIT)
        assert submit(h, pool, owner.actor, "op2", Rec(executed, "op2")) == 0
        assert submit(h, pool, owner.actor, "op3", Rec(executed, "op3")) == 0
        assert submit(h, pool, owner.actor, "op4", Rec(executed, "op4")) == 1
        assert reserve(h, pool, "op4") == 1
        assert cancel(h, pool, "op4") is False
        assert ack(h, pool, "op4") is False
    finally:
        release.set()
    assert owner.wait_ops({"op1"})
    assert submit(h, pool, owner.actor, "op5", Rec(executed, "op5")) == 1
    assert reserve(h, pool, "op5") == 1
    assert ack(h, pool, "op1") is True
    assert submit(h, pool, owner.actor, "op5", Rec(executed, "op5")) == 0
    for op in ("op2", "op3", "op5"):
        assert owner.wait_ops({op})
        assert ack(h, pool, op) is True
    assert executed.items() == ["op2", "op3", "op5"]


@case("EFF-01")
def test_eff_01_completed_awaiting_ack_holds_capacity(h) -> None:
    """Every payload finished and delivered but none acked: the pool is still full until the acks."""
    pool = h.spawn_pool(pool_size=3, capacity=3)
    owner = Owner(h, pool, ack=False)
    ops = {"op1", "op2", "op3"}
    for op in sorted(ops):
        assert submit(h, pool, owner.actor, op, Rec(Recorder(), op)) == 0
    assert owner.wait_ops(ops)
    time.sleep(0.05)
    assert reserve(h, pool, "extra") == 1
    assert submit(h, pool, owner.actor, "extra", Rec(Recorder(), "extra")) == 1
    for op in sorted(ops):
        assert ack(h, pool, op) is True
    assert reserve(h, pool, "extra") == 0
    assert cancel(h, pool, "extra") is True


@case("EFF-01")
def test_eff_01_single_worker_executes_in_strict_submission_order(h) -> None:
    """pool_size=1: payloads execute strictly in submission order (single worker, FIFO)."""
    pool = h.spawn_pool(pool_size=1, capacity=64)
    owner = h.spawn_owner(pool, Recorder())
    executed = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner, "gate", Block(release, entered)) == 0
        assert entered.wait(WAIT)
        for index in range(50):
            assert submit(h, pool, owner, f"op{index}", Rec(executed, index)) == 0
    finally:
        release.set()
    assert executed.wait_len(50)
    assert executed.items() == list(range(50))


@case("EFF-01")
def test_eff_01_multi_worker_executes_every_payload_once(h) -> None:
    """pool_size>1: order is unspecified, so only exactly-once execution of each payload is asserted."""
    pool = h.spawn_pool(pool_size=4, capacity=64)
    log = Recorder()
    owner = h.spawn_owner(pool, log)
    executed = Recorder()
    for index in range(40):
        assert submit(h, pool, owner, f"op{index}", Rec(executed, index)) == 0
    assert executed.wait_len(40)
    assert wait_until(lambda: {m.operation_id for m in log.items()} == {f"op{i}" for i in range(40)})
    assert sorted(executed.items()) == list(range(40))


@case("EFF-03")
def test_eff_03_reserve_answers_while_workers_blocked_behind_deep_backlog(h) -> None:
    """pool_reserve is answered from the token table: bounded latency with all workers blocked."""
    pool = h.spawn_pool(pool_size=2, capacity=700)
    owner = h.spawn_owner(pool, Recorder())
    executed = Recorder()
    release = threading.Event()
    gates = [threading.Event(), threading.Event()]
    reserved: list[str] = []

    def attempt() -> float:
        latencies = []
        for _ in range(50):
            name = f"res{len(reserved)}"
            started = time.monotonic()
            outcome = reserve(h, pool, name)
            latencies.append(time.monotonic() - started)
            assert outcome == 0
            reserved.append(name)
        return max(latencies)

    try:
        for index, gate in enumerate(gates):
            assert submit(h, pool, owner, f"gate{index}", Block(release, gate)) == 0
        assert all(gate.wait(WAIT) for gate in gates)
        for index in range(400):
            assert submit(h, pool, owner, f"op{index}", Rec(executed, index)) == 0
        assert_timing(attempt, 0.0, what="slowest of 50 pool_reserve calls behind a blocked backlog")
        assert executed.items() == []
        assert not release.is_set()
    finally:
        release.set()
    for name in reserved:
        assert cancel(h, pool, name) is True
    assert executed.wait_len(400, timeout=10.0)


@case("EFF-04")
def test_eff_04_submit_reserved_succeeds_on_full_queue_and_binds_owner(h) -> None:
    """A live reservation submits regardless of queue depth; the completion goes to that call's owner."""
    pool = h.spawn_pool(pool_size=1, capacity=20)
    first = Owner(h, pool)
    second = Owner(h, pool)
    executed = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert reserve(h, pool, "reserved") == 0
        assert submit(h, pool, first.actor, "gate", Block(release, entered)) == 0
        assert entered.wait(WAIT)
        for index in range(18):
            assert submit(h, pool, first.actor, f"op{index}", Rec(executed, index)) == 0
        assert submit(h, pool, first.actor, "extra", Rec(executed, "extra")) == 1
        assert submit_reserved(h, pool, second.actor, "reserved", Rec(executed, "reserved")) == 0
    finally:
        release.set()
    assert second.wait_ops({"reserved"})
    assert first.wait_ops({"gate"} | {f"op{index}" for index in range(18)})
    assert second.ops() == {"reserved"}
    assert "reserved" not in first.ops()
    assert "extra" not in executed.items()
    assert executed.items()[-1] == "reserved"


@case("EFF-05")
def test_eff_05_cancel_reservation_frees_live_unsubmitted_token(h) -> None:
    """Cancel of a live unsubmitted token -> True and frees its slot; the key is not re-created."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool)
    executed = Recorder()
    assert reserve(h, pool, "r1") == 0
    assert reserve(h, pool, "r2") == 1
    assert cancel(h, pool, "r1") is True
    assert cancel(h, pool, "r1") is False
    assert submit_reserved(h, pool, owner.actor, "r1", Rec(executed, "r1")) == 2
    assert reserve(h, pool, "r2") == 0
    assert cancel(h, pool, "r2") is True
    assert cancel(h, pool, "never-reserved") is False
    time.sleep(0.05)
    assert executed.items() == []


@case("EFF-05")
def test_eff_05_cancel_after_submit_returns_false(h) -> None:
    """After submission the payload may already run: cancel -> False and the payload still completes."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool)
    release, entered = threading.Event(), threading.Event()
    try:
        assert reserve(h, pool, "r") == 0
        assert submit_reserved(h, pool, owner.actor, "r", Block(release, entered, "done")) == 0
        assert cancel(h, pool, "r") is False
        assert entered.wait(WAIT)
        assert cancel(h, pool, "r") is False
    finally:
        release.set()
    assert owner.wait_ops({"r"})
    assert owner.log.items()[0].result == "done"
    assert wait_until(lambda: True in owner.acks.items())


@case("EFF-08")
def test_eff_08_unacked_token_pins_capacity_until_ack(h) -> None:
    """A delivered but un-acked completion keeps its slot while the owner lives; ack frees it once."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool, ack=False)
    assert submit(h, pool, owner.actor, "op1", Rec(Recorder(), "v")) == 0
    assert owner.wait_ops({"op1"})
    time.sleep(0.3)
    assert reserve(h, pool, "op2") == 1
    assert h.state(owner.actor) == "running"
    assert ack(h, pool, "op1") is True
    assert ack(h, pool, "op1") is False
    assert reserve(h, pool, "op2") == 0
    assert cancel(h, pool, "op2") is True


@case("EFF-08")
def test_eff_08_owner_death_dead_letter_settles_token(h) -> None:
    """Owner stopped before completion: the dead-lettered completion settles its token and frees the slot."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=1, dead_letters=pool_dead)
    owner = Owner(h, pool, ack=False)
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner.actor, "op1", Block(release, entered)) == 0
        assert entered.wait(WAIT)
        assert h.stop(owner.actor) is True
    finally:
        release.set()
    assert pool_dead.wait_len(1)
    assert reserve(h, pool, "op2") == 0
    assert cancel(h, pool, "op2") is True
    assert ack(h, pool, "op1") is False


@case("EFF-10")
def test_eff_10_duplicate_reserve_and_submit_reserved_are_idempotent(h) -> None:
    """Duplicate reserve -> 0 without a capacity re-check; duplicate submit -> 0, first payload wins."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool, ack=False)
    executed = Recorder()
    assert reserve(h, pool, "r") == 0
    assert reserve(h, pool, "r") == 0
    assert reserve(h, pool, "other") == 1
    assert submit_reserved(h, pool, owner.actor, "r", Rec(executed, "first")) == 0
    assert submit_reserved(h, pool, owner.actor, "r", Rec(executed, "second")) == 0
    assert submit(h, pool, owner.actor, "r", Rec(executed, "third")) == 0
    assert reserve(h, pool, "r") == 0
    assert owner.wait_ops({"r"})
    time.sleep(0.1)
    assert executed.items() == ["first"]
    assert {m.result for m in owner.log.items()} == {"first"}
    assert ack(h, pool, "r") is True
    assert ack(h, pool, "r") is False


@case("EFF-11")
def test_eff_11_serial_operations_on_pool_target(h) -> None:
    """tell -> 2, ask -> ActorAdmissionError(closed), send_after -> ValueError on a pool target."""
    pool = h.spawn_pool()
    executed = Recorder()
    assert h.tell(pool, Rec(executed, "tell")) == 2
    with pytest.raises(h.module.ActorAdmissionError) as raised:
        h.ask(pool, Rec(executed, "ask"), timeout=0.5)
    assert raised.value.args[0] == "closed"
    with pytest.raises(ValueError):
        h.send_after(pool, 0.0, Rec(executed, "timer"))
    time.sleep(0.05)
    assert executed.items() == []
    assert h.snapshot(pool)[:2] == ("running", 1)


@case("EFF-11")
def test_eff_11_pool_operations_on_serial_target(h) -> None:
    """pool_submit / pool_reserve / pool_submit_reserved -> 2 and cancel / acknowledge -> False on a serial actor."""
    serial = h.spawn()
    owner = h.spawn()
    executed = Recorder()
    engine = h.engine
    assert engine.pool_submit(serial.actor_id, serial.name, owner.actor_id, owner.name,
                              "op", 1, Rec(executed, "submit")) == 2
    assert engine.pool_reserve(serial.actor_id, serial.name, "op", 1) == 2
    assert engine.pool_submit_reserved(serial.actor_id, serial.name, owner.actor_id, owner.name,
                                       "op", 1, Rec(executed, "reserved")) == 2
    assert engine.pool_cancel_reservation(serial.actor_id, serial.name, "op", 1) is False
    assert engine.acknowledge(serial.actor_id, serial.name, "op", 1) is False
    time.sleep(0.05)
    assert executed.items() == []
    assert h.snapshot(serial)[:4] == ("running", 1, 0, 0)


def _bad_owners(owner) -> list[tuple[str, str]]:
    return [(owner.actor_id, owner.name + "-forged"), ("0" * 32, owner.name), ("unknown-id", "unknown-name")]


@case("EFF-13")
def test_eff_13_pool_submit_with_unknown_or_forged_owner_creates_no_token(h) -> None:
    """pool_submit with an unknown or forged owner -> 2 and no token: the slot stays free, the key is absent."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool)
    executed = Recorder()
    for index, (owner_id, owner_name) in enumerate(_bad_owners(owner.actor)):
        op = f"bad{index}"
        assert h.engine.pool_submit(pool.actor_id, pool.name, owner_id, owner_name, op, 1,
                                    Rec(executed, op)) == 2
        assert cancel(h, pool, op) is False
        assert ack(h, pool, op) is False
        assert submit_reserved(h, pool, owner.actor, op, Rec(executed, op)) == 2
        assert reserve(h, pool, "probe") == 0
        assert cancel(h, pool, "probe") is True
    time.sleep(0.05)
    assert executed.items() == []
    assert owner.log.items() == []


@case("EFF-13")
def test_eff_13_submit_reserved_with_bad_owner_keeps_token(h) -> None:
    """pool_submit_reserved with an unknown or forged owner -> 2; the reservation remains live."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool)
    executed = Recorder()
    assert reserve(h, pool, "r") == 0
    for owner_id, owner_name in _bad_owners(owner.actor):
        assert h.engine.pool_submit_reserved(pool.actor_id, pool.name, owner_id, owner_name, "r", 1,
                                             Rec(executed, "bad")) == 2
        assert reserve(h, pool, "probe") == 1
    assert submit_reserved(h, pool, owner.actor, "r", Rec(executed, "good")) == 0
    assert owner.wait_ops({"r"})
    assert executed.items() == ["good"]
    assert wait_until(lambda: owner.acks.items()[:1] == [True])
    assert reserve(h, pool, "r2") == 0
    for owner_id, owner_name in _bad_owners(owner.actor):
        assert h.engine.pool_submit_reserved(pool.actor_id, pool.name, owner_id, owner_name, "r2", 1,
                                             Rec(executed, "bad")) == 2
    assert cancel(h, pool, "r2") is True
    time.sleep(0.05)
    assert "bad" not in executed.items()
