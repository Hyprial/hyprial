# Decision record (file header; no inline comments by repository rule):
# - Every case drives the engine only through the ABI surface of spec 3; the
#   Harness is a thin convenience wrapper, never a second engine.
# - Commands are small callables. Handlers are the shared trampoline (callables
#   run, data echoes, "panic" panics), so the same case runs with a Python
#   handler and with the native handler injected through CL-2 (`kind` fixture).
# - Custom handlers (owners of pools, factories that fail) are Python-only by
#   nature; cases that need them use the python-only fixture.
# - PANIC_TYPE_NAME: spec 3.3/9.3 make EffectCompleted.error and the
#   child_failed code the exception TYPE NAME; a Rust panic caught at the FFI
#   boundary surfaces as PyO3's PanicException, so its name is exactly
#   "PanicException" on every backend (the Python twins use the same name).
#   HQ round-1 review: a non-empty-string check let "PanicException: <text>"
#   pass, so every panic assertion compares this exact name.
# - Timing assertions use TOL (TOL-1, +-50 ms) and go through ONE helper,
#   `assert_timing` (HQ review, macOS under load ~12): the lower bound is
#   strict on every attempt; the upper bound allows the measured scheduling
#   overshoot of an equivalent plain-Python wait (control) and best-of-N
#   independent trials. TOL never grows and exact values (restart_delay at
#   jitter 0) stay exact. Its docstring argues why no clause is weakened.
# - WAIT and every other bound that only stops a hang are not semantic bounds:
#   WAIT = 10 s, scaled by HYPRIAL_CONFORMANCE_WAIT_SCALE; Block's own release
#   limit is 3 * WAIT. HYPRIAL_CONFORMANCE_TIMING_ATTEMPTS sets N (default 5).
# - The suite-only "slow" profile holds restart_pending for SLOW_DELAY (1.0 s,
#   was 0.5 s) so cases that act during restart_pending keep a wide margin
#   between "restart_scheduled observed" and the re-entry on a loaded host.
# - Observations that are only meaningful inside a window (cancel before the
#   delay elapsed, two failures inside a restart window) use `until_conclusive`:
#   the trial checks the test thread really acted inside the window and is
#   retried with fresh setup otherwise; the assertion itself is never relaxed.
# - `inside_call` lets a handler wait until the caller is really inside the
#   blocking engine call (event-driven), instead of assuming it after a delay.
# - Waits poll with short sleeps, which release the GIL, and are bounded so
#   that a hang fails the case.
"""Shared fixtures-level helpers for the conformance cases."""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
from types import ModuleType
from typing import Any, Callable, NamedTuple, Sequence

import pytest

from backends import TRAMPOLINE, trampoline

case = pytest.mark.case

PANIC_TYPE_NAME = "PanicException"

TOL = 0.05
WAIT_SCALE = float(os.environ.get("HYPRIAL_CONFORMANCE_WAIT_SCALE", "") or 1.0)
WAIT = 10.0 * WAIT_SCALE
TIMING_ATTEMPTS = max(1, int(os.environ.get("HYPRIAL_CONFORMANCE_TIMING_ATTEMPTS", "") or 5))
SLOW_DELAY = 1.0

POLICIES: dict[str, tuple[int, float, float, float, float]] = {
    "state_authority": (3, 60.0, 0.1, 2.0, 0.1),
    "external_io": (5, 60.0, 0.25, 10.0, 0.2),
    "process_lifecycle": (2, 120.0, 0.5, 5.0, 0.1),
    "fast": (3, 60.0, 0.02, 0.2, 0.0),
    "single": (1, 60.0, 0.01, 0.05, 0.0),
    "never": (0, 60.0, 0.01, 0.01, 0.0),
    "short_window": (1, 0.4, 0.01, 0.01, 0.0),
    "slow": (3, 60.0, SLOW_DELAY, SLOW_DELAY, 0.0),
}

_names = itertools.count(1)


class Expected(Exception):
    """The suite's expected-error type, passed as ``expected_error_type``."""

    def __init__(self, code: str = "E_TEST", detail: str = "expected rejection") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ExpectedSub(Expected):
    """Subclass of the expected-error type (EXP-02)."""


