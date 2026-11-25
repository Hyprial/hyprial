"""§4 step 0: local preflight refusals send nothing and change nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import TeleportError

from .fakes import (
    AGENT,
    SOURCE_NODE,
    SOURCE_URI,
    TARGET_NODE,
    expect_failure,
    make_agent,
    rig,
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agents" / AGENT
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    return home


def _assert_untouched(r) -> None:
    assert r.link_ops() == []
    assert not {"quiesce", "export", "restart", "remove"} & set(r.names())
    assert [call for call in r.routines.calls if call[0] != "executed_by"] == []
    assert r.leftovers() == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_agent_is_agent_not_found(tmp_path: Path, dry_run: bool) -> None:
    r = rig(tmp_path)
    error = expect_failure(lambda: r.run("ghost", dry_run=dry_run))
    assert error.code == "AGENT_NOT_FOUND"
    _assert_untouched(r)


def test_agent_without_home_points_at_ensure_home(tmp_path: Path) -> None:
    r = rig(tmp_path, agent=make_agent(None))
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_RUNTIME_UNSUPPORTED"
    assert "ensure-home" in error.message
    _assert_untouched(r)


@pytest.mark.parametrize(
    "overrides",
    [
        {"headless": False},
        {"harness": "gemini"},
        {"adapter_bindings": ("lark",)},
        {"adapter_bindings": None},
    ],
    ids=["not-headless", "unsupported-harness", "adapter-bound", "adapter-bindings-unknown"],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_unsupported_runtime_is_refused(
    tmp_path: Path, overrides: dict, dry_run: bool
) -> None:
    r = rig(tmp_path, agent=make_agent(_home(tmp_path), **overrides))
    error = expect_failure(lambda: r.run(dry_run=dry_run))
    assert error.code == "TELEPORT_RUNTIME_UNSUPPORTED"
    _assert_untouched(r)


@pytest.mark.parametrize(
    "cwd",
    [
        lambda home: home.parent / "elsewhere",
        lambda home: home,
        lambda home: home / "workspace" / "nested",
    ],
    ids=["outside-home", "home-root", "below-workspace"],
)
def test_cwd_other_than_home_workspace_is_refused(tmp_path: Path, cwd) -> None:
    home = _home(tmp_path)
    r = rig(tmp_path, agent=make_agent(home, cwd=cwd(home)))
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_RUNTIME_UNSUPPORTED"
    _assert_untouched(r)


@pytest.mark.parametrize("cwd", ["workspace", None])
def test_cwd_unset_or_home_workspace_passes_preflight(tmp_path: Path, cwd) -> None:
    home = _home(tmp_path)
    agent = make_agent(home, cwd=None if cwd is None else home / cwd)
    r = rig(tmp_path, agent=agent)
    result = r.run(dry_run=True)
    assert result["ok"] is True


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_supported_harnesses_pass_preflight(tmp_path: Path, harness: str) -> None:
    r = rig(tmp_path, agent=make_agent(_home(tmp_path), harness=harness))
    assert r.run(dry_run=True)["ok"] is True
    assert r.events[-1][2].harness == harness


def test_session_that_cannot_resume_is_refused_with_its_code(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.host.failures["require_resumable"] = TeleportError(
        "TELEPORT_RUNTIME_UNSUPPORTED", "no resumable session"
    )
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_RUNTIME_UNSUPPORTED"
    _assert_untouched(r)


@pytest.mark.parametrize("dry_run", [False, True])
def test_teleport_to_this_node_is_wrong_target(tmp_path: Path, dry_run: bool) -> None:
    r = rig(tmp_path)
    error = expect_failure(lambda: r.run(to=SOURCE_NODE, dry_run=dry_run))
    assert error.code == "TELEPORT_WRONG_TARGET"
    _assert_untouched(r)


def test_preflight_lists_the_agents_routines_before_prepare(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run(to=TARGET_NODE, dry_run=True)
    assert ("executed_by", SOURCE_URI) in r.routines.calls
    assert r.names()[0] == "describe"
    assert r.names().index("require_resumable") < r.names().index("prepare")
