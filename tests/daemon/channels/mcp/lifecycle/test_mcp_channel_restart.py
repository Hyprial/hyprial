"""Daemon-restart resilience for the Claude Code stdio Channel.

The coordinator's Harness channel is a stdio MCP child that Claude Code spawns.
A daemon restart (the deploy path) drops the child's Unix socket to the daemon
but never the Claude Code <-> child stdio transport. Before this change the poll
loop raised on the dropped socket, killing the child; Claude Code cannot respawn
a dead stdio child, so only a full session restart recovered it. These tests
pin that a daemon restart mid-session now recovers with NO session restart:
the child stays alive, reconnects to the new daemon generation, re-registers,
and read/reply/ack return the new generation's truth.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.inbox_watch import Heartbeat, InboxWatch, Ok, Rejected, Unreachable
from hyprial.daemon.impl.mcp import (
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    DaemonDisconnected,
    RuntimeKind,
    StatelessDaemonProxy,
    UnixDaemonConnectionFactory,
    WakeCoordinator,
    run_channel_session,
)
from hyprial.daemon.impl.mcp.channel.loops import (
    _run_channel_daemon_loops,
    _run_channel_poll_loop,
)
from hyprial.daemon.impl.mcp.channel import session as channel_session


class FakeDaemon:
    """One daemon generation on a real Unix socket.

    Speaks the same newline-framed v1 IPC envelope as the real daemon (see
    ``hyprial.mcp.unix``). A restart is stopping one generation and starting another
    on the same socket path with fresh ``deliveryId``s and an empty session
    registry -- exactly what a real ``hyprial`` daemon restart looks like to the
    stdio child.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        pending: list[dict[str, str]],
        doorbell: bool = False,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.pending = [dict(item) for item in pending]
        # Off by default: answer message.pending.wait with METHOD_NOT_FOUND
        # (an older daemon).  On: hold it like the real daemon, at most 2 s.
        self.doorbell = doorbell
        # Like the real daemon (application.py: pending list and session
        # responses carry ``daemonEpoch``): each generation has its own
        # epoch, which is how a channel notices a restart that happened
        # between two of its polls.
        self.epoch = uuid.uuid4().hex
        self.methods: list[str] = []
        self.acked: list[str] = []
        self._batch_members: dict[str, tuple[str, ...]] = {}  # cursor -> members
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
        method = request.get("method")
        params = request.get("params") or {}
        if method == "message.pending.wait" and self.doorbell:
            with self._lock:
                self.methods.append(str(method))
            # Held outside the lock, so heartbeats and lists keep flowing.
            return {"version": 1, "id": request.get("id"), "result": self._hold(params)}
        with self._lock:
            self.methods.append(str(method))
            result = self._dispatch(str(method), params)
        if result is None:
            return {
                "version": 1,
                "id": request.get("id"),
                "error": {"code": "METHOD_NOT_FOUND", "message": method},
            }
        return {"version": 1, "id": request.get("id"), "result": result}

    def _hold(self, params: dict[str, Any]) -> dict[str, Any]:
        known = set(params.get("knownMessageIds") or ())
        deadline = time.monotonic() + min(2.0, (params.get("holdMs") or 5_000) / 1000)
        held = False
        while True:
            with self._lock:
                unseen = any(item["messageId"] not in known for item in self.pending)
            if unseen or time.monotonic() >= deadline or self._stop.is_set():
                return {"ok": True, "changed": unseen, "held": held, "daemonEpoch": self.epoch}
            held = True
            time.sleep(0.01)

    def add_pending(self, item: dict[str, str]) -> None:
        with self._lock:
            self.pending.append(dict(item))

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "session.register":
            return {
                "ok": True,
                "registered": True,
                "actor": params.get("actor"),
                "sessionRef": params.get("sessionRef"),
                "daemonEpoch": self.epoch,
            }
        if method == "session.refresh":
            return {
                "ok": True,
                "refreshed": True,
                "actor": params.get("actor"),
                "sessionRef": params.get("sessionRef"),
                "daemonEpoch": self.epoch,
            }
        if method == "session.heartbeat":
            return {
                "ok": True,
                "alive": True,
                "actor": params.get("actor"),
                "sessionRef": params.get("sessionRef"),
                "daemonEpoch": self.epoch,
            }
        if method == "session.unregister":
            return {"ok": True, "unregistered": True, "actor": params.get("actor")}
        if method in {"session.turn.started", "session.turn.ended"}:
            return {"ok": True}
        if method == "message.pending.list":
            return {
                "ok": True,
                "messages": [dict(item) for item in self.pending],
                "daemonEpoch": self.epoch,
            }
        if method == "message.pending.batch":
            requested = params.get("cursor")
            if isinstance(requested, str):
                admitted = set(self._batch_members.get(requested, ()))
                items = [
                    dict(item)
                    for item in self.pending
                    if item["messageId"] in admitted
                ]
                cursor = requested
            else:
                items = [dict(item) for item in self.pending]
                cursor = (
                    str(items[0].get("deliveryId"))
                    if items
                    else f"batch-{self.epoch[:8]}"
                )
                self._batch_members[cursor] = tuple(
                    str(item["messageId"]) for item in items
                )
            text = (
                f"[Fake inbox batch cursor={cursor}]\n"
                + "\n".join(str(item["messageId"]) for item in items)
                if items
                else ""
            )
            return {
                "ok": True, "schemaVersion": 1, "format": "inbox-batch-v1",
                "cursor": cursor, "daemonEpoch": self.epoch,
                "actor": params.get("actor"), "itemCount": len(items),
                "totalBodyBytes": 0, "inlineLimitBytes": 16384, "items": items,
                "delivery": {
                    "mode": "inline", "text": text,
                    "spillPath": None, "spillBytes": None, "spillSha256": None,
                },
            }
        if method in {"message.reply", "message.ack"}:
            message_id = params.get("messageId")
            self.acked.append(str(message_id))
            self.pending = [
                item for item in self.pending if item["messageId"] != message_id
            ]
            return {"ok": True, "acknowledged": True, "messageId": message_id}
        return None


