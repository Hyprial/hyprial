"""End-to-end daemon revision-driven member reconcile acceptance test (I6-2)."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.cells.model import CellMember
from hyprial.daemon.impl.cells.reconcile.policy import MemberLaunchPolicies, protocol_version
from hyprial.daemon.impl.cells.reconcile import (
    CELL_MEMBER_ENSURE,
    CELL_MEMBER_STOP,
    CellReconcileState,
    ReconcileMemberPhase,
)
from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.lifecycle_manager import _LifecycleStore
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.identity import org_space_name
from hyprial.identity.impl.agents import Agent
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.cells.actor.actor_support import wait_until
from tests.daemon.cells.runtime.cell_runtime_support import uid_for


class _FakeManagedProcess:
    def __init__(self, spec: HarnessLaunchSpec, ready: dict[str, object] | None = None) -> None:
        self.spec = spec
        self.running = True
        self.pid = 4242
        self.stops = 0
        self.last_ready = ready

    def wait_ready(self, *, timeout: float = 5.0) -> bool:
        return self.running

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class _RecordingLauncher:
    """Acknowledges the member policy it launched with, as the claude worker does."""

    def __init__(self, policies: MemberLaunchPolicies) -> None:
        self._lock = threading.Lock()
        self._policies = policies
        self.started: list[tuple[HarnessLaunchSpec, _FakeManagedProcess]] = []

    def start(self, spec: HarnessLaunchSpec) -> _FakeManagedProcess:
        launch = self._policies.policy_for(spec)
        ready: dict[str, object] = {"type": "ready"}
        if launch is not None:
            ready["authority"] = {
                "enabled": True,
                "protocol": protocol_version(),
                "policyDigest": launch.digest,
                "version": launch.version,
            }
        process = _FakeManagedProcess(spec, ready)
        self._policies.record_launch_process(spec, process)
        with self._lock:
            self.started.append((spec, process))
        return process


class _AgentDirectory:
    def __init__(self, *agents: Agent) -> None:
        self.items = list(agents)

    def list(self) -> tuple[Agent, ...]:
        return tuple(self.items)

    def get(self, name: str) -> Agent | None:
        return next(
            (item for item in self.items if name in {item.actor, item.uri, item.agent_id}),
            None,
        )


def test_daemon_composition_revision_changes_add_update_and_remove_members(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db = StateDatabase(state_dir / "state.sqlite3")
    desired_store = DesiredStateStore(state_dir / "desired-state.json", state_db=state_db)
    actor_hosts = ActorHostRegistry()
    harness_events = CorrelatedDomainEvents()
    policies = MemberLaunchPolicies()
    launcher = _RecordingLauncher(policies)
    harness_actor = HarnessRuntimeActor(
        launcher,
        actor_host=actor_hosts.host_for("daemon-runtime"),
        event_sink=harness_events,
        desired_state=desired_store,
    )
    harnesses = HarnessPortClient(
        harness_actor,
        harness_events,
        actor_host=actor_hosts.host_for("daemon-runtime"),
    )

    worker = Agent(
        uri="agent:alice:mac:worker",
        actor="worker",
        owner="alice",
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )
    desired_store.save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness="claude",
                    name="worker",
                    member="w1",
                    headless=True,
                    cwd=str(workdir),
                    command=("/usr/bin/claude",),
                    model="sonnet",
                ),
                HarnessLaunchSpec(
                    harness="claude",
                    name="worker",
                    member="w2",
                    headless=True,
                    cwd=str(workdir),
                    command=("/usr/bin/claude",),
                    model="haiku",
                ),
            )
        )
    )

    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    logged: list[tuple[str, dict[str, object]]] = []
    app = SimpleNamespace(
        state_dir=state_dir,
        hyprial_home=tmp_path / "home",
        owner="alice",
        user_id=uid_for("user:alice"),
        user_issuer="",
        state_db=state_db,
        desired_state=desired_store,
        agents=_AgentDirectory(worker),
        _actor_hosts=actor_hosts,
        _harnesses=harnesses,
        _cell_member_policies=policies,
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=fs),
        _log=lambda _level, _component, event, **fields: logged.append((event, fields)),
    )

    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        coordinator = getattr(app, "_cell_reconcile_coordinator", None)
        assert coordinator is not None

        cell = runtime.lookup(worker.cell_id)
        assert cell is not None
        rev1 = runtime.update(
            cell.cell_id,
            members=(CellMember("w1", "harness"),),
            params={
                "cell.entry": "w1",
                "cell.members": {"w1": {"kind": "claude", "model": "sonnet", "allow": ["Read"]}},
            },
            expect_revision=cell.revision,
        )
        runtime.run_once()

        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.effective_revision == rev1.revision
            and snap.state is CellReconcileState.ACTIVE
            and "w1" in snap.admitting_members
        )
        assert any(spec.member == "w1" and spec.model == "sonnet" for spec, _ in launcher.started)
        assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is True

        rev2 = runtime.update(
            rev1.cell_id,
            members=(CellMember("w1", "harness"), CellMember("w2", "harness")),
            params={
                "cell.entry": "w1",
                "cell.members": {
                    "w1": {"kind": "claude", "model": "opus", "allow": ["Read"]},
                    "w2": {"kind": "claude", "model": "haiku", "allow": ["Read"]},
                },
            },
            expect_revision=rev1.revision,
        )
        runtime.run_once()

        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.effective_revision == rev2.revision
            and "w2" in snap.admitting_members
            and any(m.name == "w1" and m.phase is ReconcileMemberPhase.STOPPING for m in snap.members)
        )
        assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
        assert launcher.started[0][1].stops == 0

        assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is True
        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.state is CellReconcileState.ACTIVE
            and snap.admitting_members == frozenset({"w1", "w2"})
        )
        assert launcher.started[0][1].stops == 1
        assert any(spec.member == "w1" and spec.model == "opus" for spec, _ in launcher.started)
        assert any(spec.member == "w2" and spec.model == "haiku" for spec, _ in launcher.started)

        rev3 = runtime.update(
            rev1.cell_id,
            members=(CellMember("w2", "harness"),),
            params={
                "cell.entry": "w2",
                "cell.members": {"w2": {"kind": "claude", "model": "haiku", "allow": ["Read"]}},
            },
            expect_revision=rev2.revision,
        )
        runtime.run_once()
        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.effective_revision == rev3.revision
            and [m.name for m in snap.members] == ["w2"]
            and snap.state is CellReconcileState.ACTIVE
        )
        w1_opus_processes = [proc for spec, proc in launcher.started if spec.member == "w1" and spec.model == "opus"]
        assert len(w1_opus_processes) == 1 and w1_opus_processes[0].stops == 1
        w2_processes = [proc for spec, proc in launcher.started if spec.member == "w2"]
        assert len(w2_processes) == 1 and w2_processes[0].running is True

        snap_final = coordinator.snapshot(rev1.cell_id)
        assert snap_final is not None
        store = _LifecycleStore(state_db)
        assert (
            store.cell_member_effect_status(
                cell_id=rev1.cell_id,
                epoch=snap_final.epoch,
                revision=rev2.revision,
                member="w1",
                effect_name=CELL_MEMBER_STOP,
            )
            == "completed"
        )
        assert (
            store.cell_member_effect_status(
                cell_id=rev1.cell_id,
                epoch=snap_final.epoch,
                revision=rev2.revision,
                member="w1",
                effect_name=CELL_MEMBER_ENSURE,
            )
            == "completed"
        )
    finally:
        close_cell_runtime(app)
        harnesses.stop(5.0)
        harness_events.close()
        assert actor_hosts.shutdown()


def test_app_local_member_facts_fails_closed_when_agent_missing_with_cross_agent_desired_state(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.composition.cells.reconcile import (
        AppLocalMemberFacts,
        _matching_desired_spec,
    )

    workdir = tmp_path / "other"
    workdir.mkdir()
    desired_store = DesiredStateStore(tmp_path / "desired-state.json")
    other_worker = Agent(
        uri="agent:alice:mac:other",
        actor="other",
        owner="alice",
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )
    desired_store.save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness="claude",
                    name="other",
                    member="w1",
                    headless=True,
                    cwd=str(workdir),
                    command=("/usr/bin/claude",),
                    model="opus",
                ),
            )
        )
    )
    harnesses = desired_store.load().harnesses
    assert _matching_desired_spec(harnesses, None, "w1") is None

    empty_dir_app = SimpleNamespace(agents=_AgentDirectory(), desired_state=desired_store)
    assert AppLocalMemberFacts(empty_dir_app).facts_for("agent:alice:mac:missing", "w1") is None

    other_only_app = SimpleNamespace(agents=_AgentDirectory(other_worker), desired_state=desired_store)
    facts_port = AppLocalMemberFacts(other_only_app)
    assert facts_port.facts_for("agent:alice:mac:missing", "w1") is None
    other_facts = facts_port.facts_for(other_worker.agent_id, "w1")
    assert other_facts is not None
    assert other_facts.cwd == str(workdir)
