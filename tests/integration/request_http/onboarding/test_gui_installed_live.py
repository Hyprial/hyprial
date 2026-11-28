"""The installed-wheel launch against a real daemon: admission and the one host child.

An isolated home, socket and ports under one temporary directory; nothing here
reads or touches a resident daemon, a user home or a credential. The launcher
runs through `build_plan` and `start_session` exactly as `hyprial gui` does,
with the wheel's built-in asset directory replaced by a small verified tree.
Decision: portable-process-cleanup-20261119.  Liveness is ``process_table.alive`` (``kill 0``
plus ``ps`` state), not a ``/proc`` read, so a zombie counts as gone on macOS too.
Decision (2026-11-26): a frontend left after the run is killed through its
process group where ``os.killpg`` exists and by pid otherwise (on Windows
``os.kill`` with 9 is ``TerminateProcess``), so the teardown is not POSIX-only.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import daemon_request_once
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.install import resources
from tests.support.process_table import alive as _alive
from tests.support.runtime.environment import isolated_environment

CONTRACT = Path(__file__).resolve().parents[4] / "contract" / "cli" / "gui-startup-record.v1.json"

OWNER = "installed-live-owner"
NODE = "installed-live-node"


def _assets(root: Path) -> Path:
    directory = root / "wheel-gui"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>Hyprial</title>\n", encoding="utf-8")
    (directory / "assets" / "app.js").write_text("console.log('installed');\n", encoding="utf-8")
    manifest = resources.build_manifest(
        directory,
        package_version="0.0.0",
        source_commit="c" * 40,
        source_remote="https://example.invalid/hyprial",
        gui_version="0.0.0",
    )
    resources.write_manifest(directory, manifest)
    return directory


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_socket(daemon: subprocess.Popen[bytes], socket_path: Path, log: Path) -> None:
    """Wait until the daemon serves, not merely until its socket file exists.

    The socket appears while restore is still running; admission refuses a
    restoring daemon, so a socket-only wait races it under load.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert daemon.poll() is None, log.read_text()[-6000:]
        if socket_path.exists():
            try:
                ping = daemon_request_once("ping", {}, socket_path=socket_path, timeout=2)
            except Exception:
                ping = None
            if (
                isinstance(ping, dict)
                and ping.get("phase") in {"serving", "reconciled"}
                and ping.get("restorePending") is not True
            ):
                return
        time.sleep(0.1)
    raise AssertionError(f"daemon never served on {socket_path}:\n{log.read_text()[-6000:]}")


def _post(origin: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any, Any]:
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin, **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response), response.headers
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"null"), error.headers


def test_installed_launch_serves_the_real_daemon_through_the_python_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gui-installed-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        env = isolated_environment(root)
        env.pop("HYPRIAL_OWNER", None)
        home = Path(env["HYPRIAL_HOME"])
        home.mkdir(mode=0o700, parents=True)
        (home / "settings.json").write_text(json.dumps({"version": 1, "owner": OWNER}))
        env["HYPRIAL_NODE_ID"] = NODE
        state = Path(env["HARNESS_STATE_DIR"])
        log = root / "daemon.log"
        with log.open("wb") as sink:
            daemon = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
            )
        session = None
        try:
            _wait_for_socket(daemon, state / "daemon.sock", log)
            monkeypatch.setattr(
                target_launch, "installed_asset_root", lambda: _assets(root).resolve()
            )
            port = _free_port()
            plan = launcher.build_plan("", port=port, no_open=True, environ=env, cwd=root)
            assert plan.driver == "daemon"
            assert plan.admission is not None and plan.admission.principal == f"user:{OWNER}"
            session = launcher.start_session(plan, json_output=True)
            assert session.handoff_path is not None
            launch = session.handoff_path.read_text(encoding="utf-8").strip()
            origin, _, fragment = launch.partition("#grant=")
            origin = origin.rstrip("/")
            grant = urllib.parse.unquote(fragment)
            assert origin == f"http://127.0.0.1:{port}"

            with urllib.request.urlopen(origin + "/__gui/healthz", timeout=10) as response:
                health = json.load(response)
            assert health["dataMode"] == "hosted"
            assert health["assetsVerified"] is True
            with urllib.request.urlopen(origin + "/", timeout=10) as response:
                assert b"<title>Hyprial</title>" in response.read()

            status, opened, headers = _post(origin, "/daemon/session", {"grant": grant}, {})
            assert status == 200, opened
            cookies = "; ".join(value.split(";", 1)[0] for value in headers.get_all("Set-Cookie") or [])
            assert "hyprial_gui_session=" in cookies and "hyprial_gui_control=" in cookies
            session_headers = {
                "Cookie": cookies,
                "X-Hyprial-View": str(opened["view"]),
                "X-Hyprial-Generation": str(opened["generation"]),
                "X-Hyprial-Csrf": str(opened["csrf"]),
                "X-Hyprial-Catalog-Digest": str(opened["catalog"]["digest"]),
            }
            status, result, _ = _post(origin, "/daemon/request/agent.list", {}, session_headers)
            assert status == 200, result
            assert result["ok"] is True
            assert result["method"] == "agent.list"
            assert not (root / "state" / "gui" / "daemon" / "launch.url").exists()

            status, replay, _ = _post(origin, "/daemon/session", {"grant": grant}, {})
            assert status == 409 and replay["code"] == "LAUNCH_GRANT_USED"
        finally:
            if session is not None:
                session.stop()
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=10)
        assert os.path.exists(root)


