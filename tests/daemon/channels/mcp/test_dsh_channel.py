"""dsh-tui carrier tests: plugin control frames and session registration."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest

from hyprial.daemon.impl.harnesses.dsh_tui import (
    DshCarrierAdapter,
    DshPluginWakeDriver,
    PLUGIN_SOURCE,
    process_identity_for_tui,
    run_dsh_carrier,
    write_dsh_tui_plugin,
)
from hyprial.daemon.impl.inbox_watch import Heartbeat, Ok, Refresh, Register
from hyprial.daemon.impl.mcp.wake import WakeCommand, WakeStatus


def _wake_command(prompt: str = "Call harness_read once") -> WakeCommand:
    return WakeCommand(
        kind="formal",
        actor="agent:op:node:dsh",
        delivery_id="delivery-1",
        cwd="/tmp/work",
        command=("dsh-tui",),
        prompt=prompt,
    )


def _short_private_dir() -> Path:
    directory = Path("/tmp") / f"dshp-{uuid4().hex[:12]}"
    directory.mkdir(mode=0o700)
    return directory

def test_plugin_driver_ping_and_wake_use_only_formal_prompt() -> None:
    directory = _short_private_dir()
    socket_path = directory / "control.sock"
    received: list[str] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(4)
    try:
        def serve() -> None:
            for _ in range(2):
                connection, _address = server.accept()
                with connection:
                    data = b""
                    while b"\n" not in data:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    frame = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
                    if frame["type"] == "ping":
                        response = {
                            "ok": True,
                            "ready": True,
                            "sessionId": "session-1",
                        }
                    else:
                        received.append(frame["prompt"])
                        response = {"ok": True, "sessionId": "session-1"}
                    connection.sendall(
                        json.dumps(response).encode("utf-8") + b"\n"
                    )

        thread = threading.Thread(target=serve)
        thread.start()
        driver = DshPluginWakeDriver(socket_path)
        ready_session = anyio.run(driver.wait_ready, 1.0)
        attempt = anyio.run(driver.wake, _wake_command())
        thread.join(5.0)
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        directory.rmdir()

    assert ready_session == "session-1"
    assert attempt.status is WakeStatus.ACCEPTED
    # The daemon renderer is byte-authoritative: no local prefix is added.
    assert received == ["Call harness_read once"]
    assert "summary" not in received[0].lower()
    assert "body" not in received[0].lower()


def test_plugin_driver_without_socket_is_offline(tmp_path: Path) -> None:
    driver = DshPluginWakeDriver(tmp_path / "missing.sock")
    attempt = anyio.run(driver.wake, _wake_command())
    assert attempt.status is WakeStatus.OFFLINE

def test_carrier_registration_reports_the_tui_process_fence() -> None:
    class Proxy:
        def __init__(self) -> None:
            self.request: dict[str, Any] = {}

        async def call(self, **kwargs: Any) -> dict[str, Any]:
            self.request = kwargs
            return {"registered": True}

    class Coordinator:
        pass

    proxy = Proxy()
    adapter = DshCarrierAdapter(
        proxy,  # type: ignore[arg-type]
        Coordinator(),  # type: ignore[arg-type]
        actor="agent:op:node:dsh",
        session_ref="session-1",
        cwd="/tmp/work",
        command=("dsh-tui",),
        tui_pid=4242,
        tui_identity="4242:stamp",
    )
    result = anyio.run(adapter._register)

    assert result == {"registered": True}
    assert proxy.request["method"] == "session.register"
    params = proxy.request["params"]
    assert params["source"] == "dsh-channel"
    assert params["runtime"] == "dsh_interactive"
    assert params["processPid"] == 4242
    assert params["processIdentity"] == "4242:stamp"
    assert params["ownerFence"] is True
    assert params["turnReporting"] is False


def test_dsh_carrier_wire_sends_no_channel_lease_token() -> None:
    """The dsh carrier holds no channel lease, on every request.

    ``session.register`` omits channelLeaseToken because a dsh-channel
    session may not carry one (the daemon stores a None digest for it).
    The inherited channel refresh and heartbeat must not quietly send the
    base class's random token either: today the daemon only skips lease
    verification while that digest is None, so a random token would break
    dsh liveness the moment the daemon enforces the digest. The wire stays
    lease-free so the adapter matches the daemon contract it relies on.
    """

    class Proxy:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"registered": True}

    class Coordinator:
        pass

    proxy = Proxy()
    adapter = DshCarrierAdapter(
        proxy,  # type: ignore[arg-type]
        Coordinator(),  # type: ignore[arg-type]
        actor="agent:op:node:dsh",
        session_ref="session-1",
        cwd="/tmp/work",
        command=("dsh-tui",),
        tui_pid=4242,
        tui_identity="4242:stamp",
    )

    async def scenario() -> None:
        register = await adapter.perform(Register())
        refresh = await adapter.perform(Refresh())
        heartbeat = await adapter.perform(Heartbeat())
        assert isinstance(register, Ok)
        assert isinstance(refresh, Ok)
        assert isinstance(heartbeat, Ok)

    anyio.run(scenario)

    by_method = {call["method"]: call for call in proxy.calls}
    assert set(by_method) == {
        "session.register",
        "session.refresh",
        "session.heartbeat",
    }
    for call in proxy.calls:
        assert "channelLeaseToken" not in call["params"], call["method"]


def test_plugin_source_uses_agent_followup_not_input_submit(tmp_path: Path) -> None:
    plugin_path = write_dsh_tui_plugin(tmp_path)
    source = plugin_path.read_text(encoding="utf-8")

    assert plugin_path.name == "hyprial-bridge-plugin.mjs"
    assert plugin_path.stat().st_mode & 0o777 == 0o600
    assert "ctx.agents.roots()" in source
    assert "agent.followup(" in source
    assert "prompt.append" not in source
    assert "prompt.submit" not in source
    assert "source: {" in source
    assert "kind: 'hyprial-bridge'" in source
    assert "role: 'user'" in source
    assert "randomUUID()" in source
    assert "deepFreeze(" in source
    assert PLUGIN_SOURCE == source

def test_plugin_source_parses_with_node(tmp_path: Path) -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dsh-tui Cordis plugin runtime")
    plugin_path = write_dsh_tui_plugin(tmp_path)
    completed = subprocess.run(
        [node, "--check", str(plugin_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

def test_plugin_runtime_queues_a_synthetic_user_message(tmp_path: Path) -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dsh-tui Cordis plugin runtime")
    plugin_path = write_dsh_tui_plugin(tmp_path)
    control_dir = _short_private_dir()
    control_socket = control_dir / "c.sock"
    harness = tmp_path / "plugin_harness.mjs"
    harness.write_text(
        """
