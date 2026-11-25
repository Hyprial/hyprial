"""GenerationScheduler contract retained after the ActorRuntime facade removal."""

from __future__ import annotations

import ast
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import DEFAULT_POLICIES
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler

ROOT = Path(__file__).resolve().parents[4]


class _FakeTimer:
    def __init__(
        self, interval: float, function: object, args: tuple[object, ...]
    ) -> None:
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def force_fire(self) -> None:
        assert callable(self.function)
        self.function(*self.args)


def test_scheduler_fences_stale_generation_even_if_cancelled_timer_fires() -> None:
    timers: list[_FakeTimer] = []
    observed: list[int] = []

    def timer_factory(
        interval: float, function: object, args: tuple[object, ...]
    ) -> _FakeTimer:
        timer = _FakeTimer(interval, function, args)
        timers.append(timer)
        return timer

    scheduler = GenerationScheduler(timer_factory=timer_factory)
    assert scheduler.schedule("child", 1, 10.0, observed.append) is True
    assert scheduler.schedule("child", 2, 10.0, observed.append) is True
    assert timers[0].cancelled is True

    timers[0].force_fire()
    assert observed == []
    timers[1].force_fire()
    assert observed == [2]


def test_scheduler_shutdown_waits_for_active_callback() -> None:
    timers: list[_FakeTimer] = []
    entered = threading.Event()
    release = threading.Event()

    def timer_factory(
        interval: float, function: object, args: tuple[object, ...]
    ) -> _FakeTimer:
        timer = _FakeTimer(interval, function, args)
        timers.append(timer)
        return timer

    def callback(_generation: int) -> None:
        entered.set()
        assert release.wait(2.0)

    scheduler = GenerationScheduler(timer_factory=timer_factory)
    assert scheduler.schedule("maintenance", 1, 10.0, callback)
    firing = threading.Thread(target=timers[0].force_fire)
    firing.start()
    assert entered.wait(1.0)

    settled: list[bool] = []
    stopping = threading.Thread(target=lambda: settled.append(scheduler.shutdown(1.0)))
    stopping.start()
    time.sleep(0.05)
    assert stopping.is_alive()
    release.set()
    stopping.join(1.0)
    firing.join(1.0)
    assert settled == [True]


def test_scheduler_fences_a_replaced_timer_of_the_same_generation() -> None:
    timers: list[_FakeTimer] = []
    observed: list[int] = []

    def timer_factory(
        interval: float, function: object, args: tuple[object, ...]
    ) -> _FakeTimer:
        timer = _FakeTimer(interval, function, args)
        timers.append(timer)
        return timer

    scheduler = GenerationScheduler(timer_factory=timer_factory)
    assert scheduler.schedule("child", 1, 10.0, observed.append) is True
    assert scheduler.schedule("child", 1, 10.0, observed.append) is True
    timers[0].force_fire()
    assert observed == []
    timers[1].force_fire()
    assert observed == [1]
    timers[1].force_fire()
    assert observed == [1]


def test_named_default_supervision_profiles_are_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_POLICIES["custom"] = DEFAULT_POLICIES["state_authority"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        DEFAULT_POLICIES["state_authority"].max_restarts = 99  # type: ignore[misc]


def test_no_pykka_import_or_dependency_remains() -> None:
    offenders = []
    for base in (ROOT / "src" / "hyprial", ROOT / "conformance" / "actor_engine"):
        for source in sorted(base.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                if any(name == "pykka" or name.startswith("pykka.") for name in modules):
                    offenders.append(source.relative_to(ROOT).as_posix())
    assert offenders == []
    assert "pykka" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