@dataclass
class _FakeStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class _RecordingChannel:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


#: How long a background step (a poll, a refresh, a reconnect) may take before
#: we call it hung.  It bounds a HANG, ⛔ not the machine: every predicate in
#: this module waits for something to APPEAR, so a generous budget can only make
#: a real failure slower to report -- it can never make a passing run fail.
#: 5.0s was a bet on scheduling latency, and a throttled 2-CPU runner collects
#: on it (task 16430: "condition was not met within the timeout" while the whole
#: suite ran 50% slower than baseline).
BACKGROUND_WAIT_SECONDS = 30.0


async def _until(
    predicate: Any,
    *,
    timeout: float = BACKGROUND_WAIT_SECONDS,
    interval: float = 0.02,
    diagnostic: Callable[[], str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await anyio.sleep(interval)
    detail = f"; {diagnostic()}" if diagnostic is not None else ""
    raise AssertionError(f"condition was not met within the timeout{detail}")


async def _until_async(
    predicate: Any, *, timeout: float = BACKGROUND_WAIT_SECONDS, interval: float = 0.05
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await anyio.sleep(interval)
    raise AssertionError("async condition was not met within the timeout")


@pytest.fixture
def sock_path() -> Iterator[Path]:
    # AF_UNIX paths are length-capped (~104 bytes on macOS); pytest's tmp_path is
    # too long, so bind under a short private temp dir instead.
    directory = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprialmcp"))
    try:
        yield directory / "d.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _interactive() -> InteractiveSession:
    return InteractiveSession(
        actor="coordinator",
        cwd="/work/coordinator",
        command=("claude",),
        source="claude-channel",
        session_ref="session-1",
        runtime=RuntimeKind.CLAUDE_INTERACTIVE,
        channel_confirmed=True,
    )


@pytest.mark.anyio
async def test_poll_loop_recovers_and_reregisters_across_daemon_restart(
    sock_path: Path,
) -> None:
    socket_path = sock_path
    gen1 = FakeDaemon(
        socket_path, pending=[{"messageId": "m1", "deliveryId": "gen1-d1"}]
    )
    gen1.start()
    channel = _RecordingChannel()
    coordinator = WakeCoordinator(
        _FakeStore(DesiredState(interactive_sessions=(_interactive(),))),
        ClaudeChannelDriver(channel),
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(UnixDaemonConnectionFactory(socket_path)),
        coordinator,
        actor="coordinator",
        session_ref="session-1",
        cwd="/work/coordinator",
        command=("claude",),
    )
    errors: list[BaseException] = []
    gen2 = FakeDaemon(
        socket_path, pending=[{"messageId": "m2", "deliveryId": "gen2-d2"}]
    )
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                lambda: _run_channel_poll_loop(
                    adapter,
                    poll_interval=0.02,
                    on_error=lambda error, _n: errors.append(error),
                )
            )

            # Generation 1: registered, tracking its delivery key, and the wake
            # edge fired for it.
            await _until(lambda: "session.register" in gen1.methods)
            await _until(
                lambda: coordinator.delivery_state("coordinator", "gen1-d1") is not None
            )
            await _until(
                lambda: any(
                    params["meta"]["delivery_id"] == "gen1-d1"
                    for _method, params in channel.notifications
                )
            )

            # Daemon restart. Leave a real gap so the loop actually polls the
            # dropped socket and takes the DaemonDisconnected path.
            gen1.stop()
            await anyio.sleep(0.15)
            assert errors, "poll loop should observe the daemon disconnect"
            assert all(
                isinstance(error, DaemonDisconnected | RuntimeError | OSError)
                for error in errors
            )

            gen2.start()

            # Generation 2: the SAME live loop uses a ref-guarded refresh
            # (never last-writer registration) and prunes the stale wake key.
            await _until(lambda: "session.refresh" in gen2.methods)
            assert "session.register" not in gen2.methods
            await _until(
                lambda: coordinator.delivery_state("coordinator", "gen2-d2") is not None
            )
            await _until(
                lambda: coordinator.delivery_state("coordinator", "gen1-d1") is None
            )
            assert coordinator.pending_count == 1

            # Functional recovery, not just RPC reachability: the wake edge fires
            # for the NEW generation's delivery. This depends on poll_once pruning
            # the stale gen-1 head before dispatch_due, so the new delivery is not
            # stuck behind a SIGNALLED gen-1 key with a future not_before -- an
            # ordering regression here would leave the coordinator silent.
            await _until(
                lambda: any(
                    params["meta"]["delivery_id"] == "gen2-d2"
                    for _method, params in channel.notifications
                )
            )

            # reply/ack keyed on the durable messageId succeeds against gen 2 and
            # clears the wake key -- no stale-map ghost blocks the inbox.
            result = await adapter.harness_ack("m2")
            assert result["acknowledged"] is True
            assert "m2" in gen2.acked
            await _until(lambda: coordinator.pending_count == 0)

            tasks.cancel_scope.cancel()
    finally:
        gen2.stop()
        gen1.stop()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "restart_gap",
    [
        pytest.param(0.15, id="outage-observed"),
        # The restart lands between two background polls, so the loop never
        # sees a contact error.  Only the daemonEpoch change can tell it the
        # daemon is a new generation.  Before FakeDaemon carried an epoch this
        # failed 6/6 locally -- and the 0.15s case failed the same way on CI
        # whenever one slow poll spanned the whole gap (tasks 15512 and
        # earlier: "condition was not met within the timeout").
        pytest.param(0.0, id="restart-between-polls"),
    ],
)
async def test_held_open_client_session_survives_daemon_restart(
    sock_path: Path, restart_gap: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = sock_path
    gen1 = FakeDaemon(
        socket_path, pending=[{"messageId": "m1", "deliveryId": "gen1-d1"}]
    )
    gen1.start()
    proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(socket_path))
    gen2 = FakeDaemon(
        socket_path, pending=[{"messageId": "m2", "deliveryId": "gen2-d2"}]
    )
    adapters: list[ClaudeChannelAdapter] = []
    real_adapter = channel_session.ClaudeChannelAdapter

    def capture_adapter(*args: Any, **kwargs: Any) -> ClaudeChannelAdapter:
        adapter = real_adapter(*args, **kwargs)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(channel_session, "ClaudeChannelAdapter", capture_adapter)
    try:
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    lambda: run_channel_session(
                        server_read,
                        server_write,
                        proxy,
                        actor="coordinator",
                        session_ref="session-1",
                        cwd="/work/coordinator",
                        command=("claude",),
                        poll_interval=0.02,
                        on_error=lambda _error, _n: None,
                        # This in-process test exercises daemon reconnect, not
                        # orphan reaping.  In a container the pytest/uv process
                        # may be PID 1, whose ppid is 0; using the production
                        # watcher default would os._exit(0) the whole test run
                        # before pytest can report a result or write JUnit.
                        getppid=lambda: 4242,
                    )
                )
                async with ClientSession(client_read, client_write) as client:
                    await client.initialize()

                    # Generation 1 truth over the freshly initialized session.
                    first = await client.call_tool("harness_read", {})
                    assert first.is_error is not True
                    assert first.structured_content["items"][0]["messageId"] == "m1"
                    # A held-open restart presupposes that generation 1 owns the
                    # session.  Without this fence the MCP tool can win startup
                    # scheduling and the first registration correctly lands on
                    # generation 2, for which no refresh is owed.
                    await _until(lambda: "session.register" in gen1.methods)

                    # Daemon restart underneath the SAME held-open session.
                    gen1.stop()
                    await anyio.sleep(restart_gap)
                    gen2.start()

                    # No client.initialize() again, no /mcp reconnect: the same
                    # session now reads generation-2 truth from the new daemon.
                    async def read_is_generation_two() -> bool:
                        response = await client.call_tool("harness_read", {})
                        if response.is_error:
                            return False
                        items = response.structured_content.get("items") or []
                        return bool(items) and items[0]["messageId"] == "m2"

                    await _until_async(read_is_generation_two)

                    # The background channel confirms its persisted ref without
                    # invoking last-writer registration on the new daemon.
                    def refresh_diagnostic() -> str:
                        if not adapters:
                            return (
                                f"gen2_methods={list(gen2.methods)!r}; "
                                "adapter=not-constructed"
                            )
                        watch = adapters[0].watch
                        return (
                            f"gen2_methods={list(gen2.methods)!r}; "
                            f"adapter_daemon_epoch={watch.daemon_epoch!r}; "
                            f"adapter_registered={watch.registered!r}; "
                            f"adapter_refresh_owed={watch.refresh_owed!r}"
                        )

                    await _until(
                        lambda: "session.refresh" in gen2.methods,
                        diagnostic=refresh_diagnostic,
                    )
                    assert "session.register" not in gen2.methods

                    # ack keyed on the durable messageId flows to the new daemon.
                    acked = await client.call_tool("harness_ack", {"messageId": "m2"})
                    assert acked.is_error is not True
                    assert acked.structured_content.get("acknowledged") is True
                    assert "m2" in gen2.acked

                tasks.cancel_scope.cancel()
    finally:
        gen2.stop()
        gen1.stop()


