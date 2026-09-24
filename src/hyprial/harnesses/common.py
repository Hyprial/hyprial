"""Provider-neutral PTY process and bounded startup diagnostics."""

from __future__ import annotations

import errno
import os
import pty
import re
import signal
import subprocess
import termios
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from hyprial.agents.environment import (
    whitelist_replacement_environment,
)
from hyprial.daemon.api import (
    ProcessLiveness,
    ProcessLivenessProbeError,
    ProcessLivenessState,
)

STDERR_BUFFER_BYTES = 16 * 1024
STDERR_TAIL_BYTES = 4 * 1024
STDERR_TAIL_LINES = 8
STDERR_TRUNCATION_MARKER = "… [truncated]"

_AUTHORIZATION = re.compile(
    r"(\bauthorization\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
_BEARER_OR_TOKEN = re.compile(
    r"\b(bearer|token)\s+[^\s,;]+",
    re.IGNORECASE,
)
_NAMED_CREDENTIAL = re.compile(
    r"((?:[\"']?)[a-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|password|"
    r"credential|authorization)[a-z0-9_-]*(?:[\"']?)\s*[:=]\s*)"
    r'(?:"(?:\\.|[^"\\\r\n])*"|\'(?:\\.|[^\'\\\r\n])*\'|[^\s,;]+)',
    re.IGNORECASE,
)


class OperationStatus(str, Enum):
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HarnessOperation:
    harness: str
    lookup_ref: str
    status: OperationStatus
    output: str = ""
    error: str | None = None


@dataclass(slots=True)
class _PendingOperation:
    lookup_ref: str
    output_offset: int
    output: bytes | None = None
    error: str | None = None


class HarnessStartError(RuntimeError):
    """A launch failure with only bounded, redacted harness diagnostics."""

    def __init__(
        self,
        harness: str,
        argv: Sequence[str],
        reason: str,
        *,
        returncode: int | None = None,
        stderr_tail: str = "",
    ) -> None:
        self.harness = harness
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        message = f"{harness} harness failed to start: {reason}"
        if stderr_tail:
            message += f"\nRecent stderr:\n{stderr_tail}"
        super().__init__(message)


def summarize_stderr(
    stderr: bytes | str,
    *,
    max_lines: int = STDERR_TAIL_LINES,
    max_bytes: int = STDERR_TAIL_BYTES,
) -> str:
    """Return the last non-empty redacted lines within a byte budget.

    Defaults keep routine logs tight; startup-critical failure records
    (startup_timeout, unexpected worker exits) pass a larger budget so a
    verbose error body -- e.g. a rate-limit response the CLI retried
    against -- is not truncated away from the one line that explains it.
    """

    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace")
    else:
        text = stderr
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    text = _BEARER_OR_TOKEN.sub(r"\1 [REDACTED]", text)
    text = _NAMED_CREDENTIAL.sub(r"\1[REDACTED]", text)
    lines = [line.rstrip() for line in text.splitlines() if line.rstrip()]
    if not lines:
        return ""
    recent = "\n".join(lines[-max_lines:])
    encoded = recent.encode("utf-8")
    if len(encoded) <= max_bytes:
        return recent
    # The marker shares the first retained line so the marker itself does not
    # turn an eight-line diagnostic into nine surfaced lines.
    marker = f"{STDERR_TRUNCATION_MARKER} ".encode()
    retained = encoded[-(max_bytes - len(marker)) :]
    while retained and retained[0] & 0xC0 == 0x80:
        retained = retained[1:]
    return marker.decode() + retained.decode("utf-8", errors="replace")


def without_session_arguments(args: Sequence[str]) -> tuple[str, ...]:
    """Remove caller session controls while preserving unrelated passthrough flags."""

    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--session-id", "--resume"}:
            index += 2
            continue
        if value.startswith(("--session-id=", "--resume=")):
            index += 1
            continue
        result.append(value)
        index += 1
    return tuple(result)


class PtyHarnessProcess:
    """One managed harness child with a PTY-backed injection seam."""

    def __init__(
        self,
        harness: str,
        argv: Sequence[str],
        child: subprocess.Popen[bytes],
        master_fd: int,
        *,
        stop_grace_seconds: float,
    ) -> None:
        self.harness = harness
        self.argv = tuple(argv)
        self.pid = child.pid
        self._child = child
        self._master_fd = master_fd
        self._stop_grace_seconds = stop_grace_seconds
        self._lock = threading.RLock()
        self._output = bytearray()
        self._stderr = bytearray()
        self._operations: dict[str, _PendingOperation] = {}
        self._master_closed = False
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name=f"hyprial-{harness}-{child.pid}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name=f"hyprial-{harness}-{child.pid}-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def spawn(
        cls,
        harness: str,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        complete_environment: bool = False,
        startup_probe_seconds: float = 0.08,
        stop_grace_seconds: float = 1.0,
    ) -> PtyHarnessProcess:
        if not argv:
            raise HarnessStartError(harness, argv, "harness command is empty")
        master_fd, slave_fd = pty.openpty()
        try:
            attributes = termios.tcgetattr(slave_fd)
            attributes[1] &= ~termios.OPOST
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
            try:
                child = subprocess.Popen(
                    tuple(argv),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=(
                        None
                        if env is None
                        else (
                            dict(env)
                            if complete_environment
                            else whitelist_replacement_environment(os.environ, env)
                        )
                    ),
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as error:
                os.close(master_fd)
                location = f" (cwd={cwd})" if cwd is not None else ""
                raise HarnessStartError(
                    harness,
                    argv,
                    f"{error}{location}",
                ) from error
        finally:
            os.close(slave_fd)
        process = cls(
            harness,
            argv,
            child,
            master_fd,
            stop_grace_seconds=stop_grace_seconds,
        )
        try:
            returncode = child.wait(timeout=startup_probe_seconds)
        except subprocess.TimeoutExpired:
            return process
        process._join_readers()
        process._close_master()
        stderr_tail = process.stderr_tail
        raise HarnessStartError(
            harness,
            argv,
            f"process exited during startup with status {returncode}",
            returncode=returncode,
            stderr_tail=stderr_tail,
        )

    @property
    def running(self) -> bool:
        return self._child.poll() is None

    def liveness(self) -> ProcessLiveness:
        try:
            os.killpg(self._child.pid, 0)
        except ProcessLookupError:
            return ProcessLiveness(
                ProcessLivenessState.DEAD,
                observed=True,
                pid=self._child.pid,
            )
        except OSError as error:
            raise ProcessLivenessProbeError(
                f"cannot probe {self.harness} process group {self._child.pid}: {error}"
            ) from error
        return ProcessLiveness(
            ProcessLivenessState.ALIVE,
            observed=True,
            pid=self._child.pid,
        )

    @property
    def stderr_tail(self) -> str:
        with self._lock:
            return summarize_stderr(bytes(self._stderr))

    def inject(self, message: str) -> str:
        if not message:
            raise ValueError("injection message is required")
        with self._lock:
            if not self.running:
                raise RuntimeError(f"{self.harness} harness process is not running")
            if any(
                operation.output is None and operation.error is None
                for operation in self._operations.values()
            ):
                raise RuntimeError(f"{self.harness} harness session is busy")
            lookup_ref = str(uuid4())
            operation = _PendingOperation(lookup_ref, len(self._output))
            self._operations[lookup_ref] = operation
            payload = message.encode("utf-8") + b"\n"
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(self._master_fd, view)
                    view = view[written:]
            except OSError as error:
                operation.error = f"harness injection failed: {error}"
                raise RuntimeError(operation.error) from error
            return lookup_ref

    def retrieve(self, lookup_ref: str) -> HarnessOperation:
        with self._lock:
            try:
                operation = self._operations[lookup_ref]
            except KeyError as error:
                raise KeyError(
                    f"{self.harness} operation '{lookup_ref}' was not found"
                ) from error
            if operation.output is None and operation.error is None:
                available = bytes(self._output[operation.output_offset :])
                newline = available.find(b"\n")
                if newline >= 0:
                    operation.output = available[: newline + 1]
                elif not self.running:
                    if available:
                        operation.output = available
                    else:
                        operation.error = (
                            "harness process stopped before producing a result"
                        )
            if operation.error is not None:
                return HarnessOperation(
                    self.harness,
                    lookup_ref,
                    OperationStatus.FAILED,
                    error=operation.error,
                )
            if operation.output is not None:
                return HarnessOperation(
                    self.harness,
                    lookup_ref,
                    OperationStatus.COMPLETED,
                    output=operation.output.decode("utf-8", errors="replace"),
                )
            return HarnessOperation(
                self.harness,
                lookup_ref,
                OperationStatus.IN_PROGRESS,
            )

    def stop(self) -> None:
        if self.running:
            try:
                os.killpg(self._child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._child.wait(timeout=self._stop_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._child.wait()
        self._close_master()
        self._join_readers()
        with self._lock:
            for operation in self._operations.values():
                if operation.output is None and operation.error is None:
                    available = bytes(self._output[operation.output_offset :])
                    if available:
                        operation.output = available
                    else:
                        operation.error = (
                            "harness process stopped before producing a result"
                        )

    def _read_stdout(self) -> None:
        while True:
            try:
                chunk = os.read(self._master_fd, 4096)
            except OSError as error:
                if error.errno not in {errno.EBADF, errno.EIO}:
                    raise
                return
            if not chunk:
                return
            with self._lock:
                self._output.extend(chunk)

    def _read_stderr(self) -> None:
        assert self._child.stderr is not None
        while chunk := self._child.stderr.read(4096):
            with self._lock:
                self._stderr.extend(chunk)
                if len(self._stderr) > STDERR_BUFFER_BYTES:
                    del self._stderr[:-STDERR_BUFFER_BYTES]

    def _close_master(self) -> None:
        with self._lock:
            if self._master_closed:
                return
            self._master_closed = True
            try:
                os.close(self._master_fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise

    def _join_readers(self) -> None:
        self._stdout_thread.join(timeout=1.0)
        self._stderr_thread.join(timeout=1.0)


@dataclass(frozen=True, slots=True)
class ConnectorOptions:
    command: tuple[str, ...]
    env: Mapping[str, str] | None = None
    startup_probe_seconds: float = 0.08
    stop_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("harness command must not be empty")
        if self.env is not None and not isinstance(self.env, MappingProxyType):
            object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
