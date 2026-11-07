"""PR-B: each managed dsh worker spawns, owns, and reports its own DSH.

The stub is a real executable named ``dsh`` that binds a real loopback port,
prints the real banner DSH prints, and serves a minimal ``/api`` envelope -- the
same shape the adapter parses in production.  Nothing here invents a private
test interface: the process spawns ``shutil.which("dsh")``, reads its stdout,
and the assertions read the same attributes the daemon's status projection uses.
"""

from __future__ import annotations

import os
import signal
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
)
from hyprial.daemon.impl import ManagedHarnessRuntime
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.dsh import (
    DshHarnessProcess,
)
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher

_FAKE_DSH = r'''#!/usr/bin/env python3
"""Minimal fake dsh: banner + /api envelope, for adapter unit tests."""

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("FAKE_DSH_MODE", "normal")
if "--version" in sys.argv:
    print(os.environ.get("FAKE_DSH_VERSION", "0.1.0-rc.7"), flush=True)
    raise SystemExit(0)
BANNER_CAPTURE = os.environ.get("FAKE_DSH_BANNER_CAPTURE")
REQUESTS_CAPTURE = os.environ.get("FAKE_DSH_REQUESTS_CAPTURE")
KEY_CAPTURE = os.environ.get("FAKE_DSH_KEY_CAPTURE")
PID_CAPTURE = os.environ.get("FAKE_DSH_PID_CAPTURE")
PID_LOG = os.environ.get("FAKE_DSH_PID_LOG")
PATH_CAPTURE = os.environ.get("FAKE_DSH_PATH_CAPTURE")
ENV_CAPTURE = os.environ.get("FAKE_DSH_ENV_CAPTURE")

if MODE == "leak-key":
    requested = os.environ.get("FAKE_DSH_LEAK")
    if requested:
        names = [name for name in requested.split(",") if name in os.environ]
    else:
        names = [
            name
            for name in sorted(os.environ)
            if name == "DEEPSEEK_API_KEY" or name.endswith(("_KEY", "_AUTH"))
        ]
    for name in names:
        sys.stderr.write(f"{name}={os.environ[name]}\n")
    sys.stderr.flush()
    raise SystemExit(5)

if MODE == "no-banner":
    sys.stderr.write("fake dsh boom\n")
    sys.stderr.flush()
    raise SystemExit(7)

if MODE == "ignore-term":
    signal.signal(signal.SIGTERM, lambda *_args: None)

VALUES = {
    "host.describe": {
        "version": "fake",
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
    },
    "session.create": {"sessionId": "fake-session"},
    "session.history": {"events": []},
    "session.prompt": {"accepted": True},
    "session.cancel": {"accepted": True},
    "session.list": {"items": []},
    "session.models": {
        "current": {"provider": "deepseek-official", "model": "deepseek-v4-flash"}
    },
}


def _append(path, text):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        method = body.get("method")
        _append(
            REQUESTS_CAPTURE,
            json.dumps({"method": method, "host": self.headers.get("Host")}),
        )
        if MODE == "describe-404" and method == "host.describe":
            self.send_error(404, "no host.describe")
            return
        if MODE == "describe-500" and method == "host.describe":
            body = (
                "server error: key=" + str(os.environ.get("DEEPSEEK_API_KEY"))
            ).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if MODE == "hang-turn" and method == "session.prompt":
            marker = os.environ.get("FAKE_DSH_PROMPT_MARKER")
            if marker:
                with open(marker, "a", encoding="utf-8") as handle:
                    handle.write("prompt\n")
        if method == "agentPreset.copy":
            preset = body.get("payload", {}).get("agentPreset")
            if isinstance(preset, str):
                path = os.path.join(
                    os.environ["DSH_HOME"],
                    ".agent-presets",
                    preset,
                    "agent.cordis.yml",
                )
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("- id: persona\n  name: '@deepseek-ai/dsh-persona'\n")
        payload = {
            "rpcId": body.get("rpcId"),
            "result": {"ok": True, "value": VALUES.get(method, {})},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        if MODE == "exit-after-prompt" and method == "session.prompt":
            os._exit(9)


def main():
    port = 0
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == "--port" and index + 1 < len(args):
            port = int(args[index + 1])
        elif value.startswith("--port="):
            port = int(value.split("=", 1)[1])
    key = os.environ.get("DEEPSEEK_API_KEY")
    if ENV_CAPTURE:
        _append(
            ENV_CAPTURE,
            json.dumps(
                {
                    "DEEPSEEK_API_KEY": key,
                    "ambient": os.environ.get("P1BB2_AMBIENT_LEAK"),
                    "DSH_HOME": os.environ.get("DSH_HOME"),
                    "HOME": os.environ.get("HOME"),
                    "PATH": os.environ.get("PATH"),
                },
                sort_keys=True,
            ),
        )
    if KEY_CAPTURE and key is not None:
        with open(KEY_CAPTURE, "w", encoding="utf-8") as handle:
            handle.write(key)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if PID_CAPTURE:
        with open(PID_CAPTURE, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    if PID_LOG:
        # Append-only: a mutating run may spawn many children before the test
        # ends, and every one of them must be reclaimable by PID.
        with open(PID_LOG, "a", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\n")
    if PATH_CAPTURE:
        with open(PATH_CAPTURE, "w", encoding="utf-8") as handle:
            handle.write(
                "PATH=" + str("PATH" in os.environ)
                + " MARKER=" + str(os.environ.get("FAKE_DSH_MARKER"))
            )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    banner = f"dsh web: http://127.0.0.1:{server.server_address[1]}"
    if MODE == "token-banner":
        # dsh >= 0.1.5: the web API needs a launch token exchanged for a
        # cookie; the banner carries it.
        banner += "/?token=SENTINEL-DSH-LAUNCH-TOKEN"
        # The same token on the other pipe must be masked too.
        print("dsh web: login at http://127.0.0.1:1/?token=SENTINEL-DSH-LAUNCH-TOKEN",
              file=sys.stderr, flush=True)
    _append(BANNER_CAPTURE, banner)
    print(banner, flush=True)
    try:
        thread.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
'''


