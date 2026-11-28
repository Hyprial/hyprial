# Decision (2026-11-26): teardown never needs ``os.killpg`` or
# ``signal.SIGKILL``: ``_kill_group`` kills a leader's process group where
# ``os.killpg`` exists and the pid itself otherwise, with ``SIGKILL`` where it
# exists. The cases that exercise the POSIX process-group owner (a real group
# from ``spawn_owned_group``, a ``SIGSTOP``-ed server, a patched ``os.killpg``)
# keep their behaviour unchanged; whether they are skipped on Windows is not
# decided here.
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.harnesses.codex import _managed_process_group, _spawn_managed_codex, CodexAppServerClient, _OwnedProcessGroup, _process_birth_identity
from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess

from ..support import (
    _FakeWindowsProcessGroup,
    _delayed_thread_start_client,
    _fake_windows_process_group,
    assert_owned_group_gone,
    assert_process_group_gone,
    delivery,
    fake_process,
    process_is_running,
    spawn_owned_group,
    spec,
    wait_for_pid_file,
)

KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_group(pid: int) -> None:
    """Teardown kill of a group leader's process group, or of the pid itself where there are no process groups."""

    killpg = getattr(os, "killpg", None)
    with contextlib.suppress(ProcessLookupError):
        if killpg is None:
            os.kill(pid, KILL)
        else:
            killpg(pid, KILL)


def test_windows_codex_clients_select_the_job_owned_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = spec()
    _fake_windows_process_group(monkeypatch)

    group = _managed_process_group()
    client = CodexAppServerClient(launch)

    assert isinstance(group, _FakeWindowsProcessGroup)
    assert isinstance(client._process_group, _FakeWindowsProcessGroup)
    assert client._process_group.label == "Codex app-server"


def test_windows_codex_spawn_requires_and_uses_a_job_owned_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_windows_process_group(monkeypatch)
    group = _FakeWindowsProcessGroup(label="Codex app-server")

    asyncio.run(_spawn_managed_codex(("codex", "app-server"), group))

    assert group.spawned == (("codex", "app-server"), {})

    fallback_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_exec(*args: object, **kwargs: object) -> object:
        fallback_calls.append((args, kwargs))
        return "fallback-proc"

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process.asyncio.create_subprocess_exec",
        fake_exec,
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process.spawn_kwargs",
        lambda: {"creationflags": 512},
        raising=False,
    )
    result = asyncio.run(
        _spawn_managed_codex(("codex", "app-server"), _OwnedProcessGroup())
    )
    assert result == "fallback-proc"
    assert fallback_calls == [(("codex", "app-server"), {"creationflags": 512})]


def test_npm_platform_selects_win32_when_file_modes_are_insignificant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.harnesses.codex import process as codex_process

    monkeypatch.setattr(
        "hyprial.kernel.impl.manysys.interface.file_modes_are_significant",
        lambda: False,
    )
    assert codex_process._npm_platform() == "win32"


def test_interactive_app_server_spawns_and_stops_via_manysys_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.daemon.impl.harnesses.codex.app_server as app_server_module

    _fake_windows_process_group(monkeypatch)
    popen_calls: list[dict[str, object]] = []
    terminated_trees: list[tuple[object, int | None]] = []

    class FakePopen:
        pid = 7788

        def __init__(self, _argv: object, **kwargs: object) -> None:
            popen_calls.append(kwargs)
            socket_path.write_text("", encoding="utf-8")
            self._polled = 0

        def poll(self) -> int | None:
            self._polled += 1
            return 0 if self._polled > 2 else None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    socket_path = tmp_path / "app.sock"
    monkeypatch.setattr(
        app_server_module,
        "spawn_kwargs",
        lambda: {"creationflags": 512},
        raising=False,
    )
    monkeypatch.setattr(
        app_server_module,
        "terminate_process_tree",
        lambda proc, grp, **kw: terminated_trees.append((proc, grp)),
        raising=False,
    )
    monkeypatch.setattr(app_server_module.subprocess, "Popen", FakePopen)

    class FakeConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    def fake_connect(path: Path, timeout: float = 15.0) -> FakeConnection:
        del path, timeout
        return FakeConnection()

    socket_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(app_server_module, "open_app_server_connection", fake_connect)
    server = app_server_module.CodexInteractiveAppServer(socket_path, cwd=tmp_path)
    monkeypatch.setattr(server, "request", lambda *_a, **_k: {})
    monkeypatch.setattr(server, "notify", lambda *_a, **_k: None)
    socket_path.write_text("", encoding="utf-8")
    server.start()
    try:
        assert "start_new_session" not in popen_calls[0]
        assert popen_calls[0].get("creationflags") == 512
    finally:
        server.stop()
    assert len(terminated_trees) == 1
    assert terminated_trees[0][1] == 7788


