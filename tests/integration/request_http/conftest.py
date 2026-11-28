"""Real isolated daemon + real request-http process fixtures.

Decision (2026-11-26): ``signal_group`` stops a fixture process through the
process group it leads where ``os.killpg`` exists (``SIGKILL`` for a hard stop)
and through ``terminate``/``kill`` of the process itself otherwise, so the
fixture teardown does not depend on POSIX process groups.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

import pytest

from hyprial.kernel import daemon_request_once
from tests.support.runtime.environment import UNIT_USER_ID


def signal_group(process: subprocess.Popen[Any], *, hard: bool) -> None:
    """Stop ``process`` through the group it leads where groups exist; ``terminate``/``kill`` it otherwise."""

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        if hard:
            process.kill()
        else:
            process.terminate()
        return
    killpg(os.getpgid(process.pid), getattr(signal, "SIGKILL", signal.SIGTERM) if hard else signal.SIGTERM)


OWNER ="h01-owner"
NODE = "h01-node"
ACTOR = f"user:{OWNER}"


class HeaderMap(dict[str, str]):
    """HTTP header names are case-insensitive; Uvicorn emits them lower-case."""

    def __init__(self, items: Any) -> None:
        super().__init__((str(key).lower(), str(value)) for key, value in items)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.lower())

    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key.lower(), default)


def _clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRIAL_", "HARNESS_"))
    }


@dataclass
class Isolation:
    root: Path
    home: Path
    state: Path
    user: Path
    runtime: Path
    env: dict[str, str]

    @property
    def socket_path(self) -> Path:
        return self.state / "daemon.sock"


@pytest.fixture
def isolation(request: pytest.FixtureRequest) -> Isolation:
    root = Path(tempfile.mkdtemp(prefix="h01-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    home = root / "home"
    state = root / "state"
    user = root / "user"
    runtime = root / "runtime"
    for directory in (home, state, user):
        directory.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    (home / "settings.json").write_text(json.dumps({"version": 1, "owner": OWNER}), encoding="utf-8")
    env = _clean_env()
    env.update(
        {
            "HOME": str(user),
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": OWNER,
            "HYPRIAL_USER_ID": UNIT_USER_ID,
            "HYPRIAL_NODE_ID": NODE,
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    env.pop("HARNESS_SOCKET_PATH", None)
    return Isolation(root, home, state, user, runtime, env)


class DaemonProcess:
    def __init__(
        self,
        isolation: Isolation,
        *,
        pythonpath: Path | None = None,
        module: str = "hyprial.cli",
    ) -> None:
        self.isolation = isolation
        self.pythonpath = pythonpath
        self.module = module
        self.process: subprocess.Popen[str] | None = None
        self.log_path = isolation.root / "daemon.log"

    def start(self) -> None:
        log = self.log_path.open("w", encoding="utf-8")
        env = dict(self.isolation.env)
        if self.pythonpath is not None:
            inherited = env.get("PYTHONPATH")
            parts = [str(self.pythonpath)]
            if inherited:
                parts.append(inherited)
            env["PYTHONPATH"] = os.pathsep.join(parts)
        self.process = subprocess.Popen(
            [sys.executable, "-m", self.module, "daemon", "run"],
            cwd=self.isolation.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._wait_ready(log)

    def _wait_ready(self, log: Any) -> None:
        deadline = time.monotonic() + 60
        socket_path = self.isolation.socket_path
        while time.monotonic() < deadline:
            assert self.process is not None
            if self.process.poll() is not None:
                log.close()
                raise AssertionError(
                    f"daemon exited {self.process.returncode}:\n"
                    + self.log_path.read_text(encoding="utf-8", errors="replace")
                )
            if socket_path.exists():
                try:
                    ping = daemon_request_once("ping", {}, socket_path=socket_path, timeout=1)
                except Exception:
                    time.sleep(0.05)
                    continue
                if ping.get("running") is True and ping.get("restorePending") is not True:
                    return
            time.sleep(0.05)
        self.stop()
        raise AssertionError(
            "daemon did not become ready:\n"
            + self.log_path.read_text(encoding="utf-8", errors="replace")
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            signal_group(process, hard=False)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            signal_group(process, hard=True)
            process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while self.isolation.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def restart(self) -> str:
        before = str(self._ping()["epoch"])
        self.stop()
        self.start()
        after = str(self._ping()["epoch"])
        assert after != before
        return after

    def _ping(self) -> dict[str, Any]:
        return daemon_request_once(
            "ping", {}, socket_path=self.isolation.socket_path, timeout=5
        )


@dataclass
class HttpProcess:
    isolation: Isolation
    process: subprocess.Popen[str]
    endpoint: str
    token: str
    server_json: dict[str, Any]
    log_path: Path

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
        timeout: float = 15,
    ) -> tuple[int, dict[str, str], Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.endpoint + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token if token is None else token}")
        if method == "POST":
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            if value:
                request.add_header(name, value)
            else:
                request.add_header(name, "")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                return response.status, HeaderMap(response.headers.items()), json.loads(payload or b"null")
        except urllib.error.HTTPError as error:
            with error:
                payload = error.read()
                return error.code, HeaderMap(error.headers.items()), json.loads(payload or b"null")

    @property
    def port(self) -> int:
        return int(self.endpoint.rsplit(":", 1)[1])

    def raw(self, request: bytes, *, timeout: float = 5) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as client:
            try:
                client.sendall(request)
            except OSError:
                pass
            data = bytearray()
            while True:
                try:
                    chunk = client.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
        head, _, body = bytes(data).partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        return int(status_line.split()[1]), body


def _config(
    isolation: Isolation,
    *,
    actor: str = ACTOR,
    kind: str = "local-daemon",
    request_timeout: float | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schemaVersion": 1,
        "backend": {"kind": kind, "socket": str(isolation.socket_path)},
        "principal": {"kind": "local-operator", "owner": OWNER, "actor": actor},
        "listen": {"host": "127.0.0.1", "port": 0},
        "security": {
            "allowedOrigins": ["http://127.0.0.1:4173"],
            "allowedHosts": ["127.0.0.1", "localhost"],
        },
        "runtime": {"dir": str(isolation.runtime)},
    }
    if request_timeout is not None:
        config["budgets"] = {
            "requestTimeoutSeconds": int(request_timeout),
            "headerTimeoutSeconds": 5,
            "idleTimeoutSeconds": 30,
            "maxRequestBytes": 262144,
            "maxResponseBytes": 8388608,
            "maxConcurrentRequests": 8,
            "shutdownDrainSeconds": 5,
        }
    return config


def write_config(isolation: Isolation, payload: dict[str, Any] | None = None) -> Path:
    path = isolation.root / "request-http.json"
    path.write_text(json.dumps(payload or _config(isolation)), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def daemon(isolation: Isolation) -> DaemonProcess:
    process = DaemonProcess(isolation)
    process.start()
    try:
        yield process
    finally:
        process.stop()


def _pythonpath_env(isolation: Isolation, pythonpath: Path | None) -> dict[str, str]:
    env = dict(isolation.env)
    if pythonpath is not None:
        inherited = env.get("PYTHONPATH")
        parts = [str(pythonpath)]
        if inherited:
            parts.append(inherited)
        env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


@contextmanager
def _spawn_request_http(
    isolation: Isolation,
    config: Path,
    *,
    pythonpath: Path | None = None,
) -> Any:
    """Spawn one already-configured request-http child and wait until ready."""

    log_path = isolation.root / "request-http.log"
    log = log_path.open("w", encoding="utf-8")
    env = _pythonpath_env(isolation, pythonpath)
    process = subprocess.Popen(
        [sys.executable, "-m", "hyprial.shell.impl.request_http", "serve", "--config", str(config)],
        cwd=isolation.root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    server_json_path = isolation.runtime / "server.json"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise AssertionError(
                f"request-http exited {process.returncode}:\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        if server_json_path.exists():
            break
        time.sleep(0.05)
    else:
        signal_group(process, hard=True)
        process.wait(timeout=10)
        log.close()
        raise AssertionError(
            "request-http did not publish server.json:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")
        )
    document = json.loads(server_json_path.read_text(encoding="utf-8"))
    http = HttpProcess(isolation, process, document["endpoint"], document["token"], document, log_path)
    try:
        deadline = time.monotonic() + 60
        last_ready: object = None
        while time.monotonic() < deadline:
            try:
                status, _, last_ready = http.request("GET", "/readyz", timeout=15)
            except (TimeoutError, urllib.error.URLError) as error:
                status = 0
                last_ready = f"{type(error).__name__}: {error}"
            if status == 200:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"request-http did not become ready (last={last_ready!r}):\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        yield http
    finally:
        if process.poll() is None:
            signal_group(process, hard=False)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                signal_group(process, hard=True)
                process.wait(timeout=10)
        log.close()


@contextmanager
def running_http(
    isolation: Isolation,
    *,
    pythonpath: Path | None = None,
    request_timeout: float | None = None,
) -> Any:
    config = write_config(
        isolation,
        _config(isolation, request_timeout=request_timeout),
    )
    with _spawn_request_http(
        isolation, config, pythonpath=pythonpath
    ) as http:
        yield http


@pytest.fixture
def http_server(isolation: Isolation, daemon: DaemonProcess) -> Any:
    with running_http(isolation) as http:
        yield http