class _ScriptedAdapter:
    """The adapter surface the loops drive: registration scripted to fail
    then recover; lists and waits answer an idle inbox (no doorbell)."""

    actor = "coordinator"
    session_ref = "ref"
    open_turn: str | None = None
    owed_settle: tuple[str, ...] = ()

    def __init__(self, register_script: list[Any]) -> None:
        self.watch = InboxWatch(poll_interval_ms=10, heartbeat_interval_ms=1_000)
        self._register_script = list(register_script)
        self.starts = 0
        self.polls = 0
        self.refreshes = 0
        self.stopped = False

    @property
    def forced_polls(self) -> int:
        """Lists that carried a recovery re-wake."""

        return self.watch.counters.get("rewake.recovery", 0)

    async def perform(self, action: Any) -> Any:
        await anyio.sleep(0)
        if action.op == "register":
            self.starts += 1
            if self._register_script:
                return self._register_script.pop(0)
            return Ok({"ok": True, "daemonEpoch": "e1"})
        if action.op == "refresh":
            self.refreshes += 1
            return Ok({"ok": True, "daemonEpoch": "e1"})
        if action.op == "list":
            self.polls += 1
            return Ok({"ok": True, "messages": [], "daemonEpoch": "e1"})
        if action.op == "wait":
            return Rejected("METHOD_NOT_FOUND")
        return Ok({"ok": True, "daemonEpoch": "e1"})

    async def apply(self, change: Any, response: dict[str, Any]) -> None:
        pass

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, object]:
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        self.stopped = True
        return {"ok": True}


