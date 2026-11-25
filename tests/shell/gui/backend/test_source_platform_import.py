"""Without ``fcntl`` in the GUI modules or ``os.getuid``, the GUI is not refused.

Decision gui-runtime-portable-20261121 (W1C): these probes stand in for a
platform such as Windows on Linux.  The GUI modules may not import ``fcntl``
and ``os.getuid`` is deleted; the runtime under the home must still be
created, its supervisor lock taken (a second supervisor gets GUI_RUNTIME_BUSY),
and planning must go on to its first injected step instead of answering
GUI_RUNTIME_UNAVAILABLE.  The kernel keeps its own platform lock primitive.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[5]
_PROBE = r"""
import builtins
import json
import os
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

_real_import = builtins.__import__


def _import_without_posix_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
    module = "" if globals is None else str(globals.get("__name__", ""))
    if name == "fcntl" and module.startswith("hyprial.shell.impl.gui"):
        raise ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
    return _real_import(name, globals, locals, fromlist, level)


builtins.__import__ = _import_without_posix_fcntl
import hyprial.shell.impl.gui.launcher
from hyprial.shell.impl.gui.backend import source
from hyprial.shell.impl.gui.errors import GuiError

if hasattr(os, "getuid"):
    del os.getuid

with TemporaryDirectory() as root:
    home = Path(root) / "home"
    home.mkdir(mode=0o700)
    state = home / "state"
    runtime = state / "gui" / "daemon"
    plan = SimpleNamespace(
        runtime_dir=runtime,
        admission=SimpleNamespace(home=home, state_dir=state),
    )
    outcome = {}
    try:
        lease = source.prepare_runtime(plan)
    except GuiError as error:
        outcome["first"] = error.code
    else:
        outcome["first"] = "held"
        try:
            source.prepare_runtime(plan)
        except GuiError as error:
            outcome["second"] = error.code
        lease.close()
    outcome["runtime_exists"] = runtime.is_dir()
    print(json.dumps(outcome))
"""

_ENTRY_PROBE = r"""
import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

mode = sys.argv[1]
kind = sys.argv[2]

if mode == "missing-fcntl":
    _real_import = builtins.__import__

    def _import_without_posix_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
        module = "" if globals is None else str(globals.get("__name__", ""))
        if name == "fcntl" and module.startswith("hyprial.shell.impl.gui"):
            raise ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
        return _real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _import_without_posix_fcntl

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import config
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.cli.commands.common.root import app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from typer.testing import CliRunner

if mode == "missing-getuid" and hasattr(os, "getuid"):
    del os.getuid

