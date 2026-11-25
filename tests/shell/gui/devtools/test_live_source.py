# Decision record (hyprial gui --live-source, PR 5, 2026-11-17):
# - Two independent switches. --live-source selects the Vite dev server behind
#   the host, --with-dev-tools the tool layer; the positional `dev` is an alias
#   for both and combining it with either flag changes nothing. The table case
#   reads every combination off the real plan: live source is the presence of
#   the Vite child argv, hot_reload equals it, and mode is "dev" exactly when
#   both switches are on.
# - --gui-home and HYPRIAL_DEV_GUI_HOME belong to live source: the option is an
#   INVALID_ARGUMENT naming --live-source without it, the variable is ignored.
# - With both switches the host argv also names the Swagger UI directory of the
#   project (--docs-assets), but only when both files are there: a stale
#   node_modules starts the tools without the docs page instead of refusing.
# - The startup record gains only live_source (owner decision Q-6: the Vite
#   port is not exposed).
"""The plan, argv, source rule and record of ``--live-source``."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.root import app as cli_root
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import config as backend_config
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.errors import GuiError
from tests.shell.gui.backend import test_source_session as fixtures
from tests.shell.gui.devtools.test_plan import source_plan
from tests.shell.gui.lifecycle.test_gui_launcher import _fake_inspect, _project

CORPUS = Path(__file__).resolve().parents[4] / "contract" / "cli" / "gui-startup-record.v1.json"
SWITCHES = [
    ("", False, False, False, False, "normal"),
    ("", True, False, True, False, "normal"),
    ("", False, True, False, True, "normal"),
    ("", True, True, True, True, "dev"),
    ("dev", False, False, True, True, "dev"),
    ("dev", True, False, True, True, "dev"),
    ("dev", False, True, True, True, "dev"),
    ("dev", True, True, True, True, "dev"),
]


@pytest.mark.parametrize(("action", "with_tools", "with_live", "tools", "live", "mode"), SWITCHES)
def test_the_two_switches_and_the_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    with_tools: bool,
    with_live: bool,
    tools: bool,
    live: bool,
    mode: str,
) -> None:
    plan = source_plan(tmp_path, monkeypatch, action, with_dev_tools=with_tools, live_source=with_live)
    assert (plan.dev_tools, plan.live_source, plan.hot_reload, plan.mode) == (tools, live, live, mode)
    assert ("--dev-tools" in plan.frontend_argv) is tools
    assert ("--root" in plan.frontend_argv) is not live
    assert (plan.assets_argv is not None) is live
    assert (plan.build_argv is None) is live
    assert "--live-upstream" not in plan.frontend_argv


@pytest.mark.parametrize("action", ["start", "live", "devtools", "--dev"])
def test_any_other_positional_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    with pytest.raises(GuiError) as caught:
        source_plan(tmp_path, monkeypatch, action, live_source=True)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert "--live-source" in str(caught.value) and "--with-dev-tools" in str(caught.value)


def explicit_home(tmp_path: Path) -> tuple[Path, Any]:
    home = tmp_path / "other" / "gui"
    (home / "node_modules").mkdir(parents=True)
    return home, replace(_project(tmp_path), home=home, worktree=home.parent)


def plan_with_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, **options: Any) -> Any:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(launcher, "discover_source_repo", lambda **k: pytest.fail("an explicit home must not discover"))
    environ = {
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HARNESS_STATE_DIR": str(tmp_path / "state"),
        "PATH": "/usr/bin",
        **options.pop("environ", {}),
    }
    inspected = options.pop("inspected")
    return launcher.build_plan(
        action, environ=environ, cwd=tmp_path, inspect=lambda home, **k: replace(inspected, home=home), no_open=True, **options
    )


def test_gui_home_needs_live_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, project = explicit_home(tmp_path)
    for options in ({}, {"with_dev_tools": True}):
        with pytest.raises(GuiError) as caught:
            plan_with_home(tmp_path, monkeypatch, "", gui_home=home, inspected=project, **options)
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
        assert "--live-source" in str(caught.value)
    for action, options in (("", {"live_source": True}), ("dev", {})):
        plan = plan_with_home(tmp_path, monkeypatch, action, gui_home=home, inspected=project, **options)
        assert plan.project.home == home.resolve() and plan.live_source is True


def test_the_dev_home_variable_is_read_only_with_live_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, project = explicit_home(tmp_path)
    environ = {"HYPRIAL_DEV_GUI_HOME": str(home)}
    live = plan_with_home(tmp_path, monkeypatch, "", live_source=True, environ=environ, inspected=project)
    assert live.project.home == home.resolve()
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    own = _project(tmp_path)
    built = launcher.build_plan(
        "",
        environ={"HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock", "HARNESS_STATE_DIR": str(tmp_path / "state"), **environ},
        cwd=tmp_path,
        inspect=_fake_inspect(own),
        with_dev_tools=True,
    )
    assert built.project.home == own.home and built.live_source is False


def swagger(home: Path, *names: str) -> Path:
    directory = home / "node_modules" / "swagger-ui-dist"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("x", encoding="utf-8")
    return directory


def test_live_source_with_tools_names_the_swagger_directory_of_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = swagger(tmp_path / "checkout" / "gui", "swagger-ui-bundle.js", "swagger-ui.css")
    plan = source_plan(tmp_path, monkeypatch, "dev")
    argv = plan.frontend_argv
    assert argv[argv.index("--docs-assets") + 1] == str(directory)
    assert "--dev-tools" in argv and "--root" not in argv
    alone = source_plan(tmp_path, monkeypatch, "", live_source=True)
    assert "--docs-assets" not in alone.frontend_argv and "--dev-tools" not in alone.frontend_argv
    built = source_plan(tmp_path, monkeypatch, "", with_dev_tools=True)
    assert "--docs-assets" not in built.frontend_argv


def test_an_incomplete_swagger_directory_is_not_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swagger(tmp_path / "checkout" / "gui", "swagger-ui.css")
    plan = source_plan(tmp_path, monkeypatch, "dev")
    assert "--docs-assets" not in plan.frontend_argv and plan.frontend_argv[-1] == "--dev-tools"


def test_a_managed_worktree_uses_the_docs_files_of_the_installed_tree(tmp_path: Path) -> None:
    installed = tmp_path / "_gui"
    docs = installed / "dev-tools" / "docs"
    docs.mkdir(parents=True)
    home = tmp_path / "managed" / "gui"
    swagger(home, "swagger-ui-bundle.js", "swagger-ui.css")
    assert backend_config.docs_assets(home, managed=True, installed_root=installed) is None
    for name in ("swagger-ui-bundle.js", "swagger-ui.css"):
        (docs / name).write_text("x", encoding="utf-8")
    assert backend_config.docs_assets(home, managed=True, installed_root=installed) == docs
    assert backend_config.docs_assets(home, managed=False, installed_root=installed) == home / "node_modules" / "swagger-ui-dist"


def ready(plan: Any, mode: str, **fields: Any) -> str:
    payload = json.loads(fixtures._ready_line(plan)[len(backend_source.HOST_READY_PREFIX) :])
    payload.update({"mode": mode, **fields})
    return backend_source.HOST_READY_PREFIX + json.dumps(payload)


def live_only(tmp_path: Path) -> Any:
    return replace(fixtures._dev_plan(tmp_path), mode="normal")


def test_the_host_of_a_live_source_plan_must_say_development(tmp_path: Path) -> None:
    plan = live_only(tmp_path)
    assert plan.live_source is True and plan.dev_tools is False
    assert backend_source.parse_host_ready(ready(plan, "development"), plan)["mode"] == "development"
    with pytest.raises(GuiError) as caught:
        backend_source.parse_host_ready(ready(plan, "production"), plan)
    assert caught.value.code == "GUI_BACKEND_CONTRACT"
    built = fixtures._plan(tmp_path)
    with pytest.raises(GuiError):
        backend_source.parse_host_ready(ready(built, "development"), built)


def test_a_live_source_session_without_tools_reports_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    monkeypatch.setattr(fixtures, "_ready_line", lambda plan: ready_line(plan))
    plan = live_only(tmp_path)
    session, _events, captured = fixtures._start(monkeypatch, plan, open_browser=False)
    try:
        payload = launcher.startup_payload(session)
        jsonschema.Draft202012Validator(corpus["startupRecord"]).validate(json.loads(json.dumps(payload)))
        assert (payload["mode"], payload["live_source"], payload["hot_reload"]) == ("normal", True, True)
        assert payload["dev_tools"] is False and payload["api_docs"] is False
        assert "live_source_port" not in payload
        assert isinstance(payload["assets_pid"], int)
        argv = captured["frontend"]["argv"]
        assert "--live-upstream" in argv and "--dev-tools" not in argv
        assert not [name for name in captured["assets"]["env"] if name.startswith("HYPRIAL_GUI_")]
        lines = launcher.render_startup(payload).splitlines()
        assert f"  live source: {payload['gui_home']} (edits reload in the browser)" in lines
    finally:
        session.stop()


PLAIN_READY = fixtures._ready_line


def ready_line(plan: Any) -> str:
    payload = json.loads(PLAIN_READY(plan)[len(backend_source.HOST_READY_PREFIX) :])
    payload["mode"] = "development" if plan.live_source else "production"
    return backend_source.HOST_READY_PREFIX + json.dumps(payload)


def test_a_built_session_reports_no_live_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, _events, _captured = fixtures._start(monkeypatch, fixtures._plan(tmp_path), open_browser=False)
    try:
        payload = launcher.startup_payload(session)
        assert payload["live_source"] is False and payload["hot_reload"] is False
        assert "  live source: not used" in launcher.render_startup(payload).splitlines()
    finally:
        session.stop()


def test_the_corpus_declares_live_source_and_no_port() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    schema = corpus["startupRecord"]
    assert schema["properties"]["live_source"] == {"type": "boolean"}
    assert "live_source" not in schema["required"] and "live_source_port" not in schema["properties"]
    examples = corpus["examples"]["startup"]
    assert {example.get("live_source") for example in examples} == {True, False}
    for example in examples:
        assert example["live_source"] is example["hot_reload"]
        assert "live_source_port" not in example
    assert corpus["version"] == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["gui", "--json"], (False, False)),
        (["gui", "--json", "--live-source"], (False, True)),
        (["gui", "--json", "--live-source", "--with-dev-tools"], (True, True)),
        (["gui", "dev", "--json", "--live-source"], (False, True)),
    ],
)
def test_the_command_hands_both_switches_to_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: tuple[bool, bool]
) -> None:
    seen: list[dict[str, Any]] = []

    def build(action: str, **options: Any) -> Any:
        seen.append({"action": action, **options})
        raise GuiError("INVALID_ARGUMENT", "stopped by the test before any child")

    monkeypatch.setattr(launcher, "build_plan", build)
    result = CliRunner().invoke(cli_root, argv, env={"HARNESS_STATE_DIR": str(tmp_path / "state")})
    assert result.exit_code == 1, result.output
    assert len(seen) == 1 and (seen[0]["with_dev_tools"], seen[0]["live_source"]) == expected
