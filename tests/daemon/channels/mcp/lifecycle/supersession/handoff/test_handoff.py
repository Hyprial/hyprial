from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox_watch import Ok, Rejected, Unreachable
from hyprial.daemon.impl.mcp import SESSION_SUPERSEDED_CODE, DaemonRequestRejected, StatelessDaemonProxy, UnixDaemonConnectionFactory, run_channel_session
from hyprial.daemon.impl.mcp.channel import ownership as channel_module
from hyprial.daemon.impl.mcp.channel.ownership import _owner_process_alive, _read_process_identity
from hyprial.daemon.impl.mcp.channel.loops import _run_channel_heartbeat_loop, _run_channel_poll_loop, _watch_parent

from ..support import (
    FakeSupersedeDaemon,
    _ScriptedAdapter,
    _adapter_for,
    _daemon_app,
    _pid_alive,
    _register,
    _until_async,
    pytestmark as pytestmark,
    sock_path as sock_path,
)


async def test_daemon_supersedes_prior_session_with_distinct_code(
    tmp_path: Path, own_daemon_application,
) -> None:
    app = _daemon_app(tmp_path, own_daemon_application)

    _register(app, "session-A")
    _register(app, "session-B")  # last writer wins, keyed by actor

    # Exactly one session survives in desired state, and it is B.
    sessions = app.desired_state.load().interactive_sessions
    assert [s.session_ref for s in sessions] == ["session-B"]

    # The live (new) session reads fine.
    assert app.handle(
        "message.pending.list", {"actor": "hq-adjutant", "sessionRef": "session-B"}
    ) == {"ok": True, "messages": [], "daemonEpoch": app.epoch}

    # The superseded (old) session is fenced with the terminal supersede code,
    # NOT the transient STALE_SESSION -- that is what tells its child to stop
    # re-registering rather than steal the actor back.
    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "message.pending.list",
            {"actor": "hq-adjutant", "sessionRef": "session-A"},
        )
    assert rejected.value.code == SESSION_SUPERSEDED_CODE


async def test_daemon_unregistered_actor_is_stale_not_superseded(
    tmp_path: Path, own_daemon_application,
) -> None:
    """No session at all is a re-register signal, distinct from supersede."""

    app = _daemon_app(tmp_path, own_daemon_application)
    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "message.pending.list",
            {"actor": "hq-adjutant", "sessionRef": "whoever"},
        )
    assert rejected.value.code == "STALE_SESSION"


async def test_same_session_ref_rename_retires_old_actor_registration(
    tmp_path: Path, own_daemon_application,
) -> None:
    app = _daemon_app(tmp_path, own_daemon_application)
    # These are local agents: #1753 authenticates their live Agent bindings.
    old_actor = f"agent:{app.owner}:{app.node_id}:old-name"
    new_actor = f"agent:{app.owner}:{app.node_id}:new-name"

    for actor in (old_actor, new_actor):
        registered = app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": "same-cc-session",
                "cwd": "/work",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
            },
        )

    assert registered["supersededActors"] == [old_actor]
    assert [
        (session.actor, session.session_ref)
        for session in app.desired_state.load().interactive_sessions
    ] == [(new_actor, "same-cc-session")]
    assert app._agent_session_domains.session.read_runtime(old_actor) is None
    assert app._agent_session_domains.session.read_runtime(new_actor) is not None

    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "message.pending.list",
            {"actor": old_actor, "sessionRef": "same-cc-session"},
        )
    assert rejected.value.code == SESSION_SUPERSEDED_CODE

    assert app.handle(
        "message.pending.list",
        {"actor": new_actor, "sessionRef": "same-cc-session"},
    )["ok"] is True


async def test_poll_loop_goes_quiet_on_supersede_without_reregistering() -> None:
    # Registered once, then the list is superseded -> the loop must RETURN
    # (go quiet). It must not re-register (the steal).
    adapter = _ScriptedAdapter(
        "coordinator",
        scripts={"list": [Rejected(SESSION_SUPERSEDED_CODE)]},
    )

    errors: list[BaseException] = []
    with anyio.fail_after(5):
        await _run_channel_poll_loop(
            adapter,  # type: ignore[arg-type]
            poll_interval=0.01,
            on_error=lambda error, _n: errors.append(error),
        )

    # Returned (did not loop forever), registered exactly once, saw the verdict.
    assert adapter.count("register") == 1
    assert any(
        isinstance(e, DaemonRequestRejected) and e.code == SESSION_SUPERSEDED_CODE
        for e in errors
    )


