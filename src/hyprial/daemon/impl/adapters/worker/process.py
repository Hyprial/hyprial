"""Daemon-side lifecycle for isolated adapter worker processes."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .daemon_link import AdapterDaemonLink, WorkerDisconnected
from .wire import (
    READY_SCHEMA,
    WORKER_TOKEN_BYTES,
    DecodedWorkerFrame,
    WorkerIdentity,
    WorkerWireError,
    recv_worker_frame,
    require_worker_identity,
)


READY_FD_ENV_VAR = "HYPRIAL_ADAPTER_READY_FD"
CONTROL_FD_ENV_VAR = "HYPRIAL_ADAPTER_CONTROL_FD"
TOKEN_FD_ENV_VAR = "HYPRIAL_ADAPTER_TOKEN_FD"
STATE_DIR_ENV_VAR = "HYPRIAL_ADAPTER_STATE_DIR"
STARTING = "starting"
ONLINE = "online"
CLOSING = "closing"
CLOSED = "closed"
CRASHED = "crashed"
ERROR = "error"


@dataclass(frozen=True, slots=True)
class WorkerLaunchSpec:
    instance_id: str
    kind: str
    generation: int
    protocol: str = "adapter-worker-v1"
    build: str = "unknown"
    capability_digest: str = "unknown"

    def __post_init__(self) -> None:
        WorkerIdentity(self.instance_id, self.kind, self.generation)
        for label, value in (
            ("protocol", self.protocol),
            ("build", self.build),
            ("capability_digest", self.capability_digest),
        ):
            if not value:
                raise ValueError(f"{label} must not be empty")

    @property
    def identity(self) -> WorkerIdentity:
        return WorkerIdentity(self.instance_id, self.kind, self.generation)


@dataclass(frozen=True, slots=True)
class WorkerCloseOutcome:
    outcome: str
    drained: bool
    returncode: int | None
    forced: bool = False


@dataclass(frozen=True, slots=True)
class WorkerRestartOutcome:
    outcome: str
    old_close: WorkerCloseOutcome
    process: AdapterWorkerProcess | None


class _OutputDrain:
    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._last = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def last_line(self) -> str:
        return self._last

    def _run(self) -> None:
        try:
            for line in self._stream:  # type: ignore[union-attr]
                self._last = str(line).rstrip()[-1024:]
        finally:
            try:
                self._stream.close()  # type: ignore[union-attr]
            except Exception:
                pass

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)


class AdapterWorkerProcess:
    """A child process handle with authenticated readiness and private IPC."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        spec: WorkerLaunchSpec,
        *,
        ready_fd: int,
        control_socket: socket.socket,
        token: bytes,
        startup_timeout: float,
        callback_handler=None,
        request_handler=None,
        closed_callback=None,
    ) -> None:
        self._process = process
        self.spec = spec
        self.identity = spec.identity
        self._ready_fd = ready_fd
        self._control_socket = control_socket
        self._token = bytes(token)
        self._startup_timeout = startup_timeout
        self._callback_handler = callback_handler
        self._request_handler = request_handler
        self._closed_callback = closed_callback
        self._guard = threading.Lock()
        self._state = STARTING
        self._error: str | None = None
        self._expected_close = False
        self._ready = threading.Event()
        self._exited = threading.Event()
        self._ready_frame: DecodedWorkerFrame | None = None
        self._link: AdapterDaemonLink | None = None
        self._drain = _OutputDrain(process.stdout) if process.stdout is not None else None
        self._ready_thread = threading.Thread(target=self._await_ready, daemon=True)
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._ready_thread.start()
        self._monitor_thread.start()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def running(self) -> bool:
        return self._process.poll() is None

    @property
    def state(self) -> str:
        with self._guard:
            return self._state

    @property
    def error(self) -> str | None:
        with self._guard:
            return self._error

    @property
    def ready_frame(self) -> DecodedWorkerFrame | None:
        return self._ready_frame

    @property
    def restart_generation(self) -> int:
        return self.identity.generation + 1

    def wait_ready(self, timeout: float | None = None) -> str:
        self._ready.wait(timeout)
        return self.state

    def wait_exit(self, timeout: float | None = None) -> bool:
        return self._exited.wait(timeout)

    def wait_callbacks(self, timeout: float) -> bool:
        link = self._link
        return True if link is None else link.wait_callbacks(timeout)

    def invoke(self, payload: object, *, timeout: float = 15.0) -> object:
        if self.state != ONLINE or self._link is None:
            raise WorkerDisconnected("adapter worker is not online")
        return self._link.request("invoke", payload, timeout)

    def _await_ready(self) -> None:
        try:
            frame = recv_worker_frame(
                self._ready_fd, self._token, timeout=self._startup_timeout
            )
            require_worker_identity(frame, self.identity)
            fields = frame.as_dict()
            expected = {
                "schema": READY_SCHEMA,
                "instanceId": self.identity.instance_id,
                "generation": self.identity.generation,
                "kind": self.identity.kind,
                "protocol": self.spec.protocol,
                "build": self.spec.build,
                "capabilityDigest": self.spec.capability_digest,
            }
            if fields != expected or not self.running:
                raise WorkerWireError(
                    "ADAPTER_WORKER_READY_INVALID", "ready frame does not match launch spec"
                )
            link = AdapterDaemonLink(
                self._control_socket,
                self._token,
                self.identity,
                callback_handler=self._callback_handler,
                request_handler=self._request_handler,
                closed_callback=self._closed_callback,
            )
            with self._guard:
                self._ready_frame = frame
                self._link = link
                self._state = ONLINE
        except BaseException as error:
            with self._guard:
                self._state = ERROR
                self._error = f"{type(error).__name__}: {error}"
            self._terminate()
        finally:
            try:
                self._ready_fd.close()
            except OSError:
                pass
            self._ready.set()

    def _monitor(self) -> None:
        while True:
            try:
                returncode = self._process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        with self._guard:
            if self._expected_close:
                self._state = CLOSED
            elif returncode < 0:
                self._state = CRASHED
                self._error = f"native worker terminated by signal {-returncode}"
            elif self._state != ERROR:
                self._state = ERROR
                self._error = f"native worker exited with status {returncode}"
            link = self._link
        if link is not None:
            link.close()
        self._exited.set()

    def _terminate(self) -> bool:
        if not self.running:
            return False
        try:
            self._process.terminate()
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2.0)
            return True
        except (OSError, ProcessLookupError):
            pass
        return False

    def close(self, timeout: float = 5.0) -> WorkerCloseOutcome:
        with self._guard:
            if self._expected_close or self._state == CLOSED:
                return WorkerCloseOutcome(CLOSED, True, self._process.poll())
            if self._state == CRASHED:
                return WorkerCloseOutcome(CRASHED, True, self._process.poll())
            self._expected_close = True
            self._state = CLOSING
            link = self._link
        drained = False
        if link is not None and not link.closed:
            try:
                result = link.request(
                    "close", {"drainTimeoutMs": max(0, int(timeout * 1000))}, timeout
                )
                drained = isinstance(result, dict) and result.get("drained") is True
            except WorkerDisconnected:
                drained = False
        if not self._exited.wait(max(0.0, timeout)):
            forced = self._terminate()
            self._exited.wait(2.0)
        else:
            forced = False
        if link is not None:
            link.close()
        self._ready_thread.join(timeout=2.0)
        if self._drain is not None:
            self._drain.join(2.0)
        with self._guard:
            self._state = CLOSED
        return WorkerCloseOutcome(CLOSED, drained, self._process.poll(), forced)