def test_stop_drains_owned_group_when_clean_app_server_exit_leaves_child(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    process = fake_process("orphan-on-eof", state_path=child_pid_path)
    child_pid: int | None = None
    try:
        assert process.wait_ready(timeout=5.0)
        child_pid = wait_for_pid_file(
            child_pid_path,
            description="fake app-server child",
        )
        assert process_is_running(child_pid)

        started = time.monotonic()
        process.stop()
        assert time.monotonic() - started < 3.0

        deadline = time.monotonic() + 1.0
        while process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not process_is_running(child_pid)
    finally:
        if child_pid is not None and process_is_running(child_pid):
            os.kill(child_pid, KILL)


def test_stop_force_closes_sigstopped_app_server_during_active_turn(
    tmp_path: Path,
) -> None:
    server_pid_path = tmp_path / "server.pid"
    process = fake_process("sigstop-turn", state_path=server_pid_path)
    server_pid: int | None = None
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-stopped", "hold this turn"))
        server_pid = wait_for_pid_file(
            server_pid_path,
            description="fake app-server active turn",
        )
        os.kill(server_pid, signal.SIGSTOP)

        started = time.monotonic()
        process.stop()
        assert time.monotonic() - started < 5.0

        assert not process._thread.is_alive()
        assert not process_is_running(server_pid)
        with pytest.raises(ProcessLookupError):
            os.killpg(server_pid, 0)
    finally:
        if server_pid is not None and process_is_running(server_pid):
            _kill_group(server_pid)


def test_force_close_refuses_reused_process_group_identity(monkeypatch) -> None:
    owner = _OwnedProcessGroup()
    identities = iter(("birth-original", "birth-reused"))
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", lambda _pid: next(identities)
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process.os.killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    owner.register(4242)

    owner.signal(4242, signal.SIGKILL)

    assert signals == [(4242, 0)]


def test_stop_intent_before_register_rejects_and_cleans_entire_new_group(
    tmp_path: Path,
) -> None:
    owner = _OwnedProcessGroup()
    leader, child_pid = spawn_owned_group(tmp_path)
    try:
        owner.force_close()

        with pytest.raises(ConnectionError, match="stopping"):
            owner.register(leader.pid)

        assert_owned_group_gone(leader, child_pid)
        assert owner.stopped()
    finally:
        _kill_group(leader.pid)


def test_stop_during_register_identity_query_is_sticky_and_cleans_new_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _OwnedProcessGroup()
    leader, child_pid = spawn_owned_group(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = _process_birth_identity

    def delayed_identity(pid: int) -> str | None:
        entered.set()
        assert release.wait(2.0)
        return original(pid)

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", delayed_identity)
    outcome: list[BaseException | None] = []

    def register() -> None:
        try:
            owner.register(leader.pid)
        except BaseException as error:  # noqa: BLE001 - cross-thread test seam
            outcome.append(error)
        else:
            outcome.append(None)

    register_thread = threading.Thread(target=register)
    register_thread.start()
    try:
        assert entered.wait(2.0)
        owner.force_close()
        release.set()
        register_thread.join(3.0)

        assert not register_thread.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], ConnectionError)
        assert_owned_group_gone(leader, child_pid)
        assert owner.stopped()
    finally:
        release.set()
        register_thread.join(3.0)
        _kill_group(leader.pid)


def test_stop_after_identity_before_ownership_publish_rejects_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _OwnedProcessGroup()
    leader, child_pid = spawn_owned_group(tmp_path)
    original = _process_birth_identity

    def stop_after_identity(pid: int) -> str | None:
        identity = original(pid)
        owner.force_close()
        return identity

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", stop_after_identity
    )
    try:
        with pytest.raises(ConnectionError, match="stopping"):
            owner.register(leader.pid)
        assert_owned_group_gone(leader, child_pid)
        assert owner.stopped()
    finally:
        _kill_group(leader.pid)


def test_repeated_force_close_completes_a_previously_incomplete_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _OwnedProcessGroup()
    alive = True
    destructive_signals = 0

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", lambda _pid: "birth"
    )

    def killpg(_pid: int, signum: int) -> None:
        nonlocal alive, destructive_signals
        if signum == 0:
            if not alive:
                raise ProcessLookupError
            return
        destructive_signals += 1
        if destructive_signals >= 4:
            alive = False

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.os.killpg", killpg)
    owner.register(4242)

    owner.force_close()
    assert not owner.stopped()
    owner.force_close()

    assert owner.stopped()
    assert destructive_signals == 4