async def test_post_disconnect_refresh_cannot_steal_from_new_session() -> None:
    # The daemon restarted during an outage (the next list carries a new
    # epoch); the guarded refresh is superseded -> quiet, never a register.
    adapter = _ScriptedAdapter(
        "coordinator",
        scripts={
            "list": [
                Unreachable(),
                Ok({"ok": True, "messages": [], "daemonEpoch": "e2"}),
            ],
            "refresh": [Rejected(SESSION_SUPERSEDED_CODE)],
        },
    )
    errors: list[BaseException] = []

    with anyio.fail_after(5):
        await _run_channel_poll_loop(
            adapter,  # type: ignore[arg-type]
            poll_interval=0.01,
            on_error=lambda error, _n: errors.append(error),
        )

    assert adapter.count("register") == 1
    assert adapter.count("refresh") == 1
    assert any(
        isinstance(error, DaemonRequestRejected)
        and error.code == SESSION_SUPERSEDED_CODE
        for error in errors
    )


async def test_heartbeat_loop_stops_on_supersede_without_renewing_again() -> None:
    adapter = _ScriptedAdapter(
        "coordinator",
        scripts={"heartbeat": [Rejected(SESSION_SUPERSEDED_CODE)]},
    )
    adapter.register_now()
    errors: list[BaseException] = []

    with anyio.fail_after(5):
        await _run_channel_heartbeat_loop(
            adapter,  # type: ignore[arg-type]
            heartbeat_interval=0.01,
            on_error=lambda error, _n: errors.append(error),
        )

    assert adapter.count("heartbeat") == 1
    assert len(errors) == 1
    assert isinstance(errors[0], DaemonRequestRejected)
    assert errors[0].code == SESSION_SUPERSEDED_CODE


async def test_poll_loop_keeps_reregistering_on_non_supersede_envelope() -> None:
    # A non-supersede daemon error envelope (e.g. the daemon is still booting)
    # is transient: the child must keep re-registering, not go quiet. This is
    # the daemon-restart-resilience contract the supersede change must preserve.
    adapter = _ScriptedAdapter(
        "coordinator",
        scripts={
            "register": [Rejected("DAEMON_BOOTING"), Rejected("DAEMON_BOOTING")],
        },
    )
    errors: list[BaseException] = []
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,  # type: ignore[arg-type]
                poll_interval=0.01,
                on_error=lambda error, _n: errors.append(error),
            )
        )
        # It retried registration through both envelopes and then succeeded.
        await _until_async(lambda: adapter.count("register") >= 3)
        await _until_async(lambda: adapter.count("list") >= 1)
        assert all(isinstance(e, DaemonRequestRejected) for e in errors)
        tasks.cancel_scope.cancel()


async def test_superseded_loop_cannot_steal_the_actor_back(sock_path: Path) -> None:
    daemon = FakeSupersedeDaemon(sock_path)
    daemon.start()
    actor = "hq-adjutant"
    adapter_a = _adapter_for(sock_path, actor, "ref-a")
    adapter_b = _adapter_for(sock_path, actor, "ref-b")
    a_quiet = anyio.Event()
    try:
        async with anyio.create_task_group() as tasks:

            async def run_a() -> None:
                try:
                    await _run_channel_poll_loop(
                        adapter_a, poll_interval=0.02, on_error=lambda *_: None
                    )
                finally:
                    a_quiet.set()

            tasks.start_soon(run_a)
            await _until_async(lambda: daemon.current.get(actor) == "ref-a")

            tasks.start_soon(
                lambda: _run_channel_poll_loop(
                    adapter_b, poll_interval=0.02, on_error=lambda *_: None
                )
            )
            await _until_async(lambda: daemon.current.get(actor) == "ref-b")

            # The losing loop A goes quiet (its loop returns) and never steals.
            with anyio.fail_after(5):
                await a_quiet.wait()

            # No flap: B remains the sole owner across a sustained window, and A
            # never re-registered (register_counts['ref-a'] stays at 1).
            for _ in range(25):
                assert daemon.current.get(actor) == "ref-b"
                await anyio.sleep(0.02)
            assert daemon.register_counts["ref-a"] == 1

            tasks.cancel_scope.cancel()
    finally:
        daemon.stop()