def _listen_loopback() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


class AdapterWorkerLauncher:
    """Spawn isolated workers without importing their native entrypoints."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 20.0,
        callback_handler=None,
        request_handler=None,
        closed_callback=None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.command = tuple(command or ())
        if not self.command or any(
            not isinstance(argument, str) or not argument for argument in self.command
        ):
            raise ValueError(
                "command must be a non-empty string sequence that statically imports "
                "the child-only native driver"
            )
        self.env = dict(env or os.environ)
        self.startup_timeout = startup_timeout
        self.callback_handler = callback_handler
        self.request_handler = request_handler
        self.closed_callback = closed_callback

    def spawn(self, spec: WorkerLaunchSpec) -> AdapterWorkerProcess:
        listeners = {
            "ready": _listen_loopback(),
            "control": _listen_loopback(),
            "token": _listen_loopback(),
        }
        token = secrets.token_bytes(WORKER_TOKEN_BYTES)
        environment = dict(self.env)
        environment.update(
            {
                READY_FD_ENV_VAR: f"127.0.0.1:{listeners['ready'].getsockname()[1]}",
                CONTROL_FD_ENV_VAR: f"127.0.0.1:{listeners['control'].getsockname()[1]}",
                TOKEN_FD_ENV_VAR: f"127.0.0.1:{listeners['token'].getsockname()[1]}",
                STATE_DIR_ENV_VAR: str(self.state_dir),
            }
        )
        arguments = [
            *self.command,
            "--instance-id",
            spec.instance_id,
            "--kind",
            spec.kind,
            "--generation",
            str(spec.generation),
            "--protocol",
            spec.protocol,
            "--build",
            spec.build,
            "--capability-digest",
            spec.capability_digest,
        ]
        process = None
        ready_socket = None
        control_socket = None
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
                close_fds=True,
            )
            for listener in listeners.values():
                listener.settimeout(self.startup_timeout)
            token_socket, _ = listeners["token"].accept()
            token_socket.sendall(token)
            token_socket.shutdown(socket.SHUT_WR)
            token_socket.close()
            ready_socket, _ = listeners["ready"].accept()
            control_socket, _ = listeners["control"].accept()
        except BaseException:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
            for stream in (ready_socket, control_socket):
                if stream is not None:
                    stream.close()
            for listener in listeners.values():
                listener.close()
            raise
        for listener in listeners.values():
            listener.close()
        return AdapterWorkerProcess(
            process,
            spec,
            ready_fd=ready_socket,
            control_socket=control_socket,
            token=token,
            startup_timeout=self.startup_timeout,
            callback_handler=self.callback_handler,
            request_handler=self.request_handler,
            closed_callback=self.closed_callback,
        )

    def start(self, spec: WorkerLaunchSpec) -> AdapterWorkerProcess:
        process = self.spawn(spec)
        if process.wait_ready(self.startup_timeout + 2.0) != ONLINE:
            error = process.error or "adapter worker failed before ready"
            process.close()
            raise WorkerDisconnected(error)
        return process

    def restart(
        self, process: AdapterWorkerProcess, spec: WorkerLaunchSpec
    ) -> WorkerRestartOutcome:
        if (
            spec.instance_id != process.identity.instance_id
            or spec.kind != process.identity.kind
            or spec.generation != process.identity.generation + 1
        ):
            raise ValueError("restart requires the same instance and exactly the next generation")
        closed = process.close()
        if closed.outcome != CLOSED or not closed.drained:
            return WorkerRestartOutcome("close-not-drained", closed, None)
        replacement = self.start(spec)
        return WorkerRestartOutcome("restarted", closed, replacement)


def record_native_worker_exit(value: Mapping[str, object]) -> dict[str, object]:
    generation = value.get("generation")
    signal_number = value.get("signal")
    crashed = (
        isinstance(signal_number, int)
        and signal_number > 0
        and value.get("workerPid") != value.get("daemonPid")
    )
    return {
        "state": CRASHED if crashed else CLOSED,
        "daemonAlive": bool(crashed),
        "restartGeneration": generation + 1 if isinstance(generation, int) else None,
        "sessionUnlocked": True,
    }


__all__ = [
    "CLOSED",
    "CLOSING",
    "CONTROL_FD_ENV_VAR",
    "CRASHED",
    "ERROR",
    "ONLINE",
    "READY_FD_ENV_VAR",
    "STARTING",
    "STATE_DIR_ENV_VAR",
    "TOKEN_FD_ENV_VAR",
    "AdapterWorkerLauncher",
    "AdapterWorkerProcess",
    "WorkerCloseOutcome",
    "WorkerLaunchSpec",
    "WorkerRestartOutcome",
    "record_native_worker_exit",
]
