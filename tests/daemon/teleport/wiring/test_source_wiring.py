"""The real source adapters against a fake daemon application.

No daemon, registry or harness runs here: the fake application records which
lifecycle operations the adapters drive and in what order.  What this
cannot show (a real harness stop, the real export of a real home, a real
registry removal) is reported as unexercised in the slice 5 hand-off.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.daemon.impl.lifecycle_manager import LifecycleKind
from hyprial.daemon.impl.org.network.member_agents.exclusions import (
    TRANSFER_EXCLUSIONS_RELPATH,
    OrgAgentExclusionStore,
)
from hyprial.daemon.impl.teleport import TeleportError
from hyprial.daemon.impl.teleport.source import SourceAgent, TeleportRunError
from hyprial.daemon.impl.teleport.stream import unpack_stream
from hyprial.daemon.impl.teleport.wiring import source as wiring
from hyprial.daemon.impl.teleport.wiring.source_export import export_agent_stream
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.archive.envelope import (
    ensure_recipient_key,
    recipient_key_path,
)
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.identity import AgentHomeError, AgentRegistry
from hyprial.kernel import HarnessLaunchSpec, canonical_user_uri

NAME = "w1"
URI = "agent://owner-1/node-a/w1"
SPEC = HarnessLaunchSpec(
    "claude", NAME, True, args=("--model", "m1"), session_ref="sess-1"
)


class Agents:
    def __init__(self, known: dict[str, Any]) -> None:
        self.known = known

    def get(self, name: str) -> Any:
        return self.known.get(name)

    def local_actor(self, value: str) -> str | None:
        return value if value in self.known else None


class Registry:
    def __init__(self, home: Path | None) -> None:
        self.home = home

    def home_receipt(self, actor: str) -> Any:
        if self.home is None:
            raise AgentHomeError("not-provisioned", actor, "read-registry")
        return SimpleNamespace(path=str(self.home))


class Harnesses:
    def __init__(self) -> None:
        self.reconciled = 0

    def reconcile_session_refs(self) -> dict:
        self.reconciled += 1
        return {}


def record(**overrides: Any) -> Any:
    values = {"actor": NAME, "uri": URI, "adapter_bindings": (), "entity_token": "tok"}
    values.update(overrides)
    return SimpleNamespace(**values)


def app_for(tmp_path: Path, *, specs=(SPEC,), home: Path | None = None) -> Any:
    harnesses = Harnesses()
    return SimpleNamespace(
        node_id="node-a",
        owner="owner-1",
        state_dir=tmp_path / "state",
        agents=Agents({NAME: record()}),
        desired_state=SimpleNamespace(load=lambda: DesiredState(harnesses=tuple(specs))),
        _harnesses=harnesses,
        _agent_registry=Registry(home),
        _routine_service=None,
        _transport=None,
        calls=[],
    )


def test_source_service_needs_a_transport_session(tmp_path: Path) -> None:
    with pytest.raises(TeleportRunError) as caught:
        wiring.source_service(app_for(tmp_path))
    assert caught.value.code == "TELEPORT_WRONG_TARGET"


def test_source_service_is_one_per_application_and_purges_old_exports(
    tmp_path: Path,
) -> None:
    app = app_for(tmp_path)
    app._transport = SimpleNamespace(query=lambda *a, **k: [])
    stale = app.state_dir / wiring.WORKDIR_NAME / "teleport-old"
    stale.mkdir(parents=True)
    (stale / "bundle.tar").write_bytes(b"sealed")
    first = wiring.source_service(app)
    assert wiring.source_service(app) is first
    assert list((app.state_dir / wiring.WORKDIR_NAME).iterdir()) == []
    assert oct((app.state_dir / wiring.WORKDIR_NAME).stat().st_mode & 0o777) == "0o700"


def test_describe_reads_the_launch_spec_after_syncing_session_refs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agents" / NAME
    app = app_for(tmp_path, home=home)
    described = wiring.AppSourceHost(app).describe(NAME)
    assert app._harnesses.reconciled == 1
    assert described == SourceAgent(
        name=NAME,
        uri=URI,
        harness="claude",
        headless=True,
        args=("--model", "m1"),
        session_ref="sess-1",
        cwd=None,
        home=home,
        adapter_bindings=None,
    )


def test_describe_reports_cell_adapter_bindings_once_the_runtime_answers(
    tmp_path: Path,
) -> None:
    from tests.support.identity.cell_runtime import bound_cell, cell_runtime_stub

    agent = SimpleNamespace(
        actor=NAME, uri=URI, agent_id="a." + "a" * 26, owner="owner-1",
        entity_token="tok",
    )
    cell = bound_cell(agent, ("lark-main",))
    app = app_for(tmp_path, home=tmp_path / "agents" / NAME)
    app.agents = Agents({NAME: record(cell_id=cell.cell_id)})
    app._cell_runtime = cell_runtime_stub((cell,))
    assert wiring.AppSourceHost(app).describe(NAME).adapter_bindings == ("lark-main",)
    app._cell_runtime = cell_runtime_stub(())
    assert wiring.AppSourceHost(app).describe(NAME).adapter_bindings is None


def test_describe_without_a_provisioned_home_says_so(tmp_path: Path) -> None:
    described = wiring.AppSourceHost(app_for(tmp_path, home=None)).describe(NAME)
    assert described.home is None


def test_describe_unknown_agent_is_a_lookup_error(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        wiring.AppSourceHost(app_for(tmp_path)).describe("ghost")


def test_describe_agent_without_a_running_worker_is_unsupported(tmp_path: Path) -> None:
    host = wiring.AppSourceHost(app_for(tmp_path, specs=(), home=tmp_path))
    with pytest.raises(TeleportError) as caught:
        host.describe(NAME)
    assert caught.value.code == "TELEPORT_RUNTIME_UNSUPPORTED"


def test_describe_refuses_a_containerized_worker(tmp_path: Path) -> None:
    containerized = HarnessLaunchSpec("claude", NAME, True, containerized=True)
    host = wiring.AppSourceHost(app_for(tmp_path, specs=(containerized,), home=tmp_path))
    with pytest.raises(TeleportError) as caught:
        host.describe(NAME)
    assert caught.value.code == "TELEPORT_RUNTIME_UNSUPPORTED"


def source_agent(home: Path) -> SourceAgent:
    return SourceAgent(
        name=NAME,
        uri=URI,
        harness="claude",
        headless=True,
        args=(),
        session_ref="sess-1",
        cwd=None,
        home=home,
        adapter_bindings=(),
    )


def test_quiesce_and_restart_run_lifecycle_operations_on_the_synced_spec(
    tmp_path: Path,
) -> None:
    app = app_for(tmp_path, home=tmp_path)
    app._lifecycle_spec = lambda spec: ("lifecycle", spec.harness, spec.name, spec.session_ref)
    app._run_lifecycle_operation = lambda operation, **kwargs: app.calls.append(operation)
    host = wiring.AppSourceHost(app)
    agent = source_agent(tmp_path)
    assert host.quiesce(agent) is True
    assert app._harnesses.reconciled == 1
    host.restart(agent)
    stop, start = app.calls
    assert stop.kind is LifecycleKind.DEACTIVATE
    assert stop.target == ("lifecycle", "claude", NAME, "sess-1")
    assert start.kind is LifecycleKind.CREATE
    assert start.target == ("lifecycle", "claude", NAME, "sess-1")
    with pytest.raises(RuntimeError):
        host.restart(agent)


def test_restart_without_a_recorded_spec_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        wiring.AppSourceHost(app_for(tmp_path)).restart(source_agent(tmp_path))


def removal_app(tmp_path: Path, *, still_registered: bool, home: Path) -> Any:
    app = app_for(tmp_path, home=home)
    app._lifecycle_spec = lambda spec: ("lifecycle", spec.harness, spec.name)

    def run(operation: Any, **kwargs: Any) -> None:
        app.calls.append(operation)
        if not still_registered:
            app.agents.known.pop(NAME, None)

    app._run_lifecycle_operation = run
    return app


def test_remove_runs_a_lifecycle_remove_and_checks_row_and_home(tmp_path: Path) -> None:
    home = tmp_path / "agents" / NAME
    app = removal_app(tmp_path, still_registered=False, home=home)
    wiring.AppSourceHost(app).remove(source_agent(home))
    (operation,) = app.calls
    assert operation.kind is LifecycleKind.REMOVE
    assert operation.target == ("lifecycle", "claude", NAME)


def test_remove_fails_when_the_agent_row_survives(tmp_path: Path) -> None:
    home = tmp_path / "agents" / NAME
    app = removal_app(tmp_path, still_registered=True, home=home)
    with pytest.raises(RuntimeError, match="still registered"):
        wiring.AppSourceHost(app).remove(source_agent(home))


def test_remove_fails_when_the_home_directory_survives(tmp_path: Path) -> None:
    home = tmp_path / "agents" / NAME
    home.mkdir(parents=True)
    app = removal_app(tmp_path, still_registered=False, home=home)
    with pytest.raises(RuntimeError, match="not deleted"):
        wiring.AppSourceHost(app).remove(source_agent(home))


class RoutineService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_executed_by(self, *, agent: str) -> dict:
        self.calls.append(("list_executed_by", agent))
        return {"routines": [{"name": "r1", "enabled": True}, {"name": "r2", "enabled": 0}]}

    def export(self, *, name: str) -> dict:
        return {"name": name, "yaml": "name: r1\n", "enabled": True, "owner": "o"}

    def pause(self, *, name: str) -> dict:
        self.calls.append(("pause", name))
        return {}

    def resume(self, *, name: str) -> dict:
        self.calls.append(("resume", name))
        return {}

    def remove(self, *, name: str) -> dict:
        self.calls.append(("remove", name))
        return {}

    def wait_in_flight_drained(self, *, names: Any, timeout_seconds: float) -> list[str]:
        self.calls.append(("wait", tuple(names), timeout_seconds))
        return ["r2"]


def test_routine_adapter_maps_the_routine_service(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    app._routine_service = RoutineService()
    routines = wiring.AppSourceRoutines(app)
    refs = routines.executed_by(URI)
    assert [(ref.name, ref.enabled) for ref in refs] == [("r1", True), ("r2", False)]
    exported = routines.export("r1")
    assert (exported.name, exported.yaml, exported.enabled) == ("r1", "name: r1\n", True)
    routines.pause("r1")
    routines.resume("r1")
    assert routines.wait_drained(["r1", "r2"], 5.0) == ["r2"]
    routines.remove("r1")
    assert [call[0] for call in app._routine_service.calls] == [
        "list_executed_by",
        "pause",
        "resume",
        "wait",
        "remove",
    ]


def test_routine_adapter_without_a_routine_runtime_lists_nothing(tmp_path: Path) -> None:
    assert wiring.AppSourceRoutines(app_for(tmp_path)).executed_by(URI) == []


def test_export_agent_stream_reexports_after_preserved_carried_exclusions(
    tmp_path: Path,
) -> None:
    source_agent_id = "a." + "c" * 26
    carried_payload = json.dumps(
        [{"agentId": source_agent_id, "directorySpace": "space-111"}]
    )

    src_home_a = tmp_path / "src-a"
    src_home_a.mkdir(mode=0o700)
    (src_home_a / "state").mkdir(mode=0o700)
    (src_home_a / "workspace").mkdir(mode=0o700)
    src_carried_a = src_home_a / TRANSFER_EXCLUSIONS_RELPATH
    src_carried_a.write_text(carried_payload, encoding="utf-8")
    src_carried_a.chmod(0o600)
    bundle_a = tmp_path / "bundle-a"
    export_bundle(
        src_home_a,
        bundle_a,
        owner="owner-1",
        actor="worker-a",
        source_machine="node-src",
        created_at="2026-11-23T00:00:00Z",
    )

    mid_home_a = tmp_path / "mid-a"
    mid_home_a.mkdir(mode=0o700)
    dst_home_a = tmp_path / "dst-a"
    dst_home_a.mkdir(mode=0o700)

    src_home_b = tmp_path / "src-b"
    src_home_b.mkdir(mode=0o700)
    (src_home_b / "state").mkdir(mode=0o700)
    (src_home_b / "workspace").mkdir(mode=0o700)
    src_carried_b = src_home_b / TRANSFER_EXCLUSIONS_RELPATH
    src_carried_b.write_text("[]", encoding="utf-8")
    src_carried_b.chmod(0o600)
    bundle_b = tmp_path / "bundle-b"
    export_bundle(
        src_home_b,
        bundle_b,
        owner="owner-1",
        actor="worker-b",
        source_machine="node-src",
        created_at="2026-11-23T00:00:00Z",
    )

    mid_home_b = tmp_path / "mid-b"
    mid_home_b.mkdir(mode=0o700)
    dst_home_b = tmp_path / "dst-b"
    dst_home_b.mkdir(mode=0o700)

    mid_reg_a = AgentRegistry(
        mid_home_a / "agents.sqlite3",
        owner="owner-1",
        machine="node-mid",
        hyprial_home=mid_home_a,
    )
    dst_reg_a = AgentRegistry(
        dst_home_a / "agents.sqlite3",
        owner="owner-1",
        machine="node-dst",
        hyprial_home=dst_home_a,
    )
    mid_reg_b = AgentRegistry(
        mid_home_b / "agents.sqlite3",
        owner="owner-1",
        machine="node-mid",
        hyprial_home=mid_home_b,
    )
    dst_reg_b = AgentRegistry(
        dst_home_b / "agents.sqlite3",
        owner="owner-1",
        machine="node-dst",
        hyprial_home=dst_home_b,
    )
    try:
        landed_mid_a = land_bundle_into_registry(
            bundle_a,
            registry=mid_reg_a,
            home_root=mid_home_a / "agents",
            machine_home=None,
            staging=tmp_path / "staging-mid-a",
            granted_by=canonical_user_uri("owner-1"),
            expected_actor="worker-a",
            cwd=str(mid_home_a / "agents" / "worker-a" / "workspace"),
            harness="codex",
        )
        preserved_a = landed_mid_a.destination / TRANSFER_EXCLUSIONS_RELPATH
        assert preserved_a.read_text(encoding="utf-8") == carried_payload

        mid_row_a = mid_reg_a.require("worker-a")
        OrgAgentExclusionStore(mid_home_a).exclude(mid_row_a.agent_id, "space-222")
        _priv_a, dst_pub_a = ensure_recipient_key(recipient_key_path(dst_home_a))
        mid_app_a = SimpleNamespace(
            owner="owner-1",
            node_id="node-mid",
            hyprial_home=mid_home_a,
            agents=mid_reg_a,
            _agent_registry=mid_reg_a,
        )
        exported_a = export_agent_stream(
            mid_app_a,
            SourceAgent(
                name="worker-a",
                uri=mid_row_a.uri,
                harness="codex",
                home=landed_mid_a.destination,
                headless=True,
                args=(),
                session_ref=None,
                cwd=str(landed_mid_a.destination / "workspace"),
                adapter_bindings=(),
            ),
            {
                "name": "worker-a",
                "harness": "codex",
                "cwd": str(landed_mid_a.destination / "workspace"),
                "args": [],
                "headless": True,
            },
            recipient_public_key=dst_pub_a,
            workdir=tmp_path / "reexport-a",
        )
        unpacked_a = tmp_path / "unpacked-a"
        unpack_stream(exported_a.path, unpacked_a, total=exported_a.total)
        land_bundle_into_registry(
            unpacked_a,
            registry=dst_reg_a,
            home_root=dst_home_a / "agents",
            machine_home=dst_home_a,
            staging=tmp_path / "staging-dst-a",
            granted_by=canonical_user_uri("owner-1"),
            expected_actor="worker-a",
            cwd=str(dst_home_a / "agents" / "worker-a" / "workspace"),
            harness="codex",
        )
        dst_row_a = dst_reg_a.require("worker-a")
        assert OrgAgentExclusionStore(dst_home_a).excluded_spaces(
            dst_row_a.agent_id
        ) == frozenset({"space-111", "space-222"})
        assert preserved_a.read_text(encoding="utf-8") == carried_payload

        landed_mid_b = land_bundle_into_registry(
            bundle_b,
            registry=mid_reg_b,
            home_root=mid_home_b / "agents",
            machine_home=mid_home_b,
            staging=tmp_path / "staging-mid-b",
            granted_by=canonical_user_uri("owner-1"),
            expected_actor="worker-b",
            cwd=str(mid_home_b / "agents" / "worker-b" / "workspace"),
            harness="codex",
        )
        preserved_b = landed_mid_b.destination / TRANSFER_EXCLUSIONS_RELPATH
        assert preserved_b.read_text(encoding="utf-8") == "[]"

        mid_row_b = mid_reg_b.require("worker-b")
        OrgAgentExclusionStore(mid_home_b).exclude(mid_row_b.agent_id, "space-333")
        _priv_b, dst_pub_b = ensure_recipient_key(recipient_key_path(dst_home_b))
        mid_app_b = SimpleNamespace(
            owner="owner-1",
            node_id="node-mid",
            hyprial_home=mid_home_b,
            agents=mid_reg_b,
            _agent_registry=mid_reg_b,
        )
        exported_b = export_agent_stream(
            mid_app_b,
            SourceAgent(
                name="worker-b",
                uri=mid_row_b.uri,
                harness="codex",
                home=landed_mid_b.destination,
                headless=True,
                args=(),
                session_ref=None,
                cwd=str(landed_mid_b.destination / "workspace"),
                adapter_bindings=(),
            ),
            {
                "name": "worker-b",
                "harness": "codex",
                "cwd": str(landed_mid_b.destination / "workspace"),
                "args": [],
                "headless": True,
            },
            recipient_public_key=dst_pub_b,
            workdir=tmp_path / "reexport-b",
        )
        unpacked_b = tmp_path / "unpacked-b"
        unpack_stream(exported_b.path, unpacked_b, total=exported_b.total)
        land_bundle_into_registry(
            unpacked_b,
            registry=dst_reg_b,
            home_root=dst_home_b / "agents",
            machine_home=dst_home_b,
            staging=tmp_path / "staging-dst-b",
            granted_by=canonical_user_uri("owner-1"),
            expected_actor="worker-b",
            cwd=str(dst_home_b / "agents" / "worker-b" / "workspace"),
            harness="codex",
        )
        dst_row_b = dst_reg_b.require("worker-b")
        assert OrgAgentExclusionStore(dst_home_b).excluded_spaces(
            dst_row_b.agent_id
        ) == frozenset({"space-333"})
        assert preserved_b.read_text(encoding="utf-8") == "[]"
    finally:
        mid_reg_a.close()
        dst_reg_a.close()
        mid_reg_b.close()
        dst_reg_b.close()
