"""``hyprial gui`` with the daemon driver is one Python child (2822, B3).

Decision: host-serves-daemon-in-process-20261119.  The real command
``hyprial gui --no-open --json --port 0 --stdin-stop`` runs on an isolated home
beside the controlled-entry daemon of ``daemon_lifecycle`` (a login commits an
owner without a network issuer; no ``~/.hyprial``, no real login).  The host
serves the request-http dispatch in its own process, so the launcher has exactly
one child, the record's ``backend_pid`` is null and its ``backend_endpoint``
empty, and the runtime directory never holds ``server.json``,
``frontend-authority.json`` or a request-http config.  The process picture comes
from ``ps`` and ``process_birth_identity`` (portable beyond Linux); a child is
signalled only after its birth identity matched what the record showed.  The
daemon is never stopped by a GUI: its pid and epoch are compared after the stop.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from hyprial.kernel import process_birth_identity
import pytest

from tests.integration.daemon_lifecycle.conftest import pytestmark  # noqa: F401
from tests.integration.daemon_lifecycle.support import LifecycleHome
from tests.integration.gui_daemon_first.support import OWNER, wheel_assets

START_BOUND_SECONDS = 120
STOP_BOUND_SECONDS = 60
REBIND_BOUND_SECONDS = 60
FORBIDDEN = {"server.json", "frontend-authority.json", "request-http.json", "request-http.log"}

SHIM = r'''
import sys
from pathlib import Path
from hyprial.shell.impl.gui.install import launch, resources

assets = Path(sys.argv.pop(1))
launch.installed_asset_root = lambda: assets
resources.installed_asset_root = lambda: assets
from hyprial.cli import main

sys.exit(main())
'''


@pytest.fixture
def lifecycle():
    home = LifecycleHome()
    try:
        yield home
    finally:
        home.cleanup()


def processes() -> dict[int, tuple[int, str]]:
    listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    table: dict[int, tuple[int, str]] = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = (int(fields[1]), fields[2] if len(fields) > 2 else "")
    return table


def children_of(pid: int) -> list[int]:
    return sorted(child for child, (parent, _) in processes().items() if parent == pid)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return pid in processes()


class Gui:
    """One launcher process and the identity of what it reported."""

    def __init__(self, lifecycle: Any, assets: Path) -> None:
        self.stderr_path = lifecycle.root / "gui.stderr"
        with self.stderr_path.open("wb") as stderr:
            self.process = subprocess.Popen(
                [sys.executable, "-c", SHIM, str(assets), "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"],
                cwd=lifecycle.root,
                env=lifecycle.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
        self.record: dict[str, Any] = {}
        self.known: dict[int, str | None] = {self.process.pid: process_birth_identity(self.process.pid)}

    def read_record(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        first: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: first.put(self.process.stdout.readline()), daemon=True).start()
        try:
            line = first.get(timeout=START_BOUND_SECONDS)
        except queue.Empty:
            line = ""
        assert line, self.stderr_path.read_text(encoding="utf-8", errors="replace")[-6000:]
        self.record = json.loads(line)
        assert self.record.get("ok") is True, (self.record, self.stderr_path.read_text(encoding="utf-8")[-6000:])
        self.known[int(self.record["frontend_pid"])] = process_birth_identity(int(self.record["frontend_pid"]))
        return self.record

    def stop(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        return self.process.wait(timeout=STOP_BOUND_SECONDS)

    def release(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=STOP_BOUND_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=STOP_BOUND_SECONDS)
        for pid, birth in self.known.items():
            if pid != self.process.pid and birth is not None and alive(pid) and process_birth_identity(pid) == birth:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for stream in (self.process.stdout, self.process.stdin):
            if stream is not None and not stream.closed:
                stream.close()


def origin_and_grant(record: dict[str, Any]) -> tuple[str, str]:
    launch = Path(record["handoff_path"]).read_text(encoding="utf-8").strip()
    origin, _, fragment = launch.partition("#grant=")
    return origin.rstrip("/"), urllib.parse.unquote(fragment)


def post(origin: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any, Any]:
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin, **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response), response.headers
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"null"), error.headers


def session_headers(opened: dict[str, Any], cookies: str) -> dict[str, str]:
    return {
        "Cookie": cookies,
        "X-Hyprial-View": str(opened["view"]),
        "X-Hyprial-Generation": str(opened["generation"]),
        "X-Hyprial-Csrf": str(opened["csrf"]),
        "X-Hyprial-Catalog-Digest": str(opened["catalog"]["digest"]),
    }


def cookies_of(headers: Any) -> str:
    return "; ".join(value.split(";", 1)[0] for value in headers.get_all("Set-Cookie") or [])


def serving_daemon(lifecycle: Any) -> tuple[subprocess.Popen, dict[str, Any]]:
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    lifecycle.login_and_initialize("single-process")
    return daemon, lifecycle.serving(daemon)


def test_gui_is_one_python_child_and_the_daemon_is_untouched(lifecycle) -> None:
    daemon, ping = serving_daemon(lifecycle)
    assert ping["owner"] == OWNER
    gui = Gui(lifecycle, wheel_assets(lifecycle.root))
    try:
        record = gui.read_record()
        assert record["backend_pid"] is None and record["backend_endpoint"] == ""
        assert record["backend_driver"] == "daemon" and record["record_version"] == 1
        frontend = int(record["frontend_pid"])
        assert alive(frontend) and frontend != daemon.pid
        launcher = gui.process.pid
        assert children_of(launcher) == [frontend]
        assert "hyprial.shell.impl.gui.host" in processes()[frontend][1]
        assert children_of(frontend) == []
        runtime = Path(record["handoff_path"]).parent
        assert not {path.name for path in runtime.iterdir()} & FORBIDDEN, sorted(path.name for path in runtime.iterdir())

        origin, grant = origin_and_grant(record)
        status, opened, headers = post(origin, "/daemon/session", {"grant": grant}, {})
        assert status == 200, opened
        assert opened["principal"]["owner"] == OWNER
        status, relayed, _ = post(
            origin, "/daemon/request/onboarding.status", {}, session_headers(opened, cookies_of(headers))
        )
        assert status == 200 and relayed["ok"] is True and relayed["method"] == "onboarding.status", relayed
        assert children_of(launcher) == [frontend]

        assert gui.stop() == 0, gui.stderr_path.read_text(encoding="utf-8")[-6000:]
        assert not alive(frontend)
        after = lifecycle.ping()
        assert after is not None and (after["pid"], after["epoch"]) == (ping["pid"], ping["epoch"])
        assert alive(daemon.pid) and daemon.poll() is None
    finally:
        gui.release()


def test_a_phase_change_is_followed_in_place_by_the_one_host(lifecycle) -> None:
    daemon = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, daemon)
    gui = Gui(lifecycle, wheel_assets(lifecycle.root))
    try:
        record = gui.read_record()
        assert record["backend_pid"] is None and record["backend_principal"] is None
        frontend, launcher = int(record["frontend_pid"]), gui.process.pid
        origin, grant = origin_and_grant(record)
        status, opened, headers = post(origin, "/daemon/session", {"grant": grant}, {})
        assert status == 200 and opened["principal"]["kind"] == "anonymous-bootstrap", opened
        cookies = cookies_of(headers)
        lifecycle.login_and_initialize("phase-change")
        lifecycle.serving(daemon)
        deadline = time.monotonic() + REBIND_BOUND_SECONDS
        while True:
            status, current, _ = post(origin, "/daemon/session/status", {}, session_headers(opened, cookies))
            assert status == 200, current
            if current["current"] is False:
                break
            assert time.monotonic() < deadline, "the host did not follow the daemon's phase change"
            time.sleep(0.1)
        status, refreshed, headers = post(origin, "/daemon/session", {}, session_headers(opened, cookies))
        assert status == 200, refreshed
        assert refreshed["view"] == opened["view"] and refreshed["generation"] != opened["generation"]
        assert refreshed["principal"]["kind"] == "serving" and refreshed["principal"]["owner"] == OWNER
        assert children_of(launcher) == [frontend] and alive(frontend)
        assert gui.process.poll() is None
        assert gui.stop() == 0
    finally:
        gui.release()
