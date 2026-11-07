"""A worker that could not be stopped must stay reachable.

Independent review probe on this branch: with activation persistence failing
AND process.stop() failing, the effect recorded STOP_CALLS=1 with
PROCESS_RUNNING=True, REGISTERED=False and DESIRED=False -- a live worker that
neither reconcile nor shutdown can ever reach again, because deregistration
happened before the stop attempt and the failure was swallowed.
"""

from __future__ import annotations

import uuid

from hyprial.daemon.impl.adapters.lark.runtime.commands import _IoResult
from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects


class _StubbornProcess:
    """Refuses to stop, and says so honestly."""

    def __init__(self) -> None:
        self.running = True
        self.stop_calls = 0

    def stop(self, timeout: float = 5.0) -> bool:
        del timeout
        self.stop_calls += 1
        raise OSError("worker refused to stop")

    def wait_ready(self, timeout: float | None = None) -> str:
        del timeout
        return "online"

    @property
    def readiness(self) -> str:
        return "online"

    @property
    def pid(self) -> int:
        return 4321

    @property
    def error(self) -> str | None:
        return None

    @property
    def health(self) -> dict[str, object]:
        return {}

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        return ()


class _Launcher:
    def __init__(self, process: _StubbornProcess) -> None:
        self._process = process

    def spawn(self, gateway: object) -> _StubbornProcess:
        del gateway
        return self._process


class _FailingDesiredState:
    """Activation cannot be persisted; that is what starts the cleanup."""

    def __init__(self) -> None:
        self.active: set[str] = set()

    def activate(self, name: str) -> bool:
        raise OSError("desired-state activation could not be persisted")

    def deactivate(self, name: str) -> bool:
        self.active.discard(name)
        return True


def _effects(
    launcher: _Launcher, desired: _FailingDesiredState
) -> tuple[_ProcessEffects, list[_IoResult]]:
    results: list[_IoResult] = []
    effects = _ProcessEffects(
        launcher,  # type: ignore[arg-type]
        results.append,
        desired,  # type: ignore[arg-type]
        start_confirm_timeout=1.0,
    )
    return effects, results


def test_failed_stop_keeps_the_worker_registered_and_desired() -> None:
    process = _StubbornProcess()
    desired = _FailingDesiredState()
    effects, _results = _effects(_Launcher(process), desired)
    name = "gateway-a"
    token = uuid.uuid4().hex
    effects._latest_attempt[name] = token

    result = effects._run(
        "corr-1",
        1,
        1,
        name,
        "activate-spawn",
        object(),  # gateway is only handed to the launcher
        None,
        token,
    )

    assert result.succeeded is False
    assert result.code == "WORKER_STOP_FAILED"
    assert "refused to stop" in (result.detail or "")
    # The process really did resist, and it is still running...
    assert process.stop_calls == 1
    assert process.running is True
    # ...so it must remain reachable for a later reconcile or shutdown.
    assert name in effects.processes()


def test_successful_stop_still_deregisters_the_worker() -> None:
    """The happy path is unchanged: a stopped worker leaves the registry."""

    class CooperativeProcess(_StubbornProcess):
        def stop(self, timeout: float = 5.0) -> bool:
            del timeout
            self.stop_calls += 1
            self.running = False
            return True

    process = CooperativeProcess()
    desired = _FailingDesiredState()
    effects, _results = _effects(_Launcher(process), desired)  # type: ignore[arg-type]
    name = "gateway-b"
    token = uuid.uuid4().hex
    effects._latest_attempt[name] = token

    result = effects._run(
        "corr-2", 1, 1, name, "activate-spawn", object(), None, token
    )

    assert result.succeeded is False
    assert result.code != "WORKER_STOP_FAILED"
    assert process.stop_calls == 1
    assert process.running is False
    assert name not in effects.processes()
