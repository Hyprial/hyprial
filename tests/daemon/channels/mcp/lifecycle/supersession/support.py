"""Superseded / orphaned Claude Code Channel stdio children are reaped.

Two failure modes let stale ``python -m hyprial.daemon.impl.mcp.entry
claude-channel`` children accumulate and
compete for one actor's inbound delivery ("flap"):

1. Supersede-steal. ``session.register`` is last-writer-wins keyed by actor, so
   a stale child that gets fenced and then *re-registers* steals the actor back
   from the live session. The daemon-restart-resilience poll loop did exactly
   that -- it caught the fence error (a ``RuntimeError`` envelope) and treated it
   like any transient daemon-contact failure, re-registering on the next tick.

2. Orphan survival. When the parent Claude Code process dies the stdio child
   does NOT reliably see stdin EOF (a real pipe's write end can stay open via an
   inherited fd), so an orphan lingers forever.

The fix, exercised here: the daemon distinguishes a supersede verdict
(``SESSION_SUPERSEDED`` -- a *different* live session owns the actor) from a
plain "not registered" fence (``STALE_SESSION``); on the supersede verdict the
child goes quiet (stops re-registering) but never tears itself down. Teardown is
driven solely by parent-process liveness (``os.getppid`` drift), so a restart
race can never terminate a child whose parent is alive.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox_watch import InboxWatch, Ok
from hyprial.daemon.impl.mcp import (
    SESSION_SUPERSEDED_CODE,
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    RuntimeKind,
    StatelessDaemonProxy,
    UnixDaemonConnectionFactory,
    WakeCoordinator,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _until(predicate: Any, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


async def _until_async(
    predicate: Any, *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await anyio.sleep(interval)
    raise AssertionError("async condition was not met within the timeout")


class _FakeStore:
    def __init__(self, state: DesiredState) -> None:
        self._state = state

    def load(self) -> DesiredState:
        return self._state


class _RecordingChannel:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


def _adapter_for(
    socket_path: Path, actor: str, session_ref: str
) -> ClaudeChannelAdapter:
    session = InteractiveSession(
        actor=actor,
        cwd="/work",
        command=("claude",),
        source="claude-channel",
        session_ref=session_ref,
        runtime=RuntimeKind.CLAUDE_INTERACTIVE,
        channel_confirmed=True,
    )
    coordinator = WakeCoordinator(
        _FakeStore(DesiredState(interactive_sessions=(session,))),
        ClaudeChannelDriver(_RecordingChannel()),
    )
    return ClaudeChannelAdapter(
        StatelessDaemonProxy(UnixDaemonConnectionFactory(socket_path)),
        coordinator,
        actor=actor,
        session_ref=session_ref,
        cwd="/work",
        command=("claude",),
    )


# --------------------------------------------------------------------------- #
# 1. Daemon: a newer session supersedes an older one with a distinct code
# --------------------------------------------------------------------------- #
class _FakeInbox:
    def pending_messages(self, actor: str) -> tuple[Any, ...]:
        return ()

    def pending_all(self) -> tuple[Any, ...]:
        return ()

    def outbox_count(self) -> int:
        return 0


def _daemon_app(tmp_path: Path, own_daemon_application) -> DaemonApplication:
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
    **_deep_module_daemon_dependencies()))
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    return app


def _register(app: DaemonApplication, session_ref: str) -> None:
    app.handle(
        "session.register",
        {
            "actor": "hq-adjutant",
            "sessionRef": session_ref,
            "cwd": "/work",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )








# --------------------------------------------------------------------------- #
# 2 & 3. Poll loop: terminal on supersede, still resilient to everything else
# --------------------------------------------------------------------------- #
class _ScriptedAdapter:
    """The adapter surface the loops drive, scripted per operation.

    ``scripts`` maps an inbox-watch op ("register", "refresh", "list",
    "heartbeat", "wait") to the outcomes it returns in order; an exhausted
    script answers Ok.  Waits answer "not held" by default (plain polling).
    """

    def __init__(
        self,
        actor: str,
        *,
        scripts: dict[str, list[Any]] | None = None,
        poll_interval_ms: int = 10,
        heartbeat_interval_ms: int = 10,
    ) -> None:
        self.actor = actor
        self.session_ref = "ref"
        self.watch = InboxWatch(
            poll_interval_ms=poll_interval_ms,
            heartbeat_interval_ms=heartbeat_interval_ms,
        )
        self.scripts = {op: list(outcomes) for op, outcomes in (scripts or {}).items()}
        self.calls: dict[str, int] = {}
        self.stopped = False

    def count(self, op: str) -> int:
        return self.calls.get(op, 0)

    def register_now(self) -> None:
        """Complete one registration without the poll loop (heartbeat tests)."""

        action = self.watch.poll_next(0)
        self.watch.poll_done(action, Ok({"ok": True, "daemonEpoch": "e1"}), 0)  # type: ignore[arg-type]

    async def perform(self, action: Any) -> Any:
        await anyio.sleep(0)
        self.calls[action.op] = self.calls.get(action.op, 0) + 1
        script = self.scripts.get(action.op)
        if script:
            return script.pop(0)
        if action.op == "wait":
            return Ok({"ok": True, "changed": False, "held": False, "daemonEpoch": "e1"})
        if action.op == "list":
            return Ok({"ok": True, "messages": [], "daemonEpoch": "e1"})
        return Ok({"ok": True, "daemonEpoch": "e1"})

    async def apply(self, change: Any, response: dict[str, Any]) -> None:
        pass

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, Any]:
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        self.stopped = True
        return {"ok": True}


# --------------------------------------------------------------------------- #
# 4. End-to-end over a real socket: two live loops, the loser cannot steal
# --------------------------------------------------------------------------- #
class _Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FakeSupersedeDaemon:
    """A real-socket daemon with last-writer-wins register and a supersede fence.

    Mirrors the real ``hyprial`` daemon closely enough to drive the production
    ``unix.py`` error-envelope path: ``message.pending.list`` from a session that
    no longer owns the actor comes back as a ``SESSION_SUPERSEDED`` envelope,
    which ``unix.py`` raises as ``DaemonRequestRejected``.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = Path(socket_path)
        self.current: dict[str, str] = {}
        self.register_counts: dict[str, int] = {}
        self.methods: list[str] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(64)
        self._sock.settimeout(0.05)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.socket_path.unlink(missing_ok=True)

    def set_owner(self, actor: str, session_ref: str) -> None:
        with self._lock:
            self.current[actor] = session_ref

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer.extend(chunk)
            request = json.loads(buffer.partition(b"\n")[0])
            response = self._handle(request)
            conn.sendall(json.dumps(response).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            with self._lock:
                result = self._dispatch(
                    str(request.get("method")), request.get("params") or {}
                )
        except _Reject as reject:
            return {
                "version": 1,
                "id": request_id,
                "error": {"code": reject.code, "message": reject.message},
            }
        return {"version": 1, "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        actor = str(params.get("actor"))
        ref = str(params.get("sessionRef"))
        self.methods.append(method)
        if method == "session.register":
            self.current[actor] = ref
            self.register_counts[ref] = self.register_counts.get(ref, 0) + 1
            return {"ok": True, "registered": True, "actor": actor, "sessionRef": ref}
        if method == "session.unregister":
            if self.current.get(actor) == ref:
                del self.current[actor]
            return {"ok": True, "unregistered": True, "actor": actor}
        if method == "session.refresh":
            owner = self.current.get(actor)
            if owner != ref:
                if owner is None:
                    raise _Reject("STALE_SESSION", f"no session owns {actor}")
                raise _Reject(
                    SESSION_SUPERSEDED_CODE,
                    f"{actor} now owned by {owner}, not {ref}",
                )
            return {"ok": True, "refreshed": True, "actor": actor}
        if method in {"session.heartbeat", "message.pending.list"}:
            owner = self.current.get(actor)
            if owner != ref:
                if owner is None:
                    raise _Reject("STALE_SESSION", f"no session owns {actor}")
                raise _Reject(
                    SESSION_SUPERSEDED_CODE,
                    f"{actor} now owned by {owner}, not {ref}",
                )
            return (
                {"ok": True, "alive": True, "actor": actor}
                if method == "session.heartbeat"
                else {"ok": True, "messages": []}
            )
        raise _Reject("METHOD_NOT_FOUND", method)


@pytest.fixture
def sock_path() -> Iterator[Path]:
    # AF_UNIX paths are length-capped; pytest tmp_path is too long on macOS.
    directory = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprialsup"))
    try:
        yield directory / "d.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)




# --------------------------------------------------------------------------- #
# 5. Parent-death (orphan) teardown via getppid drift
# --------------------------------------------------------------------------- #




























def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # In minimal Linux containers pid 1 may not reap an orphan immediately.
    # ``kill(pid, 0)`` still succeeds for that exited zombie, which used to make
    # this process-level contract report a live channel even after os._exit.
    # POSIX ``ps`` exposes the same state on Linux and macOS; zombies have
    # already stopped executing and count as exited for this assertion.
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )
    if status.returncode != 0:
        return False
    process_state = status.stdout.strip()
    return bool(process_state) and not process_state.startswith("Z")




# --------------------------------------------------------------------------- #
# 6. Anti-regression: supersede alone (parent alive) must NOT tear the child down
# --------------------------------------------------------------------------- #


# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FakeSupersedeDaemon',
    '_FakeInbox',
    '_FakeStore',
    '_RecordingChannel',
    '_Reject',
    '_ScriptedAdapter',
    '_adapter_for',
    '_daemon_app',
    '_pid_alive',
    '_register',
    '_until',
    '_until_async',
    'pytestmark',
    'sock_path',
]
