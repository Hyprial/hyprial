from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import hyprial.daemon.impl.application.shutdown as application_module
from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority
from hyprial.kernel import DAEMON_CLOSE_STEP_BUDGETS
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def _app(tmp_path: Path) -> tuple[DaemonApplication, list[tuple[str, dict[str, Any]]]]:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="autoupdate-close-test",
        hyprial_home=home,
        owner="owner",
    **_deep_module_daemon_dependencies())
    events: list[tuple[str, dict[str, Any]]] = []
    app._log = lambda _level, _component, event, **fields: events.append(  # type: ignore[method-assign]
        (event, fields)
    )
    return app, events


def _close_and_release(app: DaemonApplication) -> None:
    try:
        app._close()
    finally:
        app._close_home_lease()


def test_autoupdate_has_a_registered_five_second_budget() -> None:
    assert dict(DAEMON_CLOSE_STEP_BUDGETS)["autoupdate"] == 5.0


def test_runtime_stop_delay_inside_budget_closes_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, events = _app(tmp_path)
    actor = app._autoupdate
    original_stop = actor._runtime.stop

    def delayed_stop(handle: object, timeout: float) -> bool:
        time.sleep(0.3)
        if timeout < 1.0:
            return False
        return original_stop(handle, timeout)

    monkeypatch.setattr(actor._runtime, "stop", delayed_stop)
    _close_and_release(app)

    auto_events = [
        (event, fields)
        for event, fields in events
        if fields.get("resource") == "autoupdate"
    ]
    assert [event for event, _fields in auto_events] == [
        "daemon.close.begin",
        "daemon.close.end",
    ]


@pytest.mark.parametrize("stage", ["scheduler", "queue", "effects", "runtime"])
def test_each_over_budget_stop_stage_is_named_and_traced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    app, events = _app(tmp_path)
    actor = app._autoupdate
    budget = 0.02
    budget_table = tuple(
        ("autoupdate", budget) if name == "autoupdate" else (name, seconds)
        for name, seconds in application_module._CLOSE_STEP_BUDGETS
    )
    if "autoupdate" not in dict(budget_table):
        budget_table = (("autoupdate", budget),) + budget_table
    monkeypatch.setattr(application_module, "_CLOSE_STEP_BUDGETS", budget_table)

    def slow_false(*_args: object, **_kwargs: object) -> bool:
        time.sleep(0.05)
        return False

    if stage == "scheduler":
        monkeypatch.setattr(actor._scheduler, "shutdown", slow_false)
    elif stage == "queue":
        original_snapshot = actor._runtime.snapshot

        def queued_snapshot(handle: object) -> Any:
            return replace(original_snapshot(handle), queued=1)

        monkeypatch.setattr(actor._runtime, "snapshot", queued_snapshot)
    elif stage == "effects":
        monkeypatch.setattr(actor._effects, "close", slow_false)
    else:
        monkeypatch.setattr(actor._runtime, "stop", slow_false)

    try:
        with pytest.raises(
            RuntimeError, match=f"auto-update authority did not drain: {stage}"
        ):
            _close_and_release(app)
    finally:
        monkeypatch.undo()
        actor.stop(timeout=2.0)

    failed = [
        fields
        for event, fields in events
        if event == "daemon.close.failed" and fields.get("resource") == "autoupdate"
    ]
    assert len(failed) == 1
    assert failed[0]["error"] == f"auto-update authority did not drain: {stage}"


def test_idle_stop_finishes_under_its_registered_budget(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    actor = AutoUpdateAuthority(state_dir=tmp_path / "state", hyprial_home=home)
    started = time.monotonic()
    try:
        assert actor.stop(timeout=5.0)
    finally:
        actor.stop(timeout=5.0)
    assert time.monotonic() - started < 5.0
