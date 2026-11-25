# Decision record (file header; no inline comments by repository rule):
# - Completion-side pool cases of spec 3.3, 9.3, 9.4, 9.5 and 7 (EFF-02, 06,
#   07, 09, 12). Workers use the `h` trampoline (Python and native handler
#   kinds); owners are Python actors because they must call `acknowledge`.
# - EFF-02 sends `"panic"` for both handler kinds. With the Python kind the
#   trampoline raises a BaseException named PanicException (the backend's
#   twin, or the suite-local stand-in from backends), so the exact name
#   "PanicException" is asserted like every other Python-raised row. With the
#   native kind it is a Rust panic caught at the FFI boundary, whose error
#   name is backend-defined (9.3, E-9): only a non-empty str is asserted, the
#   same rule SUP-09 uses. The expected-error type is not special for pools:
#   it is a worker error too.
# - The pool's dead-letter sink is observed empty in EFF-02 and EFF-09: the
#   only pool dead-letter source is an undelivered completion to a stopped or
#   quarantined owner (7, EFF-07); failed payloads and payloads queued at stop
#   acceptance are never dead-lettered.
# - EFF-06 also covers an owner that fails while handling an un-acked
#   completion: 6.3.5 drops that command, so only continued delivery until
#   acknowledge (9.4, 9.1) lets the new generation see and ack it. The
#   delivery count is asserted as a lower bound only.
# - At-least-once (9.4) allows duplicates and does not mandate them, so cases
#   compare DISTINCT operation ids and never assert a delivery count; `ack`
#   idempotence is asserted (second ack -> False).
# - A `stop` that timed out is retried explicitly before expecting the
#   stop-side effects; the spec does not say whether confirmation proceeds
#   without a waiting `stop` call (5.4 step 6, CUS-05).
# - The spec allows workers to either pin on un-acked completions or continue;
#   no case depends on that choice.
"""EFF completion custody: worker errors, retry, owner-death dead letters, pool stop, message type."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import PANIC_TYPE_NAME, WAIT, Block, CustomBase, DeadLetters, Expected, Raise, Rec, Recorder, case, wait_until

SUPERVISION_KINDS = {"child_failed", "restart_scheduled", "child_restarted", "child_quarantined"}


class Owner:
    """Python owner actor: records completions in order, optionally acks and records ack results."""

    def __init__(self, harness, pool, ack: bool = True, capacity: int = 64, profile: str = "fast",
                 trace: Recorder | None = None, dead_letters=None, withhold: frozenset = frozenset()) -> None:
        self.pool = pool
        self.ack = ack
        self.withhold = withhold
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
                if self.ack and message.operation_id not in self.withhold:
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


def cancel(h, pool, op: str, generation: int = 1) -> bool:
    return h.engine.pool_cancel_reservation(pool.actor_id, pool.name, op, generation)


def ack(h, pool, op: str, generation: int = 1) -> bool:
    return h.engine.acknowledge(pool.actor_id, pool.name, op, generation)


def _failures(h) -> list[tuple[str, object, str]]:
    failures: list[tuple[str, object, str]] = [
        ("exception", Raise(ValueError("worker boom")), "ValueError"),
        ("expected", Raise(Expected()), "Expected"),
        ("system-exit", Raise(SystemExit(3)), "SystemExit"),
        ("keyboard-interrupt", Raise(KeyboardInterrupt()), "KeyboardInterrupt"),
        ("custom-base", Raise(CustomBase("custom")), "CustomBase"),
    ]
    failures.append(("panic", "panic", PANIC_TYPE_NAME))
    return failures


@case("EFF-02")
def test_eff_02_worker_errors_become_completions_and_worker_continues(h) -> None:
    """Any BaseException or panic -> EffectCompleted(error=type name, result=None); the one worker continues."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=32, dead_letters=pool_dead)
    owner = Owner(h, pool)
    executed = Recorder()
    failures = _failures(h)
    for label, payload, _ in failures:
        assert submit(h, pool, owner.actor, f"err-{label}", payload) == 0
        assert submit(h, pool, owner.actor, f"ok-{label}", Rec(executed, label)) == 0
    expected_ops = {f"{kind}-{label}" for label, _, _ in failures for kind in ("err", "ok")}
    assert owner.wait_ops(expected_ops)
    by_op = {message.operation_id: message for message in owner.log.items()}
    for label, _, name in failures:
        failed, succeeded = by_op[f"err-{label}"], by_op[f"ok-{label}"]
        assert (failed.error, failed.result) == (name, None), label
        assert (succeeded.error, succeeded.result) == (None, label), label
    assert executed.items() == [label for label, _, _ in failures]
    assert h.snapshot(pool)[0:2] == ("running", 1)
    assert h.snapshot(pool)[4] == 0
    assert not SUPERVISION_KINDS & {event[0] for event in h.events.items()}
    assert submit(h, pool, owner.actor, "after-all", Rec(executed, "after-all")) == 0
    assert owner.wait_ops({"after-all"})
    assert pool_dead.items() == []