import { pathToFileURL } from 'node:url'
import { connect } from 'node:net'

const plugin = await import(pathToFileURL(process.argv[2]).href)
const socketPath = process.argv[3]
let captured = null
let cleanup = null
const agent = {
  session: { id: 'session-1' },
  followup(message) { captured = message },
}
const ctx = {
  agents: { roots: () => [agent] },
  on() { return () => {} },
  effect(fn) { cleanup = fn() },
  logger: { warn() {} },
}
plugin.apply(ctx, { socketPath, sessionId: 'session-1' })
await new Promise((resolve) => setTimeout(resolve, 100))
const request = (frame) => new Promise((resolve, reject) => {
  const connection = connect(socketPath, () => {
    connection.write(JSON.stringify(frame) + '\\n')
  })
  let data = ''
  connection.on('data', (chunk) => {
    data += chunk
    if (data.includes('\\n')) {
      connection.end()
      resolve(JSON.parse(data.split('\\n')[0]))
    }
  })
  connection.on('error', reject)
})
const ping = await request({ type: 'ping' })
const wake = await request({ type: 'wake', prompt: '[Harness Network] Call harness_read', outcomeTimeoutMs: 50 })
console.log(JSON.stringify({ ping, wake, captured, frozen: Object.isFrozen(captured), contentFrozen: Object.isFrozen(captured?.content) }))
if (cleanup) await cleanup()
""",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [node, str(harness), str(plugin_path), str(control_socket)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        control_socket.unlink(missing_ok=True)
        control_dir.rmdir()
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ping"]["ready"] is True
    assert payload["wake"]["ok"] is True
    captured = payload["captured"]
    assert isinstance(captured.get("id"), str) and captured["id"]
    assert captured["role"] == "user"
    assert captured["content"] == [
        {"type": "text", "text": "[Harness Network] Call harness_read"}
    ]
    assert captured["source"]["kind"] == "hyprial-bridge"
    assert captured["source"]["plugin"] == "hyprial-bridge"
    assert payload["frozen"] is True
    assert payload["contentFrozen"] is True


def test_plugin_ping_reports_not_ready_without_a_live_root_agent(tmp_path: Path) -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dsh-tui Cordis plugin runtime")
    plugin_path = write_dsh_tui_plugin(tmp_path)
    control_dir = _short_private_dir()
    control_socket = control_dir / "c.sock"
    harness = tmp_path / "ping_harness.mjs"
    harness.write_text(
        """
