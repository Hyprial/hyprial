"""The launcher fences readiness on a per-launch nonce, on every platform.

A Windows venv / ``uv tool`` ``python.exe`` is a redirector: the process the
launcher spawned (``Popen.pid``) starts the real interpreter as another
process, so daemon.json's ``pid`` never equals ``Popen.pid`` and a pid fence
waits until timeout (#2035 0407fbd1e).  The nonce is used everywhere -- no
``os.name`` branch -- and the daemon pops it so no descendant inherits it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application.startup import _StartupMixin
from hyprial.kernel import DAEMON_LAUNCH_ID_ENV
from hyprial.shell.impl.login.generation import take_launch_id
from hyprial.shell.impl.cli.commands.common import support


def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, marker: dict[str, Any]) -> None:
    (tmp_path / "daemon.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(
        support, "get_services", lambda: SimpleNamespace(_state_dir=lambda: tmp_path)
    )


def test_ready_marker_accepts_this_launch_behind_a_redirector_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _state(monkeypatch, tmp_path, {"pid": 456, "launchId": "this-launch"})

    assert support._daemon_ready_for_process(123, launch_id="this-launch")
    assert not support._daemon_ready_for_process(123, launch_id="old-launch")
    assert not support._daemon_ready_for_process(123)


def test_an_older_generation_without_a_nonce_never_passes_a_nonce_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _state(monkeypatch, tmp_path, {"pid": 123})

    assert support._daemon_ready_for_process(123)
    assert not support._daemon_ready_for_process(456)
    assert not support._daemon_ready_for_process(123, launch_id="this-launch")


def test_daemon_marker_records_the_launch_id_it_was_given(tmp_path: Path) -> None:
    app = SimpleNamespace(
        state_dir=tmp_path, node_id="audit-node", socket_path=tmp_path / "daemon.sock"
    )
    app._launch_id = "this-launch"
    _StartupMixin._write_pid_file(app)
    marker = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
    assert marker["launchId"] == "this-launch"

    app._launch_id = None
    _StartupMixin._write_pid_file(app)
    marker = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
    assert "launchId" not in marker


def test_daemon_run_pops_the_nonce_so_descendants_cannot_inherit_it() -> None:
    environ = {DAEMON_LAUNCH_ID_ENV: "abc", "PATH": "/x"}
    assert take_launch_id(environ) == "abc"
    assert DAEMON_LAUNCH_ID_ENV not in environ
    assert take_launch_id(environ) is None
    assert take_launch_id({DAEMON_LAUNCH_ID_ENV: ""}) is None


def test_launcher_sets_a_fresh_nonce_on_every_platform_and_waits_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (CLI composition)
    from hyprial.daemon.impl.mcp import channel as mcp_channel
    from hyprial.kernel import ipc_errors
    from hyprial.shell.impl.cli.commands.common import daemon_start
    from hyprial.shell.impl.cli.commands.common.services import get_services

    services = get_services()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(DAEMON_LAUNCH_ID_ENV, "inherited-from-an-ancestor")
    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: None)

    def unavailable(**_kwargs: Any) -> Any:
        raise ipc_errors.DaemonUnavailableError("down")

    spawned: list[dict[str, Any]] = []

    class Process:
        pid = 4242

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            spawned.append(kwargs)

        def poll(self) -> None:
            return None

    waited: list[dict[str, Any]] = []

    def wait(**kwargs: Any) -> dict[str, Any]:
        waited.append(kwargs)
        return {"running": True, "pid": 9}

    monkeypatch.setattr(services, "_daemon_probe", unavailable)
    monkeypatch.setattr(daemon_start.subprocess, "Popen", Process)
    monkeypatch.setattr(services, "_wait_for_daemon", wait)

    services._launch_daemon_process_locked(ready_timeout=1.0, listen=None, connect=None)

    launch_id = spawned[0]["env"][DAEMON_LAUNCH_ID_ENV]
    assert launch_id and launch_id != "inherited-from-an-ancestor"
    assert waited[0]["launch_id"] == launch_id