def test_unreadable_rejected_generation_is_not_reported_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _OwnedProcessGroup()
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", lambda _pid: None
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process.os.killpg",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError()),
    )

    with pytest.raises(ConnectionError, match="identity"):
        owner.register(4242)

    assert not owner.stopped()


def test_process_stop_before_spawn_rejects_late_generation_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    spawned: list[int] = []
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args: object, **kwargs: object):
        entered.set()
        assert await asyncio.to_thread(release.wait, 2.0)
        process = await original(*args, **kwargs)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process.asyncio.create_subprocess_exec", delayed_spawn
    )
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.client.PROCESS_STOP_TIMEOUT_SECONDS", 0.1)
    process = fake_process("complete")
    stop_error: list[BaseException] = []

    def stop() -> None:
        try:
            process.stop()
        except BaseException as error:  # noqa: BLE001 - cross-thread assertion
            stop_error.append(error)

    stop_thread = threading.Thread(target=stop)
    started = time.monotonic()
    try:
        assert entered.wait(2.0)
        stop_thread.start()
        time.sleep(0.2)
        release.set()
        stop_thread.join(3.0)

        assert not stop_thread.is_alive()
        assert not stop_error
        assert time.monotonic() - started < 3.0
        assert not process._thread.is_alive()
        assert len(spawned) == 1
        assert_process_group_gone(spawned[0])
    finally:
        release.set()
        stop_thread.join(3.0)
        for pid in spawned:
            _kill_group(pid)


def test_process_stop_during_identity_rejects_generation_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    observed_pid: list[int] = []
    original = _process_birth_identity

    def delayed_identity(pid: int) -> str | None:
        observed_pid.append(pid)
        entered.set()
        assert release.wait(2.0)
        return original(pid)

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", delayed_identity)
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.client.PROCESS_STOP_TIMEOUT_SECONDS", 0.1)
    process = fake_process("complete")
    stop_error: list[BaseException] = []

    def stop() -> None:
        try:
            process.stop()
        except BaseException as error:  # noqa: BLE001 - cross-thread assertion
            stop_error.append(error)

    stop_thread = threading.Thread(target=stop)
    started = time.monotonic()
    try:
        assert entered.wait(2.0)
        stop_thread.start()
        time.sleep(0.2)
        release.set()
        stop_thread.join(3.0)

        assert not stop_thread.is_alive()
        assert not stop_error
        assert time.monotonic() - started < 3.0
        assert not process._thread.is_alive()
        assert len(observed_pid) == 1
        assert_process_group_gone(observed_pid[0])
    finally:
        release.set()
        stop_thread.join(3.0)
        for pid in observed_pid:
            _kill_group(pid)


def test_repeated_process_stop_finishes_an_incomplete_forced_cleanup() -> None:
    entered = threading.Event()
    release = threading.Event()
    force_calls = 0

    class BlockedClient:
        async def __aenter__(self):
            entered.set()
            assert await asyncio.to_thread(release.wait, 2.0)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self):
            if False:
                yield None

        async def interrupt(self) -> None:
            return None

    def force_stop() -> None:
        nonlocal force_calls
        force_calls += 1
        if force_calls >= 2:
            release.set()

    process = StreamingTurnProcess(
        harness="codex",
        label="test app-server",
        client_factory=BlockedClient,
        thread_name="test-repeat-stop",
        stop_timeout_seconds=0.01,
        force_stop=force_stop,
        force_stopped=lambda: force_calls >= 2,
        force_stop_join_seconds=0.1,
    )
    try:
        assert entered.wait(2.0)
        with pytest.raises(RuntimeError, match="did not stop"):
            process.stop()

        process.stop()

        assert force_calls == 2
        assert not process._thread.is_alive()
    finally:
        release.set()
        process._thread.join(2.0)


def test_thread_start_timeout_still_fails_closed_with_the_method_named(
    tmp_path: Path,
) -> None:
    async def drive() -> None:
        client = _delayed_thread_start_client(
            tmp_path, delay=0.5, request_timeout=5.0, thread_start_timeout=0.2
        )
        with pytest.raises(ConnectionError, match="RPC thread/start"):
            await client.__aenter__()
        await client.__aexit__(None, None, None)

    asyncio.run(drive())
