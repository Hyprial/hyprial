"""DL-1 against real processes: ``hyprial gui`` starts an owned home's daemon.

An isolated home with an owner and no daemon, an installed-shaped asset tree
(the wheel's ``_gui`` replaced by a small verified directory) and the real
``hyprial gui --no-open --json`` command.  The GUI process starts the daemon
through the one launch primitive, a second invocation reuses it, and ending a
GUI never stops it; the test shuts the daemon down itself.

Decision (2026-11-26): the teardown signals the GUI's process group where
``os.killpg`` exists and the GUI process itself otherwise, waits for the daemon
through ``pid_alive`` and hard-stops it with ``SIGKILL`` where it exists
(``TerminateProcess`` through ``os.kill`` on Windows).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import pytest

from hyprial.kernel import daemon_request_once
from hyprial.shell.impl.gui.install import resources
from tests.support.windows_suite.host import pid_alive

KILL = getattr(signal, "SIGKILL", signal.SIGTERM)

GUI_CHILD = r'''
import sys
from pathlib import Path
from hyprial.shell.impl.gui.install import launch as target_launch

root = Path(sys.argv[1])
target_launch.installed_asset_root = lambda: root
from hyprial.cli import main

sys.argv = ["hyprial", "gui", "--no-open", "--json", "--port", sys.argv[2]]
main()
'''

STARTUP_BUDGET_SECONDS = 120.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wheel_assets(root: Path) -> Path:
    directory = root / "wheel-gui"
    directory.mkdir()
    (directory / "index.html").write_text("<!doctype html><title>Hyprial</title>\n", encoding="utf-8")
    resources.write_manifest(
        directory,
        resources.build_manifest(
            directory,
            package_version="0.0.0",
            source_commit="d" * 40,
            source_remote="https://example.invalid/hyprial",
            gui_version="0.0.0",
        ),
    )
    return directory.resolve()


def _start_gui(env: dict[str, str], assets: Path, cwd: Path, stderr: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", GUI_CHILD, str(assets), str(_free_port())],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )


def _read_record(child: subprocess.Popen[str], stderr_path: Path) -> dict[str, Any]:
    assert child.stdout is not None
    deadline = time.monotonic() + STARTUP_BUDGET_SECONDS
    buffer = ""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([child.stdout], [], [], 0.5)
        if ready:
            line = child.stdout.readline()
            if line == "":
                break
            buffer += line
            try:
                record = json.loads(buffer)
            except ValueError:
                continue
            assert isinstance(record, dict)
            return record
        if child.poll() is not None:
            break
    raise AssertionError(
        f"no startup record (exit {child.poll()}): {buffer!r}\n"
        f"{stderr_path.read_text(encoding='utf-8', errors='replace')[-6000:]}"
    )


def _assert_started(record: dict[str, Any], stderr_path: Path, started: float) -> None:
    """Fail with the record, the elapsed seconds and the GUI process's stderr tail."""
    assert record.get("ok") is True, (
        f"GUI start failed after {time.monotonic() - started:.1f}s: {json.dumps(record, indent=2, sort_keys=True)}\n"
        f"gui stderr tail:\n{stderr_path.read_text(encoding='utf-8', errors='replace')[-6000:]}"
    )


def _signal_group(process: subprocess.Popen[str], *, hard: bool) -> None:
    """Teardown signal to the process group a child leads; a Windows child, which leads no group, is terminated itself."""

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        if hard:
            process.kill()
        else:
            process.terminate()
        return
    killpg(process.pid, KILL if hard else signal.SIGTERM)


def _end_gui(child: subprocess.Popen[str]) -> int:
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
    try:
        return child.wait(timeout=60)
    except subprocess.TimeoutExpired:
        _signal_group(child, hard=True)
        child.wait(timeout=10)
        raise


def _release(child: subprocess.Popen[str]) -> None:
    """Kill what is left of one GUI process group, reap it and close its pipe."""
    if child.poll() is None:
        try:
            _signal_group(child, hard=True)
        except ProcessLookupError:
            pass
    child.wait(timeout=10)
    if child.stdout is not None:
        child.stdout.close()


def _ping(socket_path: Path) -> dict[str, Any]:
    ping = daemon_request_once("ping", {}, socket_path=socket_path, timeout=5)
    assert isinstance(ping, dict)
    return ping


def _stop_daemon(socket_path: Path) -> None:
    try:
        ping = _ping(socket_path)
    except Exception:
        return
    pid = ping.get("pid")
    try:
        daemon_request_once(
            "shutdown",
            {"expectedEpoch": ping["epoch"]},
            socket_path=socket_path,
            timeout=5,
        )
    except Exception:
        pass
    deadline = time.monotonic() + 30
    while isinstance(pid, int) and time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    if isinstance(pid, int):
        try:
            os.kill(pid, KILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("driver", [None, "daemon"], ids=["driver-unset", "driver-daemon"])
def test_gui_starts_the_owned_daemon_and_exit_leaves_it_running(
    isolation: Any, driver: str | None
) -> None:
    env = dict(isolation.env)
    env.pop("HYPRIAL_OWNER", None)
    env.pop("HYPRIAL_GUI_BACKEND_DRIVER", None)
    if driver is not None:
        env["HYPRIAL_GUI_BACKEND_DRIVER"] = driver
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4] / "src")
    assets = _wheel_assets(isolation.root)
    stderr_path = isolation.root / "gui.stderr"
    socket_path: Path = isolation.socket_path
    children: list[subprocess.Popen[str]] = []
    try:
        with pytest.raises(Exception):
            _ping(socket_path)

        with stderr_path.open("wb") as stderr:
            first = _start_gui(env, assets, isolation.root, stderr)
        children.append(first)
        first_started = time.monotonic()
        record = _read_record(first, stderr_path)
        _assert_started(record, stderr_path, first_started)
        first_ping = _ping(socket_path)
        assert first_ping["running"] is True
        assert first_ping["owner"] == "h01-owner"
        first_pid, first_epoch = first_ping["pid"], first_ping["epoch"]
        assert _end_gui(first) in {0, 143}
        survived = _ping(socket_path)
        assert (survived["pid"], survived["epoch"]) == (first_pid, first_epoch)

        with stderr_path.open("ab") as stderr:
            second = _start_gui(env, assets, isolation.root, stderr)
        children.append(second)
        second_started = time.monotonic()
        again = _read_record(second, stderr_path)
        _assert_started(again, stderr_path, second_started)
        reused = _ping(socket_path)
        assert (reused["pid"], reused["epoch"]) == (first_pid, first_epoch)
        assert _end_gui(second) in {0, 143}
        final = _ping(socket_path)
        assert (final["pid"], final["epoch"]) == (first_pid, first_epoch)
    finally:
        for child in children:
            _release(child)
        _stop_daemon(socket_path)
