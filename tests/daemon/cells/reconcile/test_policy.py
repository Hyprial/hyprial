"""Cell member launch policy: document allow/deny applied through reconcile (I6-5a).

Migrated from ``tests/daemon/authority_set/test_supervisor.py``,
``test_launch_handshake.py`` and ``tests/daemon/authority_service/
test_restart_basis.py``: apply, ready acknowledgement, digest and rollback
scenarios now run through the reconcile effect port and the real harness
runtime actor, with only the process launcher faked as an isolated worker.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.reconcile import (
    CellReconcileCoordinator,
    CellReconcileState,
    DictLocalMemberFacts,
    LocalMemberLaunchFacts,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberRuntimeRecord,
    ReconcileMemberPhase,
    ReconcileStepKind,
    plan_cell_reconcile,
)
from hyprial.daemon.impl.cells.reconcile.policy import (
    POLICY_HARNESSES,
    MemberLaunchPolicies,
    PolicyHandshakeError,
    policy_digest,
    protocol_version,
    require_policy_ack,
    resolve_member_policy,
    supports_policy,
)
from hyprial.daemon.impl.cells.structure import (
    MEMBERS_POLICY_OVERLAP,
    MEMBERS_POLICY_TYPE,
    parse_structure,
)
from hyprial.daemon.impl.composition.cells.reconcile import AppMemberEffectPort
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.capabilities import support
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.identity.impl.agents import Agent
from tests.daemon.cells.runtime.cell_runtime_support import authority_for
from hyprial.kernel import (
    DECLARED_HARNESSES,
    Capability,
    HarnessLaunchSpec,
    SupportLevel,
    subject_cell_id,
)
from tests.daemon.cells.actor.actor_support import wait_until

WORKER_MODEL = (
    Path(__file__).resolve().parents[4]
    / "src/hyprial/daemon/impl/harnesses/agent_sdk/authority_model.py"
)
REFUSE_RULE = "Bash(refuse-ack:*)"


def _worker_model() -> Any:
    spec = importlib.util.spec_from_file_location("_test_worker_authority_model", WORKER_MODEL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cell(
    agent_id: str,
    *,
    member: dict[str, object] | None = None,
    revision: int = 1,
) -> Cell:
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=authority_for("user:owner1"),
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        name="worker",
        members=(CellMember("w1", "harness"),),
        params={"cell.members": {"w1": member}} if member is not None else {},
        revision=revision,
    )


class _WorkerProcess:
    """An isolated worker: it acknowledges the policy it actually received."""

    pid = None

    def __init__(self, ready: dict[str, object]) -> None:
        self.running = True
        self.last_ready = ready

    def wait_ready(self, *, timeout: float = 5.0) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False


class _WorkerLauncher:
    """Stands in for the worker-channel factory plus the Agent SDK worker."""

    def __init__(self, policies: MemberLaunchPolicies) -> None:
        self._policies = policies
        self._model = _worker_model()
        self._lock = threading.Lock()
        self.launches: list[tuple[HarnessLaunchSpec, dict[str, object] | None]] = []
        self.processes: list[_WorkerProcess] = []

    def start(self, spec: HarnessLaunchSpec) -> _WorkerProcess:
        launch = self._policies.policy_for(spec)
        options = None if launch is None else dict(launch.launch_options)
        ready: dict[str, object] = {"type": "ready"}
        if launch is not None and options is not None:
            allow = list(options.get("allowedTools", []))
            deny = list(options.get("disallowedTools", []))
            echoed = allow + ["tampered"] if REFUSE_RULE in allow else allow
            ready["authority"] = {
                "enabled": True,
                "protocol": self._model.AUTHORITY_PROTOCOL_VERSION,
                "policyDigest": self._model.policy_digest(
                    harness="claude", headless=True, allow=echoed, deny=deny
                ),
                "version": launch.version,
            }
        process = _WorkerProcess(ready)
        self._policies.record_launch_process(spec, process)
        with self._lock:
            self.launches.append((spec, options))
            self.processes.append(process)
        return process


def _rig(tmp_path: Path) -> tuple[CellReconcileCoordinator, _WorkerLauncher, str, Any]:
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db = StateDatabase(state_dir / "state.sqlite3")
    desired = DesiredStateStore(state_dir / "desired-state.json", state_db=state_db)
    policies = MemberLaunchPolicies()
    launcher = _WorkerLauncher(policies)
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=desired)
    harnesses = HarnessPortClient(actor, events)
    worker = Agent(
        uri="agent:alice:mac:worker",
        actor="worker",
        owner="alice",
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )
    app = SimpleNamespace(
        agents=SimpleNamespace(list=lambda: (worker,)),
        state_db=None,
        _harnesses=harnesses,
        _cell_member_policies=policies,
        logged=[],
    )
    facts = DictLocalMemberFacts(
        {
            (worker.agent_id, "w1"): LocalMemberLaunchFacts(
                agent_id=worker.agent_id,
                member="w1",
                harness="claude",
                cwd=str(workdir),
                command=("/usr/bin/claude",),
            )
        }
    )
    app._log = lambda level, _domain, event, **fields: app.logged.append(
        (level, event, fields)
    )
    port = AppMemberEffectPort(app, facts_port=facts)
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=port)
    port.bind_coordinator(coordinator)
    return coordinator, launcher, worker.agent_id, port


def _policy_events(port: Any) -> list[tuple[str, str, dict[str, Any]]]:
    return [item for item in port._app.logged if item[1].startswith("cell.member_policy_")]


def _member(coordinator: CellReconcileCoordinator, cell_id: str) -> Any:
    snapshot = coordinator.snapshot(cell_id)
    assert snapshot is not None
    return snapshot.members[0]


def _digest(allow: tuple[str, ...], deny: tuple[str, ...]) -> str:
    policy = resolve_member_policy("claude", allow, deny)
    assert policy is not None
    return policy.digest


def test_document_deny_reaches_the_member_launch(tmp_path: Path) -> None:
    coordinator, launcher, agent_id, port = _rig(tmp_path)
    try:
        cell = _cell(agent_id, member={"allow": ["Read"], "deny": ["Bash(rm:*)"]})
        coordinator.reconcile_cell(cell)
        wait_until(
            lambda: _member(coordinator, cell.cell_id).phase is ReconcileMemberPhase.READY,
            timeout=5.0,
        )
        spec, options = launcher.launches[-1]
        assert spec.member == "w1"
        assert options == {"allowedTools": ["Read"], "disallowedTools": ["Bash(rm:*)"]}
        member = _member(coordinator, cell.cell_id)
        assert member.policy_digest == _digest(("Read",), ("Bash(rm:*)",))
        assert coordinator.snapshot(cell.cell_id).state is CellReconcileState.ACTIVE
    finally:
        port.close()


def test_allow_revision_restarts_verifies_and_reports_the_new_digest(tmp_path: Path) -> None:
    coordinator, launcher, agent_id, port = _rig(tmp_path)
    try:
        rev1 = _cell(agent_id, member={"allow": ["Read"]})
        coordinator.reconcile_cell(rev1)
        wait_until(
            lambda: _member(coordinator, rev1.cell_id).phase is ReconcileMemberPhase.READY,
            timeout=5.0,
        )
        rev2 = _cell(agent_id, member={"allow": ["Read", "Edit"]}, revision=2)
        coordinator.reconcile_cell(rev2)
        wait_until(
            lambda: _member(coordinator, rev2.cell_id).policy_digest
            == _digest(("Read", "Edit"), ()),
            timeout=5.0,
        )
        assert len(launcher.launches) == 2
        assert launcher.processes[0].running is False
        assert launcher.launches[-1][1] == {"allowedTools": ["Read", "Edit"]}
        snapshot = coordinator.snapshot(rev2.cell_id)
        assert snapshot.state is CellReconcileState.ACTIVE
        assert snapshot.effective_revision == 2
        assert _member(coordinator, rev2.cell_id).phase is ReconcileMemberPhase.READY
        events = _policy_events(port)
        assert [(level, event) for level, event, _fields in events] == [
            ("info", "cell.member_policy_applied"),
            ("info", "cell.member_policy_apply_started"),
            ("info", "cell.member_policy_applied"),
        ]
        assert events[0][2]["action"] == "ensure"
        applied = events[-1][2]
        assert applied["action"] == "apply"
        assert applied["cell"] == rev2.cell_id and applied["member"] == "w1"
        assert applied["harness"] == "claude" and applied["revision"] == 2
        assert applied["policyDigest"] == _digest(("Read", "Edit"), ())
        assert applied["previousPolicyDigest"] == _digest(("Read",), ())
    finally:
        port.close()


def test_failed_ready_ack_keeps_the_last_effective_policy_and_degrades(
    tmp_path: Path,
) -> None:
    coordinator, launcher, agent_id, port = _rig(tmp_path)
    try:
        rev1 = _cell(agent_id, member={"allow": ["Read"]})
        coordinator.reconcile_cell(rev1)
        wait_until(
            lambda: _member(coordinator, rev1.cell_id).phase is ReconcileMemberPhase.READY,
            timeout=5.0,
        )
        rev2 = _cell(agent_id, member={"allow": ["Read", REFUSE_RULE]}, revision=2)
        coordinator.reconcile_cell(rev2)
        wait_until(
            lambda: coordinator.snapshot(rev2.cell_id).state is CellReconcileState.DEGRADED,
            timeout=5.0,
        )
        wait_until(lambda: len(launcher.launches) == 3, timeout=5.0)
        assert [options for _spec, options in launcher.launches] == [
            {"allowedTools": ["Read"]},
            {"allowedTools": ["Read", REFUSE_RULE]},
            {"allowedTools": ["Read"]},
        ]
        member = _member(coordinator, rev2.cell_id)
        assert member.policy_digest == _digest(("Read",), ())
        assert member.running is True
        assert member.admitting_work is False
        assert "different policy digest" in (member.last_error or "")
        assert rev2.param("cell.members") == {"w1": {"allow": ["Read", REFUSE_RULE]}}
        assert coordinator.can_admit_work(rev2.cell_id, "w1") is False
        failed = [fields for level, event, fields in _policy_events(port)
                  if event == "cell.member_policy_apply_failed"]
        assert len(failed) == 1
        assert failed[0]["code"] == "POLICY_NOT_ACKNOWLEDGED"
        assert failed[0]["rolledBack"] is True and failed[0]["rollbackCode"] is None
        assert failed[0]["policyDigest"] == _digest(("Read", REFUSE_RULE), ())
        assert failed[0]["previousPolicyDigest"] == _digest(("Read",), ())

        rev3 = _cell(agent_id, member={"allow": ["Read"]}, revision=3)
        coordinator.reconcile_cell(rev3)
        wait_until(
            lambda: coordinator.snapshot(rev3.cell_id).state is CellReconcileState.ACTIVE,
            timeout=5.0,
        )
        assert len(launcher.launches) == 3
    finally:
        port.close()


def test_isolated_worker_loads_the_policy_model_from_its_sibling_path() -> None:
    worker = WORKER_MODEL.with_name("_agent_sdk_worker.py")
    assert 'with_name("authority_model.py")' in worker.read_text(encoding="utf-8")
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "assert not any(name.startswith('hyprial') for name in sys.modules)\n"
        "print(m.policy_digest(harness='claude', headless=True, allow=['Read'], deny=[]))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(WORKER_MODEL)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=str(WORKER_MODEL.parent),
    )
    assert result.stdout.strip() == _digest(("Read",), ())


def test_parser_reads_member_allow_and_deny_verbatim() -> None:
    cell = _cell("a.policy", member={"allow": ["Read", "Edit"], "deny": ["Bash(rm:*)"]})
    member = parse_structure(cell).member("w1")
    assert member is not None
    assert member.allow == ("Read", "Edit")
    assert member.deny == ("Bash(rm:*)",)
    absent = parse_structure(_cell("a.policy", member={"kind": "claude"})).member("w1")
    assert absent is not None and absent.allow is None and absent.deny is None


def test_parser_reports_bad_policy_types_and_overlap() -> None:
    bad = parse_structure(_cell("a.policy", member={"allow": "Read", "deny": [1]}))
    assert [problem.code for problem in bad.problems] == [
        MEMBERS_POLICY_TYPE,
        MEMBERS_POLICY_TYPE,
    ]
    assert bad.member("w1").allow is None
    dup = parse_structure(_cell("a.policy", member={"allow": ["Read", "Read"]}))
    assert [problem.code for problem in dup.problems] == [MEMBERS_POLICY_TYPE]
    overlap = parse_structure(_cell("a.policy", member={"allow": ["Read"], "deny": ["Read"]}))
    assert [problem.code for problem in overlap.problems] == [MEMBERS_POLICY_OVERLAP]
    assert overlap.member("w1").allow is None and overlap.member("w1").deny is None


def _facts(agent_id: str, harness: str = "claude") -> DictLocalMemberFacts:
    return DictLocalMemberFacts(
        {
            (agent_id, "w1"): LocalMemberLaunchFacts(
                agent_id=agent_id,
                member="w1",
                harness=harness,
                cwd="/tmp/work",
                command=("/usr/bin/claude",),
            )
        }
    )


def test_planner_plans_policy_apply_only_for_a_policy_only_change() -> None:
    agent_id = "a.planner"
    rev1 = plan_cell_reconcile(
        _cell(agent_id, member={"allow": ["Read"]}),
        current_members={},
        facts_port=_facts(agent_id),
    )
    spec1 = rev1.actions[0].desired
    assert spec1 is not None and spec1.policy_digest == _digest(("Read",), ())
    running = MemberRuntimeRecord(
        member="w1",
        phase=ReconcileMemberPhase.READY,
        active_digest=spec1.config_digest,
        running=True,
        active_base_digest=spec1.base_digest,
    )
    policy_only = plan_cell_reconcile(
        _cell(agent_id, member={"allow": ["Read", "Edit"]}, revision=2),
        current_members={"w1": running},
        facts_port=_facts(agent_id),
    )
    assert policy_only.actions[0].kind is ReconcileStepKind.POLICY_APPLY
    model_change = plan_cell_reconcile(
        _cell(agent_id, member={"allow": ["Read", "Edit"], "model": "opus"}, revision=2),
        current_members={"w1": running},
        facts_port=_facts(agent_id),
    )
    assert model_change.actions[0].kind is ReconcileStepKind.DRAIN_AND_RESTART


def test_policy_on_a_harness_without_a_mapping_fails_the_member() -> None:
    agent_id = "a.codex"
    plan = plan_cell_reconcile(
        _cell(agent_id, member={"deny": ["shell"]}),
        current_members={},
        facts_port=_facts(agent_id, harness="codex"),
    )
    assert plan.actions[0].kind is ReconcileStepKind.FAIL_MISSING_FACTS
    assert plan.actions[0].reason == "policy-unsupported:w1:codex"
    assert plan.target_state_hint is CellReconcileState.DEGRADED
    no_policy = plan_cell_reconcile(
        _cell(agent_id, member={"kind": "codex"}),
        current_members={},
        facts_port=_facts(agent_id, harness="codex"),
    )
    assert no_policy.actions[0].kind is ReconcileStepKind.START


def test_policy_apply_intent_carries_the_effective_spec_for_rollback() -> None:
    agent_id = "a.intent"
    effects: list[Any] = []
    coordinator = CellReconcileCoordinator(
        facts_port=_facts(agent_id), effects_port=SimpleNamespace(submit=effects.append)
    )
    coordinator.reconcile_cell(_cell(agent_id, member={"allow": ["Read"]}))
    ensure = effects[-1]
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure.fence,
            attempt_id=ensure.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure.spec.config_digest,
        )
    )
    coordinator.reconcile_cell(_cell(agent_id, member={"allow": ["Edit"]}, revision=2))
    apply = effects[-1]
    assert apply.action is MemberEffectAction.POLICY_APPLY
    assert apply.previous == ensure.spec
    assert apply.spec.policy_digest == _digest(("Edit",), ())


def test_policy_digest_is_order_independent_and_field_sensitive() -> None:
    first = policy_digest(
        harness="claude", headless=True, allow=("Read", "Bash(hyprial:*)"), deny=("Bash(rm:*)",)
    )
    reordered = policy_digest(
        harness="claude", headless=True, allow=("Bash(hyprial:*)", "Read"), deny=("Bash(rm:*)",)
    )
    assert first == reordered
    assert first != policy_digest(
        harness="claude", headless=True, allow=("Read",), deny=("Bash(rm:*)",)
    )
    assert first != policy_digest(
        harness="claude", headless=False, allow=("Read", "Bash(hyprial:*)"), deny=("Bash(rm:*)",)
    )
    assert first != policy_digest(
        harness="pi", headless=True, allow=("Read", "Bash(hyprial:*)"), deny=("Bash(rm:*)",)
    )


def _ready(**overrides: object) -> dict[str, object]:
    authority: dict[str, object] = {
        "enabled": True,
        "protocol": protocol_version(),
        "policyDigest": "d" * 64,
        "version": 3,
    }
    authority.update(overrides)
    return {"authority": authority}


def test_require_policy_ack_accepts_the_exact_policy() -> None:
    require_policy_ack(_ready(), digest="d" * 64, version=3)


@pytest.mark.parametrize(
    "ready",
    [
        {},
        {"authority": None},
        {"authority": []},
        _ready(enabled=False),
        _ready(protocol=True),
        _ready(protocol=1.0),
        _ready(protocol="1"),
        _ready(protocol=2),
        _ready(policyDigest="e" * 64),
        _ready(version=2),
        _ready(version=True),
        _ready(version="3"),
    ],
)
def test_require_policy_ack_rejects_anything_else(ready: object) -> None:
    with pytest.raises(PolicyHandshakeError):
        require_policy_ack(ready, digest="d" * 64, version=3)  # type: ignore[arg-type]


def test_policy_harnesses_are_the_native_permission_set_rows() -> None:
    native = {
        harness
        for harness in DECLARED_HARNESSES
        for headless in (False, True)
        if support(harness, headless=headless, capability=Capability.PERMISSION_SET).level
        is SupportLevel.NATIVE
    }
    assert native == set(POLICY_HARNESSES) == {"claude"}
    for harness in DECLARED_HARNESSES:
        assert supports_policy(harness) is (harness in native)


def test_replaying_a_revision_for_a_member_awaiting_facts_leaves_a_rolled_back_sibling_alone(
    tmp_path: Path,
) -> None:
    coordinator, launcher, agent_id, port = _rig(tmp_path)

    def two_members(rules: dict[str, object], revision: int) -> Cell:
        return Cell(
            cell_id=subject_cell_id("agent", agent_id),
            kind="agent",
            authority=authority_for("user:owner1"),
            scope=CellScope.of_org(),
            binding=CellBinding.subject(agent_id),
            name="worker",
            members=(CellMember("w1", "harness"), CellMember("w2", "harness")),
            params={"cell.members": {"w1": rules, "w2": {}}},
            revision=revision,
        )

    try:
        rev1 = two_members({"allow": ["Read"]}, 1)
        coordinator.reconcile_cell(rev1)
        wait_until(
            lambda: _member(coordinator, rev1.cell_id).phase is ReconcileMemberPhase.READY,
            timeout=5.0,
        )
        rev2 = two_members({"allow": ["Read", REFUSE_RULE]}, 2)
        coordinator.reconcile_cell(rev2)
        wait_until(lambda: len(launcher.launches) == 3, timeout=5.0)
        wait_until(
            lambda: coordinator.snapshot(rev2.cell_id).state is CellReconcileState.DEGRADED,
            timeout=5.0,
        )
        members = {m.name: m for m in coordinator.snapshot(rev2.cell_id).members}
        assert members["w2"].phase is ReconcileMemberPhase.FAILED

        for _ in range(3):
            coordinator.reconcile_cell(rev2)
        coordinator.on_index((rev2,))
        with pytest.raises(AssertionError):
            wait_until(lambda: len(launcher.launches) > 3, timeout=1.0)

        assert len(launcher.launches) == 3
        member = {m.name: m for m in coordinator.snapshot(rev2.cell_id).members}["w1"]
        assert member.policy_digest == _digest(("Read",), ())
        assert member.running is True and member.admitting_work is False
    finally:
        port.close()