with TemporaryDirectory() as root_text:
    root = Path(root_text)
    state = root / "state"
    events = []

    class Reached(Exception):
        pass

    def unreachable(*args, **kwargs):
        events.append("reached")
        raise Reached()

    if kind == "build-plan":
        try:
            config.build_plan(
                "dev",
                environ={
                    "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
                    "HARNESS_STATE_DIR": str(state),
                },
                cwd=root,
                discover_source=unreachable,
                run=unreachable,
                inspect=unreachable,
                assert_port_free=unreachable,
                admit=lambda **kwargs: unreachable(),
            )
        except GuiError as error:
            code = error.code
        except Reached:
            code = None
        else:
            code = None
        output = {
            "code": code,
            "events": events,
            "worktree": (root / ".worktrees").exists(),
            "gui_state": (state / "gui").exists(),
        }
    else:
        calls = []

        def deny(*args, **kwargs):
            calls.append("subprocess")
            raise AssertionError("subprocess reached")

        subprocess.run = deny
        subprocess.Popen = deny
        home = root / "home"
        hyprial_home = root / "hyprial"
        home.mkdir()
        hyprial_home.mkdir()
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "HYPRIAL_HOME": str(hyprial_home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        }
        os.environ.clear()
        os.environ.update(environment)
        result = CliRunner().invoke(
            app,
            ["gui", "dev", "--json", "--port", "5173"],
            env=environment,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        output = {
            "exit": result.exit_code,
            "code": payload.get("code"),
            "events": calls,
            "worktree": (root / ".worktrees").exists(),
            "gui_state": (state / "gui").exists(),
        }
    print(json.dumps(output))
"""


def test_runtime_and_its_lock_work_without_posix_locks_and_uid() -> None:
    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else os.pathsep.join((source, inherited))
    )
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "first": "held",
        "second": "GUI_RUNTIME_BUSY",
        "runtime_exists": True,
    }


def _run_entry_probe(mode: str, kind: str, cwd: Path) -> dict:
    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else os.pathsep.join((source, inherited))
    )
    completed = subprocess.run(
        [sys.executable, "-c", _ENTRY_PROBE, mode, kind],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("mode", ["missing-fcntl", "missing-getuid"])
def test_build_plan_is_not_refused_for_the_platform(
    mode: str, tmp_path: Path
) -> None:
    result = _run_entry_probe(mode, "build-plan", tmp_path)

    assert result["code"] != "GUI_RUNTIME_UNAVAILABLE"
    assert result["events"] == ["reached"]


@pytest.mark.parametrize("mode", ["missing-fcntl", "missing-getuid"])
def test_cli_is_not_refused_for_the_platform(
    mode: str, tmp_path: Path
) -> None:
    result = _run_entry_probe(mode, "cli", tmp_path)

    assert result["code"] != "GUI_RUNTIME_UNAVAILABLE"


def test_gui_package_has_zero_platform_branches_and_delegates_to_manysys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ast
    import errno
    from hyprial.shell.impl.gui import project as gui_project
    from hyprial.shell.impl.gui import worktree as gui_worktree
    from hyprial.shell.impl.gui.backend import admission as gui_admission
    from hyprial.shell.impl.gui.backend import handoff as gui_handoff
    from hyprial.shell.impl.gui.errors import GuiError

    gui_root = _ROOT / "src" / "hyprial" / "shell" / "impl" / "gui"
    forbidden_modules = {"_thread", "fcntl", "msvcrt", "select"}
    forbidden_attrs = {
        ("os", "name"),
        ("os", "getuid"),
        ("os", "geteuid"),
        ("sys", "platform"),
        ("signal", "pthread_kill"),
        ("_thread", "interrupt_main"),
    }
    offenders: list[str] = []
    for file_path in sorted(gui_root.rglob("*.py")):
        rel = file_path.relative_to(_ROOT).as_posix()
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules:
                        offenders.append(f"{rel}:{node.lineno}:import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden_modules:
                    offenders.append(f"{rel}:{node.lineno}:from {node.module}")
            elif isinstance(node, ast.Attribute) and isinstance(
                node.value, ast.Name
            ):
                if (node.value.id, node.attr) in forbidden_attrs:
                    offenders.append(
                        f"{rel}:{node.lineno}:{node.value.id}.{node.attr}"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr"}
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                if (node.args[0].id, node.args[1].value) in forbidden_attrs:
                    offenders.append(
                        f"{rel}:{node.lineno}:{node.func.id}({node.args[0].id}, {node.args[1].value!r})"
                    )
    assert offenders == []

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(gui_admission.manysys, "posix_metadata_enforced", lambda: False)
    ownerless_mock = gui_admission.SourceAdmission(
        driver="contract-mock",
        owner=None,
        node="local",
        principal=None,
        home=None,
        state_dir=None,
        socket_path=None,
        phase="bootstrap",
    )
    assert ownerless_mock.runtime_root == tmp_path / "hyprial-gui-user"

    monkeypatch.setattr(
        gui_project.manysys,
        "managed_launcher_names",
        lambda name: (f"{name}.cmd", name),
    )
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")
    assert gui_project._npm_prefix("C:\\nodejs\\npm.cmd") == [
        "C:\\Windows\\System32\\cmd.exe",
        "/d",
        "/s",
        "/c",
        "C:\\nodejs\\npm.cmd",
    ]
    assert gui_worktree._npm_prefix(
        "C:\\nodejs\\npm.cmd", {"COMSPEC": "C:\\Windows\\System32\\cmd.exe"}
    ) == [
        "C:\\Windows\\System32\\cmd.exe",
        "/d",
        "/s",
        "/c",
        "C:\\nodejs\\npm.cmd",
    ]

    class FakeExclusiveSocket:
        def __enter__(self) -> FakeExclusiveSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def setsockopt(self, *_args: object) -> None:
            return None

        def bind(self, _addr: object) -> None:
            raise PermissionError(errno.EACCES, "WSAEACCES")

    monkeypatch.setattr(gui_handoff.socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    monkeypatch.setattr(gui_handoff.socket, "socket", lambda *_a, **_k: FakeExclusiveSocket())
    with pytest.raises(GuiError) as caught:
        gui_handoff.assert_port_free("127.0.0.1", 4173)
    assert caught.value.code == "GUI_PORT_UNAVAILABLE"
    assert "WSAEACCES" in str(caught.value)
