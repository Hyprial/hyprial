"""Race-safe, process-identity-fenced ownership of one HYPRIAL home."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.home import HYPRIALHomeNotInitialized

if TYPE_CHECKING:
    from hyprial.mcp.channel import _OwnerProcessStatus

DEFAULT_KEEPALIVE_DURATION = 5.0
KEEPALIVE_ENV = "HYPRIAL_DAEMON_KEEPALIVE_DURATION"
# Shared with the state-dir daemon.lock bounded wait (application.py): both
# cover the same rapid-restart window where `daemon stop` has already reported
# success but the previous process is still tearing down. 0 disables the wait.
CLAIM_WAIT_ENV = "HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT"
DEFAULT_CLAIM_WAIT_TIMEOUT = 15.0


class HYPRIALHomeInUse(RuntimeError):
    """A recent heartbeat proves that another daemon owns this home."""

    code = ipc_errors.HYPRIAL_HOME_IN_USE

    def __init__(self, home: Path, pid: int) -> None:
        self.home = home
        self.pid = pid
        self.data: dict[str, Any] = {"path": str(home), "pid": pid}
        super().__init__(f"HYPRIAL home {home} is being used by another daemon (pid {pid})")


def keepalive_duration_from_environment() -> float:
    raw = os.environ.get(KEEPALIVE_ENV)
    if raw is None:
        return DEFAULT_KEEPALIVE_DURATION
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{KEEPALIVE_ENV} must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{KEEPALIVE_ENV} must be a positive number")
    return value


def live_daemon_pid(home: Path) -> int | None:
    """The pid of the daemon currently holding ``home``'s heartbeat, or ``None``.

    Read-only counterpart to :class:`ActiveDaemonHeartbeat` — the same
    ``.active_daemon`` record, the same freshness window (twice the keepalive
    the writer itself advertised in the record), and the same fail-safe
    process verdicts.  It exists for callers that must *refuse* while a
    daemon owns the home (``hyprial login --switch-account``, login U5) and
    therefore may not claim anything: claiming is what a daemon does.

    Fail-safe direction: a heartbeat that is fresh but whose owner process
    cannot be positively reaped (``UNKNOWN`` — permission denied, unreadable
    birth marker) counts as **running**.  The caller is about to write state
    a live daemon believes it owns; "cannot prove it died" must land on the
    refuse side, exactly as :func:`hyprial.mcp.channel._owner_process_alive`
    lands on "alive" for the same reason.  Only ``PID_MISSING`` and
    ``IDENTITY_MISMATCH`` — positive death and positive PID reuse — read as
    stopped.
    """

    # Deferred import: same cycle shape as ActiveDaemonHeartbeat.__init__
    # (hyprial.mcp.channel -> daemon.desired_state -> daemon.application -> here).
    from hyprial.mcp.channel import (
        _OwnerProcessStatus,
        _owner_process_status,
    )

    try:
        record = json.loads((Path(home) / ".active_daemon").read_text("utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    identity = record.get("processIdentity")
    heartbeat = record.get("heartbeatMonotonic")
    keepalive = record.get("keepaliveDurationSeconds")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(identity, str)
        or not identity
        or not isinstance(heartbeat, (int, float))
        or isinstance(heartbeat, bool)
    ):
        return None
    if (
        not isinstance(keepalive, (int, float))
        or isinstance(keepalive, bool)
        or keepalive <= 0
    ):
        keepalive = DEFAULT_KEEPALIVE_DURATION
    age = time.monotonic() - float(heartbeat)
    if age < 0 or age >= 2 * float(keepalive):
        return None
    status = _owner_process_status(pid, identity)
    if status in {
        _OwnerProcessStatus.PID_MISSING,
        _OwnerProcessStatus.IDENTITY_MISMATCH,
    }:
        return None
    return pid


def claim_wait_timeout_from_environment() -> float:
    raw = os.environ.get(CLAIM_WAIT_ENV)
    if raw is None:
        return DEFAULT_CLAIM_WAIT_TIMEOUT
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{CLAIM_WAIT_ENV} must be a non-negative number") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{CLAIM_WAIT_ENV} must be a non-negative number")
    return value


class ActiveDaemonHeartbeat:
    """Claim and refresh ``$HYPRIAL_HOME/.active_daemon``.

    The sibling flock serializes each read/claim/write transaction, but is not
    held for the daemon lifetime: a daemon whose heartbeat really goes stale
    can be replaced. Every refresh verifies the generation before writing, so
    a stale daemon that wakes after takeover cannot clobber the new owner.
    """

    def __init__(
        self,
        home: Path,
        *,
        keepalive_duration: float = DEFAULT_KEEPALIVE_DURATION,
        claim_wait_timeout: float | None = None,
        pid: int | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        identity_reader: Callable[[int], str | None] | None = None,
        process_status: Callable[[int, str], _OwnerProcessStatus] | None = None,
        ownership_lost: Callable[[], None] | None = None,
    ) -> None:
        # Deferred import: hyprial.mcp.channel transitively imports this module
        # (channel -> daemon.desired_state -> daemon.application -> here), so
        # a module-level import breaks whichever side is entered first.
        from hyprial.mcp.channel import _owner_process_status, _read_process_identity

        if identity_reader is None:
            identity_reader = _read_process_identity
        if process_status is None:
            process_status = _owner_process_status
        if not math.isfinite(keepalive_duration) or keepalive_duration <= 0:
            raise ValueError("keepalive_duration must be a positive number")
        self.home = Path(home)
        self.path = self.home / ".active_daemon"
        self.lock_path = self.home / ".active_daemon.lock"
        self.keepalive_duration = keepalive_duration
        if claim_wait_timeout is None:
            claim_wait_timeout = claim_wait_timeout_from_environment()
        if not math.isfinite(claim_wait_timeout) or claim_wait_timeout < 0:
            raise ValueError("claim_wait_timeout must be a non-negative number")
        self.claim_wait_timeout = claim_wait_timeout
        self.pid = os.getpid() if pid is None else pid
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._identity_reader = identity_reader
        self._process_status = process_status
        self._ownership_lost = ownership_lost
        self._identity: str | None = None
        self._generation = uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._claimed = False

    def claim(self) -> None:
        if not self.home.is_dir():
            source = (
                "HYPRIAL_HOME environment variable"
                if "HYPRIAL_HOME" in os.environ
                else "default value (~/.hyprial)"
            )
            raise HYPRIALHomeNotInitialized(self.home.resolve(), source)
        identity = self._identity_reader(self.pid)
        if identity is None:
            raise RuntimeError(
                f"cannot establish daemon process identity for pid {self.pid}; "
                "refusing an unfenced HYPRIAL home claim"
            )
        self._identity = identity
        # `daemon stop` reports success at socket-absence, before the previous
        # process reaches its own marker cleanup, so a rapid restart routinely
        # observes a fresh-but-final heartbeat (same shape as the state-dir
        # daemon.lock wait in application.py, surfaced by E2E-006). Wait out a
        # non-advancing heartbeat for a bounded time; an ADVANCING heartbeat
        # is a live serving daemon and fails immediately with its pid.
        deadline: float | None = None
        first_seen: tuple[int, float] | None = None
        while True:
            with self._locked():
                current = self._read_record()
                blocking_pid = self._blocking_pid(current)
                if blocking_pid is None:
                    self._write_record()
                    break
                assert current is not None
                heartbeat = float(current["heartbeatMonotonic"])
                now = self._monotonic_clock()
                if deadline is None:
                    deadline = now + self.claim_wait_timeout
                    first_seen = (blocking_pid, heartbeat)
                if (blocking_pid, heartbeat) != first_seen or now >= deadline:
                    raise HYPRIALHomeInUse(self.home.resolve(), blocking_pid)
            time.sleep(min(0.05, self.keepalive_duration))
        self._claimed = True
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="hyprial-home-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=max(1.0, min(self.keepalive_duration, 5.0)))
        if not self._claimed:
            return
        try:
            with self._locked():
                if self._owns(self._read_record()):
                    self.path.unlink(missing_ok=True)
        finally:
            self._claimed = False

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.keepalive_duration):
            try:
                with self._locked():
                    if not self._owns(self._read_record()):
                        self._lose_ownership()
                        return
                    self._write_record()
            except Exception:  # noqa: BLE001 - losing the fence stops the daemon
                self._lose_ownership()
                return

    def _lose_ownership(self) -> None:
        self._claimed = False
        self._stop.set()
        if self._ownership_lost is not None:
            self._ownership_lost()

    def _blocking_pid(self, record: dict[str, Any] | None) -> int | None:
        if record is None:
            return None
        pid = record.get("pid")
        identity = record.get("processIdentity")
        heartbeat = record.get("heartbeatMonotonic")
        if (
            not isinstance(pid, int)
            or pid <= 0
            or pid == self.pid
            or not isinstance(identity, str)
            or not identity
            or not isinstance(heartbeat, (int, float))
            or isinstance(heartbeat, bool)
        ):
            return None
        age = self._monotonic_clock() - float(heartbeat)
        if age < 0 or age >= 2 * self.keepalive_duration:
            return None
        from hyprial.mcp.channel import _OwnerProcessStatus

        status = self._process_status(pid, identity)
        if status in {
            _OwnerProcessStatus.PID_MISSING,
            _OwnerProcessStatus.IDENTITY_MISMATCH,
        }:
            return None
        return pid

    def _owns(self, record: dict[str, Any] | None) -> bool:
        return bool(
            record is not None
            and record.get("pid") == self.pid
            and record.get("processIdentity") == self._identity
            and record.get("generation") == self._generation
        )

    def _read_record(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_record(self) -> None:
        assert self._identity is not None
        record = {
            "schemaVersion": 1,
            "pid": self.pid,
            "processIdentity": self._identity,
            "generation": self._generation,
            "heartbeatAt": datetime.fromtimestamp(self._wall_clock(), UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "heartbeatMonotonic": self._monotonic_clock(),
            "keepaliveDurationSeconds": self.keepalive_duration,
        }
        temporary = self.home / f".active_daemon.{self.pid}.{self._generation}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._fsync_home()
        finally:
            temporary.unlink(missing_ok=True)

    def _fsync_home(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(self.home, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            # Atomic rename is the correctness boundary. Directory fsync is a
            # durability improvement that some filesystems do not implement.
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        stream = self.lock_path.open("a+b")
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            stream.close()
