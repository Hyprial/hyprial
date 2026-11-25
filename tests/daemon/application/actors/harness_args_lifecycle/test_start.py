"""Stored harness arguments across explicit start, ignore, and restore."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors

from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    application,
)
from tests.daemon.harnesses.actors.authority.support import _Launcher


@pytest.fixture
def app(tmp_path: Path) -> Iterator[DaemonApplication]:
    yield application(tmp_path)


def _desired_args(app: DaemonApplication, name: str) -> tuple[str, ...]:
    return next(
        spec.args for spec in app.desired_state.load().harnesses
        if spec.name == name
    )


def test_agent_start_declares_one_shot_ignore_and_invalid_data() -> None:
    spec = METHOD_REGISTRY.get("agent.start").spec  # type: ignore[union-attr]
    ignored = spec.params_schema["properties"]["ignoreStoredHarnessArgs"]
    errors = {error.code: error for error in spec.errors}

    assert ignored["type"] == "boolean"
    assert ipc_errors.HARNESS_ARGS_INVALID in errors
    assert errors[ipc_errors.HARNESS_ARGS_INVALID].data_schema is not None


def test_invalid_stored_args_fail_before_retry_then_clear_allows_start(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    invalid = ("--dangerously-skip-permissions", "--json")
    app.agents.create(
        "foo", harness_args={"claude": invalid}, preferred_harness="claude"
    )
    captured: list[HarnessLaunchSpec] = []
    original_start = app._harnesses.start
    monkeypatch.setattr(
        app._harnesses,
        "start",
        lambda spec: captured.append(spec) or original_start(spec),
    )
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.start", {"name": "foo"})
    assert failure.value.code == ipc_errors.HARNESS_ARGS_INVALID
    assert failure.value.data["harness"] == "claude"
    assert app._lifecycle_manager._results == {}
    assert captured == []

    app.handle(
        "agent.harness-args.clear",
        {
            "name": "foo",
            "harness": "claude",
            "expectedHarnessArgs": {"claude": list(invalid)},
        },
    )
    started = app.handle("agent.start", {"name": "foo"})
    assert started["ok"] is True
    assert len(captured) == 1
    assert "--json" not in captured[0].args
    assert _desired_args(app, "foo") == ()


def test_one_shot_ignore_does_not_mutate_stored_args(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    invalid = ("--dangerously-skip-permissions", "--json")
    app.agents.create(
        "foo", harness_args={"claude": invalid}, preferred_harness="claude"
    )
    with pytest.raises(DaemonRequestError) as preflight:
        app.handle(
            "agent.start.preflight",
            {"name": "foo", "harness": "claude", "runtime": "interactive"},
        )
    assert preflight.value.code == ipc_errors.HARNESS_ARGS_INVALID
    ignored_preflight = app.handle(
        "agent.start.preflight",
        {
            "name": "foo", "harness": "claude", "runtime": "interactive",
            "ignoreStoredHarnessArgs": True,
        },
    )
    assert ignored_preflight["ok"] is True

    started = app.handle(
        "agent.start",
        {"name": "foo", "ignoreStoredHarnessArgs": True},
    )
    assert any("Ignoring stored harness_args" in item for item in started["warnings"])
    assert _desired_args(app, "foo") == ()
    assert app.agents.current("foo").harness_args == {"claude": invalid}

    app.handle("down", {"target": "foo"})
    with pytest.raises(DaemonRequestError) as second:
        app.handle("agent.start", {"name": "foo"})
    assert second.value.code == ipc_errors.HARNESS_ARGS_INVALID


def test_explicit_options_override_stored_and_session_updates_preserve_map(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {"pi": ("--model", "stored")}
    app.agents.create("foo", harness_args=stored, preferred_harness="pi")
    captured: list[HarnessLaunchSpec] = []
    original_start = app._harnesses.start
    monkeypatch.setattr(
        app._harnesses,
        "start",
        lambda spec: captured.append(spec) or original_start(spec),
    )
    result = app.handle(
        "lifecycle.start",
        {
            "provider": "pi", "name": "foo", "headless": True,
            "args": ["--model", "explicit"], "cwd": str(app.state_dir),
        },
    )
    assert result["ok"] is True
    assert any(
        "hyprial agent args show|set|clear" in item
        for item in result["warnings"]
    )
    assert captured[0].args == ("--model", "explicit")
    app.agents.record_session("foo", harness="pi", session_id="session-1")
    assert app.agents.current("foo").harness_args == stored


def test_clear_turns_invalid_restore_into_success(
    app: DaemonApplication,
) -> None:
    invalid = ("--dangerously-skip-permissions", "--json")
    app.agents.create("foo", harness_args={"claude": invalid})
    base = HarnessLaunchSpec(
        "claude", "foo", True, cwd=str(app.state_dir)
    )
    merged = app._merge_stored_harness_args((base,))
    assert merged[0].args == invalid
    first = ManagedHarnessRuntime(_Launcher(), failure_budget=3)
    try:
        summary = first.restore(merged)
        assert summary.failed == 1
        assert first._actor._records["claude:foo"].failures == 0
    finally:
        first.stop()
    app.handle(
        "agent.harness-args.clear",
        {
            "name": "foo", "harness": "claude",
            "expectedHarnessArgs": {"claude": list(invalid)},
        },
    )
    cleared = app._merge_stored_harness_args((base,))
    assert cleared[0].args == ()
    second = ManagedHarnessRuntime(_Launcher(), failure_budget=3)
    try:
        summary = second.restore(cleared)
        assert summary.restored == 1 and summary.failed == 0
    finally:
        second.stop()
