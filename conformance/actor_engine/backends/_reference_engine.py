# Decision record (file header; no inline comments by repository rule):
# - Serial actors: one daemon thread per actor serves the mailbox, runs the
#   restart delay, calls the factory on restart and performs stop
#   confirmation, so every per-actor transition is ordered by construction
#   (5.1, 5.4, EVT-01). The restart-budget deque is pruned only on a failure
#   (6.3.1), cleared by reset (6.4) and frozen at quarantine (5.1 row 5);
#   snapshot reports its length without mutating it (5.1 counters, 5.3).
# - Drain waits until ALL targets are idle at the same instant before stopping
#   any of them; stopping idle owners early would dead-letter completions that
#   DRN-09 requires the draining owner to receive.
# - Timer fire-time classification checks stopping/stopped/quarantined before
#   the generation fence, so a timer racing a stop is dead-lettered, never
#   silently dropped (8, TMR-05). The spec table leaves the stop+mismatch
#   combination open; this order follows the "never silently lost" sentence.
# - A timer that fires into a draining actor meets closed admission and is
#   dropped: drain cancels pending timers at its start (5.5) and the spec
#   names no dead-letter reason for drain.
# - I2 keyed actors: spawn may carry a (group, key) pair. The engine keeps
#   one index from the pair to the live record; lookup resolves the pair, and
#   the index entry is released exactly when the record confirms its stop, so
#   a later same-key spawn mints a new actor. A supervision restart keeps the
#   record and therefore the key. Keyed event tuples append (group, key) after
#   restart_delay; keyless tuples keep the frozen eight fields.
# - Keyed spawn claims a pair in a "starting" slot before building its
#   handler. Every other spawn sees the occupied slot and raises the stable
#   key-in-use spawn refusal immediately. There are no waiters or shared
#   outcomes. Factory failure, pump-start failure and shutdown retire the
#   claim before returning. A thread-start failure inside the install lock
#   removes the record and the key entry under the SAME lock hold: releasing
#   and reacquiring around the cleanup let a concurrent shutdown snapshot a
#   record whose thread never started and crash joining it (the fifth review
#   interleave, 2026-11-20); the keyless path keeps its origin/dev behavior.
# - Key semantics across stop states: lookup resolves the original handle
#   while the actor is stopping, restart_pending or quarantined; a same-key
#   spawn in those states is rejected. Only a CONFIRMED stop releases the
#   pair: the remaining mailbox dead-letters with reason "stopped" under
#   the existing stop semantics, and the next same-key spawn starts a new
#   actor with an empty mailbox.
"""Reference engine: serial actors, supervision, stop, drain, timers, ask."""

from __future__ import annotations

import heapq
import math
import random
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from backends._reference_pools import Pool

RUNNING = "running"
RESTART_PENDING = "restart_pending"
QUARANTINED = "quarantined"
STOPPING = "stopping"
STOPPED = "stopped"
OVERLOAD_RETRY = 0.01


class _Reply:
    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None


class _Entry:
    __slots__ = ("command", "reply")

    def __init__(self, command: object, reply: _Reply | None) -> None:
        self.command = command
        self.reply = reply


class _Timer:
    __slots__ = ("timer_id", "actor_id", "generation", "command", "state")

    def __init__(self, timer_id: str, actor_id: str, generation: int, command: object) -> None:
        self.timer_id = timer_id
        self.actor_id = actor_id
        self.generation = generation
        self.command = command
        self.state = "pending"


class _KeyStart:
    """Claimed-but-not-yet-live key slot."""