async def test_watch_parent_reports_orphan_on_ppid_drift() -> None:
    calls = [0]

    def getppid() -> int:
        calls[0] += 1
        return 1234 if calls[0] <= 2 else 1  # drift to the reaper on the 3rd read

    with anyio.fail_after(5):
        orphaned = await _watch_parent(getppid=getppid, poll_interval=0.01)
    assert orphaned is True


async def test_watch_parent_waits_while_parent_alive() -> None:
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.3):
            await _watch_parent(getppid=lambda: 4321, poll_interval=0.01)


async def test_watch_parent_reports_orphan_when_started_under_reaper() -> None:
    with anyio.fail_after(2):
        orphaned = await _watch_parent(getppid=lambda: 1, poll_interval=0.01)
    assert orphaned is True


def test_owner_process_identity_rejects_pid_reuse() -> None:
    assert not _owner_process_alive(
        1234,
        "original-start",
        signal_process=lambda _pid, _signal: None,
        read_identity=lambda _pid: "reused-start",
    )


def test_process_identity_uses_linux_proc_starttime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "1234"
    process_dir.mkdir(parents=True)
    # Fields after comm begin at field 3; index 19 is field 22 (starttime).
    remainder = ["S", *[str(index) for index in range(4, 22)], "987654", "tail"]
    (process_dir / "stat").write_text(
        f"1234 (owner with ) parens) {' '.join(remainder)}\n", encoding="utf-8"
    )
    monkeypatch.setattr(channel_module, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(channel_module.sys, "platform", "linux")

    assert _read_process_identity(1234) == "proc-starttime:987654"


def test_owner_process_identity_is_fail_safe_when_permission_denied() -> None:
    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError

    assert _owner_process_alive(
        1234,
        "original-start",
        signal_process=denied,
        read_identity=lambda _pid: pytest.fail(
            "identity must not be read after a permission-denied liveness probe"
        ),
    )


def test_owner_process_identity_rejects_a_missing_pid() -> None:
    def missing(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    assert not _owner_process_alive(
        1234,
        "original-start",
        signal_process=missing,
        read_identity=lambda _pid: pytest.fail(
            "identity must not be read for a missing process"
        ),
    )


async def test_real_owner_birth_marker_survives_in_place_package_swap(
    tmp_path: Path,
) -> None:
    """Replacing the owner's package tree must not look like PID reuse."""

    package = tmp_path / "tool-current"
    package.mkdir()
    script = package / "owner.py"
    script.write_text("import time; time.sleep(120)\n", encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(script)])
    try:
        expected = _read_process_identity(owner.pid)
        assert expected is not None
        async with anyio.create_task_group() as tasks:
            outcome: list[bool] = []
            swapping = True

            def observe_birth(pid: int) -> str | None:
                # Model the short inconsistent observation seen while uv swaps
                # an installed tool tree. The kernel evidence is asserted
                # separately below and remains stable throughout.
                if swapping:
                    return "transient-swap-observation"
                return _read_process_identity(pid)

            async def watch() -> None:
                outcome.append(
                    await _watch_parent(
                        getppid=lambda: 9999,
                        poll_interval=0.02,
                        owner_pid=owner.pid,
                        owner_identity=expected,
                        read_identity=observe_birth,
                    )
                )

            tasks.start_soon(watch)
            for generation in range(4):
                replacement = tmp_path / f"tool-next-{generation}"
                replacement.mkdir()
                (replacement / "owner.py").write_text(
                    "import time; time.sleep(120)\n", encoding="utf-8"
                )
                retired = tmp_path / f"tool-retired-{generation}"
                package.rename(retired)
                replacement.rename(package)
                shutil.rmtree(retired)
                await anyio.sleep(0.08)
                assert owner.poll() is None
                assert _read_process_identity(owner.pid) == expected
                assert outcome == []

            swapping = False
            await anyio.sleep(0.08)
            assert outcome == []

            # Real owner death remains immediate even though identity mismatch
            # confirmation now has a debounce window.
            owner.terminate()
            owner.wait(timeout=5)
            with anyio.fail_after(2):
                while outcome == []:
                    await anyio.sleep(0.02)
            assert outcome == [True]
            tasks.cancel_scope.cancel()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


async def test_watch_parent_waits_for_the_same_live_owner_after_reparenting() -> None:
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.3):
            await _watch_parent(
                getppid=lambda: 9999,  # an unrelated subreaper
                poll_interval=0.01,
                owner_pid=1234,
                owner_identity="original-start",
                signal_process=lambda _pid, _signal: None,
                read_identity=lambda _pid: "original-start",
            )


