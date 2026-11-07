from __future__ import annotations

import inspect
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import get_type_hints

import pytest

import hyprial.kernel.impl.actor_runtime as actor_runtime
from hyprial.kernel.impl.actor_runtime import DEFAULT_POLICIES, ActorHandle, ActorRuntime
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler


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


def test_pykka_is_imported_only_by_backend_and_never_leaks_from_public_api() -> None:
    package_root = Path(inspect.getfile(ActorRuntime)).parent
    importers = []
    for source in sorted(package_root.rglob("*.py")):
        if "import pykka" in source.read_text():
            importers.append(source.relative_to(package_root).as_posix())
    assert importers == ["backends/pykka.py"]

    handle = ActorHandle(actor_id="opaque", name="test")
    assert set(handle.__slots__) == {"actor_id", "name"}
    assert all(
        "pykka" not in repr(getattr(actor_runtime, exported))
        for exported in actor_runtime.__all__
    )

    for method_name in ("start", "tell", "snapshot", "stop", "reset", "drain"):
        hints = get_type_hints(getattr(ActorRuntime, method_name))
        assert all("pykka" not in repr(annotation) for annotation in hints.values())
        source = inspect.getsource(getattr(ActorRuntime, method_name))
        assert ".ask(" not in source
        assert ".get(" not in source
        assert ".proxy(" not in source


def test_named_default_supervision_profiles_are_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_POLICIES["custom"] = DEFAULT_POLICIES["state_authority"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        DEFAULT_POLICIES["state_authority"].max_restarts = 99  # type: ignore[misc]
