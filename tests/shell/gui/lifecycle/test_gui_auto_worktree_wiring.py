"""Launcher wiring for the automatic ``gui dev`` worktree and browser opener."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui import worktree as auto_worktree

from .test_gui_launcher import (
    FakeProcess,
    _fake_inspect,
    _fake_plan,
    _project,
)


def test_build_plan_dev_without_explicit_home_uses_auto_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    source = tmp_path / "auto-source"
    source.mkdir()
    home = source / ".worktrees" / "gui-dev-auto" / "gui"
    home.mkdir(parents=True)
    project = replace(
        _project(tmp_path),
        home=home,
        worktree=home.parent,
        branch=auto_worktree.AUTO_BRANCH,
    )
    install = auto_worktree.DependencyInstall(
        command=("/fake/npm", "ci", "--ignore-scripts"),
        cwd=home,
        ran=True,
        returncode=0,
        output="added 1 package",
    )
    auto = auto_worktree.AutoGuiWorktree(
        source=source,
        path=home.parent,
        branch=auto_worktree.AUTO_BRANCH,
        base="a" * 40,
        home=home,
        created=True,
        install=install,
    )
    calls: list[dict] = []

    def fake_ensure(**kwargs):
        calls.append(kwargs)
        return auto

    monkeypatch.setattr(launcher, "ensure_dev_worktree", fake_ensure)

    plan = launcher.build_plan(
        "dev",
        environ={"PATH": "/usr/bin"},
        cwd=source,
        inspect=_fake_inspect(project),
        no_open=True,
    )

    assert len(calls) == 1
    assert calls[0]["cwd"] == source
    assert plan.auto_worktree is auto
    assert plan.project.home == home
    assert plan.env["HYPRIAL_GUI_HOME"] == str(home)
    assert plan.open_browser is False


def test_build_plan_dev_explicit_env_home_does_not_create_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("auto worktree creation must not run")

    monkeypatch.setattr(launcher, "ensure_dev_worktree", fail)

    plan = launcher.build_plan(
        "dev",
        environ={"HYPRIAL_DEV_GUI_HOME": str(project.home)},
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.auto_worktree is None
    assert plan.env["HYPRIAL_GUI_HOME"] == str(project.home)


def test_build_plan_dev_explicit_option_home_does_not_create_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("auto worktree creation must not run")

    monkeypatch.setattr(launcher, "ensure_dev_worktree", fail)

    plan = launcher.build_plan(
        "dev",
        gui_home=project.home,
        environ={},
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.auto_worktree is None
    assert plan.env["HYPRIAL_GUI_HOME"] == str(project.home)


def test_build_plan_normal_without_home_does_not_create_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("normal mode must not create an auto worktree")

    monkeypatch.setattr(launcher, "ensure_dev_worktree", fail)

    plan = launcher.build_plan(
        "",
        environ={},
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.mode == "normal"
    assert plan.auto_worktree is None
    assert plan.project.home == project.home


def test_startup_payload_reports_true_auto_gui_home_and_worktree(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    auto_path = source / ".worktrees" / "gui-dev-auto"
    home = auto_path / "gui"
    home.mkdir(parents=True)
    project = replace(
        _project(tmp_path),
        home=home,
        worktree=auto_path,
        branch=auto_worktree.AUTO_BRANCH,
    )
    install = auto_worktree.DependencyInstall(
        command=("/fake/npm", "ci", "--ignore-scripts"),
        cwd=home,
        ran=True,
        returncode=0,
    )
    auto = auto_worktree.AutoGuiWorktree(
        source=source,
        path=auto_path,
        branch=auto_worktree.AUTO_BRANCH,
        base="b" * 40,
        home=home,
        created=True,
        install=install,
    )
    plan = replace(_fake_plan(project, mode="dev"), auto_worktree=auto)
    session = launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild(
            "frontend", FakeProcess(("npm", "run", "dev"), pid=701), None
        ),
        backend=None,
    )

    payload = launcher.startup_payload(session)

    assert payload["gui_home"] == str(home)
    assert payload["git_worktree"] == str(auto_path)
    assert payload["git_branch"] == auto_worktree.AUTO_BRANCH
    assert payload["auto_worktree"]["source"] == str(source)
    assert payload["auto_worktree"]["path"] == str(auto_path)
    assert payload["auto_worktree"]["branch"] == auto_worktree.AUTO_BRANCH
    assert payload["auto_worktree"]["created"] is True
    assert payload["auto_worktree"]["install"]["command"] == [
        "/fake/npm",
        "ci",
        "--ignore-scripts",
    ]
    assert payload["auto_worktree"]["install"]["cwd"] == str(home)
    human = launcher.render_startup(payload)
    assert f"GUI_HOME : {home}" in human
    assert f"Git      : {auto_worktree.AUTO_BRANCH} at {auto_path}" in human


def test_default_opener_runs_once_after_readiness_and_no_open_suppresses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    events: list[tuple[str, str]] = []

    def spawn(argv, **kwargs):
        return FakeProcess(argv, pid=801)

    monkeypatch.setattr(
        launcher,
        "_wait_for_http",
        lambda url, *args, **kwargs: events.append(("ready", url)),
    )
    plan = replace(_fake_plan(project, mode="dev"), open_browser=True)

    session = launcher.start_session(
        plan,
        popen=spawn,
        opener=lambda url: events.append(("open", url)),
        sleep=lambda seconds: None,
    )

    assert events == [("ready", plan.frontend_health_url), ("open", plan.url)]
    assert session.frontend.process.pid == 801

    no_open_events: list[str] = []
    launcher.start_session(
        replace(plan, open_browser=False),
        popen=spawn,
        opener=no_open_events.append,
        sleep=lambda seconds: None,
    )
    assert no_open_events == []
