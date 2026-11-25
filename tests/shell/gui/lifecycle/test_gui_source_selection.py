"""Launch-target selection: source checkout, explicit dev home, and normal-mode rejections."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui import worktree as auto_worktree
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

from .test_gui_launcher import (
    FakeProcess,
    _fake_inspect,
    _fake_plan,
    _project,
)


def _git_env(home: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home or Path(tempfile.gettempdir())),
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "R5 Auto Worktree Tests",
            "GIT_AUTHOR_EMAIL": "r5-auto@example.invalid",
            "GIT_COMMITTER_NAME": "R5 Auto Worktree Tests",
            "GIT_COMMITTER_EMAIL": "r5-auto@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env or _git_env(),
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr}"
        )
    return completed.stdout.strip()


def _clear_hyprial_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("HYPRIAL_")]:
        monkeypatch.delenv(name)


def _write_project(repo: Path) -> None:
    gui = repo / "gui"
    (gui / "src").mkdir(parents=True)
    (gui / "scripts").mkdir()
    (gui / "package.json").write_text(
        json.dumps(
            {
                "name": "@hyprial/gui-app",
                "version": "0.0.0",
                "scripts": {
                    "build": "vite build",
                    "dev": "node scripts/serve.mjs",
                },
                "engines": {"node": ">=24"},
            }
        ),
        encoding="utf-8",
    )
    (gui / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "@hyprial/gui-app",
                "version": "0.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {"name": "@hyprial/gui-app", "version": "0.0.0"}
                },
            }
        ),
        encoding="utf-8",
    )
    (gui / "src" / "app.js").write_text("export const app = true;\n", encoding="utf-8")
    (gui / "scripts" / "serve.mjs").write_text("// serve\n", encoding="utf-8")
    (gui / "scripts" / "stub-backend.mjs").write_text("// stub\n", encoding="utf-8")
    (repo / "src" / "hyprial").mkdir(parents=True)
    (repo / "src" / "hyprial" / "not_gui.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_not_gui.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "not-materialized.md").write_text("docs\n", encoding="utf-8")
    (repo / "README.md").write_text("source checkout\n", encoding="utf-8")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    env = _git_env()
    _git(repo, "init", "-q", "-b", "main", env=env)
    _git(repo, "config", "user.name", "R5 Auto Worktree Tests", env=env)
    _git(repo, "config", "user.email", "r5-auto@example.invalid", env=env)
    _git(repo, "config", "commit.gpgsign", "false", env=env)
    _write_project(repo)
    _git(repo, "add", "--", ".", env=env)
    _git(repo, "commit", "-q", "-m", "base", env=env)
    return repo


def _source_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HARNESS_STATE_DIR": str(tmp_path / "state"),
        **extra,
    }


def test_build_plan_dev_without_explicit_home_uses_the_checkout_gui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    source = tmp_path / "checkout"
    home = source / "gui"
    (home / "node_modules").mkdir(parents=True)
    project = replace(_project(tmp_path), home=home, worktree=source)
    calls: list[dict] = []

    def fake_discover(**kwargs):
        calls.append(kwargs)
        return source

    monkeypatch.setattr(launcher, "discover_source_repo", fake_discover)

    plan = launcher.build_plan(
        "dev",
        environ=_source_env(tmp_path, PATH="/usr/bin"),
        cwd=source,
        inspect=_fake_inspect(project),
        no_open=True,
    )

    assert len(calls) == 1
    assert calls[0]["cwd"] == source
    assert plan.auto_worktree is None
    assert plan.project.home == home
    assert plan.env["HYPRIAL_GUI_HOME"] == str(home)
    assert not (source / ".worktrees").exists()

def test_dev_in_a_real_checkout_creates_no_worktree_branch_or_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_repo: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    git = shutil.which("git")
    assert git is not None
    home = source_repo / "gui"
    (home / "node_modules").mkdir()

    def which(name: str) -> str | None:
        return git if name == "git" else None

    def run(argv, **kwargs):
        assert argv[0] in {git, "git"}
        return subprocess.run(argv, **kwargs)

    def inspect(path, **kwargs):
        assert path == home
        return replace(_project(tmp_path), home=home, worktree=source_repo)

    plan = launcher.build_plan(
        "dev",
        environ={**_git_env(tmp_path), **_source_env(tmp_path)},
        cwd=source_repo,
        inspect=inspect,
        which=which,
        run=run,
    )

    assert plan.auto_worktree is None
    assert not (source_repo / ".worktrees").exists()
    assert _git(source_repo, "branch", "--list", "work/gui-dev-auto") == ""
    assert _git(source_repo, "status", "--porcelain") == ""
    assert list((home / "node_modules").iterdir()) == []
    assert f"current HEAD {_git(source_repo, 'rev-parse', 'HEAD')}" in plan.dev_prompt

def test_build_plan_dev_explicit_env_home_does_not_discover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("source discovery must not run")

    monkeypatch.setattr(launcher, "discover_source_repo", fail)

    plan = launcher.build_plan(
        "dev",
        environ=_source_env(
            tmp_path,
            HYPRIAL_DEV_GUI_HOME=str(project.home),
        ),
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.auto_worktree is None
    assert plan.env["HYPRIAL_GUI_HOME"] == str(project.home)


def test_build_plan_dev_explicit_option_home_does_not_discover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("source discovery must not run")

    monkeypatch.setattr(launcher, "discover_source_repo", fail)

    plan = launcher.build_plan(
        "dev",
        gui_home=project.home,
        environ=_source_env(tmp_path),
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.auto_worktree is None
    assert plan.env["HYPRIAL_GUI_HOME"] == str(project.home)


def test_build_plan_normal_without_home_does_not_discover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    def fail(**kwargs):
        raise AssertionError("normal mode must not discover a source checkout")

    monkeypatch.setattr(launcher, "discover_source_repo", fail)

    plan = launcher.build_plan(
        "",
        environ=_source_env(tmp_path),
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )

    assert plan.mode == "normal"
    assert plan.auto_worktree is None
    assert plan.project.home == project.home


@pytest.mark.parametrize(
    "case",
    ["default-driver", "invalid-driver", "missing-home", "owner-override"],
)
def test_dev_admission_rejects_before_discovery_or_product_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    home = tmp_path / "home"
    environ = {"HARNESS_STATE_DIR": str(tmp_path / "state")}
    expected = "GUI_BACKEND_DRIVER"
    if case == "invalid-driver":
        environ["HYPRIAL_GUI_BACKEND_DRIVER"] = "fixture"
    elif case in {"default-driver", "missing-home", "owner-override"}:
        if case != "default-driver":
            environ["HYPRIAL_GUI_BACKEND_DRIVER"] = "daemon"
        environ["HYPRIAL_HOME"] = str(home)
        if case == "owner-override":
            home.mkdir(mode=0o700)
            (home / "settings.json").write_text('{"version": 1}\n', encoding="utf-8")
            environ["HYPRIAL_OWNER"] = "forbidden"
            expected = "GUI_OWNER_OVERRIDE"
        else:
            expected = "GUI_HOME_NOT_INITIALIZED"

    events: list[str] = []

    def unreachable(*args, **kwargs):
        events.append("reached")
        raise AssertionError("mutation or product subprocess ran before admission")

    monkeypatch.setattr(launcher, "discover_source_repo", unreachable)

    with pytest.raises(launcher.GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ=environ,
            cwd=tmp_path,
            run=unreachable,
            inspect=unreachable,
        )

    assert caught.value.code == expected
    assert events == []
    assert not (tmp_path / ".worktrees").exists()
    assert not (tmp_path / "state" / "gui").exists()


@pytest.mark.parametrize(
    "case",
    ["default-driver", "invalid-driver", "missing-home", "owner-override"],
)
def test_cli_dev_admission_is_rejected_before_discovery_or_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    _clear_hyprial_env(monkeypatch)
    home = tmp_path / "home"
    environment = {"HARNESS_STATE_DIR": str(tmp_path / "state")}
    expected = "GUI_BACKEND_DRIVER"
    if case == "invalid-driver":
        environment["HYPRIAL_GUI_BACKEND_DRIVER"] = "fixture"
    elif case in {"default-driver", "missing-home", "owner-override"}:
        if case != "default-driver":
            environment["HYPRIAL_GUI_BACKEND_DRIVER"] = "daemon"
        environment["HYPRIAL_HOME"] = str(home)
        if case == "owner-override":
            home.mkdir(mode=0o700)
            (home / "settings.json").write_text('{"version": 1}\n', encoding="utf-8")
            environment["HYPRIAL_OWNER"] = "forbidden"
            expected = "GUI_OWNER_OVERRIDE"
        else:
            expected = "GUI_HOME_NOT_INITIALIZED"
    events: list[str] = []

    def unreachable(*args, **kwargs):
        events.append("reached")
        raise AssertionError("CLI reached discovery or session startup")

    monkeypatch.setattr(launcher, "discover_source_repo", unreachable)
    monkeypatch.setattr(launcher, "start_session", unreachable)

    result = CliRunner().invoke(
        _cli_root_app,
        ["gui", "dev", "--json", "--port", "5173"],
        env=environment,
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == expected
    assert events == []
    assert not (tmp_path / ".worktrees").exists()


def test_startup_payload_for_checkout_dev_reports_the_checkout(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    plan = _fake_plan(project, mode="dev")
    session = launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild(
            "frontend", FakeProcess(("npm", "run", "dev"), pid=701), None
        ),
        backend=None,
    )

    payload = launcher.startup_payload(session)

    assert payload["auto_worktree"] is None
    assert payload["gui_home"] == str(project.home)
    assert payload["git_worktree"] == str(project.worktree)
    assert payload["assets"] == "source"
    assert "assets   : source GUI project" in launcher.render_startup(payload)


def test_no_source_repository_is_a_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    fake_module = empty / "module.py"
    fake_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(auto_worktree, "__file__", str(fake_module))

    with pytest.raises(GuiError) as caught:
        auto_worktree.discover_source_repo(
            cwd=empty,
            environ=_git_env(),
            which=shutil.which,
            run=subprocess.run,
        )

    assert caught.value.code == "GUI_AUTO_WORKTREE_SOURCE"
    assert "source Git repository" in str(caught.value)


def test_cli_normal_rejects_gui_home_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_hyprial_env(monkeypatch)

    def unreachable(*args, **kwargs):
        raise AssertionError("normal mode reached session startup")

    monkeypatch.setattr(launcher, "start_session", unreachable)

    result = CliRunner().invoke(
        _cli_root_app,
        ["gui", "--json", "--gui-home", str(tmp_path)],
        env={"HARNESS_STATE_DIR": str(tmp_path / "state")},
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_cli_normal_rejects_inherited_hyprial_gui_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_hyprial_env(monkeypatch)

    def unreachable(*args, **kwargs):
        raise AssertionError("normal mode reached session startup")

    monkeypatch.setattr(launcher, "start_session", unreachable)

    result = CliRunner().invoke(
        _cli_root_app,
        ["gui", "--json"],
        env={
            "HYPRIAL_GUI_HOME": str(tmp_path),
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
        },
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "GUI_CONFIG_CONFLICT"
    assert payload["data"]["variables"] == ["HYPRIAL_GUI_HOME"]


def _deps_inspect(home: Path, tmp_path: Path):
    project = replace(_project(tmp_path), home=home, worktree=home.parent)

    def inspect(path, **kwargs):
        assert path == home
        return project

    return inspect


def _bare_gui(tmp_path: Path, name: str = "checkout") -> Path:
    home = tmp_path / name / "gui"
    home.mkdir(parents=True)
    return home


def _assert_missing_dependencies(caught, home: Path) -> None:
    assert caught.value.code == "GUI_HOME_INVALID"
    command = f"npm --prefix {home} ci --ignore-scripts"
    assert command in str(caught.value)
    assert caught.value.data["gui_home"] == str(home)
    assert caught.value.data["command"] == command


def test_checkout_dev_without_node_modules_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _bare_gui(tmp_path)
    monkeypatch.setattr(
        launcher, "_assert_port_free", lambda host, port: pytest.fail("bound a port")
    )
    monkeypatch.setattr(launcher, "discover_source_repo", lambda **k: home.parent)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ=_source_env(tmp_path),
            cwd=home.parent,
            inspect=_deps_inspect(home, tmp_path),
        )

    _assert_missing_dependencies(caught, home)
    assert not (home / "node_modules").exists()


def test_checkout_dev_with_node_modules_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _bare_gui(tmp_path)
    (home / "node_modules").mkdir()
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(launcher, "discover_source_repo", lambda **k: home.parent)

    plan = launcher.build_plan(
        "dev",
        environ=_source_env(tmp_path),
        cwd=home.parent,
        inspect=_deps_inspect(home, tmp_path),
    )

    assert plan.project.home == home


@pytest.mark.parametrize("via", ["option", "env"])
def test_explicit_dev_home_without_node_modules_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via: str
) -> None:
    home = _bare_gui(tmp_path, "explicit")
    monkeypatch.setattr(
        launcher, "_assert_port_free", lambda host, port: pytest.fail("bound a port")
    )
    extra = {"HYPRIAL_DEV_GUI_HOME": str(home)} if via == "env" else {}

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            gui_home=home if via == "option" else None,
            environ=_source_env(tmp_path, **extra),
            cwd=tmp_path,
            inspect=_deps_inspect(home, tmp_path),
        )

    _assert_missing_dependencies(caught, home)


def test_source_checkout_normal_without_node_modules_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _bare_gui(tmp_path)
    monkeypatch.setattr(
        launcher, "_assert_port_free", lambda host, port: pytest.fail("bound a port")
    )
    monkeypatch.setattr(target_launch, "resolve_gui_home", lambda cwd=None: home)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ=_source_env(tmp_path),
            cwd=home.parent,
            inspect=_deps_inspect(home, tmp_path),
        )

    _assert_missing_dependencies(caught, home)
