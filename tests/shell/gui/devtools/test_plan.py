# Decision record (hyprial gui --with-dev-tools, PR 4b-C, 2026-11-17):
# - The flag is a launch argument of the host and nothing else: the cases read
#   the host argv of the plan for a source checkout, for `dev` and for an
#   installed tree, and require `--dev-tools` exactly when the plan says so. A
#   default plan of either kind must not hold the argument; that is what the
#   native shell and a plain `hyprial gui` rely on.
# - `dev` implies the tools; `dev --with-dev-tools` is the same plan.
# - The host confirms the flag on its ready line; a host that reports the other
#   value is a contract failure, not a silently different session.
"""The launch plan, child argv and child environment of ``--with-dev-tools``."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.shell.impl.cli.commands.common.root import app as cli_root
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import launch as target_launch
from tests.shell.gui.backend import test_source_session as fixtures
from tests.shell.gui.installed.test_installed_gui import _admission, _assets
from tests.shell.gui.lifecycle.test_gui_launcher import _fake_inspect, _project

FLAG = "--dev-tools"


def source_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, **options: Any) -> Any:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    checkout = tmp_path
    if action == "dev" or options.get("live_source"):
        checkout = tmp_path / "checkout"
        (checkout / "gui" / "node_modules").mkdir(parents=True, exist_ok=True)
        project = replace(project, home=checkout / "gui", worktree=checkout)
    monkeypatch.setattr(launcher, "discover_source_repo", lambda **kwargs: checkout)
    return launcher.build_plan(
        action,
        environ={
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
            "PATH": "/usr/bin",
        },
        cwd=checkout,
        inspect=_fake_inspect(project),
        no_open=True,
        **options,
    )


def installed_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options: Any) -> Any:
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    return launcher.build_plan(
        "",
        environ={},
        cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("an installed plan must not inspect npm"),
        admit=lambda **k: _admission(tmp_path),
        **options,
    )


def test_a_default_source_plan_has_no_developer_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = source_plan(tmp_path, monkeypatch, "")
    assert plan.mode == "normal" and plan.dev_tools is False and plan.api_docs is False
    assert FLAG not in plan.frontend_argv


def test_the_flag_adds_the_host_argument_to_a_source_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = source_plan(tmp_path, monkeypatch, "", with_dev_tools=True)
    assert plan.mode == "normal" and plan.hot_reload is False
    assert plan.dev_tools is True and plan.api_docs is False
    assert plan.frontend_argv[-1] == FLAG and plan.frontend_argv.count(FLAG) == 1
    assert "--root" in plan.frontend_argv
    assert plan.build_argv is not None and FLAG not in plan.build_argv


@pytest.mark.parametrize("with_dev_tools", [False, True])
def test_dev_implies_developer_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_dev_tools: bool) -> None:
    plan = source_plan(tmp_path, monkeypatch, "dev", with_dev_tools=with_dev_tools)
    assert plan.mode == "dev" and plan.dev_tools is True
    assert plan.frontend_argv[-1] == FLAG and plan.frontend_argv.count(FLAG) == 1
    assert plan.assets_argv is not None and FLAG not in plan.assets_argv


def test_a_default_installed_plan_has_no_developer_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = installed_plan(tmp_path, monkeypatch)
    assert plan.project.installed is True and plan.dev_tools is False
    assert FLAG not in plan.frontend_argv


def test_an_installed_plan_accepts_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = installed_plan(tmp_path, monkeypatch, with_dev_tools=True)
    assert plan.project.installed is True and plan.dev_tools is True
    assert plan.frontend_argv[-1] == FLAG and plan.frontend_argv.count(FLAG) == 1


def test_no_environment_variable_turns_the_tools_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    plan = launcher.build_plan(
        "",
        environ={
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
            "HYPRIAL_DEV_TOOLS": "1",
            "HYPRIAL_GUI_DEV_TOOLS": "1",
        },
        cwd=tmp_path,
        inspect=_fake_inspect(project),
    )
    assert plan.dev_tools is False and FLAG not in plan.frontend_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["gui", "--json"], False), (["gui", "--json", "--with-dev-tools"], True), (["gui", "dev", "--json"], False)],
)
def test_the_command_hands_the_flag_to_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    seen: list[dict[str, Any]] = []

    def build(action: str, **options: Any) -> Any:
        seen.append({"action": action, **options})
        raise GuiError("INVALID_ARGUMENT", "stopped by the test before any child")

    monkeypatch.setattr(launcher, "build_plan", build)
    result = CliRunner().invoke(cli_root, argv, env={"HARNESS_STATE_DIR": str(tmp_path / "state")})
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
    assert len(seen) == 1 and seen[0]["with_dev_tools"] is expected


def ready(plan: Any, **fields: Any) -> str:
    payload = json.loads(fixtures._ready_line(plan)[len(backend_source.HOST_READY_PREFIX) :])
    payload.update(fields)
    return backend_source.HOST_READY_PREFIX + json.dumps(payload)


def test_the_ready_line_must_confirm_the_flag_of_the_plan(tmp_path: Path) -> None:
    plain = fixtures._plan(tmp_path)
    tools = replace(plain, dev_tools=True)
    assert backend_source.parse_host_ready(ready(plain), plain)["port"] == plain.port
    assert backend_source.parse_host_ready(ready(plain, devTools=False, apiDocs=False), plain)["devTools"] is False
    assert backend_source.parse_host_ready(ready(tools, devTools=True, apiDocs=False), tools)["devTools"] is True
    for plan, line in (
        (plain, ready(plain, devTools=True)),
        (tools, ready(tools)),
        (tools, ready(tools, devTools=False)),
        (tools, ready(tools, devTools="true")),
    ):
        with pytest.raises(GuiError) as caught:
            backend_source.parse_host_ready(line, plan)
        assert caught.value.code == "GUI_BACKEND_CONTRACT"