class Cell:
    kind = "serial"

    def __init__(self, engine: Engine, actor_id: str, name: str, factory: Any,
                 capacity: int, policy: tuple, dead_letter_sink: Any, event_sink: Any,
                 group: str | None = None, key: str | None = None) -> None:
        self.actor_id = actor_id
        self.name = name
        self.group = group
        self.key = key
        self.factory = factory
        self.capacity = capacity
        self.policy = policy
        self.dead_letter_sink = dead_letter_sink
        self.event_sink = event_sink
        self.state = RUNNING
        self.generation = 1
        self.mailbox: deque[_Entry] = deque()
        self.executing = False
        self.handler: Any = None
        self.failures: deque[float] = deque()
        self.restart_due: float | None = None
        self.draining = False
        self.accepted_stop = False
        self.confirmed = threading.Event()
        self.timer_ids: set[str] = set()
        self.cv = threading.Condition(engine._lock)
        self.thread: threading.Thread | None = None

    def idle(self) -> bool:
        return not self.mailbox and not self.executing

    def load(self) -> tuple[int, int]:
        return len(self.mailbox), 1 if self.executing else 0

    def accept_stop(self) -> None:
        self.accepted_stop = True
        self.state = STOPPING
        self.restart_due = None
        self.cv.notify_all()


