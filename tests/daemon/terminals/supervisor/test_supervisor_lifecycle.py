from pathlib import Path
import threading
import time

from hyprial.daemon.impl.terminals.acquisition.lock import HcLock, HcTargetSpec
from hyprial.daemon.impl.terminals.client.rpc import HcClient
from hyprial.daemon.impl.terminals.supervisor.supervisor import PrivateCoreSupervisor
from tests.daemon.terminals.fake_hc.server import FakeHcServer


def _lock(version: str = "0.1.22") -> HcLock:
    return HcLock(
        version=version,
        generation=1,
        targets={"linux-x86_64": HcTargetSpec("https://example.test/hc", "0" * 64, 1)},
    )


def _wait_until(predicate) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_idle_restore_does_not_acquire_or_spawn(tmp_path: Path) -> None:
    calls: list[str] = []
    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock(),
        acquirer=lambda: calls.append("acquire"),
        launcher=lambda _argv, _env: calls.append("launch"),
    )
    try:
        supervisor.probe_on_restore()
        assert calls == []
        assert supervisor.snapshot().running is False
    finally:
        supervisor.close_for_daemon_shutdown()


def test_first_terminal_starts_private_core_and_subscribes(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    calls: list[tuple[str, object]] = []
    servers: list[FakeHcServer] = []

    def launch(argv, env):
        calls.append(("launch", (tuple(argv), dict(env))))
        server = FakeHcServer(endpoint)
        servers.append(server)
        return server

    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock(),
        acquirer=lambda: tmp_path / "state" / "hc" / "bin" / "0.1.22" / "herdr-core",
        launcher=launch,
        source_env={"HOME": "/host", "PATH": "/bin", "ANTHROPIC_API_KEY": "secret"},
    )
    try:
        terminal = supervisor.ensure_running_and_run_terminal(
            ["claude"], agent_id="agent-1", member="member-1", role="tui", spec_digest="digest"
        )
        assert terminal.labels["hyprial.agent"] == "agent-1"
        assert terminal.labels["hyprial.member"] == "member-1"
        launch_env = calls[0][1][1]
        assert "ANTHROPIC_API_KEY" not in launch_env
        assert launch_env["HC_SESSION"].startswith("hyprial-")
        assert supervisor.snapshot().running is True
    finally:
        supervisor.close_for_daemon_shutdown()
        for server in servers:
            server.close()


def test_removing_last_terminal_stops_core_and_cleans_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint)
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        "hyprial.daemon.impl.terminals.supervisor.supervisor.cleanup_private_hc_runtime_dirs",
        lambda session: cleanup_calls.append(session) or (tmp_path / "undeleted",),
    )
    supervisor = PrivateCoreSupervisor(tmp_path, lock=_lock())
    try:
        terminal = supervisor.ensure_running_and_run_terminal(
            ["claude"], agent_id="agent-1", member="member-1", role="tui", spec_digest="digest"
        )
        supervisor.remove_terminal(terminal.address)
        _wait_until(lambda: any(frame.get("method") == "server.stop" for frame in server.frames))
        assert cleanup_calls
        assert supervisor.snapshot().runtime_cleanup_warnings == (str(tmp_path / "undeleted"),)
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_missed_snapshot_rebuilds_tracking_and_reclaims_idle_core(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint)
    supervisor = PrivateCoreSupervisor(
        tmp_path, lock=_lock(), active_launch_rows=[{"agent_id": "a1", "member": "m1"}]
    )
    try:
        terminal = HcClient(endpoint).terminal_run(
            ["claude"],
            labels={
                "hyprial.agent": "a1",
                "hyprial.member": "m1",
                "hyprial.role": "tui",
                "hyprial.spec_digest": "d1",
            },
        )
        supervisor.ensure_running()
        server.emit_missed([], seq=10)
        _wait_until(lambda: supervisor.snapshot().running is False)
        assert terminal.address not in supervisor.snapshot().tracked_terminals
        assert server.stopped is True
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_remove_and_new_terminal_are_serialized_across_idle_stop(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint)
    supervisor = PrivateCoreSupervisor(tmp_path, lock=_lock())
    try:
        first = supervisor.ensure_running_and_run_terminal(
            ["claude"], agent_id="a1", member="m1", role="tui", spec_digest="d1"
        )
        subscription = supervisor._subscription
        if subscription is not None:
            subscription.close()
            supervisor._subscription = None
        entered = threading.Event()
        release = threading.Event()
        original_idle_stop = supervisor._maybe_stop_when_idle

        def delayed_idle_stop():
            entered.set()
            assert release.wait(2)
            return original_idle_stop()

        supervisor._maybe_stop_when_idle = delayed_idle_stop
        remover = threading.Thread(target=supervisor.remove_terminal, args=(first.address,))
        remover.start()
        assert entered.wait(2)
        created: list[object] = []
        launcher = threading.Thread(
            target=lambda: created.append(
                supervisor.ensure_running_and_run_terminal(
                    ["claude"], agent_id="a2", member="m2", role="tui", spec_digest="d2"
                )
            )
        )
        launcher.start()
        time.sleep(0.05)
        assert launcher.is_alive()
        release.set()
        remover.join(2)
        launcher.join(2)
        assert not launcher.is_alive()
        assert created
        assert server.stopped is False
        assert server._terminals[created[0].address].state == "live"
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_terminal_events_before_run_response_are_reconciled(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint, terminal_run_events=("finished", "removed"))
    supervisor = PrivateCoreSupervisor(tmp_path, lock=_lock())
    try:
        terminal = supervisor.ensure_running_and_run_terminal(
            ["claude"], agent_id="a1", member="m1", role="tui", spec_digest="d1"
        )
        _wait_until(lambda: supervisor.snapshot().running is False)
        assert terminal.state in {"finished", "removed"}
        assert supervisor.snapshot().tracked_terminals == ()
        assert server.stopped is True
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_restore_adopts_and_removes_unmatched_live_member(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint)
    client = HcClient(endpoint)
    client.terminal_run(
        ["claude"],
        labels={
            "hyprial.agent": "orphan",
            "hyprial.member": "m1",
            "hyprial.role": "tui",
            "hyprial.spec_digest": "d1",
        },
    )
    supervisor = PrivateCoreSupervisor(tmp_path, lock=_lock(), active_launch_rows=[])
    try:
        supervisor.ensure_running()
        _wait_until(
            lambda: [frame.get("method") for frame in server.frames].count("terminal.rm") == 1
        )
        assert supervisor.snapshot().tracked_terminals == ()
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_daemon_shutdown_disconnects_without_stopping_or_killing(
    tmp_path: Path,
) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint)
    supervisor = PrivateCoreSupervisor(tmp_path, lock=_lock())
    try:
        terminal = supervisor.ensure_running_and_run_terminal(
            ["claude"], agent_id="agent-1", member="member-1", role="tui", spec_digest="digest"
        )
        supervisor.close_for_daemon_shutdown()
        methods = [frame.get("method") for frame in server.frames]
        assert "server.stop" not in methods
        assert HcClient(endpoint).terminal_get(terminal.address).state == "live"
    finally:
        server.close()