@case("EFF-06")
def test_eff_06_completion_retried_while_owner_mailbox_full(h) -> None:
    """Owner mailbox full -> the completion retries (token kept) until admitted, handled and acked."""
    pool = h.spawn_pool(pool_size=1, capacity=1, retry_interval=0.01)
    owner = Owner(h, pool, capacity=1)
    executed = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(owner.actor, Block(release, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.tell(owner.actor, Rec(executed, "filler")) == 0
        assert h.tell(owner.actor, Rec(executed, "overflow")) == 1
        assert submit(h, pool, owner.actor, "op", Rec(executed, "payload")) == 0
        assert wait_until(lambda: "payload" in executed.items())
        time.sleep(0.1)
        assert owner.log.items() == []
        assert reserve(h, pool, "probe") == 1
    finally:
        release.set()
    assert owner.wait_ops({"op"})
    assert executed.items()[:2] == ["payload", "filler"]
    assert wait_until(lambda: True in owner.acks.items())
    assert owner.ops() == {"op"}
    assert wait_until(lambda: reserve(h, pool, "probe") == 0)
    assert cancel(h, pool, "probe") is True


@case("EFF-06")
def test_eff_06_restart_pending_owner_keeps_completion_for_new_generation(h) -> None:
    """A restart_pending owner admits the completion into its kept mailbox; the new generation acks it."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    owner = Owner(h, pool, profile="slow")
    assert h.tell(owner.actor, Raise(RuntimeError("owner fails"))) == 0
    assert h.wait_state(owner.actor, "restart_pending")
    assert submit(h, pool, owner.actor, "op", Rec(Recorder(), "v")) == 0
    assert wait_until(lambda: h.snapshot(owner.actor)[2] >= 1)
    assert h.state(owner.actor) == "restart_pending"
    assert owner.log.items() == []
    assert owner.wait_ops({"op"})
    assert h.events.wait_kind(owner.actor, "child_restarted")
    assert h.generation(owner.actor) == 2
    assert wait_until(lambda: True in owner.acks.items())
    assert wait_until(lambda: reserve(h, pool, "probe") == 0)
    assert cancel(h, pool, "probe") is True


@case("EFF-06")
def test_eff_06_owner_failing_on_unacked_completion_gets_it_again(h) -> None:
    """The owner fails while handling the completion before acking: delivery continues to the new generation."""
    pool = h.spawn_pool(pool_size=1, capacity=1)
    completed = h.module.EffectCompleted
    engine = h.engine
    seen = Recorder()
    acks = Recorder()

    def handle(message: object) -> object:
        if isinstance(message, completed):
            seen.append(message.operation_id)
            if len(seen) == 1:
                raise RuntimeError("owner fails before acking")
            acks.append(engine.acknowledge(pool.actor_id, pool.name, message.operation_id, message.generation))
            return None
        return message() if callable(message) else message

    owner = h.spawn(profile="fast", factory=lambda: handle)
    assert submit(h, pool, owner, "op", Rec(Recorder(), "v")) == 0
    assert h.events.wait_kind(owner, "child_restarted")
    assert wait_until(lambda: True in acks.items())
    assert h.generation(owner) == 2
    assert seen.items().count("op") >= 2
    assert set(seen.items()) == {"op"}
    assert wait_until(lambda: reserve(h, pool, "probe") == 0)
    assert cancel(h, pool, "probe") is True


@case("EFF-06")
def test_eff_06_at_least_once_distinct_ids_and_idempotent_ack(h) -> None:
    """Without ack every completion keeps arriving (duplicates allowed); ack settles once, then False."""
    pool = h.spawn_pool(pool_size=5, capacity=8)
    owner = Owner(h, pool, ack=False)
    ops = {f"op{index}" for index in range(5)}
    for op in sorted(ops):
        assert submit(h, pool, owner.actor, op, Rec(Recorder(), op)) == 0
    assert owner.wait_ops(ops)
    time.sleep(0.1)
    assert owner.ops() == ops
    assert all(message.result == message.operation_id for message in owner.log.items())
    for op in sorted(ops):
        assert ack(h, pool, op) is True
        assert ack(h, pool, op) is False
    time.sleep(0.1)
    settled = len(owner.log)
    time.sleep(0.2)
    assert len(owner.log) == settled
    for index in range(8):
        assert reserve(h, pool, f"probe{index}") == 0
    for index in range(8):
        assert cancel(h, pool, f"probe{index}") is True


@case("EFF-07")
def test_eff_07_stopped_owner_completion_dead_lettered_once_to_pool_sink(h) -> None:
    """Owner stopped -> the EffectCompleted goes ONCE to the pool's sink, reason 'stopped', token settled."""
    pool_dead, owner_dead = DeadLetters(), DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=1, dead_letters=pool_dead)
    owner = Owner(h, pool, dead_letters=owner_dead)
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner.actor, "op", Block(release, entered, "late", limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.stop(owner.actor) is True
    finally:
        release.set()
    assert pool_dead.wait_len(1)
    assert reserve(h, pool, "probe") == 0
    assert cancel(h, pool, "probe") is True
    message, reason = pool_dead.items()[0]
    assert reason == "stopped"
    assert type(message) is h.module.EffectCompleted
    assert (message.operation_id, message.generation, message.result, message.error) == ("op", 1, "late", None)
    time.sleep(0.2)
    assert len(pool_dead) == 1
    assert owner_dead.items() == []
    assert owner.log.items() == []
    assert ack(h, pool, "op") is False


@case("EFF-07")
def test_eff_07_quarantined_owner_completion_dead_lettered_with_owner_reason(h) -> None:
    """Owner quarantined -> the completion goes once to the pool's sink with reason 'quarantined'."""
    pool_dead, owner_dead = DeadLetters(), DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=1, dead_letters=pool_dead)
    owner = Owner(h, pool, profile="never", dead_letters=owner_dead)
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner.actor, "op", Block(release, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.tell(owner.actor, Raise(RuntimeError("owner fails"))) == 0
        assert h.wait_state(owner.actor, "quarantined")
    finally:
        release.set()
    assert pool_dead.wait_len(1)
    assert reserve(h, pool, "probe") == 0
    assert cancel(h, pool, "probe") is True
    message, reason = pool_dead.items()[0]
    assert reason == "quarantined"
    assert type(message) is h.module.EffectCompleted
    assert message.operation_id == "op"
    time.sleep(0.2)
    assert len(pool_dead) == 1
    assert owner_dead.items() == []
    assert owner.log.items() == []


@case("EFF-07")
def test_eff_07_stopping_owner_keeps_retrying_until_stopped(h) -> None:
    """Owner stopping (stop timed out) is not terminal: no dead letter, token held, until stop confirms."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=1, dead_letters=pool_dead)
    owner = Owner(h, pool)
    hold, held = threading.Event(), threading.Event()
    gate, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(owner.actor, Block(hold, held, limit=5.0)) == 0
        assert held.wait(WAIT)
        assert submit(h, pool, owner.actor, "op", Block(gate, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.stop(owner.actor, timeout=0.05) is False
        assert h.state(owner.actor) == "stopping"
        gate.set()
        time.sleep(0.2)
        assert pool_dead.items() == []
        assert reserve(h, pool, "probe") == 1
        assert h.state(owner.actor) == "stopping"
    finally:
        gate.set()
        hold.set()
    assert h.stop(owner.actor) is True
    assert pool_dead.wait_len(1)
    assert [reason for _, reason in pool_dead.items()] == ["stopped"]
    assert reserve(h, pool, "probe") == 0
    assert cancel(h, pool, "probe") is True
    assert owner.log.items() == []


@case("EFF-07")
def test_eff_07_draining_owner_keeps_receiving_completions(h) -> None:
    """A draining owner still receives the completion (external tell closed), handles and acks it."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=1, dead_letters=pool_dead)
    owner = Owner(h, pool)
    hold, held = threading.Event(), threading.Event()
    gate, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(owner.actor, Block(hold, held, limit=5.0)) == 0
        assert held.wait(WAIT)
        assert submit(h, pool, owner.actor, "op", Block(gate, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        completed, _, remaining = h.drain([owner.actor], timeout=0.05)
        assert (completed, remaining) == (False, [owner.actor.name])
        assert h.tell(owner.actor, Rec(Recorder(), "external")) == 2
        gate.set()
        assert wait_until(lambda: h.snapshot(owner.actor)[2] >= 1)
    finally:
        gate.set()
        hold.set()
    assert owner.wait_ops({"op"})
    assert wait_until(lambda: owner.acks.items()[:1] == [True])
    assert pool_dead.items() == []
    assert h.drain([owner.actor], timeout=WAIT)[0] is True
    assert h.state(owner.actor) == "stopped"


@case("EFF-09")
def test_eff_09_pool_stop_executes_queue_and_waits_for_acks(h) -> None:
    """Stop closes token creation, queued payloads still run, workers exit only after every ack."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=4, dead_letters=pool_dead)
    owner = Owner(h, pool, withhold=frozenset({"op3"}))
    executed = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert submit(h, pool, owner.actor, "op1", Block(release, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert submit(h, pool, owner.actor, "op2", Rec(executed, "op2")) == 0
        assert submit(h, pool, owner.actor, "op3", Rec(executed, "op3")) == 0
        assert h.stop(pool, timeout=0.05) is False
        assert h.state(pool) == "stopping"
        assert submit(h, pool, owner.actor, "op4", Rec(executed, "op4")) == 2
        assert reserve(h, pool, "op5") == 2
    finally:
        release.set()
    assert owner.wait_ops({"op1", "op2", "op3"})
    assert executed.items() == ["op2", "op3"]
    assert wait_until(lambda: owner.acks.items().count(True) == 2)
    assert h.stop(pool, timeout=0.1) is False
    assert h.state(pool) == "stopping"
    assert ack(h, pool, "op3") is True
    assert h.stop(pool) is True
    assert pool_dead.items() == []
    assert h.state(pool) == "stopped"
    assert h.events.wait_kind(pool, "child_stopped")
    time.sleep(0.05)
    assert h.events.kinds(pool).count("child_stopped") == 1


@case("EFF-09")
def test_eff_09_pool_stop_settles_by_cancel_and_owner_death(h) -> None:
    """A stopping pool settles a reservation by cancel and an un-acked completion by owner-death dead letter."""
    pool_dead = DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=4, dead_letters=pool_dead)
    owner = Owner(h, pool, ack=False)
    release, entered = threading.Event(), threading.Event()
    try:
        assert reserve(h, pool, "reserved") == 0
        assert submit(h, pool, owner.actor, "op", Block(release, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.stop(pool, timeout=0.05) is False
        assert cancel(h, pool, "reserved") is True
    finally:
        release.set()
    assert owner.wait_ops({"op"})
    assert h.stop(pool, timeout=0.1) is False
    assert pool_dead.items() == []
    assert h.stop(owner.actor) is True
    assert h.stop(pool) is True
    assert [(m.operation_id, reason) for m, reason in pool_dead.items()] == [("op", "stopped")]


@case("EFF-12")
def test_eff_12_completion_fields_exact(h) -> None:
    """operation_id / generation echo the submit key; result is the returned object; error the type name."""
    pool = h.spawn_pool(pool_size=1, capacity=8)
    owner = Owner(h, pool)
    sentinel = object()
    assert submit(h, pool, owner.actor, "value", lambda: sentinel, generation=7) == 0
    assert submit(h, pool, owner.actor, "none", lambda: None, generation=8) == 0
    assert submit(h, pool, owner.actor, "error", Raise(LookupError("x")), generation=9) == 0
    assert owner.wait_ops({"value", "none", "error"})
    by_op = {message.operation_id: message for message in owner.log.items()}
    for message in by_op.values():
        assert type(message) is h.module.EffectCompleted
    assert by_op["value"].generation == 7
    assert by_op["value"].result is sentinel
    assert by_op["value"].error is None
    assert (by_op["none"].generation, by_op["none"].result, by_op["none"].error) == (8, None, None)
    assert (by_op["error"].generation, by_op["error"].result, by_op["error"].error) == (9, None, "LookupError")
    assert wait_until(lambda: owner.acks.items().count(True) == 3)


@case("EFF-12")
def test_eff_12_effect_completed_signature_and_immutability(backend) -> None:
    """EffectCompleted(operation_id, generation, result=None, error=None) constructs and is immutable."""
    completed_type = backend.EffectCompleted
    payload = object()
    plain = completed_type("op", 3)
    assert (plain.operation_id, plain.generation, plain.result, plain.error) == ("op", 3, None, None)
    positional = completed_type("op", 4, payload, None)
    assert positional.result is payload and positional.error is None
    keyword = completed_type(operation_id="op", generation=5, result=None, error="ValueError")
    assert (keyword.operation_id, keyword.generation, keyword.result, keyword.error) == ("op", 5, None, "ValueError")
    for field, value in (("operation_id", "x"), ("generation", 9), ("result", 1), ("error", "E")):
        with pytest.raises((AttributeError, TypeError)):
            setattr(keyword, field, value)
        with pytest.raises((AttributeError, TypeError)):
            delattr(keyword, field)
    assert (keyword.operation_id, keyword.generation, keyword.result, keyword.error) == ("op", 5, None, "ValueError")


@case("EFF-12")
def test_eff_12_delivered_as_plain_tell_to_designated_owner(h) -> None:
    """The completion enters the designated owner's FIFO mailbox like a tell; other owners see nothing."""
    pool = h.spawn_pool(pool_size=1, capacity=4, retry_interval=1.0)
    trace = Recorder()
    owner = Owner(h, pool, trace=trace)
    bystander = Owner(h, pool)
    release, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(owner.actor, Block(release, entered, limit=5.0)) == 0
        assert entered.wait(WAIT)
        assert h.tell(owner.actor, Rec(trace, "before")) == 0
        assert submit(h, pool, owner.actor, "op", lambda: "payload") == 0
        assert wait_until(lambda: h.snapshot(owner.actor)[2] == 2)
        assert h.tell(owner.actor, Rec(trace, "after")) == 0
    finally:
        release.set()
    assert wait_until(lambda: "after" in trace.items())
    items = trace.items()
    first = next(i for i, item in enumerate(items) if isinstance(item, h.module.EffectCompleted))
    assert items.index("before") < first < items.index("after")
    assert items[first].operation_id == "op" and items[first].result == "payload"
    assert bystander.log.items() == []
    completed = [e for e in h.events.of(owner.actor) if e[0] == "command_completed"]
    assert "EffectCompleted" in [event[4] for event in completed]