class _BackpressuredAdapter:
    """The first list blocks four seconds in a slow stdio notification."""

    actor = "coordinator"
    session_ref = "ref"
    open_turn: str | None = None
    owed_settle: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.watch = InboxWatch(poll_interval_ms=5_000, heartbeat_interval_ms=250)
        self.poll_entered = anyio.Event()
        self.heartbeats = 0

    async def perform(self, action: Any) -> Any:
        if action.op == "heartbeat":
            self.heartbeats += 1
            return Ok({"ok": True, "alive": True, "daemonEpoch": "e1"})
        if action.op == "list":
            # Four seconds represents notifier backpressure. poll_interval=5
            # below must not become the operational-liveness cadence.
            self.poll_entered.set()
            await anyio.sleep(4.0)
            return Ok({"ok": True, "messages": [], "daemonEpoch": "e1"})
        if action.op == "wait":
            return Rejected("METHOD_NOT_FOUND")
        return Ok({"ok": True, "daemonEpoch": "e1"})

    async def apply(self, change: Any, response: dict[str, Any]) -> None:
        pass

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, object]:
        return {"ok": True}


@pytest.mark.anyio
async def test_heartbeat_is_independent_of_five_second_poll_and_stdio_backpressure() -> (
    None
):
    adapter = _BackpressuredAdapter()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_daemon_loops(
                adapter,  # type: ignore[arg-type]
                poll_interval=5.0,
                heartbeat_interval=0.25,
            )
        )
        with anyio.fail_after(5.0):
            await adapter.poll_entered.wait()
            await anyio.sleep(4.1)
        assert adapter.heartbeats >= 12
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_poll_loop_survives_error_envelope_and_disconnect_without_crashing() -> (
    None
):
    # A daemon error envelope surfaces as RuntimeError (mcp.unix); a dropped
    # socket surfaces as DaemonDisconnected. Neither may crash the stdio child.
    adapter = _ScriptedAdapter([Rejected("DAEMON_BOOTING"), Unreachable()])
    errors: list[BaseException] = []
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,  # type: ignore[arg-type]
                poll_interval=0.01,
                on_error=lambda error, _n: errors.append(error),
            )
        )
        # It kept retrying registration through both failures rather than dying.
        await _until(lambda: adapter.starts >= 3)
        assert any(isinstance(error, RuntimeError) for error in errors)
        assert any(isinstance(error, DaemonDisconnected) for error in errors)
        # After the scripted failures, start() succeeds and the loop begins
        # polling -- proof the task is still alive.
        await _until(lambda: adapter.polls >= 1)
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lifecycle_recovery_pulse_forces_poll_without_new_delivery(
    tmp_path: Path,
) -> None:
    adapter = _ScriptedAdapter([])
    recovery_signal = tmp_path / "session.recover"
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,  # type: ignore[arg-type]
                poll_interval=0.01,
                recovery_signal=recovery_signal,
            )
        )
        await _until(lambda: adapter.polls >= 1)

        from hyprial.daemon.impl.mcp.channel.loops import signal_channel_recovery

        signal_channel_recovery(recovery_signal)
        await _until(lambda: adapter.forced_polls == 1)

        assert not recovery_signal.exists()
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_turn_pulses_are_forwarded_then_removed(tmp_path: Path) -> None:
    class TurnAdapter(_ScriptedAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.turn_reports: list[tuple[str, ...]] = []

        async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, object]:
            self.turn_reports.append(report_ids)
            return {"ok": True}

    adapter = TurnAdapter()
    turn_signal_dir = tmp_path / "session.turns"
    from hyprial.biz.impl.turn_pulse import signal_turn_ended

    signal_turn_ended(turn_signal_dir)
    signal_turn_ended(turn_signal_dir)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,  # type: ignore[arg-type]
                poll_interval=0.01,
                turn_signal_dir=turn_signal_dir,
            )
        )
        await _until(lambda: len(adapter.turn_reports) == 1)

        assert len(adapter.turn_reports[0]) == 2
        assert not tuple(turn_signal_dir.iterdir())
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_rejected_turn_report_keeps_pulses_and_never_degrades_delivery(
    tmp_path: Path,
) -> None:
    """A daemon that rejects session.turn.ended (older daemon, runtime
    mismatch) must not flip the poll loop into its failure/refresh path:
    delivery polling continues unforced, and the pulses wait on disk."""

    from hyprial.daemon.impl.mcp.api import DaemonRequestRejected

    class RejectingTurnAdapter(_ScriptedAdapter):
        actor = "coordinator"

        def __init__(self) -> None:
            super().__init__([])
            self.report_attempts = 0
            self.accept_after = 3

        async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, object]:
            self.report_attempts += 1
            if self.report_attempts < self.accept_after:
                raise DaemonRequestRejected("INVALID_ARGUMENT", "unknown method")
            return {"ok": True}

    adapter = RejectingTurnAdapter()
    turn_signal_dir = tmp_path / "session.turns"
    from hyprial.biz.impl.turn_pulse import signal_turn_ended

    signal_turn_ended(turn_signal_dir)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,  # type: ignore[arg-type]
                poll_interval=0.01,
                turn_signal_dir=turn_signal_dir,
            )
        )
        await _until(lambda: adapter.report_attempts >= 2)
        # Rejected: the pulse is still there, and no refresh or forced
        # re-wake ran -- telemetry never touches the delivery path.
        assert len(tuple(turn_signal_dir.iterdir())) == 1
        assert adapter.forced_polls == 0
        assert adapter.refreshes == 0
        await _until(lambda: adapter.report_attempts >= 3)
        await _until(lambda: not tuple(turn_signal_dir.iterdir()))
        assert adapter.forced_polls == 0
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_superseded_turn_report_still_ends_the_channel(tmp_path: Path) -> None:
    from hyprial.daemon.impl.mcp.api import SESSION_SUPERSEDED_CODE, DaemonRequestRejected

    class SupersededTurnAdapter(_ScriptedAdapter):
        actor = "coordinator"

        async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, object]:
            raise DaemonRequestRejected(SESSION_SUPERSEDED_CODE, "newer session")

    adapter = SupersededTurnAdapter([])
    turn_signal_dir = tmp_path / "session.turns"
    from hyprial.biz.impl.turn_pulse import signal_turn_ended

    signal_turn_ended(turn_signal_dir)
    with anyio.fail_after(5):
        await _run_channel_poll_loop(
            adapter,  # type: ignore[arg-type]
            poll_interval=0.01,
            turn_signal_dir=turn_signal_dir,
        )


