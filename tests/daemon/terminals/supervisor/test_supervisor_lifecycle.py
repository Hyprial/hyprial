from pathlib import Path
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
