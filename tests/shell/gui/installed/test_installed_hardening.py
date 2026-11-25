"""Installed launch hardening: child import path, unlisted files, loopback and verification proof."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
from typing import Any

import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import lifecycle
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.install import resources
from tests.shell.gui.backend.test_source_session import (
    GRANT as SESSION_GRANT,
    FakeProcess,
    _install_success_seams,
    _installed_plan as _session_plan,
    _ready_line,
)
from tests.shell.gui.installed.test_installed_gui import _admission, _assets
from tests.shell.gui.host.support import GRANT, UPSTREAM_TOKEN


def _installed_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    plan = launcher.build_plan(
        "",
        environ={},
        cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("installed normal mode must not inspect npm"),
        admit=lambda **k: _admission(tmp_path),
    )
    return root, plan


def test_the_installed_host_argv_drops_the_current_directory_from_the_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = _installed_plan(tmp_path, monkeypatch)
    assert plan.frontend_argv[:3] == (sys.executable, "-P", "-m")


def _host_env(port: int) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
        "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:9",
        "HYPRIAL_GUI_UPSTREAM_TOKEN": UPSTREAM_TOKEN,
        "HYPRIAL_GUI_LAUNCH_GRANT": GRANT,
    }


def _run_host_in(root: Path, argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=root,
        env=_host_env(0),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _plant_and_start(root: Path, argv: list[str], marker: Path) -> bool:
    (root / resources.MANIFEST_NAME).unlink()
    (root / "json.py").write_text(
        f"open({str(marker)!r}, 'w').write('planted')\nraise ImportError('planted')\n",
        encoding="utf-8",
    )
    child = _run_host_in(root, argv)
    try:
        child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    return marker.exists()


def test_a_json_py_planted_in_the_asset_root_is_not_imported_by_the_host_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, plan = _installed_plan(tmp_path, monkeypatch)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])
    argv = list(plan.frontend_argv)
    argv[argv.index("--port") + 1] = port
    marker = tmp_path / "ran"
    control = [item for item in argv if item != "-P"]
    assert _plant_and_start(root, control, marker) is True
    marker.unlink()
    child = _run_host_in(root, argv)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().startswith("HYPRIAL_GUI_READY ")
        child.terminate()
        child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert not marker.exists()


def test_a_file_the_manifest_does_not_list_fails_installed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    (root / "json.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail("unlisted files reached inspect"),
            admit=lambda **k: _admission(tmp_path),
        )
    assert caught.value.code == "GUI_ASSETS_INVALID"
    assert "json.py" in str(caught.value)


def test_the_manifest_itself_and_listed_nested_files_are_not_unlisted(tmp_path: Path) -> None:
    root = _assets(tmp_path)
    (root / "assets" / "chunk.js").write_text("x\n", encoding="utf-8")
    manifest = resources.build_manifest(
        root, package_version="1", source_commit="a" * 40, source_remote="https://example.invalid/h", gui_version="1"
    )
    resources.write_manifest(root, manifest)
    assert resources.load_assets(root).manifest["files"]
    assert json.loads((root / resources.MANIFEST_NAME).read_text())["files"]


@pytest.mark.parametrize("value", ["0.0.0.0", "::", "192.0.2.1", "127.0.0.2", "::ffff:127.0.0.1", "10.0.0.5"])
def test_a_non_loopback_ip_is_refused_before_admission_or_any_child(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    monkeypatch.setattr(launcher, "_assert_port_free", lambda *a: pytest.fail("port probe ran for a non-loopback ip"))
    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            ip=value,
            environ={},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail("inspect ran for a non-loopback ip"),
            admit=lambda **k: pytest.fail("admission ran for a non-loopback ip"),
        )
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(("value", "expected"), [("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("::1", "::1")])
def test_exactly_the_hosts_own_loopback_binds_are_accepted(value: str, expected: str) -> None:
    assert launcher.parse_host(value) == expected


def test_the_launcher_loopback_set_is_the_hosts_own_bind_set() -> None:
    from hyprial.shell.impl.gui.backend.config import LOOPBACK_IPS
    from hyprial.shell.impl.gui.host.__main__ import LOOPBACK_BINDS

    assert LOOPBACK_IPS | {"localhost"} == set(LOOPBACK_BINDS)


class _Health:
    def __init__(self, body: Any) -> None:
        self._raw = json.dumps(body).encode()
        self.status = 200

    def __enter__(self) -> "_Health":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


VERIFIED_HEALTH = {"ok": True, "dataMode": "hosted", "assets": "installed", "assetsVerified": True}


def _drive_installed_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    health: Any = VERIFIED_HEALTH,
    host_ready: bool = True,
    host_exit: int | None = None,
) -> tuple[list[str], Path, Any]:
    plan = _session_plan(tmp_path, open_browser=False)
    events, _captured = _install_success_seams(monkeypatch, plan)

    def spawn(active, argv, *, name, env, cwd, log_path, popen, mock_capture=False, redactions=None):
        events.append(f"spawn:{name}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("log\n")
        lines: queue.Queue[Any] = queue.Queue()
        if name == "frontend" and host_ready:
            lines.put(_ready_line(active))
        code = host_exit if name == "frontend" else None
        return lifecycle.GuiChild(name, FakeProcess(os.getpid(), code), None, lines)

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    clock = itertools.count(0, 100)
    outcome: list[Any] = []
    try:
        outcome.append(
            lifecycle.start_session(
                plan,
                wait_for_http=lambda url, process, *, label, **kw: events.append(f"health:{label}"),
                terminate_child=lambda child: events.append(f"terminate:{child.name}"),
                sleep=lambda _: None,
                monotonic=lambda: float(next(clock)),
                urlopen=lambda url, timeout: _Health(health),
                popen=lambda *a, **k: pytest.fail("fake Popen must not be used"),
            )
        )
    except GuiError as error:
        outcome.append(error)
    return events, tmp_path / "runtime", outcome[0]


def _assert_cleaned(events: list[str], runtime: Path) -> None:
    assert "terminate:frontend" in events
    assert not [event for event in events if "request-http" in event]
    for name in ("launch.url", "launch.ready", "host.log", "request-http.log", "request-http.json", "server.json"):
        assert not (runtime / name).exists(), name


@pytest.mark.parametrize(
    "health",
    [
        {"ok": True, "assets": "explicit-unverified", "assetsVerified": False},
        {"ok": True, "assets": "installed"},
        {"ok": True, "assets": "installed", "assetsVerified": "true"},
        {"ok": True, "assets": "source", "assetsVerified": True},
        ["not", "an", "object"],
    ],
)
def test_an_installed_start_is_refused_unless_the_host_reports_verified_installed_assets(
    health: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events, runtime, outcome = _drive_installed_start(monkeypatch, tmp_path, health=health)
    assert isinstance(outcome, GuiError) and outcome.code == "GUI_ASSETS_INVALID"
    assert "handoff" not in events and "publication" not in events
    _assert_cleaned(events, runtime)


def test_an_installed_start_with_verified_assets_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events, _runtime, outcome = _drive_installed_start(monkeypatch, tmp_path)
    try:
        assert not isinstance(outcome, GuiError)
        assert events[-2:] == ["handoff", "publication"]
        assert SESSION_GRANT
    finally:
        if not isinstance(outcome, GuiError):
            outcome.stop()


def test_an_installed_host_that_never_becomes_ready_is_stopped_and_removes_runtime_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events, runtime, outcome = _drive_installed_start(monkeypatch, tmp_path, host_ready=False)
    assert isinstance(outcome, GuiError) and outcome.code == "GUI_READY_TIMEOUT"
    _assert_cleaned(events, runtime)


def test_an_installed_host_that_exits_before_its_ready_line_is_stopped_and_removes_runtime_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events, runtime, outcome = _drive_installed_start(monkeypatch, tmp_path, host_ready=False, host_exit=2)
    assert isinstance(outcome, GuiError) and outcome.code == "GUI_START_FAILED"
    _assert_cleaned(events, runtime)