@pytest.mark.anyio
async def test_a_heartbeat_must_not_swallow_the_generation_change(
    sock_path: Path,
) -> None:
    """The restart signal has two observers; only one of them acts on it.

    ``_read_daemon`` (the poll loop) compares the daemon epoch and issues
    ``session.refresh`` when it changed.  ``heartbeat`` -- a SIBLING task,
    running concurrently -- only ``_remember_daemon_epoch``: it overwrites
    the stored epoch and tells nobody.  So whichever observer reaches the
    new daemon first decides whether the refresh ever happens, and when the
    heartbeat wins, the poll sees "no change" and the session keeps talking
    to a generation it never confirmed.

    This test fixes the order instead of racing for it: heartbeat first,
    then poll.  ⛔ No sleeps, no budgets -- the failure this reproduces is
    "the event never occurs", which no timeout can distinguish from "slow"
    (CI tasks 16430, 16581, 16644: the wait reached the full 30s).
    """

    socket_path = sock_path
    gen1 = FakeDaemon(socket_path, pending=[])
    gen1.start()
    coordinator = WakeCoordinator(
        _FakeStore(DesiredState(interactive_sessions=(_interactive(),))),
        ClaudeChannelDriver(_RecordingChannel()),
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(UnixDaemonConnectionFactory(socket_path)),
        coordinator,
        actor="coordinator",
        session_ref="session-1",
        cwd="/work/coordinator",
        command=("claude",),
    )
    gen2 = FakeDaemon(socket_path, pending=[])
    try:
        await adapter.start()
        assert "session.register" in gen1.methods

        gen1.stop()
        gen2.start()

        # The heartbeat observes the new generation FIRST.
        await _heartbeat_step(adapter)
        # ...and then the poll loop's read happens.
        await adapter.poll_once()

        assert "session.refresh" in gen2.methods, (
            "the generation change was observed by the heartbeat and never "
            "acted on: the poll then saw no change"
        )

        # ONE generation, ONE confirmation.  Both observers act on the change
        # now, so the check and the act have to be a single step -- otherwise
        # two of them pass "changed?" and the daemon is asked to confirm the
        # same session twice for one restart.
        before = gen2.methods.count("session.refresh")
        await _heartbeat_step(adapter)
        await adapter.poll_once()
        assert gen2.methods.count("session.refresh") == before == 1
    finally:
        gen2.stop()
        gen1.stop()