class CustomBase(BaseException):
    """A custom BaseException base that is not an Exception (SUP-09)."""


class Recorder:
    """Thread-safe append-only list with bounded waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Any] = []

    def append(self, item: Any) -> None:
        with self._lock:
            self._items.append(item)

    def items(self) -> list[Any]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def wait_len(self, count: int, timeout: float = WAIT) -> bool:
        return wait_until(lambda: len(self) >= count, timeout)


class EventLog(Recorder):
    """Collects event tuples; ``sink`` is passed as an event_sink."""

    def sink(self, event: tuple) -> None:
        self.append(event)

    def of(self, actor: "Actor | str") -> list[tuple]:
        actor_id = actor if isinstance(actor, str) else actor.actor_id
        return [e for e in self.items() if e[1] == actor_id]

    def kinds(self, actor: "Actor | str") -> list[str]:
        return [e[0] for e in self.of(actor)]

    def wait_kind(self, actor: "Actor | str", kind: str, count: int = 1, timeout: float = WAIT) -> bool:
        return wait_until(lambda: self.kinds(actor).count(kind) >= count, timeout)


class DeadLetters(Recorder):
    """Collects ``(message, reason)``; ``sink`` is passed as dead_letter_sink."""

    def sink(self, message: object, reason: str) -> None:
        self.append((message, reason))


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def control_overshoot(nominal: float) -> float:
    """How late the platform wakes a plain-Python wait of ``nominal`` seconds right now.

    The control is an ``threading.Event().wait(nominal)`` on the calling thread, run
    while no engine call is in progress, so an engine that stalls the interpreter
    cannot inflate it. ``nominal <= 0`` measures a non-blocking check.
    """
    started = time.monotonic()
    threading.Event().wait(max(0.0, nominal))
    return max(0.0, time.monotonic() - started - max(0.0, nominal))


class TimingFailure(AssertionError):
    """A timing clause failed in every attempt (or a lower bound failed once)."""


def assert_timing(attempt: Callable[[], float | Sequence[float]], nominal: float | Sequence[float],
                  *, tol: float = TOL, attempts: int | None = None, what: str = "",
                  control: Callable[[], float] | None = None) -> tuple[float, ...]:
    """Assert a spec timing clause (TOL-1, TOL-2) in a load-robust way that does not weaken it.

    ``attempt()`` runs ONE complete trial, its own setup included, and returns the
    measured interval(s); ``nominal`` is the clause's nominal value (one per measured
    interval, or one for all). The clause is ``nominal - tol <= measured <= nominal + tol``.

    - Lower bound: strict, always, on every attempt. Load only ever lengthens a
      measured interval (start is taken before the engine call, end after its
      effect), so an interval shorter than ``nominal - tol`` is an engine that gave
      up or fired early; it fails at once and is never retried.
    - Upper bound: an attempt passes when every interval is ``<= nominal + tol``.
      If not, a control is measured immediately afterwards: the scheduling
      overshoot of an equivalent plain-Python wait (``control_overshoot``), taken
      outside any engine call. The attempt then passes when every interval is
      ``<= nominal + tol + overshoot``: the platform itself was that late in the
      same moment, so the extra time is not the engine's.
      A measurement whose plain-Python equivalent is not a timed wait passes its
      own ``control`` callable (the same measurement taken without the engine
      activity under test, e.g. LIV-03's pause counter before the retry starts).
    - Best of N: up to ``attempts`` independent trials (default
      ``TIMING_ATTEMPTS``); one passing trial satisfies the clause.

    Why this is not a weaker clause: the tolerance itself never grows, and on an
    unloaded host the control is ~0 so the strict bound applies. A semantically
    wrong engine is wrong in EVERY trial: a doubled delay or a timeout that waits
    for the handler overshoots by far more than ``tol`` each time while the control
    stays small in most trials; an early return or early fire fails the strict
    lower bound immediately; a non-positive deadline that blocks fails every trial
    because the control of a non-blocking check is ~0. Only transient scheduling
    noise, which differs between trials, is absorbed. Returns the passing trial's
    intervals.
    """
    count = TIMING_ATTEMPTS if attempts is None else attempts
    trials: list[tuple[tuple[float, ...], float | None]] = []
    for _ in range(max(1, count)):
        raw = attempt()
        measured = tuple(raw) if isinstance(raw, (tuple, list)) else (float(raw),)
        nominals = (tuple(nominal) if isinstance(nominal, (tuple, list))
                    else (float(nominal),) * len(measured))
        if len(nominals) != len(measured):
            raise ValueError(f"{len(measured)} intervals measured for {len(nominals)} nominal values")
        early = [(m, n) for m, n in zip(measured, nominals) if m < n - tol]
        if early:
            raise TimingFailure(f"{what}: interval(s) {early} (measured, nominal) end earlier than "
                                f"nominal - {tol}; the lower bound is strict (TOL-1)")
        if all(m <= n + tol for m, n in zip(measured, nominals)):
            return measured
        overshoot = max(0.0, control()) if control is not None else control_overshoot(max(nominals))
        trials.append((measured, overshoot))
        if all(m <= n + tol + overshoot for m, n in zip(measured, nominals)):
            return measured
    raise TimingFailure(f"{what}: upper bound nominal {nominal} + {tol} (+ control overshoot) "
                        f"missed in all {len(trials)} attempts; (intervals, control overshoot) per "
                        f"attempt: {trials}")


def until_conclusive(attempt: Callable[[], bool], attempts: int | None = None, what: str = "") -> None:
    """Run ``attempt()`` until one trial is conclusive (returns True).

    For cases whose observation is only meaningful inside a time window (e.g. "a
    cancel issued before the delay elapsed returns True"): the trial measures
    whether the test thread actually acted inside the window, asserts the clause
    when it did, and returns False (inconclusive, retried with fresh setup) when
    host load delayed the test thread past the window. The assertion itself is
    never relaxed; a run where no trial was conclusive fails.
    """
    count = TIMING_ATTEMPTS if attempts is None else attempts
    for _ in range(max(1, count)):
        if attempt():
            return
    raise TimingFailure(f"{what}: no conclusive trial in {count} attempts (host too loaded to "
                        f"act inside the window)")


def inside_call(thread_id: int, *names: str) -> bool:
    """True while thread ``thread_id`` is inside one of the named ``Harness`` methods.

    Lets a handler wait, event-driven, until the caller has really entered the
    blocking engine call (ask / stop / drain) instead of assuming it has after a
    fixed time.
    """
    frame = sys._current_frames().get(thread_id)
    while frame is not None:
        code = frame.f_code
        if code.co_name in names and code.co_filename == __file__:
            return True
        frame = frame.f_back
    return False


class Rec:
    """Command: append ``value`` to ``log`` and return it."""

    def __init__(self, log: Recorder, value: object) -> None:
        self.log = log
        self.value = value

    def __call__(self) -> object:
        self.log.append(self.value)
        return self.value


class Block:
    """Command: signal ``entered``, wait for ``release`` (bounded), return value."""

    def __init__(self, release: threading.Event, entered: threading.Event | None = None,
                 value: object = None, limit: float = 3 * WAIT) -> None:
        self.release = release
        self.entered = entered or threading.Event()
        self.value = value
        self.limit = limit

    def __call__(self) -> object:
        self.entered.set()
        self.release.wait(self.limit)
        return self.value


class Raise:
    """Command: raise the given exception instance."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self) -> object:
        raise self.exc


class Call:
    """Command: call ``fn()`` and return its result."""

    def __init__(self, fn: Callable[[], object]) -> None:
        self.fn = fn

    def __call__(self) -> object:
        return self.fn()


class Sleep:
    """Command: sleep ``seconds`` (GIL released), then return ``value``."""

    def __init__(self, seconds: float, value: object = None) -> None:
        self.seconds = seconds
        self.value = value

    def __call__(self) -> object:
        time.sleep(self.seconds)
        return self.value


class Actor(NamedTuple):
    actor_id: str
    name: str


class Harness:
    """One engine per case, built from the selected backend module."""

    def __init__(self, module: ModuleType, kind: str = "python", jitter_seed: int | None = 0,
                 policies: dict | None = None, expected: type = Expected) -> None:
        self.module = module
        self.kind = kind
        self.events = EventLog()
        self.engine = module.Engine(policies=dict(policies or POLICIES), jitter_seed=jitter_seed,
                                    event_sink=self.events.sink, expected_error_type=expected)

    def factory(self, builds: Recorder | None = None) -> Any:
        """Trampoline factory of this harness' handler kind; ``builds`` counts calls."""
        if self.kind == "native":
            native = self.engine.native_handler_factory(TRAMPOLINE)
            if builds is not None:
                raise ValueError("native factories cannot count builds from Python")
            return native

        def build() -> Callable[[object], object]:
            if builds is not None:
                builds.append(1)
            return trampoline

        return build

    def spawn(self, name: str | None = None, capacity: int = 16, profile: str = "fast",
              dead_letters: DeadLetters | None = None, event_sink: Callable | None = None,
              factory: Any = None) -> Actor:
        name = name or f"actor-{next(_names)}"
        actor_id = self.engine.spawn(name, factory if factory is not None else self.factory(),
                                     capacity, profile,
                                     dead_letters.sink if dead_letters is not None else None,
                                     event_sink)
        return Actor(actor_id, name)

    def spawn_pool(self, name: str | None = None, pool_size: int = 1, capacity: int = 8,
                   profile: str = "fast", dead_letters: DeadLetters | None = None,
                   event_sink: Callable | None = None, retry_interval: float = 0.01,
                   factory: Any = None) -> Actor:
        name = name or f"pool-{next(_names)}"
        actor_id = self.engine.spawn_pool(name, factory if factory is not None else self.factory(),
                                          pool_size, capacity, profile,
                                          dead_letters.sink if dead_letters is not None else None,
                                          event_sink, retry_interval)
        return Actor(actor_id, name)

    def spawn_owner(self, pool: Actor | None, log: Recorder, ack: bool = True, capacity: int = 64,
                    name: str | None = None, dead_letters: DeadLetters | None = None) -> Actor:
        """Python owner: records every EffectCompleted, acks it, runs callables."""
        engine = self.engine
        target: list[Actor | None] = [pool]

        def handle(message: object) -> object:
            if isinstance(message, self.module.EffectCompleted):
                log.append(message)
                if ack and target[0] is not None:
                    engine.acknowledge(target[0].actor_id, target[0].name,
                                       message.operation_id, message.generation)
                return None
            return message() if callable(message) else message

        return self.spawn(name=name, capacity=capacity, dead_letters=dead_letters,
                          factory=lambda: handle)

    def tell(self, actor: Actor, command: object) -> int:
        return self.engine.tell(actor.actor_id, actor.name, command)

    def ask(self, actor: Actor, command: object, timeout: float = WAIT) -> object:
        return self.engine.ask(actor.actor_id, actor.name, command, timeout)

    def snapshot(self, actor: Actor) -> tuple[str, int, int, int, int]:
        return self.engine.snapshot(actor.actor_id, actor.name)

    def state(self, actor: Actor) -> str:
        return self.snapshot(actor)[0]

    def generation(self, actor: Actor) -> int:
        return self.snapshot(actor)[1]

    def stop(self, actor: Actor, timeout: float = WAIT) -> bool:
        return self.engine.stop(actor.actor_id, actor.name, timeout)

    def reset(self, actor: Actor) -> bool:
        return self.engine.reset(actor.actor_id, actor.name)

    def send_after(self, actor: Actor, delay: float, command: object) -> str:
        return self.engine.send_after(actor.actor_id, actor.name, delay, command)

    def drain(self, actors: list[Actor] | None, timeout: float = WAIT) -> tuple[bool, float, list[str]]:
        ids = None if actors is None else [a.actor_id for a in actors]
        return self.engine.drain(ids, timeout)

    def wait_state(self, actor: Actor, state: str, timeout: float = WAIT) -> bool:
        return wait_until(lambda: self.state(actor) == state, timeout)

    def wait_idle(self, actor: Actor, timeout: float = WAIT) -> bool:
        return wait_until(lambda: self.snapshot(actor)[2:4] == (0, 0), timeout)

    def close(self, timeout: float = 5.0) -> bool:
        return self.engine.shutdown(timeout)