class Engine:
    """ABI v1 ``Engine`` (spec 3.1/3.2), pure Python."""

    _errors: tuple = ()
    _native_type: type = object
    _lookup_native: Callable[[str], Any]

    def __init__(self, *, policies: dict, jitter_seed: int | None,
                 event_sink: Callable[[tuple], None], expected_error_type: type) -> None:
        self._policies = _validate_policies(policies)
        if not isinstance(expected_error_type, type):
            raise ValueError("expected_error_type must be a type")
        self._rng = random.Random(jitter_seed) if jitter_seed is not None else random.Random()
        self._sink = event_sink
        self._expected = expected_error_type
        self._lock = threading.RLock()
        self._activity = threading.Condition(self._lock)
        self._records: dict[str, Any] = {}
        self._keys: dict[tuple[str, str], Cell | _KeyStart] = {}
        self._test_pump_start_failure = False
        self._lifecycle = "active"
        self._timers: dict[str, _Timer] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._seq = 0
        self._timer_cv = threading.Condition(self._lock)
        self._timers_closed = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_result: bool | None = None
        self._timer_thread = threading.Thread(target=self._timer_loop, name="ref-timers", daemon=True)
        self._timer_thread.start()

    def spawn(self, name: str, handler_factory: Any, capacity: int, profile: str,
              dead_letter_sink: Any = None, event_sink: Any = None,
              group: Any = None, key: Any = None) -> str:
        policy = self._validate_spec(name, handler_factory, capacity, profile)
        pair = _key_pair(group, key, required=False)
        self._require_spawnable()
        if pair is None:
            handler = self._build_first(handler_factory)
            actor_id = uuid.uuid4().hex
            cell = Cell(self, actor_id, name, handler_factory, capacity, policy,
                        dead_letter_sink, event_sink)
            cell.handler = handler
            with self._lock:
                self._require_spawnable()
                self._records[actor_id] = cell
                cell.thread = threading.Thread(target=self._serve, args=(cell,), name=f"ref-{name}", daemon=True)
                cell.thread.start()
            return actor_id
        with self._lock:
            if self._live_by_key(pair) is not None or pair in self._keys:
                raise self._errors[2]("key-in-use")
            starter = _KeyStart()
            self._keys[pair] = starter
        try:
            handler = self._build_first(handler_factory)
        except BaseException:
            with self._lock:
                if self._keys.get(pair) is starter:
                    del self._keys[pair]
            raise
        actor_id = uuid.uuid4().hex
        cell = Cell(self, actor_id, name, handler_factory, capacity, policy,
                    dead_letter_sink, event_sink, group, key)
        cell.handler = handler
        try:
            with self._lock:
                self._require_spawnable()
                if self._keys.get(pair) is not starter:
                    raise RuntimeError("engine is drained; spawn refused")
                self._records[actor_id] = cell
                self._keys[pair] = cell
                if self._test_pump_start_failure:
                    self._test_pump_start_failure = False
                    raise RuntimeError("injected pump start failure")
                cell.thread = threading.Thread(target=self._serve, args=(cell,), name=f"ref-{name}", daemon=True)
                try:
                    cell.thread.start()
                except BaseException:
                    self._records.pop(actor_id, None)
                    if self._keys.get(pair) is cell:
                        del self._keys[pair]
                    raise
        except BaseException as exc:
            with self._lock:
                self._records.pop(actor_id, None)
                if self._keys.get(pair) is cell:
                    del self._keys[pair]
                elif self._keys.get(pair) is starter:
                    del self._keys[pair]
            if str(exc) == "engine is drained; spawn refused":
                raise
            failure = self._errors[2](f"engine failed to start actor: {exc}")
            raise failure from exc
        return actor_id

    def _test_registry_counts(self) -> tuple[int, int, int]:
        return len(self._records), len(self._keys), 0

    def lookup(self, group: Any, key: Any) -> tuple[str, str]:
        pair = _key_pair(group, key, required=True)
        with self._lock:
            cell = self._live_by_key(pair)
            if cell is None:
                raise KeyError(pair)
            return cell.actor_id, cell.name

    def spawn_pool(self, name: str, worker_handler_factory: Any, pool_size: int, capacity: int,
                   profile: str, dead_letter_sink: Any = None, event_sink: Any = None,
                   retry_interval: float = 0.01) -> str:
        self._validate_spec(name, worker_handler_factory, capacity, profile)
        if not _is_int(pool_size) or pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        if not _is_number(retry_interval) or not retry_interval > 0:
            raise ValueError("retry_interval must be > 0")
        self._require_spawnable()
        handlers = [self._build_first(worker_handler_factory) for _ in range(pool_size)]
        actor_id = uuid.uuid4().hex
        pool = Pool(self, actor_id, name, handlers, capacity, dead_letter_sink, event_sink, float(retry_interval))
        with self._lock:
            self._require_spawnable()
            self._records[actor_id] = pool
            pool.start()
        return actor_id

    def tell(self, actor_id: str, name: str, command: object) -> int:
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None or record.kind != "serial":
                return 2
            return self._admit(record, _Entry(command, None), external=True)

    def ask(self, actor_id: str, name: str, command: object, timeout: float) -> object:
        admission_error = self._errors[0]
        reply = _Reply()
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None or record.kind != "serial":
                raise admission_error("closed")
            outcome = self._admit(record, _Entry(command, reply), external=True)
        if outcome == 1:
            raise admission_error("overloaded")
        if outcome == 2:
            raise admission_error("closed")
        if not reply.done.wait(max(0.0, float(timeout))):
            raise TimeoutError("ask timed out")
        if reply.error is not None:
            raise reply.error
        return reply.value

    def send_after(self, actor_id: str, name: str, delay: float, command: object) -> str:
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None:
                raise KeyError(actor_id)
            if record.kind != "serial":
                raise ValueError("send_after target is a pool")
            if not _is_number(delay) or delay < 0 or math.isnan(delay):
                raise ValueError("delay must be >= 0")
            timer_id = uuid.uuid4().hex
            self._timers[timer_id] = _Timer(timer_id, actor_id, record.generation, command)
            record.timer_ids.add(timer_id)
            self._push_timer(time.monotonic() + float(delay), timer_id)
            return timer_id

    def cancel_after(self, timer_id: str) -> bool:
        with self._lock:
            timer = self._timers.get(timer_id)
            if timer is None or timer.state != "pending":
                return False
            self._drop_timer(timer)
            return True

    def acknowledge(self, actor_id: str, name: str, operation_id: str, generation: int) -> bool:
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None or record.kind != "pool":
                return False
            return record.acknowledge(operation_id, generation)

    def snapshot(self, actor_id: str, name: str) -> tuple[str, int, int, int, int]:
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None:
                raise KeyError(actor_id)
            queued, in_flight = record.load()
            if record.kind == "pool":
                return record.state, 1, queued, in_flight, 0
            return record.state, record.generation, queued, in_flight, len(record.failures)

    def stop(self, actor_id: str, name: str, timeout: float) -> bool:
        with self._lock:
            record = self._resolve(actor_id, name)
            if record is None:
                return False
            if record.state == STOPPED:
                return True
            self_call = (record.kind == "serial" and record.executing
                         and record.thread is not None
                         and threading.get_ident() == record.thread.ident)
            if not record.accepted_stop:
                self._accept_stop(record)
            if self_call:
                return True
        return record.confirmed.wait(max(0.0, float(timeout)))

    def reset(self, actor_id: str, name: str) -> bool:
        with self._lock:
            record = self._resolve(actor_id, name)
            if (record is None or record.kind != "serial" or record.state != QUARANTINED
                    or record.draining):
                return False
            record.failures.clear()
            record.state = RESTART_PENDING
            record.restart_due = None
            generation = record.generation
        self._emit(record, ("restart_scheduled", record.actor_id, record.name, generation,
                            None, None, None, 0.0))
        with self._lock:
            if record.state == RESTART_PENDING and record.restart_due is None:
                record.restart_due = time.monotonic()
                record.cv.notify_all()
        return True

    def drain(self, actor_ids: list[str] | None, timeout: float) -> tuple[bool, float, list[str]]:
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout))
        engine_wide = actor_ids is None
        with self._lock:
            if engine_wide:
                targets = list(self._records.values())
                if self._lifecycle == "active":
                    self._lifecycle = "draining"
            else:
                for actor_id in actor_ids:
                    if actor_id not in self._records:
                        raise KeyError(actor_id)
                targets = [self._records[actor_id] for actor_id in actor_ids]
            for target in targets:
                target.draining = True
                self._cancel_timers(target)
        while True:
            with self._lock:
                if all(target.state == STOPPED or target.idle() for target in targets):
                    for target in targets:
                        if target.state != STOPPED and not target.accepted_stop:
                            self._accept_stop(target)
                    break
            if time.monotonic() >= deadline:
                return self._drain_result(started, targets, engine_wide)
            time.sleep(0.002)
        for target in targets:
            if target.state != STOPPED:
                target.confirmed.wait(max(0.0, deadline - time.monotonic()))
        return self._drain_result(started, targets, engine_wide)

    def shutdown(self, timeout: float) -> bool:
        with self._shutdown_lock:
            if self._shutdown_result is not None:
                return self._shutdown_result
            deadline = time.monotonic() + max(0.0, float(timeout))
            with self._lock:
                self._lifecycle = "drained"
                self._cancel_key_starts_locked()
                for timer in list(self._timers.values()):
                    self._drop_timer(timer)
                serial = [r for r in self._records.values() if r.kind == "serial"]
                pools = [r for r in self._records.values() if r.kind == "pool"]
                for record in serial:
                    if record.state != STOPPED and not record.accepted_stop:
                        self._accept_stop(record)
            ok = all(r.confirmed.wait(max(0.0, deadline - time.monotonic())) or r.state == STOPPED
                     for r in serial)
            with self._lock:
                for record in pools:
                    if record.state != STOPPED and not record.accepted_stop:
                        self._accept_stop(record)
            ok = all(r.confirmed.wait(max(0.0, deadline - time.monotonic())) or r.state == STOPPED
                     for r in pools) and ok
            with self._lock:
                self._timers_closed = True
                self._timer_cv.notify_all()
            threads = [self._timer_thread]
            for record in serial + pools:
                threads.extend(record.threads() if record.kind == "pool" else [record.thread])
            for thread in threads:
                if thread is not None and thread is not threading.current_thread():
                    thread.join(max(0.0, deadline - time.monotonic()))
            ok = ok and not any(t is not None and t.is_alive() for t in threads)
            self._shutdown_result = ok
            return ok

    def _cancel_key_starts_locked(self) -> None:
        self._keys = {
            pair: slot
            for pair, slot in self._keys.items()
            if not isinstance(slot, _KeyStart)
        }

    def pool_submit(self, actor_id: str, name: str, owner_id: str, owner_name: str,
                    operation_id: str, generation: int, payload: object) -> int:
        with self._lock:
            pool = self._pool(actor_id, name)
            if pool is None or self._owner(owner_id, owner_name) is None:
                return 2
            return pool.submit(operation_id, generation, (owner_id, owner_name), payload)

    def pool_reserve(self, actor_id: str, name: str, operation_id: str, generation: int) -> int:
        with self._lock:
            pool = self._pool(actor_id, name)
            return 2 if pool is None else pool.reserve(operation_id, generation)

    def pool_submit_reserved(self, actor_id: str, name: str, owner_id: str, owner_name: str,
                             operation_id: str, generation: int, payload: object) -> int:
        with self._lock:
            pool = self._pool(actor_id, name)
            if pool is None or not pool.has_token(operation_id, generation):
                return 2
            if self._owner(owner_id, owner_name) is None:
                return 2
            return pool.submit_reserved(operation_id, generation, (owner_id, owner_name), payload)

    def pool_cancel_reservation(self, actor_id: str, name: str, operation_id: str,
                                generation: int) -> bool:
        with self._lock:
            pool = self._pool(actor_id, name)
            return False if pool is None else pool.cancel(operation_id, generation)

    def native_handler_factory(self, name: str) -> Any:
        return self._lookup_native(name)

    def _serve(self, cell: Cell) -> None:
        while True:
            with self._lock:
                action = self._next_action(cell)
            if action[0] == "confirm":
                self._confirm(cell)
                return
            if action[0] == "run":
                self._execute(cell, action[1], action[2], action[3])
            elif action[0] == "restart":
                self._restart(cell)

    def _next_action(self, cell: Cell) -> tuple:
        while True:
            if cell.state == STOPPING:
                return ("confirm",)
            if cell.state == RUNNING and cell.mailbox:
                cell.executing = True
                return ("run", cell.mailbox.popleft(), cell.handler, cell.generation)
            if cell.state == RESTART_PENDING and cell.restart_due is not None:
                wait = cell.restart_due - time.monotonic()
                if wait <= 0:
                    cell.restart_due = None
                    return ("restart",)
                cell.cv.wait(wait)
                continue
            cell.cv.wait()

    def _execute(self, cell: Cell, entry: _Entry, handler: Any, generation: int) -> None:
        command_type = type(entry.command).__name__
        try:
            result = handler(entry.command)
        except BaseException as exc:
            if isinstance(exc, self._expected):
                self._emit(cell, ("command_rejected", cell.actor_id, cell.name, generation,
                                  command_type, getattr(exc, "code", None),
                                  getattr(exc, "detail", None), None))
                self._finish(cell, entry.reply, error=exc)
            else:
                self._fail(cell, exc, command_type)
                self._finish(cell, entry.reply, error=self._errors[1](f"{type(exc).__name__}: {exc}"),
                             executing_cleared=True)
            return
        self._emit(cell, ("command_completed", cell.actor_id, cell.name, generation,
                          command_type, None, None, None))
        self._finish(cell, entry.reply, value=result)

    def _finish(self, cell: Cell, reply: _Reply | None, value: object = None,
                error: BaseException | None = None, executing_cleared: bool = False) -> None:
        if not executing_cleared:
            with self._lock:
                cell.executing = False
                self._activity.notify_all()
        if reply is not None:
            reply.value = value
            reply.error = error
            reply.done.set()

    def _restart(self, cell: Cell) -> None:
        try:
            handler = cell.factory()
        except BaseException as exc:
            self._fail(cell, exc, None)
            return
        with self._lock:
            if cell.state != RESTART_PENDING:
                return
            cell.generation += 1
            cell.handler = handler
            cell.state = RUNNING
            generation = cell.generation
        self._emit(cell, ("child_restarted", cell.actor_id, cell.name, generation, None, None, None, None))

    def _fail(self, cell: Cell, exc: BaseException, command_type: str | None) -> None:
        now = time.monotonic()
        dead: list[object] = []
        code, detail = type(exc).__name__, str(exc)
        with self._lock:
            cell.executing = False
            generation = cell.generation
            events = [("child_failed", cell.actor_id, cell.name, generation, command_type, code, detail, None)]
            if cell.state == RUNNING or cell.state == RESTART_PENDING:
                max_restarts, window, base, cap, jitter = cell.policy
                cell.failures.append(now)
                while cell.failures and cell.failures[0] < now - window:
                    cell.failures.popleft()
                if len(cell.failures) > max_restarts:
                    cell.state = QUARANTINED
                    dead = [entry.command for entry in cell.mailbox]
                    cell.mailbox.clear()
                    self._cancel_timers(cell)
                    events.append(("child_quarantined", cell.actor_id, cell.name, generation,
                                   None, code, detail, None))
                else:
                    attempt = len(cell.failures)
                    nominal = min(cap, base * (2 ** (attempt - 1)))
                    delay = max(0.0, nominal + nominal * jitter * (2 * self._rng.random() - 1))
                    cell.state = RESTART_PENDING
                    cell.restart_due = now + delay
                    events.append(("restart_scheduled", cell.actor_id, cell.name, generation,
                                   None, None, None, delay))
            self._activity.notify_all()
        for event in events:
            self._emit(cell, event)
        self._dead_letter(cell, dead, "quarantined")

    def _confirm(self, cell: Cell) -> None:
        with self._lock:
            dead = [entry.command for entry in cell.mailbox]
            cell.mailbox.clear()
            generation = cell.generation
        self._dead_letter(cell, dead, "stopped")
        self._emit(cell, ("child_stopped", cell.actor_id, cell.name, generation, None, None, None, None))
        with self._lock:
            cell.state = STOPPED
            cell.handler = None
            if cell.group is not None:
                pair = (cell.group, cell.key)
                if self._keys.get(pair) is cell:
                    del self._keys[pair]
            cell.confirmed.set()
            self._activity.notify_all()

    def _admit(self, cell: Cell, entry: _Entry, external: bool) -> int:
        if cell.accepted_stop or cell.state in (STOPPING, STOPPED, QUARANTINED):
            return 2
        if external and cell.draining:
            return 2
        if len(cell.mailbox) >= cell.capacity:
            return 1
        cell.mailbox.append(entry)
        cell.cv.notify_all()
        return 0

    def _deliver(self, owner: tuple[str, str], message: object) -> tuple[int, str]:
        with self._lock:
            cell = self._resolve(owner[0], owner[1])
            if cell is None or cell.kind != "serial":
                return 2, STOPPED
            return self._admit(cell, _Entry(message, None), external=False), cell.state

    def _accept_stop(self, record: Any) -> None:
        self._cancel_timers(record)
        record.accept_stop()

    def _timer_loop(self) -> None:
        while True:
            with self._lock:
                timer = self._next_timer()
                if timer is None:
                    return
            self._fire(timer)

    def _next_timer(self) -> _Timer | None:
        while True:
            if self._timers_closed:
                return None
            now = time.monotonic()
            if self._heap and self._heap[0][0] <= now:
                _, _, timer_id = heapq.heappop(self._heap)
                timer = self._timers.get(timer_id)
                if timer is not None and timer.state == "pending":
                    return timer
                continue
            self._timer_cv.wait(self._heap[0][0] - now if self._heap else None)

    def _fire(self, timer: _Timer) -> None:
        reason = None
        with self._lock:
            if timer.state != "pending":
                return
            cell = self._records[timer.actor_id]
            if cell.state in (STOPPING, STOPPED) or cell.accepted_stop:
                reason = "stopped"
            elif cell.state == QUARANTINED:
                reason = "quarantined"
            elif cell.generation != timer.generation:
                self._drop_timer(timer)
                return
            else:
                outcome = self._admit(cell, _Entry(timer.command, None), external=True)
                if outcome == 1:
                    self._push_timer(time.monotonic() + OVERLOAD_RETRY, timer.timer_id)
                    return
            self._drop_timer(timer)
        if reason is not None:
            self._dead_letter(cell, [timer.command], reason)

    def _push_timer(self, due: float, timer_id: str) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (due, self._seq, timer_id))
        self._timer_cv.notify_all()

    def _drop_timer(self, timer: _Timer) -> None:
        timer.state = "done"
        self._timers.pop(timer.timer_id, None)
        record = self._records.get(timer.actor_id)
        if record is not None:
            record.timer_ids.discard(timer.timer_id)

    def _cancel_timers(self, record: Any) -> None:
        for timer_id in list(getattr(record, "timer_ids", ())):
            timer = self._timers.get(timer_id)
            if timer is not None:
                self._drop_timer(timer)

    def _drain_result(self, started: float, targets: list, engine_wide: bool) -> tuple[bool, float, list[str]]:
        with self._lock:
            remaining = sorted(t.name for t in targets if t.state != STOPPED)
            if not remaining and engine_wide:
                self._lifecycle = "drained"
        return (not remaining, time.monotonic() - started, remaining)

    def _resolve(self, actor_id: object, name: object) -> Any:
        record = self._records.get(actor_id) if isinstance(actor_id, str) else None
        if record is None or record.name != name:
            return None
        return record

    def _pool(self, actor_id: str, name: str) -> Pool | None:
        record = self._resolve(actor_id, name)
        return record if record is not None and record.kind == "pool" else None

    def _owner(self, owner_id: str, owner_name: str) -> Cell | None:
        record = self._resolve(owner_id, owner_name)
        return record if record is not None and record.kind == "serial" else None

    def _require_spawnable(self) -> None:
        if self._lifecycle != "active":
            raise RuntimeError(f"engine is {self._lifecycle}; spawn refused")

    def _validate_spec(self, name: object, factory: object, capacity: object, profile: object) -> tuple:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-blank string")
        if not _is_int(capacity) or capacity < 1:
            raise ValueError("capacity must be >= 1")
        if profile not in self._policies:
            raise ValueError(f"unknown policy profile {profile!r}")
        if not isinstance(factory, self._native_type) and not callable(factory):
            raise ValueError("handler_factory must be callable or a NativeHandlerFactory")
        return self._policies[profile]

    def _build_first(self, factory: Any) -> Any:
        try:
            return factory()
        except BaseException as exc:
            raise self._errors[2]("handler factory raised at first start") from exc

    def _live_by_key(self, pair: tuple[str, str]) -> Cell | None:
        cell = self._keys.get(pair)
        if isinstance(cell, Cell) and cell.state != STOPPED:
            return cell
        return None

    def _emit(self, record: Any, event: tuple) -> None:
        if getattr(record, "group", None) is not None:
            event = event + (record.group, record.key)
        for sink in (record.event_sink, self._sink):
            if sink is None:
                continue
            try:
                sink(event)
            except Exception:
                pass

    def _dead_letter(self, record: Any, messages: list[object], reason: str) -> None:
        sink = record.dead_letter_sink
        if sink is None:
            return
        for message in messages:
            try:
                sink(message, reason)
            except Exception:
                pass


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _key_pair(group: object, key: object, *, required: bool) -> tuple[str, str] | None:
    if group is None and key is None:
        if required:
            raise ValueError("group and key must be given together")
        return None
    if group is None or key is None:
        raise ValueError("group and key must be given together")
    if not isinstance(group, str) or not isinstance(key, str):
        raise ValueError("group and key must be strings")
    for label, value in (("group", group), ("key", key)):
        if not value.strip():
            raise ValueError(f"{label} must be a non-blank string")
    return (group, key)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_policies(policies: object) -> dict[str, tuple]:
    if not isinstance(policies, dict) or not policies:
        raise ValueError("policies must be a non-empty dict")
    table: dict[str, tuple] = {}
    for profile, values in policies.items():
        if not isinstance(profile, str) or not isinstance(values, tuple) or len(values) != 5:
            raise ValueError(f"invalid policy {profile!r}")
        max_restarts, window, base, cap, jitter = values
        if not _is_int(max_restarts) or max_restarts < 0:
            raise ValueError(f"invalid max_restarts for {profile!r}")
        if not all(_is_number(v) for v in (window, base, cap, jitter)):
            raise ValueError(f"invalid policy numbers for {profile!r}")
        if window <= 0 or base < 0 or cap < base or not 0 <= jitter <= 1:
            raise ValueError(f"invalid policy bounds for {profile!r}")
        table[profile] = (max_restarts, float(window), float(base), float(cap), float(jitter))
    return table
