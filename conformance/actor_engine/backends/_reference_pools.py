# Decision record (file header; no inline comments by repository rule):
# - Pools freeze the shipped EffectLane custody semantics (spec 9, R2):
#   capacity counts live tokens; a token leaves only by acknowledge, cancel or
#   owner-death dead-lettering; completion delivery repeats every
#   retry_interval until the token settles (at-least-once, EFF-06).
# - Delivery runs on one courier thread per pool, so workers keep executing
#   the next payload while earlier completions wait for their ack. The spec
#   permits either worker pinning or a separate courier; the suite asserts
#   neither.
# - Owner validation happens at submit time in the engine (9.4); the pool
#   stores the validated (owner_id, owner_name) pair with the token.
"""Reference worker-pool actor and the EffectCompleted message type."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"


class EffectCompleted:
    """Immutable completion message (spec 3.3)."""

    __slots__ = ("operation_id", "generation", "result", "error")

    def __init__(self, operation_id: str, generation: int, result: object = None,
                 error: str | None = None) -> None:
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "error", error)

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("EffectCompleted is immutable")

    def __delattr__(self, key: str) -> None:
        raise AttributeError("EffectCompleted is immutable")

    def __repr__(self) -> str:
        return (f"EffectCompleted(operation_id={self.operation_id!r}, generation={self.generation}, "
                f"result={self.result!r}, error={self.error!r})")


class _Token:
    __slots__ = ("submitted", "owner", "payload", "completion", "next_attempt")

    def __init__(self) -> None:
        self.submitted = False
        self.owner: tuple[str, str] | None = None
        self.payload: object = None
        self.completion: EffectCompleted | None = None
        self.next_attempt = 0.0


class Pool:
    kind = "pool"

    def __init__(self, engine: Any, actor_id: str, name: str, handlers: list, capacity: int,
                 dead_letter_sink: Any, event_sink: Any, retry_interval: float) -> None:
        self.engine = engine
        self.actor_id = actor_id
        self.name = name
        self.handlers = handlers
        self.capacity = capacity
        self.dead_letter_sink = dead_letter_sink
        self.event_sink = event_sink
        self.retry = retry_interval
        self.state = RUNNING
        self.generation = 1
        self.draining = False
        self.accepted_stop = False
        self.confirmed = threading.Event()
        self.timer_ids: set[str] = set()
        self.tokens: dict[tuple[str, int], _Token] = {}
        self.queue: deque[tuple[str, int]] = deque()
        self.in_flight = 0
        self.work_cv = threading.Condition(engine._lock)
        self.courier_cv = threading.Condition(engine._lock)
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for index, handler in enumerate(self.handlers):
            thread = threading.Thread(target=self._work, args=(handler,),
                                      name=f"ref-{self.name}-{index}", daemon=True)
            self._threads.append(thread)
        self._threads.append(threading.Thread(target=self._courier, name=f"ref-{self.name}-courier",
                                              daemon=True))
        for thread in self._threads:
            thread.start()

    def threads(self) -> list[threading.Thread]:
        return list(self._threads)

    def load(self) -> tuple[int, int]:
        return len(self.queue), self.in_flight

    def idle(self) -> bool:
        return not self.queue and not self.in_flight and not self.tokens

    def accept_stop(self) -> None:
        self.accepted_stop = True
        self.state = STOPPING
        self.work_cv.notify_all()
        self.courier_cv.notify_all()

    def _open(self) -> bool:
        return (self.state == RUNNING and not self.accepted_stop and not self.draining
                and self.engine._lifecycle == "active")

    def reserve(self, operation_id: str, generation: int) -> int:
        key = (operation_id, generation)
        if not self._open():
            return 2
        if key in self.tokens:
            return 0
        if len(self.tokens) >= self.capacity:
            return 1
        self.tokens[key] = _Token()
        return 0

    def has_token(self, operation_id: str, generation: int) -> bool:
        return (operation_id, generation) in self.tokens

    def submit(self, operation_id: str, generation: int, owner: tuple[str, str], payload: object) -> int:
        outcome = self.reserve(operation_id, generation)
        if outcome != 0:
            return outcome
        return self.submit_reserved(operation_id, generation, owner, payload)

    def submit_reserved(self, operation_id: str, generation: int, owner: tuple[str, str],
                        payload: object) -> int:
        key = (operation_id, generation)
        token = self.tokens.get(key)
        if token is None:
            return 2
        if token.submitted:
            return 0
        token.submitted = True
        token.owner = owner
        token.payload = payload
        self.queue.append(key)
        self.work_cv.notify_all()
        return 0

    def cancel(self, operation_id: str, generation: int) -> bool:
        key = (operation_id, generation)
        token = self.tokens.get(key)
        if token is None or token.submitted:
            return False
        del self.tokens[key]
        self._settled()
        return True

    def acknowledge(self, operation_id: str, generation: int) -> bool:
        key = (operation_id, generation)
        token = self.tokens.get(key)
        if token is None or not token.submitted:
            return False
        del self.tokens[key]
        self._settled()
        return True

    def _settled(self) -> None:
        self.courier_cv.notify_all()
        self.work_cv.notify_all()
        self.engine._activity.notify_all()

    def _work(self, handler: Any) -> None:
        lock = self.engine._lock
        while True:
            with lock:
                key = None
                while key is None:
                    if self.state == STOPPED:
                        return
                    if self.queue:
                        key = self.queue.popleft()
                        token = self.tokens.get(key)
                        if token is None:
                            key = None
                            continue
                        self.in_flight += 1
                        break
                    self.work_cv.wait()
            try:
                completion = EffectCompleted(key[0], key[1], result=handler(token.payload))
            except BaseException as exc:
                completion = EffectCompleted(key[0], key[1], error=type(exc).__name__)
            with lock:
                self.in_flight -= 1
                if self.tokens.get(key) is token:
                    token.completion = completion
                    token.next_attempt = 0.0
                self._settled()

    def _courier(self) -> None:
        lock = self.engine._lock
        while True:
            with lock:
                due = self._due_or_wait()
                if due is None:
                    self._confirm_locked()
                    break
            for key, token in due:
                self._deliver(key, token)
        self.engine._emit(self, ("child_stopped", self.actor_id, self.name, 1, None, None, None, None))
        with lock:
            self.state = STOPPED
            self.confirmed.set()
            self.work_cv.notify_all()
            self.engine._activity.notify_all()

    def _due_or_wait(self) -> list | None:
        while True:
            if self.state == STOPPING and not self.tokens and not self.queue and not self.in_flight:
                return None
            now = time.monotonic()
            pending = [(k, t) for k, t in self.tokens.items() if t.completion is not None]
            due = [(k, t) for k, t in pending if t.next_attempt <= now]
            if due:
                return due
            wake = min((t.next_attempt for _, t in pending), default=None)
            self.courier_cv.wait(None if wake is None else max(0.0, wake - now))

    def _confirm_locked(self) -> None:
        self.queue.clear()

    def _deliver(self, key: tuple[str, int], token: _Token) -> None:
        outcome, owner_state = self.engine._deliver(token.owner, token.completion)
        with self.engine._lock:
            if self.tokens.get(key) is not token:
                return
            if outcome == 2 and owner_state in (STOPPED, "quarantined"):
                del self.tokens[key]
                self._settled()
                reason = owner_state
            else:
                token.next_attempt = time.monotonic() + self.retry
                return
        self.engine._dead_letter(self, [token.completion], reason)