async def _heartbeat_step(adapter: ClaudeChannelAdapter) -> None:
    """One heartbeat-lane request, performed now (no waiting out its pace)."""

    _HEARTBEAT_CLOCK[0] += 10**9  # always past the lane's next due time
    now = _HEARTBEAT_CLOCK[0]
    action = adapter.watch.heartbeat_next(now)
    assert isinstance(action, Heartbeat), action
    adapter.watch.heartbeat_done(action, await adapter.perform(action), now)


_HEARTBEAT_CLOCK = [10**12]


# -- inbox-watch at the harness layer (contract/inbox-watch G12, G8) ---------


def _wakes_for(channel: _RecordingChannel, delivery_id: str) -> int:
    return sum(
        1
        for _method, params in channel.notifications
        if params["meta"]["delivery_id"] == delivery_id
    )


def _channel_adapter(socket_path: Path, channel: _RecordingChannel) -> ClaudeChannelAdapter:
    return ClaudeChannelAdapter(
        StatelessDaemonProxy(UnixDaemonConnectionFactory(socket_path)),
        WakeCoordinator(
            _FakeStore(DesiredState(interactive_sessions=(_interactive(),))),
            ClaudeChannelDriver(channel),
        ),
        actor="coordinator",
        session_ref="session-1",
        cwd="/work/coordinator",
        command=("claude",),
    )