async def test_watch_parent_tolerates_one_transient_identity_mismatch() -> None:
    identities = iter(("transient-mismatch", "original-start"))

    def read_identity(_pid: int) -> str:
        return next(identities, "original-start")

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.15):
            await _watch_parent(
                getppid=lambda: 9999,
                poll_interval=0.01,
                owner_pid=1234,
                owner_identity="original-start",
                signal_process=lambda _pid, _signal: None,
                read_identity=read_identity,
            )


async def test_watch_parent_exits_after_continuous_identity_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        "WARNING", logger="hyprial.daemon.impl.mcp.channel.loops"
    )
    moments = iter((100.0, 111.0))
    with anyio.fail_after(1):
        orphaned = await _watch_parent(
            getppid=lambda: 9999,
            poll_interval=0.01,
            owner_pid=1234,
            owner_identity="secret-birth-marker",
            signal_process=lambda _pid, _signal: None,
            read_identity=lambda _pid: "different-secret-marker",
            monotonic=lambda: next(moments, 111.0),
        )
    assert orphaned is True
    assert "reason=identity-mismatch" in caplog.text
    assert "secret-birth-marker" not in caplog.text
    assert "different-secret-marker" not in caplog.text


async def test_watch_parent_resets_mismatch_window_after_matching_birth_marker() -> (
    None
):
    identities = iter(
        (
            "mismatch",
            "original-start",
            "mismatch",
            "original-start",
        )
    )
    moments = iter((100.0, 200.0))

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.12):
            await _watch_parent(
                getppid=lambda: 9999,
                poll_interval=0.01,
                owner_pid=1234,
                owner_identity="original-start",
                signal_process=lambda _pid, _signal: None,
                read_identity=lambda _pid: next(identities, "original-start"),
                monotonic=lambda: next(moments, 200.0),
            )


async def test_watch_parent_missing_pid_exits_with_safe_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        "WARNING", logger="hyprial.daemon.impl.mcp.channel.loops"
    )

    def missing(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    with anyio.fail_after(1):
        orphaned = await _watch_parent(
            getppid=lambda: 9999,
            poll_interval=0.01,
            owner_pid=1234,
            owner_identity="secret-birth-marker",
            signal_process=missing,
            read_identity=lambda _pid: pytest.fail("missing pid has no identity"),
        )
    assert orphaned is True
    assert "reason=pid-missing" in caplog.text
    assert "secret-birth-marker" not in caplog.text


async def test_parent_death_forces_channel_session_shutdown(sock_path: Path) -> None:
    """Wiring: on ppid drift the session best-effort unregisters then exits.

    Uses an injected ``on_parent_death`` in place of ``os._exit`` (which the
    real path calls) so the teardown can be observed in-process. A real
    subprocess exercises the ``os._exit`` path in the test below.
    """

    daemon = FakeSupersedeDaemon(sock_path)
    daemon.start()
    proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(sock_path))
    ppid = [5000]
    exited = anyio.Event()
    try:
        async with create_client_server_memory_streams() as (
            _client_streams,
            server_streams,
        ):
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tasks:

                def on_death() -> None:
                    exited.set()
                    tasks.cancel_scope.cancel()

                async def flip() -> None:
                    await anyio.sleep(0.1)
                    ppid[0] = 1  # parent died

                tasks.start_soon(flip)
                tasks.start_soon(
                    lambda: run_channel_session(
                        server_read,
                        server_write,
                        proxy,
                        actor="hq-adjutant",
                        session_ref="mine",
                        cwd="/work",
                        command=("claude",),
                        poll_interval=0.05,
                        on_error=lambda *_: None,
                        getppid=lambda: ppid[0],
                        parent_poll_interval=0.02,
                        on_parent_death=on_death,
                    )
                )
                with anyio.fail_after(5):
                    await exited.wait()

            # The best-effort unregister ran before the exit hook.
            assert "session.unregister" in daemon.methods
    finally:
        daemon.stop()


