"""S3 login daemon orchestration, fences, and failure projection."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'tests/fixtures'))


from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.daemon.impl.network import tailcat as tailcat_module
from hyprial.identity.impl.agents import AgentRegistry, SecretSource
from hyprial.identity.impl.device import DeviceRecord
from hyprial.kernel import DaemonLaunchResult

OLD = "old-owner"
NEW = "alicesmith"

#: The fields the launcher's custody CliError carries across the launch-log
#: boundary (#513): the same bounded set ``_CUSTODY_STARTUP_ERROR_SHAPES``
#: admits from the daemon child's ``--json`` failure line.
CUSTODY_CONFLICT_LAUNCH_DATA = {
    "phase": "startup",
    "errorType": "custodyRefusal",
    "exitCode": 1,
    "logPath": "<state>/daemon-launch.log",
    "old": OLD,
    "new": NEW,
    "grants": 1,
    "homes": 1,
}


def _device_record(owner: str = NEW) -> DeviceRecord:
    return DeviceRecord(
        device_id="orchestrate-test-node",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=1,
    )


def _seed_agent_with_grant(state: Path, home: Path, *, owner: str = OLD) -> None:
    """A real registry-seeded agent holding one live secret grant.

    This is the exact state the startup custody gate exists for: state
    written under ``owner`` whose agents carry credential-delivery
    authority that a silent owner rewrite must not inherit.
    """

    registry = AgentRegistry(
        state / "agents.sqlite3",
        owner=owner,
        machine="node",
        hyprial_home=home,
    )
    try:
        registry.create("worker")
        registry.grant_secret(
            "worker",
            grant_id="provider-demo",
            source=SecretSource.AGENT_PRIVATE,
            entry_id="provider-demo.json",
            field_name="key",
            environment_names=("OPENAI_API_KEY",),
            revision=1,
        )
    finally:
        registry.close()


def _seed_hosted_collision(state: Path, home: Path, hosted_owner: str) -> None:
    registry = AgentRegistry(
        state / "agents.sqlite3", owner=OLD, machine="node", hyprial_home=home,
    )
    try:
        registry.create("native")
        registry.create_transfer_hosted("visitor", pinned_owner=hosted_owner)
        registry.pin("lark:synthetic-alias", "visitor")
    finally:
        registry.close()


def _profile(server) -> NetworkProfile:
    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id="fake-client-id",
    )


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    return home, state


def _seed_agents(state: Path, owner: str = OLD) -> None:
    database = sqlite3.connect(state / "agents.sqlite3")
    with database:
        database.execute("CREATE TABLE agents (owner TEXT, uri TEXT)")
        database.execute(
            "INSERT INTO agents VALUES (?, ?)",
            (owner, f"agent:{owner}:node:worker"),
        )
    database.close()


def _seed_wal_state_after_clean_shutdown(state: Path, owner: str = OLD) -> None:
    """Six WAL-mode migration inputs as a cleanly stopped daemon leaves them.

    A normal daemon shutdown checkpoints and removes every ``-wal``/``-shm``
    file while the database headers stay in WAL mode.  The post-stop replay
    preview must still classify this state, and it must not materialize
    ``-wal``/``-shm`` files of its own in the source directory.
    """

    seeds = {
        "agents.sqlite3": [
            "CREATE TABLE agents (owner TEXT, uri TEXT)",
            f"INSERT INTO agents VALUES ('{owner}', 'agent:{owner}:node:worker')",
        ],
        "adapters.sqlite3": [
            "CREATE TABLE adapters (owner TEXT, uri TEXT)",
            f"INSERT INTO adapters VALUES ('{owner}', 'adapter:{owner}:node:feishu')",
        ],
        "inbox.sqlite3": ["CREATE TABLE empty (value TEXT)"],
        "lifecycle-operations.sqlite3": ["CREATE TABLE empty (value TEXT)"],
        "routines.sqlite3": ["CREATE TABLE empty (value TEXT)"],
        "workflows.sqlite3": ["CREATE TABLE empty (value TEXT)"],
    }
    for name, statements in seeds.items():
        database = sqlite3.connect(state / name)
        database.execute("PRAGMA journal_mode=WAL")
        with database:
            for statement in statements:
                database.execute(statement)
        database.close()
    (state / "desired-state.json.v1").write_text(
        json.dumps({"actor": f"agent:{owner}:node:worker"}), encoding="utf-8"
    )
    (state / "users.json").write_text(
        json.dumps({"owner": owner}), encoding="utf-8"
    )
    leftovers = [
        path.name for path in state.iterdir() if path.name.endswith(("-wal", "-shm"))
    ]
    assert leftovers == [], f"clean shutdown left journal files: {leftovers}"


def _launch_result(profile: NetworkProfile, *, epoch: str = "new-epoch") -> DaemonLaunchResult:
    return DaemonLaunchResult.launched(
        {
            "running": True,
            "pid": 4242,
            "epoch": epoch,
            "phase": "restoring",
            "owner": NEW,
            "identityMode": "casdoor",
            "identityIssuer": profile.issuer,
            "migration": {"status": "applied", "rewrittenCells": 2},
            "zenoh": {"listen": ["tcp/127.0.0.1:7447"], "connect": []},
        }
    )


def _invoke_login(
    server,
    monkeypatch: pytest.MonkeyPatch,
    *arguments: str,
    device_error: BaseException | None = None,
    launch=None,
):
    profile = _profile(server)
    monkeypatch.setattr(_cli_get_services(), "resolve_profile", lambda: (profile, "test-profile"))

    def ensure_device_key(_home, *, owner, device_id, environ=None):
        if device_error is not None:
            raise device_error
        return _device_record(owner)

    monkeypatch.setattr(tailcat_module, "ensure_device_key", ensure_device_key)
    if launch is not None:
        monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)
    return CliRunner().invoke(
        _cli_root_app,
        ["login", "--json", "--no-open", *arguments],
    )


# -- S3 review e2: fail-safe daemon-absence proof (HIGH) -------------------------


def _write_generation_records(
    home: Path,
    state: Path,
    *,
    pid: int,
    process_identity: str | None,
) -> None:
    """Leave the durable records a real ``daemon stop`` survivor leaves.

    ``daemon.lock`` is free (teardown finished, `_close` released it), the
    socket is gone (probe answers nothing), and both the heartbeat and the
    state marker still name the old generation's pid — the 2026-08-30
    shape: lock free, process alive.
    """

    heartbeat: dict[str, object] = {
        "pid": pid,
        "heartbeatMonotonic": time.monotonic(),
        "keepaliveDurationSeconds": 30,
    }
    if process_identity is not None:
        heartbeat["processIdentity"] = process_identity
    (home / ".active_daemon").write_text(json.dumps(heartbeat), encoding="utf-8")
    (state / "daemon.json").write_text(
        json.dumps({"pid": pid}), encoding="utf-8"
    )


def _spawn_watched_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
