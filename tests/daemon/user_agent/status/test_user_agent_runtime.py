"""The ensure loop's edges: it ends right after its last attempt, and never creates while stopping.

After ``MAX_ATTEMPTS`` failed creates the loop ends at once instead of
waiting one more back-off.  A stop (daemon stop or logout) set before the
create is submitted means no create is submitted: a logged-out daemon does
not mint a user agent for the owner who just left.  The ensure, the loop and
the registry fakes are explicit; no daemon is started.
"""

from __future__ import annotations

import threading
from typing import Any

from hyprial.daemon.impl.user_agent import DEFERRED, ensure_user_agent
from hyprial.daemon.impl.user_agent import runtime

OWNER = "owner@example"


class _Registry:
    def __init__(self, *, fail: bool = True) -> None:
        self.owner = OWNER
        self.fail = fail
        self.creates: list[str] = []

    def list(self) -> tuple[Any, ...]:
        return ()

    def get(self, _name: str) -> None:
        return None

    def create(self, name: str, **_fields: Any) -> Any:
        self.creates.append(name)
        if self.fail:
            raise RuntimeError("registry busy")
        raise AssertionError("not reached in these tests")


class _Stop:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.flag


class _App:
    def __init__(self, agents: _Registry, stop: _Stop) -> None:
        self.agents = agents
        self.stop_event = stop
        self.events: list[str] = []

    def _log(self, _level: str, _component: str, event: str, **_fields: Any) -> None:
        self.events.append(event)


def _run_loop(app: _App) -> None:
    state = {"thread": threading.current_thread(), "again": False}
    setattr(app, runtime._ATTRIBUTE, state)
    runtime._loop(app, state)


def test_the_loop_ends_right_after_the_last_failed_attempt() -> None:
    registry, stop = _Registry(), _Stop()
    app = _App(registry, stop)

    _run_loop(app)

    assert len(registry.creates) == runtime.MAX_ATTEMPTS
    assert len(stop.waits) == runtime.MAX_ATTEMPTS - 1
    assert runtime.user_agent_status(app)["state"] == "unavailable"


def test_no_create_is_submitted_once_the_daemon_is_stopping() -> None:
    """The stop arrives after the loop's own check, before the create."""

    registry, stop = _Registry(), _Stop()
    app = _App(registry, stop)
    checks: list[bool] = []

    def is_set() -> bool:
        checks.append(True)
        return len(checks) > 1

    stop.is_set = is_set  # type: ignore[method-assign]
    _run_loop(app)

    assert registry.creates == []
    assert ensure_user_agent(agents=registry, should_stop=lambda: True) == DEFERRED
    assert registry.creates == []