@pytest.mark.anyio
async def test_a_reconnecting_child_wakes_an_existing_backlog_at_once(
    sock_path: Path,
) -> None:
    # 2026-10-05 21:01: after /mcp reconnect a backlog of five woke nothing
    # for minutes.  A fresh child must wake what is already pending on its
    # first list -- well inside one doorbell hold plus one poll interval.
    daemon = FakeDaemon(sock_path, pending=[{"messageId": "m1", "deliveryId": "d1"}])
    daemon.start()
    channel = _RecordingChannel()
    adapter = _channel_adapter(sock_path, channel)
    try:
        started = time.monotonic()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                lambda: _run_channel_daemon_loops(
                    adapter, poll_interval=0.5, heartbeat_interval=1.0
                )
            )
            await _until(lambda: _wakes_for(channel, "d1") == 1, timeout=5.5)
            assert time.monotonic() - started < 5.5
            tasks.cancel_scope.cancel()
        assert adapter.watch.counters["first_wake_ms_after_connect"] < 5_500
    finally:
        daemon.stop()


@pytest.mark.anyio
async def test_a_restart_seen_first_by_the_heartbeat_rewakes_the_signalled_backlog(
    sock_path: Path,
) -> None:
    # The old Channel heartbeat refreshed on a new epoch but dropped the
    # "re-wake owed" result, so a backlog already SIGNALLED to the previous
    # daemon waited out its 30 s+ re-notify delay.  The same delivery id on
    # both generations isolates that: only a re-wake can notify it again.
    pending = [{"messageId": "m1", "deliveryId": "d1"}]
    gen1 = FakeDaemon(sock_path, pending=pending)
    gen1.start()
    channel = _RecordingChannel()
    adapter = _channel_adapter(sock_path, channel)
    gen2 = FakeDaemon(sock_path, pending=pending)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                lambda: _run_channel_daemon_loops(
                    adapter, poll_interval=1.5, heartbeat_interval=0.05
                )
            )
            await _until(lambda: _wakes_for(channel, "d1") == 1)
            gen1.stop()
            gen2.start()
            # Heartbeats run every 50 ms and polls every 1.5 s, so the
            # heartbeat lane sees the new epoch first.
            await _until(lambda: _wakes_for(channel, "d1") == 2, timeout=4.0)
            assert "session.register" not in gen2.methods
            # Two lanes saw the new epoch; the core let exactly one refresh.
            assert gen2.methods.count("session.refresh") == 1
            tasks.cancel_scope.cancel()
    finally:
        gen2.stop()
        gen1.stop()


@pytest.mark.anyio
async def test_a_message_arriving_during_a_held_wait_wakes_before_the_hold_ends(
    sock_path: Path,
) -> None:
    # G12 under a real hold: the child is parked on message.pending.wait, and
    # a new message must end that hold, not wait it out (2 s here).
    daemon = FakeDaemon(sock_path, pending=[], doorbell=True)
    daemon.start()
    channel = _RecordingChannel()
    adapter = _channel_adapter(sock_path, channel)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                lambda: _run_channel_daemon_loops(
                    adapter, poll_interval=0.5, heartbeat_interval=1.0
                )
            )
            await _until(lambda: "message.pending.wait" in daemon.methods)
            await anyio.sleep(0.2)  # well inside the 2 s hold
            arrived = time.monotonic()
            daemon.add_pending({"messageId": "m1", "deliveryId": "d1"})
            await _until(lambda: _wakes_for(channel, "d1") == 1, timeout=2.0)
            assert time.monotonic() - arrived < 1.5
            tasks.cancel_scope.cancel()
    finally:
        daemon.stop()