_CLI_SHIM = """
import sys
from pathlib import Path

from hyprial.shell.impl.gui.install import launch, resources

assets = Path(sys.argv.pop(1))
launch.installed_asset_root = lambda: assets
resources.installed_asset_root = lambda: assets
from hyprial.cli import main

sys.exit(main())
"""


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_cli_port_zero_and_stdin_stop_leave_the_daemon_running() -> None:
    import jsonschema

    with tempfile.TemporaryDirectory(prefix="gui-port0-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        env = isolated_environment(root)
        env.pop("HYPRIAL_OWNER", None)
        home = Path(env["HYPRIAL_HOME"])
        home.mkdir(mode=0o700, parents=True)
        (home / "settings.json").write_text(json.dumps({"version": 1, "owner": OWNER}))
        env["HYPRIAL_NODE_ID"] = NODE
        state = Path(env["HARNESS_STATE_DIR"])
        daemon_log = root / "daemon.log"
        with daemon_log.open("wb") as sink:
            daemon = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
            )
        cli: subprocess.Popen[str] | None = None
        pids: list[int] = []
        try:
            _wait_for_socket(daemon, state / "daemon.sock", daemon_log)
            cli_log = root / "cli.log"
            with cli_log.open("wb") as err:
                cli = subprocess.Popen(
                    [
                        sys.executable, "-c", _CLI_SHIM, str(_assets(root).resolve()),
                        "gui", "--no-open", "--json", "--port", "0", "--stdin-stop",
                    ],
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=err,
                    text=True,
                )
            first: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: first.put(cli.stdout.readline()), daemon=True).start()
            try:
                line = first.get(timeout=90)
            except queue.Empty:
                line = ""
            assert line, cli_log.read_text()[-6000:]
            record = json.loads(line)
            corpus = json.loads(CONTRACT.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(corpus["startupRecord"]).validate(record)
            assert record["backend_pid"] is None and record["backend_endpoint"] == ""
            pids = [int(record["frontend_pid"])]
            port = int(record["port"])
            assert port not in (0, 4173)
            assert record["url"] == f"http://127.0.0.1:{port}/"
            assert _listening(port)

            handoff = Path(record["handoff_path"])
            launch = handoff.read_text(encoding="utf-8").strip()
            origin, _, fragment = launch.partition("#grant=")
            assert origin.rstrip("/") == f"http://127.0.0.1:{port}"
            grant = urllib.parse.unquote(fragment)
            assert grant and grant not in line
            status, opened, _ = _post(origin.rstrip("/"), "/daemon/session", {"grant": grant}, {})
            assert status == 200, opened
            assert not handoff.exists()

            assert cli.stdin is not None
            cli.stdin.close()
            assert cli.wait(timeout=60) == 0, cli_log.read_text()[-6000:]
            assert cli.stdout.read() == ""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
                time.sleep(0.1)
            assert not any(_alive(pid) for pid in pids)
            assert not _listening(port)
            assert daemon.poll() is None
            ping = daemon_request_once("ping", {}, socket_path=state / "daemon.sock", timeout=5)
            assert ping["phase"] in {"serving", "reconciled"}
        finally:
            if cli is not None and cli.poll() is None:
                cli.kill()
                cli.wait(timeout=10)
            kill = getattr(os, "killpg", os.kill)
            for pid in pids:
                if _alive(pid):
                    try:
                        kill(pid, 9)
                    except (ProcessLookupError, PermissionError):
                        pass
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=10)
