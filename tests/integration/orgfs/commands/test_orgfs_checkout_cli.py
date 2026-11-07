from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from tests.support.tailnet_daemon import bind_admitted

import os
from pathlib import Path
import socket
import time

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


@pytest.fixture(autouse=True)
def _restore_checkout_permissions(tmp_path: Path):
    """M9: leave pytest able to remove read-only checkout projections."""

    yield
    for current, directories, files in os.walk(
        tmp_path, topdown=False, followlinks=False
    ):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o600)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o700)
        current_path.chmod(0o700)


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, FileNotFoundError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _application(tmp_path) -> DaemonApplication:
    return DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-a",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
    **_deep_module_daemon_dependencies())


def _close(application: DaemonApplication) -> None:
    if application._orgfs_runtime is not None:
        application._orgfs_runtime.close()
        application._orgfs_runtime = None
    application._agent_session_domains.close()


def test_checkout_cli_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    application = _application(tmp_path)
    runner = CliRunner()
    endpoint = f"tcp/127.0.0.1:{_port()}"
    application_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer")
    monkeypatch.setattr(
        'hyprial.shell.impl.cli.commands.common.support._daemon_request',
        lambda method, params=None, **_kwargs: application._handle_orgfs(
            method, params or {}
        ),
    )
    try:
        time.sleep(0.3)
        online = {"node-a", "node-peer"}.__contains__
        runtime = application._require_orgfs_runtime()
        # Cold loopback replicas model already-admitted private peers.
        bind_admitted(runtime, application_transport, supplier_online=online)
        bind_admitted(
            peer, peer_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-a",),
        )
        created = application._handle_orgfs("orgfs.create", {"name": "team"})
        space_id = created["space_id"]
        application._handle_orgfs(
            "orgfs.invite", {"spaceId": space_id, "user": "user:peer", "mode": "rw"}
        )
        runtime._meshes[space_id].announce()
        peer.facade.join(space_id)
        enabled = runner.invoke(app, ["org", "fs", "space", "checkout", space_id, "--enable", "--json"])
        assert enabled.exit_code == 0, enabled.output
        root = Path(application.state_dir) / "orgfs" / "spaces" / space_id / "checkout"
        remote = peer.facade.write_text(space_id, "remote.md", "from inbound mesh")
        materialized = root / "remote.md"
        _eventually(
            lambda: (
                materialized.read_text(encoding="utf-8") == "from inbound mesh"
                or (_ for _ in ()).throw(AssertionError())
            )
        )
        assert materialized.stat().st_mode & 0o222 == 0
        peer.facade.remove(space_id, f"id:{remote.node_id}")
        _eventually(
            lambda: (
                (not materialized.exists()) or (_ for _ in ()).throw(AssertionError())
            )
        )
        disabled = runner.invoke(
            app, ["org", "fs", "space", "checkout", space_id, "--disable", "--json"]
        )
        assert disabled.exit_code == 0, disabled.output
        assert not root.exists()
    finally:
        peer.close()
        peer_transport.close()
        application_transport.close()
        _close(application)


def test_checkout_survives_daemon_restart_reconciles(tmp_path) -> None:
    state = tmp_path / "state"
    first = OrgFsRuntime(state, node_id="node-a", author="user:owner")
    space = first.facade.create_space("team")
    first.facade.write_text(space.space_id, "current.md", "authoritative")
    result = first.checkout(space.space_id, True)
    root = Path(str(result["path"]))
    first.close()

    os.chmod(root, 0o755)
    stale = root / "stale" / "nested"
    stale.mkdir(parents=True)
    (stale / "file.txt").write_text("stale", encoding="utf-8")
    stale.chmod(0o555)
    stale.parent.chmod(0o555)
    second = OrgFsRuntime(state, node_id="node-a", author="user:owner")
    try:
        assert (root / "current.md").read_text(encoding="utf-8") == "authoritative"
        assert not (root / "stale").exists()
        status = root / ".orgfs-checkout.json"
        assert status.is_file()
    finally:
        second.checkout(space.space_id, False)
        second.close()
