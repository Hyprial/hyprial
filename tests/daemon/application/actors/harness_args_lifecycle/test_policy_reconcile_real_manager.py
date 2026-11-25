"""Own-member restarts through the production lifecycle manager (I6-5 critic V2).

The rig of ``test_policy_reconcile`` with one difference: the lifecycle
manager is the one ``_start_lifecycle_manager`` composes (the production
``LifecycleCoordinator`` over the real journal), not the inline stand-in, so
its recovery scan runs over the journal rows member reconcile writes while
an own-member restart is in flight.

It also pins the integration critic P2-2 ruling: under a persistent
difference between the live and the desired digest, the drift restart runs
once per desired digest, never once per unrelated revision.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.cells.reconcile import CellReconcileState, ReconcileMemberPhase
from hyprial.daemon.impl.composition.cells import agents as cell_agents
from hyprial.daemon.impl.composition.cells import reconcile as effects_module
from hyprial.daemon.impl.composition.cells import close_cell_runtime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.turn_delivery.channel.templates import (
    load_worker_channel_template,
)
from tests.daemon.application.actors.harness_args_lifecycle import test_policy_reconcile as base
from tests.daemon.application.actors.harness_args_lifecycle.test_policy_reconcile import (
    REFUSE_RULE,
    _cell,
    _digest,
    _Launcher,
    _member,
    _rows,
    _set_rules,
    _start,
)
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox,
    _LocalDelivery,
    _Transport,
)
from tests.daemon.cells.actor.actor_support import wait_until
from tests.identity.agents.registry.entity.support import (
    _TEST_APPLICATIONS,
    _close_test_applications as _close_test_applications,
    application,
)

_EXTRA_DENY = "Bash(hyprial agent stop:*)"


@pytest.fixture
def real_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, _Launcher]]:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    app = application(tmp_path)
    launcher = _Launcher(app)
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state)
    client = HarnessPortClient(actor, events)
    app._harnesses = client  # type: ignore[assignment]
    app._lifecycle_manager = None  # type: ignore[assignment]
    app._start_lifecycle_manager(
        transport=_Transport(), inbox=_Inbox(), local_delivery=_LocalDelivery(), harnesses=client,
    )
    app._restore_done.set()
    workspace = tmp_path / "work"
    workspace.mkdir()
    app.test_workspace = str(workspace)  # type: ignore[attr-defined]
    yield app, launcher
    close_cell_runtime(app)
    if app in _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.remove(app)
    app._close()
    app._close_home_lease()
    events.close()


def _template_rules() -> tuple[list[str], list[str]]:
    allow, deny = load_worker_channel_template("managed-worker").member_rules()
    return list(allow), list(deny)


def _started_with_template(app: Any) -> Any:
    _start(app)
    _runtime, _agent, cell = _cell(app, "foo")
    allow, deny = _template_rules()
    wait_until(
        lambda: (member := _member(app, cell.cell_id)) is not None
        and member.policy_digest == _digest(allow, deny),
        timeout=15.0,
    )
    return cell


def _manager_healthy(app: Any) -> None:
    manager = app._lifecycle_manager
    assert manager.crashed is False, manager.last_error


def _stop_and_start_accepted(app: Any) -> None:
    stopped = app.handle("agent.stop", {"name": "foo"})
    assert stopped["ok"] is True, stopped
    _start(app)
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running


def test_a_policy_edit_through_the_real_manager_restarts_in_place(real_rig: Any) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    allow, deny = _template_rules()

    revised = _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    expected = _digest(allow, [*deny, _EXTRA_DENY])
    wait_until(lambda: _member(app, cell.cell_id).policy_digest == expected, timeout=30.0)

    _manager_healthy(app)
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].harness_id == "claude:foo" and rows[0].running
    assert launcher.launches[-1][1]["disallowedTools"][-1] == _EXTRA_DENY
    snapshot = app._cell_reconcile_coordinator.snapshot(revised.cell_id)
    assert snapshot.state is CellReconcileState.ACTIVE
    _stop_and_start_accepted(app)
    _manager_healthy(app)


def test_a_refused_apply_through_the_real_manager_rolls_back(real_rig: Any) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    allow, deny = _template_rules()

    _set_rules(app, "foo", allow=[*allow, REFUSE_RULE], deny=deny)
    wait_until(
        lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.DEGRADED,
        timeout=30.0,
    )

    _manager_healthy(app)
    member = _member(app, cell.cell_id)
    assert member.policy_digest == _digest(allow, deny)
    assert member.running is True
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    assert REFUSE_RULE not in launcher.launches[-1][1]["allowedTools"]
    _stop_and_start_accepted(app)
    _manager_healthy(app)


def test_a_failed_model_restart_relaunches_the_running_model(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    _allow, deny = _template_rules()
    start = launcher.start

    def refuse_new_model(spec: Any) -> Any:
        process = start(spec)
        authority = process.last_ready.get("authority")
        if spec.model == "claude-other-model" and isinstance(authority, dict):
            authority["policyDigest"] = "0" * 64
        return process

    monkeypatch.setattr(launcher, "start", refuse_new_model)
    runtime, agent, current = _cell(app, "foo")
    members = current.param("cell.members")
    members[agent.uri] = {**members[agent.uri], "model": "claude-other-model"}
    params = {key: current.param(key) for key in current.params}
    runtime.update(cell.cell_id, params={**params, "cell.members": members},
                   expect_revision=current.revision)
    wait_until(
        lambda: (member := _member(app, cell.cell_id)).last_error is not None
        and member.running is True,
        timeout=30.0,
    )

    _manager_healthy(app)
    assert app._cell_reconcile_coordinator.snapshot(cell.cell_id).state is (
        CellReconcileState.DEGRADED
    )
    relaunched = launcher.launches[-1]
    assert relaunched[0].model != "claude-other-model"
    assert relaunched[1] is not None and relaunched[1]["disallowedTools"] == deny
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    desired = [spec for spec in app.desired_state.load().harnesses if spec.name == "foo"]
    assert len(desired) == 1 and desired[0].model != "claude-other-model"


def test_a_kind_mismatch_stops_nothing_and_a_revert_recovers(real_rig: Any) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    runtime, agent, current = _cell(app, "foo")
    saved = dict(current.param("cell.members"))
    params = {key: current.param(key) for key in current.params}
    params["cell.members"] = {agent.uri: {"kind": "codex"}}
    runtime.update(cell.cell_id, params=params, expect_revision=current.revision)
    wait_until(
        lambda: "the document names codex" in (_member(app, cell.cell_id).last_error or ""),
        timeout=30.0,
    )

    _manager_healthy(app)
    assert len(launcher.launches) == 1 and launcher.launches[0][2].running is True
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    assert any(spec.name == "foo" for spec in app.desired_state.load().harnesses)

    runtime, agent, current = _cell(app, "foo")
    params = {key: current.param(key) for key in current.params}
    params["cell.members"] = saved
    runtime.update(cell.cell_id, params=params, expect_revision=current.revision)
    allow, deny = _template_rules()
    wait_until(
        lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.ACTIVE
        and _member(app, cell.cell_id).policy_digest == _digest(allow, deny),
        timeout=30.0,
    )
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running


def _unseeded_running_then_composed(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled")
    _start(app)
    assert getattr(app, "_cell_runtime", None) is None
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "claude")
    return _cell(app, "foo")[2]


def test_a_refused_first_ensure_relaunches_what_ran_and_an_operator_start_reads_the_rules(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    original = base.policy_digest
    refuse = {"on": True}

    def digest(**kwargs: Any) -> str:
        value = original(**kwargs)
        return "0" * 64 if refuse["on"] and kwargs else value

    monkeypatch.setattr(base, "policy_digest", digest)
    cell = _unseeded_running_then_composed(app, monkeypatch)
    wait_until(
        lambda: (member := _member(app, cell.cell_id)) is not None
        and member.last_error is not None
        and member.running is True,
        timeout=30.0,
    )
    _manager_healthy(app)
    assert launcher.launches[-1][1] is None
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    assert any(spec.name == "foo" for spec in app.desired_state.load().harnesses)
    settled = len(launcher.launches)
    time.sleep(1.5)
    assert len(launcher.launches) == settled

    refuse["on"] = False
    _stop_and_start_accepted(app)
    allow, deny = _template_rules()
    wait_until(
        lambda: _member(app, cell.cell_id).policy_digest == _digest(allow, deny)
        and app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.ACTIVE,
        timeout=30.0,
    )
    assert launcher.launches[-1][1] is not None
    assert launcher.launches[-1][1]["disallowedTools"] == deny
    _manager_healthy(app)


def test_a_model_restart_under_a_persistent_handshake_fault_keeps_the_agent_up(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    original = base.policy_digest
    monkeypatch.setattr(
        base, "policy_digest", lambda **kwargs: "0" * 64 if kwargs else original(**kwargs)
    )
    runtime, agent, current = _cell(app, "foo")
    members = current.param("cell.members")
    members[agent.uri] = {**members[agent.uri], "model": "claude-other-model"}
    params = {key: current.param(key) for key in current.params}
    runtime.update(cell.cell_id, params={**params, "cell.members": members},
                   expect_revision=current.revision)
    wait_until(lambda: "rollback failed" in (_member(app, cell.cell_id).last_error or ""),
               timeout=30.0)

    _manager_healthy(app)
    assert launcher.launches[-1][0].model != "claude-other-model"
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    desired = [spec for spec in app.desired_state.load().harnesses if spec.name == "foo"]
    assert len(desired) == 1 and desired[0].model != "claude-other-model"


def _degraded_unacknowledged(app: Any, cell_id: str) -> bool:
    snapshot = app._cell_reconcile_coordinator.snapshot(cell_id)
    member = _member(app, cell_id)
    return (
        snapshot.state is CellReconcileState.DEGRADED
        and member is not None
        and member.admitting_work is False
        and "acknowledged a different policy digest" in (member.last_error or "")
    )


def test_a_member_kept_unacknowledged_stays_degraded_across_an_unrelated_edit_and_restart(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    original = base.policy_digest
    fault = {"on": True}

    def digest(**kwargs: Any) -> str:
        return "0" * 64 if fault["on"] and kwargs else original(**kwargs)

    monkeypatch.setattr(base, "policy_digest", digest)
    _start(app)
    runtime, _agent, cell = _cell(app, "foo")
    wait_until(lambda: _degraded_unacknowledged(app, cell.cell_id), timeout=30.0)

    current = runtime.lookup(cell.cell_id)
    params = {key: current.param(key) for key in current.params}
    launches = len(launcher.launches)
    revised = runtime.update(cell.cell_id, params={**params, "cell.note": "unrelated"},
                             expect_revision=current.revision)
    wait_until(lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).revision
               == revised.revision, timeout=30.0)
    time.sleep(1.0)
    assert _degraded_unacknowledged(app, cell.cell_id)
    assert len(launcher.launches) == launches

    _stop_and_start_accepted(app)
    wait_until(lambda: _degraded_unacknowledged(app, cell.cell_id)
               and _member(app, cell.cell_id).phase.value == "ready", timeout=30.0)
    time.sleep(1.0)
    assert _degraded_unacknowledged(app, cell.cell_id)

    fault["on"] = False
    _stop_and_start_accepted(app)
    allow, deny = _template_rules()
    wait_until(
        lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.ACTIVE
        and _member(app, cell.cell_id).policy_digest == _digest(allow, deny),
        timeout=30.0,
    )
    _manager_healthy(app)


def _note(app: Any, text: str) -> Any:
    runtime, _agent, cell = _cell(app, "foo")
    current = {key: cell.param(key) for key in cell.params}
    return runtime.update(cell.cell_id, params={**current, "cell.note": text},
                          expect_revision=cell.revision)


def _mismatch_at(app: Any, cell_id: str, revision: int) -> bool:
    member = _member(app, cell_id)
    snapshot = app._cell_reconcile_coordinator.snapshot(cell_id)
    return (
        snapshot is not None
        and snapshot.revision >= revision
        and member is not None
        and member.phase is ReconcileMemberPhase.FAILED
        and member.last_error == "config-digest-mismatch"
    )


def test_a_persistent_digest_difference_restarts_once_per_desired_digest(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    monkeypatch.setattr(effects_module, "live_member_digest", lambda *_a, **_k: "f" * 64)
    _start(app)
    cell = _cell(app, "foo")[2]
    wait_until(lambda: _mismatch_at(app, cell.cell_id, cell.revision), timeout=15.0)
    assert len(launcher.launches) == 2

    for index in range(3):
        noted = _note(app, f"unrelated-{index}")
        wait_until(lambda: _mismatch_at(app, noted.cell_id, noted.revision), timeout=15.0)
        assert len(launcher.launches) == 2

    allow, deny = _template_rules()
    edited = _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    wait_until(lambda: _mismatch_at(app, edited.cell_id, edited.revision)
               and len(launcher.launches) == 3, timeout=15.0)
    noted = _note(app, "unrelated-after-edit")
    wait_until(lambda: _mismatch_at(app, noted.cell_id, noted.revision), timeout=15.0)
    assert len(launcher.launches) == 3
    assert sum(1 for _spec, _options, process in launcher.launches if process.running) == 1
    _manager_healthy(app)
