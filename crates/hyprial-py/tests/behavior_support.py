"""Shared helpers of the native engine behaviour self-check.

``engine_behavior.py`` is the entry point; this module holds the engine
construction, collectors and command helpers that both group modules use.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src"))

from hyprial._core import actors as native  # noqa: E402

ENGINE_ABI_VERSION = native.ENGINE_ABI_VERSION
Engine = native.Engine
EffectCompleted = native.EffectCompleted

POLICIES = {
    "state_authority": (3, 60, 0.1, 2.0, 0.1),
    "external_io": (5, 60, 0.25, 10.0, 0.2),
    "process_lifecycle": (2, 120, 0.5, 5.0, 0.1),
}


class Expected(Exception):
    code = "E_TEST"
    detail = "expected"


class ExpectedSub(Expected):
    pass


class Sink:
    """Thread-safe event/dead-letter collector."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[tuple] = []
        self.dead: list[tuple[object, str]] = []

    def event(self, event: tuple) -> None:
        with self.lock:
            self.events.append(event)

    def dead_letter(self, message: object, reason: str) -> None:
        with self.lock:
            self.dead.append((message, reason))

    def kinds(self) -> list[str]:
        with self.lock:
            return [e[0] for e in self.events]

    def dead_reasons(self) -> list[str]:
        with self.lock:
            return [r for _, r in self.dead]


def make_engine(sink: Sink | None = None, jitter_seed: int | None = 7) -> Engine:
    sink = sink or Sink()
    return Engine(
        policies=POLICIES,
        jitter_seed=jitter_seed,
        event_sink=sink.event,
        expected_error_type=Expected,
    )


def make_engine_with(policy: dict, sink: Sink, jitter_seed: int | None = 1) -> Engine:
    return Engine(
        policies=policy,
        jitter_seed=jitter_seed,
        event_sink=sink.event,
        expected_error_type=Expected,
    )


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def check(name: str) -> None:
    print(f"PASS {name}", flush=True)


def calling_factory():
    """Handler factory: a callable command is called, anything else echoed."""
    return lambda cmd: cmd() if callable(cmd) else cmd


def blocker(release: threading.Event, entered: threading.Event):
    """Command that signals ``entered`` and holds the handler until ``release``."""

    def run() -> str:
        entered.set()
        release.wait(5.0)
        return "released"

    return run
