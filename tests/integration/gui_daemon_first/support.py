"""Real ``hyprial gui`` hosts beside a real bootstrap daemon.

Decision: daemon-first-z6a-child-support-20261117, moved to the one-process host
by 2822 B4.  The daemon is the controlled identity entry of ``daemon_lifecycle``
(a login commits an owner without a network issuer); the host is the unmodified
``hyprial gui --no-open --json --port 0 --stdin-stop`` command on the same
isolated home, which serves the request-http dispatch in its own process.  A test
reaches it as a browser does: the launch grant opens a session, and every call
carries that session's cookies, CSRF and view proof.  The old child's ``server.json``
bearer and its rotation are gone; the same facts are read from the session
(``current`` turns false after a principal change, a refresh keeps the view and
issues a new generation, and the headers of the retired generation are refused).
Every process is tracked by pid and birth identity and stopped in a context manager
or the ``LifecycleHome`` fixture.  Waits are bounded; none is retried around an
assertion.
Decision: portable-process-cleanup-20261119.  The child listing, the zombie check
and "is it gone" no longer read ``/proc``: ``HostGui`` and ``GuiCommand`` both keep
their pids in a ``process_table.Ledger`` (``ps`` listing, ``process_birth_identity``,
birth re-checked before every signal), each launcher starts in its own session and
registers its ``release`` with the ``LifecycleHome`` so the fixture runs it after a
failed assertion, and ``release`` asserts that nothing it recorded survives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from tests.integration.daemon_lifecycle.support import LifecycleHome
from tests.support.process_table import Ledger, alive as alive, children_of

FOLLOW_SECONDS = 60
OWNER = "z2-owner@example.test"
ISSUER = "https://issuer.example.test"


HOST_SHIM = r'''
import sys
from pathlib import Path
from hyprial.shell.impl.gui.install import launch, resources

assets = Path(sys.argv.pop(1))
launch.installed_asset_root = lambda: assets
resources.installed_asset_root = lambda: assets
from hyprial.cli import main

sys.exit(main())
'''


class HostGui:
    """One real ``hyprial gui --no-open --json --port 0 --stdin-stop`` and its one host.

    Used as a context manager: leaving it stops the launcher through its stdin
    and then kills only the processes whose pid and birth identity were
    recorded from the startup record, so nothing found by name is ever signalled.
    """

    def __init__(self, lifecycle: LifecycleHome) -> None:
        self.lifecycle = lifecycle
        self.ledger = Ledger(lifecycle.recorded_pids)
        self.stderr_path = lifecycle.root / "gui.stderr"
        with self.stderr_path.open("wb") as stderr:
            self.process = subprocess.Popen(
                [sys.executable, "-c", HOST_SHIM, str(wheel_assets(lifecycle.root)),
                 "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"],
                cwd=lifecycle.root, env=lifecycle.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, text=True, start_new_session=True,
            )
        self.ledger.record(self.process.pid)
        lifecycle.releasers.append(self.release)
        self.record: dict[str, Any] = {}

    def __enter__(self) -> "HostGui":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def logs(self) -> str:
        text = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        handoff = self.record.get("handoff_path")
        if isinstance(handoff, str) and handoff:
            host_log = Path(handoff).parent / "host.log"
            if host_log.exists():
                text += host_log.read_text(encoding="utf-8", errors="replace")
        return text

    def read_record(self) -> dict[str, Any]:
        import queue
        import threading

        assert self.process.stdout is not None
        first: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: first.put(self.process.stdout.readline()), daemon=True).start()
        try:
            line = first.get(timeout=STARTUP_SECONDS)
        except queue.Empty:
            line = ""
        assert line, self.stderr_path.read_text(encoding="utf-8", errors="replace")[-6000:]
        self.record = json.loads(line)
        assert self.record.get("ok") is True, (self.record, self.logs()[-6000:])
        self.ledger.record(int(self.record["frontend_pid"]))
        self.ledger.adopt_descendants(self.process.pid)
        return self.record

    @property
    def host_pid(self) -> int:
        return int(self.record["frontend_pid"])

    def children(self) -> list[int]:
        return children_of(self.process.pid)

    def stop(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        return self.process.wait(timeout=60)

    def release(self) -> None:
        import signal

        if self.process.poll() is None:
            self.ledger.adopt_descendants(self.process.pid)
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=60)
        survivors = self.ledger.stop(kill=20, exclude={self.process.pid})
        for stream in (self.process.stdout, self.process.stdin):
            if stream is not None and not stream.closed:
                stream.close()
        assert survivors == [], f"processes of the GUI launcher outlived its release: {self.ledger.describe(survivors)}"

    def open_session(self) -> "HostSession":
        launch = Path(self.record["handoff_path"]).read_text(encoding="utf-8").strip()
        origin, _, fragment = launch.partition("#grant=")
        session = HostSession(origin.rstrip("/"))
        session.grant = urllib.parse.unquote(fragment)
        session.open(session.grant)
        return session


class HostSession:
    """One browser view of the host: launch grant, cookies, CSRF and view proof."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.opened: dict[str, Any] = {}
        self.cookies = ""
        self.grant = ""

    def _send(self, method: str, path: str, body: dict | None, headers: dict[str, str]) -> tuple[int, Any, Any]:
        request = urllib.request.Request(
            self.origin + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", "Origin": self.origin, **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"null"), error.headers

    def open(self, grant: str) -> None:
        status, payload, headers = self._send("POST", "/daemon/session", {"grant": grant}, {})
        assert status == 200, payload
        self.opened = payload
        self.cookies = "; ".join(v.split(";", 1)[0] for v in headers.get_all("Set-Cookie") or [])

    def headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookies,
            "X-Hyprial-View": str(self.opened["view"]),
            "X-Hyprial-Generation": str(self.opened["generation"]),
            "X-Hyprial-Csrf": str(self.opened["csrf"]),
            "X-Hyprial-Catalog-Digest": str(self.opened["catalog"]["digest"]),
        }

    def call(self, method: str, body: dict | None = None, *, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        status, payload, _ = self._send("POST", "/daemon/request/" + method, body or {}, headers or self.headers())
        return status, payload

    def request(self, method: str, body: dict | None = None) -> Any:
        status, payload = self.call(method, body)
        assert status == 200 and payload["ok"] is True, (status, payload)
        return payload["result"]

    def ready(self, *, headers: dict[str, str] | None = None) -> int:
        status, _, _ = self._send("GET", "/daemon/readyz", None, headers or self.headers())
        return status

    def ready_epoch(self) -> str:
        status, payload, _ = self._send("GET", "/daemon/readyz", None, self.headers())
        assert status == 200, payload
        return str(payload["backend"]["epoch"])

    def current(self) -> bool:
        status, payload, _ = self._send("POST", "/daemon/session/status", {}, self.headers())
        assert status == 200, payload
        return bool(payload["current"])

    def wait_changed(self, bound: float = FOLLOW_SECONDS) -> None:
        deadline = time.monotonic() + bound
        while self.current():
            assert time.monotonic() < deadline, "the host did not follow the daemon's change"
            time.sleep(0.1)

    def refresh(self) -> dict[str, Any]:
        status, payload, headers = self._send("POST", "/daemon/session", {}, self.headers())
        assert status == 200, payload
        self.opened = payload
        fresh = "; ".join(v.split(";", 1)[0] for v in headers.get_all("Set-Cookie") or [])
        self.cookies = fresh or self.cookies
        return payload


def reach_serving(lifecycle: LifecycleHome, daemon: subprocess.Popen, session: HostSession, tag: str) -> None:
    begun = session.request("auth.login.begin", {"intent": "login", "operationKey": f"z6a-login-key-{tag}"})
    got = session.request("auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated", got
    receipt = session.request("onboarding.initialize", {"operationKey": f"z6a-initialize-{tag}"})
    assert receipt["state"] == "promoting", receipt
    lifecycle.serving(daemon)
    session.wait_changed()
    session.refresh()


def assert_unavailable(status: int, payload: Any) -> None:
    assert status == 503, (status, payload)
    assert payload["code"] == "DAEMON_UNAVAILABLE" and payload["dispatch"] == "not-dispatched", payload


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
STARTUP_SECONDS = 120.0


def wheel_assets(root: Path) -> Path:
    from hyprial.shell.impl.gui.install import resources

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


def start_gui_command(lifecycle: LifecycleHome, assets: Path, stderr: Any) -> GuiCommand:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    process = subprocess.Popen(
        [sys.executable, "-c", GUI_CHILD, str(assets), str(port)],
        cwd=lifecycle.root, env=lifecycle.env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=stderr, text=True, start_new_session=True,
    )
    command = GuiCommand(process, lifecycle.recorded_pids)
    lifecycle.releasers.append(command.release)
    return command


def read_record(command: GuiCommand, stderr_path: Path) -> dict:
    import select

    child = command.process
    assert child.stdout is not None
    deadline = time.monotonic() + STARTUP_SECONDS
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


class GuiCommand:
    """One ``hyprial gui`` launcher and every process it started, identified by pid and birth.

    The static host runs in its own session, so killing the launcher's process
    group does not reach it.  ``snapshot`` records the launcher's descendants
    while the launcher is alive; ``release`` stops exactly those (pid plus birth
    identity), never anything found by name, and never the daemon whose pids
    ``spare`` names (the ``LifecycleHome`` owns that one).
    """

    def __init__(self, process: subprocess.Popen, spare: Any) -> None:
        self.process = process
        self.ledger = Ledger(spare)
        self.ledger.record(process.pid)
        self.released = False

    def snapshot(self) -> None:
        if self.process.returncode is None and self.ledger.same(self.process.pid):
            self.ledger.adopt_descendants(self.process.pid)

    def release(self) -> None:
        import signal

        if self.released:
            return
        self.released = True
        self.snapshot()
        process = self.process
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        survivors = self.ledger.stop(kill=20, exclude={process.pid})
        assert survivors == [], f"processes of the GUI command outlived its release: {self.ledger.describe(survivors)}"

    def survivors(self) -> list[int]:
        return self.ledger.live(exclude={self.process.pid})


def end_gui_command(command: GuiCommand) -> int:
    import signal

    command.snapshot()
    child = command.process
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
    return child.wait(timeout=60)
