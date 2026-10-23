"""Launcher-owned carrier for the dsh-tui Hyprial bridge."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

import anyio

from hyprial.daemon.impl.inbox_watch import Heartbeat, Refresh, Request
from hyprial.daemon.impl.mcp.channel.adapter import (
    ClaudeChannelAdapter,
    RuntimeKind,
)
from hyprial.daemon.impl.mcp.channel.loops import _run_channel_daemon_loops
from hyprial.daemon.impl.mcp.channel.ownership import (
    _DAEMON_CONTACT_ERRORS,
    _OwnerProcessStatus,
    _owner_process_status,
    _read_process_identity,
)
from hyprial.daemon.impl.mcp.proxy import StatelessDaemonProxy
from hyprial.daemon.impl.mcp.wake import (
    WakeAttempt,
    WakeCommand,
    WakeCoordinator,
    WakeStatus,
)
from hyprial.kernel import (
    Logger,
    private_metadata_violation,
    private_tree_violation,
)

CARRIER_READY_TIMEOUT_SECONDS = 30.0
CONTROL_SOCKET_TIMEOUT_SECONDS = 2.0
DSH_WAKE_RESIGNAL_SECONDS = 300.0
# The plugin answers a wake with the turn's outcome inside this window; the
# socket read must outlive it.
DSH_WAKE_OUTCOME_SECONDS = 8.0
INJECTION_EDGE_LOSS_GRACE_SECONDS = 5.0
DSH_SESSION_ID_FILENAME = "dsh-session-id"
# Launch-directory state the carrier publishes for its launcher. The carrier
# owns the TUI process (the daemon's process fence requires the registration
# peer to be the claimed process's direct parent), so the launcher can only
# learn the pid, identity and exit code from these files.
DSH_TUI_PID_FILENAME = "tui-pid"
DSH_TUI_IDENTITY_FILENAME = "tui-identity"
DSH_TUI_EXIT_CODE_FILENAME = "tui-exit-code"
DSH_TUI_SPAWN_ERROR_FILENAME = "tui-spawn-error"


def dsh_session_id_path(control_socket: Path) -> Path:
    """Where the carrier publishes the DSH session it bound the bridge to.

    The launcher owns the launch directory and removes it on teardown; the
    carrier only publishes the id so a dying bridge can name a resumable
    session instead of leaving the user with an opaque exit status.
    """

    return Path(control_socket).with_name(DSH_SESSION_ID_FILENAME)


def dsh_wake_failure_delay(attempt: int) -> float:
    """Retry delay after a failed DSH wake: 30s doubling to the 300s cap.

    A failed wake is a failed model turn (or a refused followup), not a busy
    transport; the shared 1s..60s failure curve would re-run it every minute.
    """

    return min(DSH_WAKE_RESIGNAL_SECONDS, 30.0 * float(2 ** min(max(attempt, 1) - 1, 4)))


def write_launch_state(directory: Path, name: str, value: str) -> None:
    """Publish one launch-directory state file with owner-only permissions."""

    path = Path(directory) / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def _publish_dsh_session_id(path: Path, session_id: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(session_id)
    except OSError:
        # The id is diagnostic only; never fail a live bridge for it.
        return


class DshPluginWakeDriver:
    """Talk only to the launch-scoped Cordis plugin control socket."""

    def __init__(
        self,
        control_socket: Path,
        *,
        timeout_seconds: float = CONTROL_SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self.control_socket = Path(control_socket).expanduser()
        self.timeout_seconds = timeout_seconds
        self.wake_timeout_seconds = DSH_WAKE_OUTCOME_SECONDS + timeout_seconds

    def socket_present(self) -> bool:
        return self._safe_socket() is not None

    def _safe_socket(self) -> Path | None:
        try:
            info = self.control_socket.lstat()
            parent = self.control_socket.parent.lstat()
        except OSError:
            return None
        if not stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            return None
        try:
            socket_violation = private_metadata_violation(
                self.control_socket, info, mode=None
            )
            parent_violation = private_tree_violation(
                self.control_socket.parent, parent
            )
        except OSError:
            return None
        if socket_violation is not None or parent_violation is not None:
            return None
        return self.control_socket

    def _roundtrip(
        self, frame: dict[str, object], timeout: float | None = None
    ) -> dict[str, Any] | None:
        path = self._safe_socket()
        if path is None:
            return None
        body = (
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(
                    self.timeout_seconds if timeout is None else timeout
                )
                connection.connect(str(path))
                connection.sendall(body)
                chunks: list[bytes] = []
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except OSError:
            return None
        raw = b"".join(chunks).split(b"\n", 1)[0]
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    async def wait_ready(self, timeout: float = CARRIER_READY_TIMEOUT_SECONDS) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await anyio.to_thread.run_sync(
                self._roundtrip, {"type": "ping"}
            )
            if response is not None and response.get("ready") is True:
                session_id = response.get("sessionId")
                return session_id if isinstance(session_id, str) else ""
            await anyio.sleep(0.05)
        return None

    async def wake(self, command: WakeCommand) -> WakeAttempt:
        # The daemon renderer is byte-authoritative: no local prefix, no local
        # reformatting.  The plugin injects exactly this text.
        frame: dict[str, object] = {
            "type": "wake",
            "prompt": command.prompt,
            "outcomeTimeoutMs": int(DSH_WAKE_OUTCOME_SECONDS * 1000),
        }
        response = await anyio.to_thread.run_sync(
            self._roundtrip, frame, self.wake_timeout_seconds
        )
        if response is None:
            return WakeAttempt(
                WakeStatus.OFFLINE,
                "dsh-tui Hyprial injection plugin is not reachable",
            )
        if response.get("ok") is True:
            state = response.get("state")
            return WakeAttempt(
                WakeStatus.ACCEPTED,
                f"dsh-tui wake {state}" if isinstance(state, str) else None,
            )
        detail = response.get("detail")
        return WakeAttempt(
            WakeStatus.FAILED,
            f"dsh-tui wake failed: {response.get('code') or 'unknown'}"
            + (f" ({detail})" if isinstance(detail, str) and detail else ""),
        )


class DshCarrierAdapter(ClaudeChannelAdapter):
    """Register the dsh-tui process under the shared carrier protocol."""

    def __init__(
        self,
        proxy: StatelessDaemonProxy,
        coordinator: WakeCoordinator,
        *,
        actor: str,
        session_ref: str,
        cwd: str,
        command: tuple[str, ...],
        tui_pid: int,
        tui_identity: str,
    ) -> None:
        super().__init__(
            proxy,
            coordinator,
            actor=actor,
            session_ref=session_ref,
            cwd=cwd,
            command=command,
            owner_fence=True,
        )
        self.tui_pid = tui_pid
        self.tui_identity = tui_identity

    async def _register(self) -> dict[str, Any]:
        return await self._call(
            "session.register",
            {
                "cwd": self.cwd,
                "command": list(self.command),
                "source": "dsh-channel",
                "runtime": RuntimeKind.DSH_INTERACTIVE,
                "channelConfirmed": True,
                "turnReporting": False,
                "ownerFence": True,
                "processPid": self.tui_pid,
                "processIdentity": self.tui_identity,
            },
            mutation=True,
        )

    async def _request(self, action: Request) -> dict[str, Any]:
        if isinstance(action, Refresh):
            # A dsh-channel session holds no channel lease: _register omits
            # channelLeaseToken (the daemon refuses a token for this source
            # and stores a None digest). The inherited refresh/heartbeat
            # send the base class's random token, which only passes while
            # the daemon skips lease verification for lease-less sessions;
            # keep the wire lease-free so enforcing the digest daemon-side
            # cannot break dsh liveness.
            return await self._call("session.refresh", {}, mutation=False)
        if isinstance(action, Heartbeat):
            return await self._call("session.heartbeat", {}, mutation=False)
        return await super()._request(action)


class _SessionStore:
    def __init__(self, session: Any) -> None:
        self._session = session

    def load(self) -> Any:
        from hyprial.daemon.impl.desired_state import DesiredState

        return DesiredState(interactive_sessions=(self._session,))


async def run_dsh_carrier(
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    cwd: str,
    command: tuple[str, ...],
    control_socket: Path,
    tui_pid: int,
    tui_identity: str,
    logger: Logger | None = None,
    poll_interval: float = 0.5,
    ready_timeout: float = CARRIER_READY_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the single launcher-owned carrier until stopped."""

    from hyprial.daemon.impl.desired_state import InteractiveSession

    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    session = InteractiveSession(
        actor=actor,
        cwd=cwd,
        command=command,
        source="dsh-channel",
        session_ref=session_ref,
        runtime=RuntimeKind.DSH_INTERACTIVE,
        channel_confirmed=True,
    )
    driver = DshPluginWakeDriver(control_socket)
    coordinator = WakeCoordinator(
        _SessionStore(session),
        driver,
        logger=logger,
        retry_delay=dsh_wake_failure_delay,
        resignal_delay=lambda _attempt: DSH_WAKE_RESIGNAL_SECONDS,
        read_retry_delay=DSH_WAKE_RESIGNAL_SECONDS,
    )
    adapter = DshCarrierAdapter(
        proxy,
        coordinator,
        actor=actor,
        session_ref=session_ref,
        cwd=cwd,
        command=command,
        tui_pid=tui_pid,
        tui_identity=tui_identity,
    )
    try:
        async with anyio.create_task_group() as tasks:

            async def poll_carrier() -> None:
                plugin_session = await driver.wait_ready(ready_timeout)
                if plugin_session is None:
                    tasks.cancel_scope.cancel()
                    return
                if plugin_session:
                    await anyio.to_thread.run_sync(
                        _publish_dsh_session_id,
                        dsh_session_id_path(control_socket),
                        plugin_session,
                    )
                await _run_channel_daemon_loops(
                    adapter,
                    poll_interval=poll_interval,
                )

            async def wait_for_stop() -> None:
                if stop_event is None:
                    while True:
                        await anyio.sleep(3600.0)
                # Polling, not a blocking `run_sync(stop_event.wait)`: the
                # thread form is not cancellable, so the task group stayed open
                # after the injection edge, the TUI or the readiness bound had
                # already asked it to stop (measured: blocked past 25 s, no
                # unregister). A cancelled `run_sync` would also leave the
                # abandoned worker thread for interpreter shutdown to join.
                while not stop_event.is_set():
                    await anyio.sleep(0.2)
                tasks.cancel_scope.cancel()

            async def watch_injection_edge() -> None:
                while not adapter.watch.registered:
                    await anyio.sleep(0.5)
                missing_since: float | None = None
                while True:
                    await anyio.sleep(0.5)
                    response = await anyio.to_thread.run_sync(
                        driver._roundtrip, {"type": "ping"}
                    )
                    ready = (
                        response is not None
                        and response.get("ready") is True
                    )
                    if ready:
                        missing_since = None
                        continue
                    if missing_since is None:
                        missing_since = time.monotonic()
                        continue
                    if (
                        time.monotonic() - missing_since
                        >= INJECTION_EDGE_LOSS_GRACE_SECONDS
                    ):
                        # The launch-scoped plugin is gone or has lost its DSH
                        # agent. Stop the carrier and unregister rather than
                        # advertising an unwakeable actor.
                        tasks.cancel_scope.cancel()
                        return

            async def watch_tui_process() -> None:
                while True:
                    await anyio.sleep(1.0)
                    status = await anyio.to_thread.run_sync(
                        _owner_process_status,
                        tui_pid,
                        tui_identity,
                    )
                    if status in {
                        _OwnerProcessStatus.PID_MISSING,
                        _OwnerProcessStatus.IDENTITY_MISMATCH,
                    }:
                        tasks.cancel_scope.cancel()
                        return

            tasks.start_soon(poll_carrier)
            tasks.start_soon(wait_for_stop)
            tasks.start_soon(watch_injection_edge)
            tasks.start_soon(watch_tui_process)
    finally:
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(1.0):
                try:
                    await adapter.stop()
                except _DAEMON_CONTACT_ERRORS:
                    pass


def process_identity_for_tui(pid: int) -> str | None:
    return _read_process_identity(pid)


def dsh_tui_process_gone(pid: int, identity: str) -> bool:
    """True only on positive evidence: the pid is gone or was reused.

    An unreadable marker is not evidence either way (UNKNOWN), so doctor
    never calls a live TUI stale because ``ps`` was unavailable.
    """

    return _owner_process_status(pid, identity) in {
        _OwnerProcessStatus.PID_MISSING,
        _OwnerProcessStatus.IDENTITY_MISMATCH,
    }
