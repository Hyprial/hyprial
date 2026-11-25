"""S8 review: the agent cell follows the record on every cycle, not once.

The registrar must reconcile an external agent's cell after any number of
attach/detach flips, heal a cell write that failed (without a daemon
restart) and repair a membership a stale writer left behind.  Each case
runs on a real golden daemon with the production cell runtime.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import hyprial.daemon.impl.composition.cells.agents as registrar_module
from tests.daemon.acceptance.acceptance_support import boot_like_services
from tests.daemon.attach.attach_support import attach, detach
from tests.support.golden.app import golden_daemon as golden_daemon

WAIT_S = 10.0


def _eventually(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + WAIT_S
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _app(golden_daemon: Any, monkeypatch: Any) -> Any:
    daemon, _recorder = boot_like_services(
        golden_daemon(bypass=False, links=False), monkeypatch
    )
    return daemon.app


def _flip(app: Any, attached: bool) -> None:
    current = app.agents.get("ext")
    wanted = replace(current, capabilities={"custody": "external", "attached": attached})
    assert app.agents.compare_and_swap(current, wanted) is True


def test_every_record_flip_reaches_the_cell(golden_daemon, monkeypatch) -> None:
    app = _app(golden_daemon, monkeypatch)
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    for _cycle in range(3):
        _flip(app, False)
        assert _eventually(lambda: runtime.lookup(agent.cell_id).members == ())
        _flip(app, True)
        assert _eventually(lambda: runtime.lookup(agent.cell_id).has_member(agent.uri))


def test_attach_and_detach_cycles_keep_the_cell_in_step(golden_daemon, monkeypatch) -> None:
    app = _app(golden_daemon, monkeypatch)
    for _cycle in range(3):
        assert attach(app, "ext")["cell"]["state"] == "recorded"
        agent = app.agents.get("ext")
        assert app._cell_runtime.lookup(agent.cell_id).has_member(agent.uri)
        assert detach(app, "ext")["cell"]["state"] == "recorded"
        assert app._cell_runtime.lookup(agent.cell_id).members == ()


def test_a_failed_cell_write_is_healed_without_a_restart(golden_daemon, monkeypatch) -> None:
    monkeypatch.setattr(registrar_module, "RETRY_BASE_S", 0.05, raising=False)
    app = _app(golden_daemon, monkeypatch)
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    detach(app, "ext")
    assert runtime.lookup(agent.cell_id).members == ()
    real_update = runtime.update
    failures = {"left": 2}

    def flaky_update(*args: Any, **kwargs: Any) -> Any:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("transient cell cache failure")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(runtime, "update", flaky_update)
    answered = attach(app, "ext")
    assert answered["changed"] is True
    assert answered["cell"]["state"] in {"recorded", "deferred"}
    assert _eventually(lambda: runtime.lookup(agent.cell_id).has_member(agent.uri))
    assert failures["left"] == 0


def test_a_stale_membership_write_is_repaired(golden_daemon, monkeypatch) -> None:
    app = _app(golden_daemon, monkeypatch)
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    harness = runtime.lookup(agent.cell_id).members
    _stale_write(runtime, agent.cell_id, ())
    assert _eventually(lambda: runtime.lookup(agent.cell_id).has_member(agent.uri))
    detach(app, "ext")
    _stale_write(runtime, agent.cell_id, harness)
    assert _eventually(lambda: runtime.lookup(agent.cell_id).members == ())


def _stale_write(runtime: Any, cell_id: str, members: tuple[Any, ...]) -> None:
    cell = runtime.lookup(cell_id)
    runtime.update(cell_id, members=members, expect_revision=cell.revision)
    runtime.kick()


def test_a_retired_cell_answers_refused_and_stays_retired(golden_daemon, monkeypatch) -> None:
    app = _app(golden_daemon, monkeypatch)
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    runtime.retire(agent.cell_id)
    answered = detach(app, "ext")
    assert answered["changed"] is True
    assert answered["cell"] == {"state": "refused", "revision": None}
    assert runtime.lookup(agent.cell_id).retired is True
    assert attach(app, "ext")["cell"]["state"] == "refused"