import { pathToFileURL } from 'node:url'
import { connect } from 'node:net'
const plugin = await import(pathToFileURL(process.argv[2]).href)
const socketPath = process.argv[3]
let cleanup = null
const ctx = {
  agents: { roots: () => [] },
  effect(fn) { cleanup = fn() },
  logger: { warn() {} },
}
plugin.apply(ctx, { socketPath })
await new Promise((resolve) => setTimeout(resolve, 100))
const response = await new Promise((resolve, reject) => {
  const connection = connect(socketPath, () => {
    connection.write(JSON.stringify({ type: 'ping' }) + '\\n')
  })
  let data = ''
  connection.on('data', (chunk) => {
    data += chunk
    if (data.includes('\\n')) {
      connection.end()
      resolve(JSON.parse(data.split('\\n')[0]))
    }
  })
  connection.on('error', reject)
})
console.log(JSON.stringify(response))
if (cleanup) await cleanup()
""",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [node, str(harness), str(plugin_path), str(control_socket)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        control_socket.unlink(missing_ok=True)
        control_dir.rmdir()
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ready"] is False
    assert payload["ok"] is False


def test_driver_rejects_unsafe_socket_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _short_private_dir()
    socket_path = directory / "control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        server.listen(1)
        driver = DshPluginWakeDriver(socket_path)
        assert driver.socket_present() is True
        directory.chmod(0o777)
        assert driver.socket_present() is False
        directory.chmod(0o700)
        import os as _os

        actual_uid = _os.getuid()
        monkeypatch.setattr(_os, "getuid", lambda: actual_uid + 1)
        assert driver.socket_present() is False
        monkeypatch.setattr(_os, "getuid", lambda: actual_uid)
        regular = directory / "not-a-socket"
        regular.write_text("x")
        assert DshPluginWakeDriver(regular).socket_present() is False
        link = directory / "link.sock"
        link.symlink_to(socket_path)
        assert DshPluginWakeDriver(link).socket_present() is False
    finally:
        server.close()
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_carrier_does_not_register_before_plugin_ready() -> None:
    import threading

    class Proxy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(str(kwargs.get("method")))
            return {"registered": True}

    proxy = Proxy()
    stop_event = threading.Event()
    timer = threading.Timer(0.05, stop_event.set)
    timer.start()

    async def scenario() -> None:
        await run_dsh_carrier(
            proxy,  # type: ignore[arg-type]
            actor="agent:op:node:dsh",
            session_ref="session-1",
            cwd="/tmp/work",
            command=("dsh-tui",),
            control_socket=Path("/tmp/definitely-missing-dsh-control.sock"),
            tui_pid=4242,
            tui_identity="4242:stamp",
            ready_timeout=0.5,
            stop_event=stop_event,
        )

    try:
        anyio.run(scenario)
    finally:
        timer.cancel()
    assert "session.register" not in proxy.calls


class _RecordingProxy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(str(kwargs.get("method")))
        return {"registered": True}


class _ReadyPlugin:
    """A launch-scoped control socket that answers ping like the plugin."""

    def __init__(self, directory: Path, session_id: str = "dsh-session-1") -> None:
        self.directory = directory
        self.socket_path = directory / "control.sock"
        self.session_id = session_id
        self.ready = True
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.server.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_ReadyPlugin":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self.server.close()
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                try:
                    line = connection.recv(4096).split(b"\n", 1)[0]
                    frame = json.loads(line)
                except (OSError, ValueError):
                    continue
                if frame.get("type") == "ping":
                    body = {
                        "ok": self.ready,
                        "ready": self.ready,
                        "sessionId": self.session_id if self.ready else None,
                        "roots": 1,
                    }
                    connection.sendall(json.dumps(body).encode() + b"\n")
                elif frame.get("type") == "wake":
                    connection.sendall(json.dumps({"ok": True}).encode() + b"\n")


def _live_tui_binding() -> tuple[int, str]:
    import os

    pid = os.getpid()
    identity = process_identity_for_tui(pid)
    assert identity
    return pid, identity


def test_carrier_publishes_the_dsh_session_it_bound() -> None:
    directory = _short_private_dir()
    proxy = _RecordingProxy()
    stop = threading.Event()
    tui_pid, tui_identity = _live_tui_binding()
    try:
        with _ReadyPlugin(directory) as plugin:
            timer = threading.Timer(0.3, stop.set)
            timer.start()

            async def scenario() -> None:
                with anyio.fail_after(10):
                    await run_dsh_carrier(
                        proxy,  # type: ignore[arg-type]
                        actor="agent:op:node:dsh",
                        session_ref="session-1",
                        cwd="/tmp/work",
                        command=("dsh-tui",),
                        control_socket=plugin.socket_path,
                        tui_pid=tui_pid,
                        tui_identity=tui_identity,
                        ready_timeout=5.0,
                        stop_event=stop,
                    )

            anyio.run(scenario)
            timer.cancel()
        published = (directory / "dsh-session-id").read_text(encoding="utf-8")
        assert published == "dsh-session-1"
        assert (directory / "dsh-session-id").stat().st_mode & 0o777 == 0o600
        assert "session.register" in proxy.calls
    finally:
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_carrier_stops_when_the_tui_process_disappears() -> None:
    directory = _short_private_dir()
    proxy = _RecordingProxy()
    stop = threading.Event()
    try:
        with _ReadyPlugin(directory) as plugin:

            async def scenario() -> None:
                with anyio.fail_after(10):
                    await run_dsh_carrier(
                        proxy,  # type: ignore[arg-type]
                        actor="agent:op:node:dsh",
                        session_ref="session-1",
                        cwd="/tmp/work",
                        command=("dsh-tui",),
                        control_socket=plugin.socket_path,
                        tui_pid=9_999_999,
                        tui_identity="9999999:gone",
                        ready_timeout=5.0,
                        stop_event=stop,
                    )

            anyio.run(scenario)
        assert "session.register" in proxy.calls
        assert "session.unregister" in proxy.calls
    finally:
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_carrier_stops_when_the_plugin_reports_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.harnesses.dsh_tui import carrier as carrier_module

    monkeypatch.setattr(
        carrier_module, "INJECTION_EDGE_LOSS_GRACE_SECONDS", 0.2
    )
    directory = _short_private_dir()
    proxy = _RecordingProxy()
    tui_pid, tui_identity = _live_tui_binding()
    stop = threading.Event()
    try:
        with _ReadyPlugin(directory) as plugin:

            async def scenario() -> None:
                async def lose_the_agent() -> None:
                    while "session.register" not in proxy.calls:
                        await anyio.sleep(0.05)
                    # The socket stays up; the DSH session behind it is gone
                    # (resume pinned a session the user switched away from).
                    plugin.ready = False

                with anyio.fail_after(15):
                    async with anyio.create_task_group() as group:
                        group.start_soon(lose_the_agent)
                        await run_dsh_carrier(
                            proxy,  # type: ignore[arg-type]
                            actor="agent:op:node:dsh",
                            session_ref="session-1",
                            cwd="/tmp/work",
                            command=("dsh-tui",),
                            control_socket=plugin.socket_path,
                            tui_pid=tui_pid,
                            tui_identity=tui_identity,
                            ready_timeout=5.0,
                            stop_event=stop,
                        )
                        group.cancel_scope.cancel()

            anyio.run(scenario)
        assert "session.unregister" in proxy.calls
    finally:
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_carrier_counts_the_injection_edge_grace_from_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injection-edge grace clocks only from registration.

    A first launch can keep the control socket unanswered well past the
    grace (TUI and plugin still starting). If the grace were counted from
    before registration, such a start would spend it waiting and tear the
    carrier down before it ever registered.
    """

    from hyprial.daemon.impl.harnesses.dsh_tui import carrier as carrier_module

    monkeypatch.setattr(
        carrier_module, "INJECTION_EDGE_LOSS_GRACE_SECONDS", 0.4
    )
    directory = _short_private_dir()
    proxy = _RecordingProxy()
    tui_pid, tui_identity = _live_tui_binding()
    stop = threading.Event()
    survived = threading.Event()
    try:
        with _ReadyPlugin(directory) as plugin:
            plugin.ready = False
            timer = threading.Timer(2.0, lambda: setattr(plugin, "ready", True))
            timer.start()

            async def scenario() -> None:
                async def stop_after_a_survival_window() -> None:
                    while "session.register" not in proxy.calls:
                        await anyio.sleep(0.05)
                    # Outlive the shortened grace plus one watch poll while
                    # the plugin answers ready: the carrier must stay up.
                    await anyio.sleep(1.2)
                    survived.set()
                    assert "session.unregister" not in proxy.calls
                    stop.set()

                with anyio.fail_after(20):
                    async with anyio.create_task_group() as group:
                        group.start_soon(stop_after_a_survival_window)
                        await run_dsh_carrier(
                            proxy,  # type: ignore[arg-type]
                            actor="agent:op:node:dsh",
                            session_ref="session-1",
                            cwd="/tmp/work",
                            command=("dsh-tui",),
                            control_socket=plugin.socket_path,
                            tui_pid=tui_pid,
                            tui_identity=tui_identity,
                            ready_timeout=5.0,
                            stop_event=stop,
                        )
                        group.cancel_scope.cancel()

            anyio.run(scenario)
            timer.cancel()
            assert survived.is_set()
            assert "session.register" in proxy.calls
            assert "session.unregister" in proxy.calls
    finally:
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_carrier_uses_the_bounded_dsh_resend_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSH wake costs a whole model turn; the coordinator must not use the
    default 1s..60s cadence. The 300s curve and the same read-retry delay are
    pinned here so removing either is a red test, not a silent policy change."""

    from hyprial.daemon.impl.harnesses.dsh_tui import carrier as carrier_module

    captured: dict[str, Any] = {}

    original = carrier_module.WakeCoordinator

    class Recorder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            self._inner = original(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    monkeypatch.setattr(carrier_module, "WakeCoordinator", Recorder)
    directory = _short_private_dir()
    proxy = _RecordingProxy()
    stop = threading.Event()
    try:
        with _ReadyPlugin(directory) as plugin:
            timer = threading.Timer(0.2, stop.set)
            timer.start()

            async def scenario() -> None:
                with anyio.fail_after(10):
                    await run_dsh_carrier(
                        proxy,  # type: ignore[arg-type]
                        actor="agent:op:node:dsh",
                        session_ref="session-1",
                        cwd="/tmp/work",
                        command=("dsh-tui",),
                        control_socket=plugin.socket_path,
                        tui_pid=9_999_999,
                        tui_identity="9999999:gone",
                        ready_timeout=5.0,
                        stop_event=stop,
                    )

            anyio.run(scenario)
            timer.cancel()
        delay = captured["resignal_delay"]
        assert delay(0) == carrier_module.DSH_WAKE_RESIGNAL_SECONDS == 300.0
        assert captured["read_retry_delay"] == 300.0
        assert captured["retry_delay"] is carrier_module.dsh_wake_failure_delay
    finally:
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)