def test_orphaned_channel_subprocess_exits_despite_inherited_stdin(
    tmp_path: Path,
) -> None:
    """The definitive orphan-reaping proof against a real ``hyprial`` child process.

    Reproduces the production accumulation: a *sibling* process holds the WRITE
    end of the channel child's stdin pipe open, so the child never sees EOF when
    its parent (Claude Code) dies. stdin EOF therefore cannot reap it -- only
    ``getppid`` drift can. With the watcher this orphan shuts itself down; the
    mutation-verification run confirms it lingers forever without it.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    parent_src = textwrap.dedent(
        """
        import os, subprocess, sys
        from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity
        state_dir = sys.argv[1]
        env = dict(os.environ)
        env["HYPRIAL_HOME"] = state_dir
        env["HARNESS_STATE_DIR"] = state_dir
        env.pop("HYPRIAL_ZENOH_LISTEN", None)
        read_fd, write_fd = os.pipe()
        owner_pid = os.getpid()
        owner_identity = _read_process_identity(owner_pid)
        assert owner_identity is not None
        child = subprocess.Popen(
            [sys.executable, "-m", "hyprial.daemon.impl.mcp.entry", "claude-channel",
             "--actor", "orphan-probe", "--session-ref", "orphan-ref",
             "--cwd", state_dir, "--command", "claude", "--poll-interval", "0.1",
             "--owner-pid", str(owner_pid), "--owner-identity", owner_identity],
            stdin=read_fd, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env,
        )
        # A sibling that keeps the child's stdin WRITE end open, so the child
        # never sees EOF even after we (the parent) die.
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            pass_fds=[write_fd],
        )
        os.close(read_fd)
        os.close(write_fd)
        sys.stdout.write(f"{child.pid} {sibling.pid}\\n")
        sys.stdout.flush()
        # Die immediately.  The channel's Python/MCP imports may still be in
        # progress, so the watcher can start only after the child has already
        # been reparented.  This is the startup edge the production reaper must
        # cover; waiting here would hide it.
        os._exit(0)  # parent dies -> child orphaned, but stdin never EOFs
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_src, str(state_dir)],
        stdout=subprocess.PIPE,
    )
    assert parent.stdout is not None
    child_pid, sibling_pid = (
        int(value) for value in parent.stdout.readline().decode().split()
    )
    parent.wait(timeout=10)  # intermediate parent exits -> child orphaned

    try:
        # Up to the parent-watch interval (1.0s) + the unregister timeout + margin.
        deadline = time.time() + 12.0
        exited = False
        while time.time() < deadline:
            if not _pid_alive(child_pid):
                exited = True
                break
            time.sleep(0.1)
        assert exited, "orphaned channel child did not exit after its parent died"
    finally:
        for pid in (child_pid, sibling_pid):
            if _pid_alive(pid):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass


async def test_supersede_alone_does_not_tear_down_a_live_parent_child(
    sock_path: Path,
) -> None:
    daemon = FakeSupersedeDaemon(sock_path)
    daemon.start()
    actor = "hq-adjutant"
    proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(sock_path))
    errors: list[BaseException] = []
    try:
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tasks:
                session_done = anyio.Event()

                async def run() -> None:
                    try:
                        await run_channel_session(
                            server_read,
                            server_write,
                            proxy,
                            actor=actor,
                            session_ref="mine",
                            cwd="/work",
                            command=("claude",),
                            poll_interval=0.02,
                            on_error=lambda error, _n: errors.append(error),
                            # Parent stays alive for the whole test.
                            getppid=lambda: 999_999,
                            parent_poll_interval=100.0,
                        )
                    finally:
                        session_done.set()

                tasks.start_soon(run)
                async with ClientSession(client_read, client_write) as client:
                    await client.initialize()
                    await _until_async(lambda: daemon.current.get(actor) == "mine")
                    registers_when_owned = daemon.register_counts.get("mine", 0)

                    # A newer session takes over: mine is now superseded.
                    daemon.set_owner(actor, "other")

                    # The poll loop observes the supersede verdict...
                    await _until_async(
                        lambda: any(
                            isinstance(e, DaemonRequestRejected)
                            and e.code == SESSION_SUPERSEDED_CODE
                            for e in errors
                        )
                    )
                    # ...and goes quiet: it must NOT tear the child down (parent
                    # is alive) and must NOT re-register (no steal of 'other').
                    await anyio.sleep(0.3)
                    assert not session_done.is_set()
                    assert daemon.register_counts.get("mine", 0) == registers_when_owned
                    assert daemon.current.get(actor) == "other"

                tasks.cancel_scope.cancel()
    finally:
        daemon.stop()