def _install_fake_dsh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _allow_fake_process_identity(monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "dsh"
    script.write_text(_FAKE_DSH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


def _allow_fake_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep executable tests independent of a sandboxed Darwin ``ps``."""

    from hyprial.kernel.impl.processes.owned_process import OwnedProcessGroup

    monkeypatch.setattr(
        OwnedProcessGroup,
        "_process_birth_identity",
        staticmethod(lambda pid: f"test-birth:{pid}"),
    )


def _spec(
    name: str = "dsh-self-launch", *, args: tuple[str, ...] = ()
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness="dsh", name=name, headless=True, args=args)


def _complete_launch(
    environment: dict[str, str],
    *,
    actor: str = "agent:test:node:dsh-worker",
    grants: tuple[tuple[str, int], ...] = (("deepseek-r1", 1),),
) -> ChildEnvironmentLaunch:
    return ChildEnvironmentLaunch(
        environment=CompleteChildEnvironment(tuple(environment.items())),
        actor=actor,
        grants=grants,
    )


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _registered_stub_pids(pid_log: Path) -> list[int]:
    """Every stub ``dsh`` PID this fixture spawned, in start order."""

    if not pid_log.exists():
        return []
    pids: list[int] = []
    for line in pid_log.read_text(encoding="utf-8").split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _stub_children_alive(pids: list[int]) -> list[int]:
    """PIDs that are still THIS process's live children.

    ``waitpid(WNOHANG)`` is the discriminator: a live child yields ``(0, 0)``,
    a zombie is reaped here and yields its PID, and anything that is no longer
    our child (already reaped, or the PID was reused by the OS) raises
    ``ChildProcessError`` -- never signalled, never counted as a leak.
    """

    alive: list[int] = []
    for pid in pids:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if waited == 0:
            alive.append(pid)
    return alive


def _reap_stub_pids(pids: list[int]) -> list[int]:
    """SIGTERM -> SIGKILL only PIDs proven to be our live children, then reap.

    A PID ``waitpid`` no longer recognises as our child is left strictly
    alone: on a shared host it may already belong to another process.  Never
    ``pkill -f``.  Returns the PIDs that could not be proven gone.
    """

    for pid in _stub_children_alive(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    ours = _stub_children_alive(pids)
    deadline = time.monotonic() + 1.0
    while ours and time.monotonic() < deadline:
        time.sleep(0.02)
        ours = _stub_children_alive(pids)
    for pid in ours:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    remaining = ours
    deadline = time.monotonic() + 3.0
    while remaining and time.monotonic() < deadline:
        time.sleep(0.02)
        remaining = _stub_children_alive(pids)
    return remaining


def _ready_or_fail(process: DshHarnessProcess, timeout: float = 20.0) -> None:
    if not process.wait_ready(timeout=timeout):
        raise AssertionError(
            f"fake dsh never became ready: {process.last_error} / {process.exit_error}"
        )






















































class _IdleClient:
    """A pump client that connects and then idles until stop()."""

    running = False
    exit_error = None

    async def __aenter__(self) -> "_IdleClient":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def force_stop(self) -> None:
        return None

    def force_stopped(self) -> bool:
        return True






_OLD_AND_NEW_SECRET_NAMES = (
    "DEEPSEEKAPIKEY",
    "ACCESSTOKEN",
    "MY_ACCESSTOKEN",
    "BEARERTOKEN",
    "API-KEY",
    "X_API-KEY",
    "DB_PASSWORDS",
    "APP_SECRETS",
    "MY_CREDENTIALS",
    "SSH_AUTH_SOCK",
    "MY_KEY",
)






def _runtime_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    identity_reader: Any = None,
) -> ManagedHarnessRuntime:
    _install_fake_dsh(tmp_path, monkeypatch)
    kwargs: dict[str, Any] = {"restart_backoff_seconds": 0.01}
    if identity_reader is not None:
        kwargs["process_identity_reader"] = identity_reader
    return ManagedHarnessRuntime(
        HarnessLauncher(state_dir=tmp_path / "state"), **kwargs
    )









# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_FAKE_DSH',
    '_IdleClient',
    '_OLD_AND_NEW_SECRET_NAMES',
    '_allow_fake_process_identity',
    '_complete_launch',
    '_install_fake_dsh',
    '_pid_alive',
    '_ready_or_fail',
    '_reap_stub_pids',
    '_registered_stub_pids',
    '_runtime_for',
    '_spec',
    '_stub_children_alive',
    '_wait_for',
]
