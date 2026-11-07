"""Runtime survives actual persistence/lifecycle projections; rollback never drops host intent."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import SmolvmRuntimeSpec
from hyprial.daemon.impl.composition import harness_launch_projection
from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    )
from hyprial.kernel import DesiredStateError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.contracts import _launch_spec
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecycleSpec,
    _operation_json,
    _operation_from_json,
)
from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence, DaemonOwnershipBusy
from hyprial.daemon.impl.network.route_ports import RouteSpec
from hyprial.daemon.impl.transfer.landing.runtime import downgrade_state, RuntimeDowngradeError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def schema2_runtime(monkeypatch):
    """Keep historical downgrade safety assertions against the accepted base."""
    from tests.daemon.desired_state.compat import base_runtime
    import sys

    api = base_runtime(monkeypatch)
    module = sys.modules[__name__]
    for name in ("DesiredState", "DesiredStateStore", "HarnessLaunchSpec",
                 "downgrade_state", "RuntimeDowngradeError"):
        monkeypatch.setattr(module, name, getattr(api, name))


def runtime():
    return SmolvmRuntimeSpec(
        "/private/bundle/smolvm",
        "a" * 64,
        "/private/rootfs",
        "b" * 64,
        "/private/bin/resize2fs",
        "c" * 64,
        ("/usr/local/bin/codex",),
        "/usr/bin/python3",
        "d" * 64,
    )


def vm_spec():
    return HarnessLaunchSpec("codex", "vm-worker", True, execution_runtime=runtime())


def snapshot(db):
    with sqlite3.connect(db) as connection:
        return {
            name: connection.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid'
            ).fetchall()
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_sqlite_and_lifecycle_roundtrip_preserve_runtime_and_sticky_schema(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    host = HarnessLaunchSpec("pi", "host", True)
    store.save(DesiredState(harnesses=(host,)))
    assert store.load().schema_version == 3
    vm = vm_spec()
    projection = harness_launch_projection(vm)
    operation = LifecycleOperation.create(
        "op-runtime",
        LifecycleSpec(
            agent_name=vm.name,
            actor="agent:a:n:vm-worker",
            harness=projection,
            route=RouteSpec("agent:a:n:vm-worker", "live/worker", "inbox/worker"),
        ),
    )
    restored = _operation_from_json(_operation_json(operation))
    assert _launch_spec(restored.target.harness) == vm
    store.upsert_harness(_launch_spec(restored.target.harness))
    assert store.load().schema_version == 3
    assert set(store.load().harnesses) == {host, vm}
    # An older in-memory document cannot silently lower the rollback barrier.
    store.save(DesiredState(harnesses=(host,)))
    assert store.load().schema_version == 3
    assert store.load().harnesses == (host,)


def test_v1_new_runtime_and_unknown_keys_fail_closed():
    doc = DesiredState(harnesses=(vm_spec(),)).to_json()
    doc["schemaVersion"] = 1
    with pytest.raises(DesiredStateError, match="requires.*2"):
        DesiredState.from_json(doc)
    for patch in (
        {"owner": "other"},
        {"kind": "docker"},
        {"version": True},
        {"rootfsDigest": "not-a-hash"},
        {"guestCommand": ["codex"]},
        {"rootfsPath": "/tmp/../rootfs"},
    ):
        with pytest.raises(ValueError):
            SmolvmRuntimeSpec.from_json({**runtime().to_json(), **patch})
    for patch in (
        {"provider": "pi"},
        {"containerized": True},
        {"containerImage": "image"},
        {"endpoint": "url"},
    ):
        with pytest.raises(DesiredStateError):
            HarnessLaunchSpec.from_json({**vm_spec().to_json(), **patch}, "worker")


def test_existing_columnless_database_is_migrated_without_losing_host(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    host = HarnessLaunchSpec(
        "codex", "host", True, containerized=True, container_image="pinned"
    )
    store.upsert_harness(host)
    with sqlite3.connect(store.state_db.path) as db:
        db.execute("ALTER TABLE harnesses DROP COLUMN execution_runtime_json")
    assert store.load().harnesses == (host,)
    store.upsert_harness(vm_spec())
    assert store.load().harnesses == (host, vm_spec())


def test_downgrade_requires_offline_clean_resources_and_preserves_other_tables(
    tmp_path, schema2_runtime,
):
    state = tmp_path / "state"
    state.mkdir()
    store = DesiredStateStore(state / "desired-state.json")
    host = HarnessLaunchSpec("pi", "host", True)
    store.save(DesiredState(harnesses=(host, vm_spec())))
    with sqlite3.connect(store.state_db.path) as db:
        db.execute("CREATE TABLE untouched (value TEXT)")
        db.execute("INSERT INTO untouched VALUES ('identity-token-canary')")
    before = snapshot(store.state_db.path)
    with DaemonStateOwnershipFence.acquire(state):
        with pytest.raises(DaemonOwnershipBusy):
            downgrade_state(state, tmp_path / "blocked-lock")
    active = state / "smolvm-runtimes"
    active.mkdir()
    (active / "worker").write_text("owned")
    with pytest.raises(RuntimeDowngradeError, match="resources remain"):
        downgrade_state(state, tmp_path / "blocked-resources")
    assert snapshot(store.state_db.path) == before
    (active / "worker").unlink()
    result = downgrade_state(state, tmp_path / "export")
    assert result["schemaVersion"] == 1
    assert store.load().harnesses == (host,)
    assert snapshot(store.state_db.path)["untouched"] == before["untouched"]
    raw = Path(result["export"]).read_text()
    assert json.loads(raw)["removed"] == [vm_spec().to_json()]
    assert "identity-token-canary" not in raw
    assert (tmp_path / "export").stat().st_mode & 0o777 == 0o700


def test_export_failure_does_not_partially_downgrade(tmp_path, monkeypatch, schema2_runtime):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(vm_spec())
    before = snapshot(store.state_db.path)

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr("hyprial.daemon.impl.transfer.landing.runtime.os.fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        downgrade_state(tmp_path, tmp_path / "export")
    assert snapshot(store.state_db.path) == before


@pytest.mark.parametrize("terminal", ["running", "compensating", "failed", "unknown"])
def test_downgrade_refuses_unsettled_operation(tmp_path, terminal, schema2_runtime):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(vm_spec())
    with sqlite3.connect(store.state_db.path) as db:
        db.execute(
            "CREATE TABLE lifecycle_operations(operation_id TEXT, request_json TEXT, state TEXT)"
        )
        db.execute(
            "INSERT INTO lifecycle_operations VALUES (?,?,?)",
            ("op", json.dumps({"target": vm_spec().to_json()}), terminal),
        )
    before = snapshot(store.state_db.path)
    with pytest.raises(RuntimeDowngradeError, match="lifecycle operation"):
        downgrade_state(tmp_path, tmp_path / "export")
    assert snapshot(store.state_db.path) == before
    assert not (tmp_path / "export").exists()


def test_cli_old_daemon_refused_before_agent_creation_and_new_method_keeps_spec(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime().to_json()))
    calls = []
    supported = False

    def daemon(method, params=None, **_):
        calls.append((method, params))
        if method == "ps":
            return {"executionRuntimes": ["smolvm-v1"]} if supported else {"ok": True}
        if method == "agent.create":
            return {"ok": True, "created": True, "agent": {"name": "worker"}}
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    args = [
        "agent",
        "start",
        "codex",
        "--headless",
        "--name",
        "worker",
        "--smolvm-spec",
        str(path),
        "--json",
    ]
    result = CliRunner().invoke(_cli_root_app, args)
    assert result.exit_code != 0 and "does not support smolvm" in result.output
    assert [m for m, _ in calls] == ["ps"]
    supported = True
    calls.clear()
    result = CliRunner().invoke(_cli_root_app, args)
    assert result.exit_code == 0, result.output
    method, params = calls[-1]
    assert method == "lifecycle.start-smolvm"
    assert params["executionRuntime"] == runtime().to_json()
    assert "command" not in params and str(path) not in json.dumps(params)


def test_daemon_ipc_retains_spec_and_refuses_old_transfer_paths(tmp_path, monkeypatch):
    from tests.integration.transfer.lifecycle.test_transfer_daemon import _application
    from hyprial.daemon.impl.application import DaemonRequestError
    from hyprial.daemon.impl.harnesses import smolvm_runtime

    app = _application(tmp_path, "node")
    app.agents.create("vm-worker")
    monkeypatch.setattr(smolvm_runtime, "validate_materials", lambda spec: None)
    try:
        with pytest.raises(DaemonRequestError, match="requires lifecycle.start-smolvm"):
            app.handle("lifecycle.start", vm_spec().to_json())
        assert app.desired_state.load().schema_version == 3
        app.handle("lifecycle.start-smolvm", vm_spec().to_json())
        assert app.desired_state.load().harnesses[0].execution_runtime == runtime()
        assert app.desired_state.load().schema_version == 3
        with pytest.raises(DaemonRequestError, match="awaits AT07"):
            app.handle("transfer.plan", {"name": "vm-worker"})
    finally:
        app._harnesses.stop()
        app.agents.close()


def test_failed_initial_daemon_launch_leaves_no_desired_row(tmp_path, monkeypatch):
    """AT08 R5 re-ruled onto dev's U0b: a start that never came up leaves no row.

    The AT08 freeze originally required the failed first smolvm launch to keep a
    ``failed`` desired row so the operator could see and re-drive it.  Allen
    re-ruled R5 onto dev's U0b (2026-09-30, relayed by hq-adjutant): a failing
    ``hyprial agent start`` reports the error and leaves nothing in desired state --
    "在创建的时候直接报错并不留记录" -- because the old admission-time row was
    what let a crash or a failed start leave a ghost row that restore kept
    restarting.  R5 minus the retained row still holds: the failure is reported
    as itself, the attempt does not fall back to a host launch (see
    test_codex_spawns_the_runtime_wrapped_argv_not_the_guest_command for the
    argv guard), and this attempt leaves nothing behind.
    """

    from tests.integration.transfer.lifecycle.test_transfer_daemon import _application
    from hyprial.daemon.impl.harnesses import smolvm_runtime

    app = _application(tmp_path, "node")
    app.agents.create("vm-worker")
    monkeypatch.setattr(smolvm_runtime, "validate_materials", lambda spec: None)

    def failure(_):
        raise ValueError("launch failed")

    monkeypatch.setattr(app, "_run_lifecycle_operation", failure)
    try:
        with pytest.raises(ValueError, match="launch failed"):
            app.handle("lifecycle.start-smolvm", vm_spec().to_json())
        state = app.desired_state.load()
        # U0b: no desired row was persisted. Adapter schema 3 is independent
        # of runtime admission and was already established before launch.
        assert state.harnesses == ()
        assert state.schema_version == 3
        # No residue either: the failed attempt leaves no lifecycle operation,
        # effect or meta row behind, so a later start sees a clean slate.
        with sqlite3.connect(app.desired_state.state_db.path) as connection:
            for table in ("lifecycle_operations", "lifecycle_effects", "lifecycle_meta"):
                assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
    finally:
        app._harnesses.stop()
        app.agents.close()
