from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.acquisition.lock import HcLock, HcTargetSpec
from hyprial.daemon.impl.terminals.supervisor.supervisor import (
    HcGenerationIncompatibleError,
    PrivateCoreSupervisor,
)
from tests.daemon.terminals.fake_hc.server import FakeHcServer


def _lock(version: str, generation: int = 1) -> HcLock:
    return HcLock(
        version=version,
        generation=generation,
        targets={"linux-x86_64": HcTargetSpec("https://example.test/hc", "0" * 64, 1)},
    )


def test_connect_switches_old_core_with_pinned_executable_and_defers_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    old_root = tmp_path / "state" / "hc" / "bin" / "0.1.21"
    old_root.mkdir(parents=True)
    old_marker = old_root / "old"
    old_marker.write_text("old", encoding="utf-8")
    server = FakeHcServer(endpoint, version="0.1.21", holders=[{"pid": 1, "instance": "old", "current": False, "terminals": 1}])
    restarts: list[tuple[tuple[str, ...], dict[str, str]]] = []
    removed: list[str] = []

    def restart(argv, env):
        restarts.append((tuple(argv), dict(env)))
        server.version = "0.1.22"

    monkeypatch.setattr(
        "hyprial.daemon.impl.terminals.supervisor.supervisor.remove_hc_version_dir",
        lambda version, *_args, **_kwargs: removed.append(version) or True,
    )
    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock("0.1.22"),
        acquirer=lambda: tmp_path / "state" / "hc" / "bin" / "0.1.22" / "herdr-core",
        restart_runner=restart,
        source_env={"HOME": "/host", "PATH": "/bin", "HYPRIAL_TOKEN": "secret"},
    )
    try:
        supervisor.ensure_running()
        assert restarts[0][0] == (
            str(tmp_path / "state" / "hc" / "bin" / "0.1.22" / "herdr-core"),
            "server",
            "restart",
        )
        assert "HYPRIAL_TOKEN" not in restarts[0][1]
        assert old_marker.exists()
        assert server._holders[0]["current"] is False
        assert supervisor.snapshot().pending_old_versions == ("0.1.21",)
        server._holders = []
        supervisor.reconcile_versions()
        assert removed == ["0.1.21"]
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_version_switch_is_symmetric_for_rollback(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint, version="0.1.22")
    restarts: list[tuple[str, ...]] = []

    def restart(argv, _env):
        restarts.append(tuple(argv))
        server.version = "0.1.21"

    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock("0.1.21"),
        acquirer=lambda: tmp_path / "state" / "hc" / "bin" / "0.1.21" / "herdr-core",
        restart_runner=restart,
    )
    try:
        supervisor.ensure_running()
        assert restarts == [
            (
                str(tmp_path / "state" / "hc" / "bin" / "0.1.21" / "herdr-core"),
                "server",
                "restart",
            )
        ]
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_failed_acquisition_keeps_compatible_core_in_degraded_mode(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint, version="0.1.21", generation=1)
    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock("0.1.22", generation=1),
        acquirer=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    try:
        supervisor.ensure_running()
        snapshot = supervisor.snapshot()
        assert snapshot.version_lagging is True
        assert snapshot.core_version == "0.1.21"
        assert snapshot.pinned_version == "0.1.22"
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()


def test_failed_acquisition_rejects_new_terminal_for_incompatible_generation(
    tmp_path: Path,
) -> None:
    endpoint = tmp_path / "state" / "hc" / "core" / "hc.sock"
    server = FakeHcServer(endpoint, version="0.1.21", generation=2)
    supervisor = PrivateCoreSupervisor(
        tmp_path,
        lock=_lock("0.1.22", generation=1),
        acquirer=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    try:
        with pytest.raises(HcGenerationIncompatibleError):
            supervisor.ensure_running()
        assert supervisor.snapshot().generation_incompatible is True
        assert supervisor.snapshot().running is True
    finally:
        supervisor.close_for_daemon_shutdown()
        server.close()
