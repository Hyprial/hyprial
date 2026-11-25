"""An own-member restart keeps the agent's launch intent (I6-5 critic V3 P1-1).

Through the production lifecycle coordinator: a reconcile restart stops the
harness without removing its desired-state row, so a start that fails in the
window, or a daemon killed there, leaves a ``running`` row that boot restore
relaunches.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.composition.cells import agents as cell_agents
from hyprial.daemon.impl.composition.cells import close_cell_runtime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.state_db import StateDatabase
from tests.daemon.application.actors.harness_args_lifecycle.test_policy_reconcile import (
    _cell,
    _Launcher,
    _member,
    _rows,
    _set_rules,
    _start,
)
from tests.daemon.application.actors.harness_args_lifecycle.test_policy_reconcile_real_manager import (
    _manager_healthy,
    _started_with_template,
    _template_rules,
    real_rig as real_rig,
)
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox,
    _LocalDelivery,
    _Transport,
)
from tests.daemon.cells.actor.actor_support import wait_until
from tests.identity.agents.registry.entity.support import (
    _TEST_APPLICATIONS,
    _close_test_applications as _close_test_applications,
    application,
)

_EXTRA_DENY = "Bash(hyprial agent stop:*)"


def _durable_rows(app: Any) -> list[Any]:
    state_db = StateDatabase(Path(app.state_dir) / "lifecycle-operations.sqlite3")
    store = DesiredStateStore(Path(app.state_dir) / "desired-state.json", state_db=state_db)
    return [spec for spec in store.load().harnesses if spec.name == "foo"]


def _break(launcher: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    start = launcher.start
    broken = {"on": True}

    def failing(spec: Any) -> Any:
        if broken["on"]:
            raise RuntimeError("spawn failed")
        return start(spec)

    monkeypatch.setattr(launcher, "start", failing)
    return broken


def _restored(app: Any, launcher: Any) -> None:
    launches = len(launcher.launches)
    app._restore_harnesses()
    wait_until(lambda: len(_rows(app, "foo")) == 1 and _rows(app, "foo")[0].running,
               timeout=15.0)
    assert len(launcher.launches) == launches + 1


def _keeps_intent_after_failed_restart(app: Any, launcher: Any, broken: dict[str, bool],
                                       cell_id: str) -> None:
    wait_until(lambda: (member := _member(app, cell_id)) is not None
               and member.last_error is not None, timeout=30.0)
    assert not any(row.running for row in _rows(app, "foo"))
    rows = _durable_rows(app)
    assert len(rows) == 1 and rows[0].status == "running", rows
    broken["on"] = False
    _restored(app, launcher)


def test_a_policy_restart_whose_starts_fail_keeps_a_restorable_launch_intent(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    allow, deny = _template_rules()
    broken = _break(launcher, monkeypatch)
    _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    _keeps_intent_after_failed_restart(app, launcher, broken, cell.cell_id)


def test_a_first_compose_ensure_whose_starts_fail_keeps_a_restorable_launch_intent(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled")
    _start(app)
    assert getattr(app, "_cell_runtime", None) is None
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "claude")
    broken = _break(launcher, monkeypatch)
    cell = _cell(app, "foo")[2]
    _keeps_intent_after_failed_restart(app, launcher, broken, cell.cell_id)


def test_a_daemon_killed_between_the_stop_and_the_start_restores_the_agent(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    app, launcher = real_rig
    _started_with_template(app)
    allow, deny = _template_rules()
    entered = threading.Event()
    release = threading.Event()
    seen: dict[str, list[Any]] = {}

    def blocked(_spec: Any) -> Any:
        seen["rows"] = _durable_rows(app)
        entered.set()
        release.wait(30.0)
        raise RuntimeError("daemon killed")

    process = launcher.launches[-1][2]
    stop = process.stop

    def observed_stop() -> None:
        seen["at_stop"] = _durable_rows(app)
        stop()

    monkeypatch.setattr(process, "stop", observed_stop)
    monkeypatch.setattr(launcher, "start", blocked)
    _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    assert entered.wait(30.0)
    try:
        assert [row.status for row in seen["at_stop"]] == ["running"]
        assert [row.status for row in seen["rows"]] == ["running"]
        home = tmp_path_factory.mktemp("killed-home")
        shutil.copytree(app.state_dir, home, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.sock", "*.lock", "*.lease"))
    finally:
        release.set()
    booted, relauncher, close = _boot(home, monkeypatch)
    try:
        _manager_healthy(booted)
        assert [row.status for row in _durable_rows(booted)] == ["running"]
        _restored(booted, relauncher)
        assert relauncher.launches[-1][0].name == "foo"
        _manager_healthy(booted)
    finally:
        close()


def _boot(home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Launcher, Any]:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    app = application(home)
    app._expire_previous_generation_lifecycle_state()
    launcher = _Launcher(app)
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state)
    client = HarnessPortClient(actor, events)
    app._harnesses = client  # type: ignore[assignment]
    app._lifecycle_manager = None  # type: ignore[assignment]
    app._start_lifecycle_manager(
        transport=_Transport(), inbox=_Inbox(), local_delivery=_LocalDelivery(), harnesses=client,
    )
    app._restore_done.set()

    def close() -> None:
        close_cell_runtime(app)
        if app in _TEST_APPLICATIONS:
            _TEST_APPLICATIONS.remove(app)
        app._close()
        app._close_home_lease()
        events.close()

    return app, launcher, close


def test_an_operator_stop_still_drops_the_intent_and_a_later_edit_does_not_resurrect_it(
    real_rig: Any,
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    allow, deny = _template_rules()
    assert app.handle("agent.stop", {"name": "foo"})["ok"] is True
    assert _durable_rows(app) == []
    launches = len(launcher.launches)
    _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    wait_until(lambda: (member := _member(app, cell.cell_id)) is not None
               and "missing-local-facts" in (member.last_error or ""), timeout=30.0)
    assert len(launcher.launches) == launches
    assert _durable_rows(app) == []
    _manager_healthy(app)
