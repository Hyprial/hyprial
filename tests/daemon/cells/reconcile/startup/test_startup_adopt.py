"""Daemon-startup member reconcile: ownership, barrier, idempotent ensure (I6-2).

These cases pin I6-2's restore and adoption rules without a member policy,
so the I6-5 registrar seed of the claude template rules is disabled here;
the seeded variants live in ``test_startup_adopt_policy.py``.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.harness_args import (
    _HarnessArgsLifecycleMixin,
)
from hyprial.daemon.impl.cells.reconcile import (
    CellReconcileState,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
    ReconcileMemberPhase,
)
from hyprial.daemon.impl.cells.reconcile.policy import MemberLaunchPolicies
from hyprial.daemon.impl.cells.reconcile.effects import (
    DesiredMemberSpec,
    DictLocalMemberFacts,
    ReconcileFence,
    ReconcileMemberKey,
)
from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.composition.cells import agents as cell_agents
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.composition.cells.reconcile import (
    AppMemberEffectPort,
    CellReconcileStartBarrier,
)
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.lifecycle_manager import _LifecycleStore
from hyprial.daemon.impl.lifecycle_manager.steps import (
    CELL_MEMBER_ENSURE,
    CELL_MEMBER_STOP,
    cell_member_effect_operation_id,
)
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.identity import org_space_name
from hyprial.identity.impl.agents import Agent
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.cells.actor.actor_support import wait_until
from tests.daemon.cells.reconcile.test_daemon_reconcile_e2e import (
    _AgentDirectory,
    _RecordingLauncher,
)
from tests.daemon.cells.runtime.cell_runtime_support import uid_for


@pytest.fixture(autouse=True)
def _no_template_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled")


def _worker(harness_args: dict[str, tuple[str, ...]] | None = None) -> Agent:
    return Agent(
        uri="agent:alice:mac:worker",
        actor="worker",
        owner="alice",
        machine="mac",
        entity_token=uuid.uuid4().hex,
        harness_args=harness_args or {},
    )


def _legacy_spec(workdir: Path, *, status: str) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="claude",
        name="worker",
        headless=True,
        cwd=str(workdir),
        command=("/usr/bin/claude",),
        model="sonnet",
        status=status,
    )


class _Directory(_AgentDirectory):
    def current(self, name: str) -> Agent | None:
        return self.get(name)


class _ArgsHolder(_HarnessArgsLifecycleMixin):
    def __init__(self, agents: _Directory) -> None:
        self.agents = agents


class _World:
    def __init__(
        self,
        tmp_path: Path,
        *,
        barrier: CellReconcileStartBarrier,
        status: str = "running",
        harness_args: dict[str, tuple[str, ...]] | None = None,
        row: dict[str, object] | None = None,
    ) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_db = StateDatabase(state_dir / "state.sqlite3")
        desired = DesiredStateStore(state_dir / "desired-state.json", state_db=state_db)
        self.spec = replace(_legacy_spec(workdir, status=status), **(row or {}))
        desired.save(DesiredState(harnesses=(self.spec,)))
        self.spec = desired.load().harnesses[0]
        self.actor_hosts = ActorHostRegistry()
        self.events = CorrelatedDomainEvents()
        self.policies = MemberLaunchPolicies()
        self.launcher = _RecordingLauncher(self.policies)
        actor = HarnessRuntimeActor(
            self.launcher,
            actor_host=self.actor_hosts.host_for("daemon-runtime"),
            event_sink=self.events,
            desired_state=desired,
        )
        self.harnesses = HarnessPortClient(
            actor, self.events, actor_host=self.actor_hosts.host_for("daemon-runtime")
        )
        self.worker = _worker(harness_args)
        directory = _Directory(self.worker)
        fs = LocalOrgFs(author="user:alice")
        fs.create_space(org_space_name("group-acme"))
        self.logged: list[tuple[str, dict[str, object]]] = []
        self.barrier = barrier
        self.app = SimpleNamespace(
            state_dir=state_dir,
            hyprial_home=tmp_path / "home",
            owner="alice",
            user_id=uid_for("user:alice"),
            user_issuer="",
            state_db=state_db,
            desired_state=desired,
            agents=directory,
            _actor_hosts=self.actor_hosts,
            _harnesses=self.harnesses,
            _require_orgfs_runtime=lambda: SimpleNamespace(facade=fs),
            _log=lambda _level, _component, event, **fields: self.logged.append(
                (event, fields)
            ),
            _effective_harness_args=_ArgsHolder(directory)._effective_harness_args,
            _cell_reconcile_barrier=barrier,
            _cell_member_policies=self.policies,
        )
        self.runtime = cell_runtime(self.app)
        assert self.runtime is not None
        self.coordinator = self.app._cell_reconcile_coordinator
        assert self.coordinator is not None

    def declare_member(self, fields: dict[str, object] | None = None) -> None:
        cell = self.runtime.lookup(self.worker.cell_id)
        assert cell is not None
        self.runtime.update(
            cell.cell_id,
            params={**dict(cell.params), "cell.members": {self.worker.uri: fields or {}}},
            expect_revision=cell.revision,
        )
        self.runtime.run_once()

    def persisted_row(self) -> HarnessLaunchSpec:
        rows = self.app.desired_state.load().harnesses
        assert len(rows) == 1
        return rows[0]

    def member(self) -> object | None:
        snap = self.coordinator.snapshot(self.worker.cell_id)
        if snap is None:
            return None
        return next((m for m in snap.members if m.name == self.worker.uri), None)

    def member_phase(self, phase: ReconcileMemberPhase) -> bool:
        member = self.member()
        return member is not None and member.phase is phase

    def close(self) -> None:
        close_cell_runtime(self.app)
        self.harnesses.stop(5.0)
        self.events.close()
        assert self.actor_hosts.shutdown()


def test_undeclared_agent_cell_stays_owned_by_restore(tmp_path: Path) -> None:
    for status in ("failed", "running"):
        world = _World(
            tmp_path / status, barrier=CellReconcileStartBarrier(opened=False), status=status
        )
        try:
            world.barrier.open()
            wait_until(
                lambda: (snap := world.coordinator.snapshot(world.worker.cell_id)) is not None
                and snap.state is CellReconcileState.EMPTY
            )
            assert world.member() is None
            assert world.launcher.started == []
        finally:
            world.close()


def test_declared_failed_member_stays_down_whatever_the_revision(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), status="failed")
    try:
        world.declare_member()
        world.barrier.open()
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.FAILED))
        assert world.member().last_error == "member-held"
        assert world.launcher.started == []

        cell = world.runtime.lookup(world.worker.cell_id)
        assert cell is not None
        renamed = world.runtime.update(
            cell.cell_id, name="worker-renamed", expect_revision=cell.revision
        )
        world.runtime.run_once()
        world.declare_member({"model": "opus"})
        wait_until(
            lambda: (snap := world.coordinator.snapshot(world.worker.cell_id)) is not None
            and snap.revision > renamed.revision
        )
        world.runtime.run_once()
        assert world.member_phase(ReconcileMemberPhase.FAILED)
        assert world.member().last_error == "member-held"
        assert world.launcher.started == []
        assert world.persisted_row().status == "failed"
    finally:
        world.close()


def test_declared_member_starts_once_the_startup_barrier_opens(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        world.declare_member()
        assert world.coordinator.snapshot(world.worker.cell_id) is None
        assert world.launcher.started == []

        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1
        started_spec = world.launcher.started[0][0]
        assert started_spec.name == "worker"
        assert started_spec.member in {None, "worker"}
    finally:
        world.close()


def test_ensure_adopts_a_running_restored_harness_without_restart(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member()
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1
        assert world.launcher.started[0][1].stops == 0
        assert world.launcher.started[0][1].running is True
    finally:
        world.close()


def test_restored_member_with_merged_harness_args_is_not_relaunched(tmp_path: Path) -> None:
    world = _World(
        tmp_path,
        barrier=CellReconcileStartBarrier(opened=False),
        harness_args={"claude": ("--verbose",)},
    )
    try:
        restored = replace(world.spec, args=("--verbose",))
        world.harnesses.start(restored)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member()
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1
        assert world.launcher.started[0][0].args == ("--verbose",)
        assert world.launcher.started[0][1].stops == 0
        assert world.launcher.started[0][1].running is True
        assert not any(
            event == "cell.reconcile.member.adopted_with_difference"
            for event, _fields in world.logged
        )
    finally:
        world.close()


def test_live_member_whose_spec_differs_from_the_document_is_restarted(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        world.harnesses.start(replace(world.spec, args=("--verbose",)))
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member()
        world.barrier.open()

        wait_until(lambda: len(world.launcher.started) == 2)
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert world.launcher.started[0][1].stops == 1
        assert world.launcher.started[1][0] == world.spec
        assert world.launcher.started[1][1].running is True
    finally:
        world.close()


_FULL_ROW: dict[str, object] = {
    "session_ref": "sess-1",
    "containerized": True,
    "container_image": "img",
    "pinned_owner": "alice",
    "nickname": "nick",
    "idle_timeout_seconds": 5.0,
}


def test_reconcile_start_launches_the_full_restore_spec(tmp_path: Path) -> None:
    world = _World(
        tmp_path,
        barrier=CellReconcileStartBarrier(opened=False),
        harness_args={"claude": ("--verbose",)},
        row=_FULL_ROW,
    )
    try:
        world.declare_member()
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1
        assert world.launcher.started[0][0] == replace(world.spec, args=("--verbose",))
    finally:
        world.close()


def test_document_model_change_relaunches_the_full_spec_and_persists_it(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), row=_FULL_ROW)
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member()
        world.barrier.open()
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1

        world.declare_member({"model": "opus"})
        wait_until(lambda: len(world.launcher.started) == 2)
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert world.launcher.started[0][1].stops == 1
        assert world.launcher.started[1][0] == replace(world.spec, model="opus")
        assert world.persisted_row() == replace(world.spec, model="opus")
    finally:
        world.close()


def test_document_edited_while_down_restarts_the_adopted_member(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), row=_FULL_ROW)
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member({"model": "opus"})
        world.barrier.open()

        wait_until(lambda: len(world.launcher.started) == 2)
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert world.launcher.started[0][1].stops == 1
        assert world.launcher.started[1][0] == replace(world.spec, model="opus")
        assert world.persisted_row() == replace(world.spec, model="opus")
        member = world.member()
        assert member is not None and member.last_error is None
        snap = world.coordinator.snapshot(world.worker.cell_id)
        assert snap is not None
        store = _LifecycleStore(world.app.state_db)
        fence = {
            "cell_id": world.worker.cell_id,
            "epoch": snap.epoch,
            "revision": snap.effective_revision,
            "member": world.worker.uri,
        }
        assert store.cell_member_effect_status(**fence, effect_name=CELL_MEMBER_ENSURE) == (
            "completed"
        )
        assert store.cell_member_effect_status(**fence, effect_name=CELL_MEMBER_STOP) == (
            "completed"
        )
        assert store.pending() == ()
        with world.app.state_db.transaction() as db:
            journaled = db.execute(
                "SELECT resource_token FROM lifecycle_effects WHERE operation_id = ?",
                (cell_member_effect_operation_id(**fence, effect_name=CELL_MEMBER_ENSURE),),
            ).fetchone()
        assert journaled is not None and journaled[0] == member.active_digest

        cell = world.runtime.lookup(world.worker.cell_id)
        assert cell is not None
        world.runtime.update(cell.cell_id, name="worker-renamed", expect_revision=cell.revision)
        world.runtime.run_once()
        wait_until(
            lambda: (snap := world.coordinator.snapshot(world.worker.cell_id)) is not None
            and snap.effective_revision == cell.revision + 1
        )
        assert len(world.launcher.started) == 2
    finally:
        world.close()


def test_persisted_document_change_is_adopted_after_a_daemon_restart(tmp_path: Path) -> None:
    world = _World(
        tmp_path,
        barrier=CellReconcileStartBarrier(opened=False),
        row={**_FULL_ROW, "model": "opus"},
    )
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member({"model": "opus"})
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert len(world.launcher.started) == 1
        assert world.launcher.started[0][1].stops == 0
    finally:
        world.close()


def test_drift_restart_runs_once_when_the_stop_does_not_take(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        removes: list[object] = []
        world.harnesses.remove = lambda *args, **kwargs: removes.append(args)
        world.declare_member({"model": "opus"})
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.FAILED))
        assert world.member().last_error == "config-digest-mismatch"
        assert len(removes) == 1
        assert len(world.launcher.started) == 1
    finally:
        world.close()


def test_model_write_back_keeps_a_session_ref_learned_during_start(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        real_start = world.harnesses.start

        def start_and_learn_session(spec: HarnessLaunchSpec) -> object:
            result = real_start(spec)
            world.app.desired_state.sync_harness_session_refs(
                {("claude", "worker", "worker"): "learned-sess"}
            )
            return result

        world.harnesses.start = start_and_learn_session
        world.declare_member({"model": "opus"})
        world.barrier.open()

        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        assert world.persisted_row() == replace(
            world.spec, model="opus", session_ref="learned-sess"
        )
    finally:
        world.close()


def test_document_kind_change_replaces_the_restored_harness(tmp_path: Path) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), row=_FULL_ROW)
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.declare_member({"kind": "codex"})
        world.barrier.open()

        wait_until(lambda: len(world.launcher.started) == 2)
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.READY))
        live = [spec.harness for spec, handle in world.launcher.started if handle.running]
        assert live == ["codex"]
        assert world.launcher.started[0][1].stops == 1
        assert world.launcher.started[1][0].harness == "codex"
        assert world.launcher.started[1][0].session_ref is None
        row = world.persisted_row()
        assert (row.harness, row.session_ref) == ("claude", "sess-1")
    finally:
        world.close()


def test_a_raising_barrier_callback_does_not_skip_the_others() -> None:
    barrier = CellReconcileStartBarrier(opened=False)
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    barrier.when_open(boom)
    barrier.when_open(lambda: ran.append("second"))
    errors = barrier.open()
    assert ran == ["second"]
    assert [str(error) for error in errors] == ["boom"]
    assert barrier.is_open


class _RecordingHarnessPort:
    def __init__(self) -> None:
        self.starts: list[HarnessLaunchSpec] = []
        self.actor = SimpleNamespace(
            start_timeout=0.0,
            read_harness=lambda _harness_id: None,
            read_harnesses=lambda: (),
            projection=SimpleNamespace(read_launch_spec=lambda _harness_id: None),
        )

    def start(self, spec: HarnessLaunchSpec) -> bool:
        self.starts.append(spec)
        return True

    def wait_ready(self, *_args: object, **_kwargs: object) -> bool:
        return False


class _CompletionSink:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.completions: list[MemberEffectCompletion] = []

    def complete_effect(self, completion: MemberEffectCompletion) -> None:
        self.completions.append(completion)
        self.done.set()


def test_invalid_member_name_never_reaches_harness_start() -> None:
    worker = _worker()
    port = _RecordingHarnessPort()
    app = SimpleNamespace(_harnesses=port, agents=_AgentDirectory(worker), state_db=None)
    effects = AppMemberEffectPort(app, facts_port=DictLocalMemberFacts({}))
    sink = _CompletionSink()
    effects.bind_coordinator(sink)  # type: ignore[arg-type]
    spec = DesiredMemberSpec(
        key=ReconcileMemberKey(worker.agent_id, worker.uri),
        role="harness",
        harness="claude",
        model=None,
        model_vendor=None,
        cwd="/tmp",
        command=("/usr/bin/claude",),
        args=(),
        approval=None,
        env_ref=None,
        config_digest="d",
    )
    try:
        effects.submit(
            MemberEffectIntent(
                fence=ReconcileFence(worker.cell_id, "e1", 1, worker.uri),
                attempt_id="a1",
                agent_id=worker.agent_id,
                action=MemberEffectAction.ENSURE,
                spec=spec,
            )
        )
        assert sink.done.wait(5.0)
    finally:
        effects.close()
    assert port.starts == []
    assert sink.completions[0].succeeded is False
    assert sink.completions[0].error_code == "HARNESS_START_FAILED"
