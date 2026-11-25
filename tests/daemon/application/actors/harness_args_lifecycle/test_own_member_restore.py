"""I6-2 restore guarantees on the agent's own member (I6-5 merged onto I6-2).

dev's I6-2 startup tests drive the generic effect path.  In production a
claude agent's own member (the document member named by the agent URI) takes
the own-member path instead, so these cases boot a copied home through the
production lifecycle coordinator, let boot restore launch the row, then let
member reconcile meet it:

- an unchanged document is adopted with no restart;
- a ``model`` edited while the daemon was down restarts the one harness once,
  in place, with the desired-state row ``running`` throughout and the new
  model persisted;
- a restart whose starts failed leaves a ``running`` row with no process;
  after a reboot restore launches it once and reconcile adopts that process;
- a ``failed`` row is held: no document revision starts it, only an
  operator ``agent start`` does (integration critic P2-1 ruling).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.cells.reconcile import CellReconcileState, ReconcileMemberPhase
from tests.daemon.application.actors.harness_args_lifecycle.test_policy_reconcile import (
    _cell,
    _digest,
    _member,
    _rows,
    _set_rules,
    _start,
)
from tests.daemon.application.actors.harness_args_lifecycle.test_policy_reconcile_real_manager import (
    _manager_healthy,
    _started_with_template,
    _template_rules,
    real_rig as real_rig,
)
from tests.daemon.application.actors.harness_args_lifecycle.test_restart_keeps_intent import (
    _EXTRA_DENY,
    _boot,
    _break,
    _durable_rows,
    _restored,
)
from tests.daemon.cells.actor.actor_support import wait_until
from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
)


def _copy_home(app: Any, factory: pytest.TempPathFactory) -> Path:
    home = factory.mktemp("rebooted-home")
    shutil.copytree(app.state_dir, home, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.sock", "*.lock", "*.lease"))
    return home


def _settled(app: Any, cell_id: str, digest: str) -> bool:
    member = _member(app, cell_id)
    snapshot = app._cell_reconcile_coordinator.snapshot(cell_id)
    return (
        member is not None
        and member.phase is ReconcileMemberPhase.READY
        and member.admitting_work
        and member.policy_digest == digest
        and snapshot is not None
        and snapshot.state is CellReconcileState.ACTIVE
    )


def test_an_unchanged_document_adopts_the_restored_own_harness(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    app, _launcher = real_rig
    _started_with_template(app)
    booted, relauncher, close = _boot(_copy_home(app, tmp_path_factory), monkeypatch)
    try:
        _restored(booted, relauncher)
        allow, deny = _template_rules()
        cell = _cell(booted, "foo")[2]
        wait_until(lambda: _settled(booted, cell.cell_id, _digest(allow, deny)), timeout=15.0)
        booted._cell_reconcile_coordinator.on_index((cell,))
        assert len(relauncher.launches) == 1
        assert relauncher.launches[0][2].running is True
        assert relauncher.launches[0][1] is not None
        _manager_healthy(booted)
    finally:
        close()


def test_a_model_edited_while_down_restarts_the_own_harness_once_in_place(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    app, _launcher = real_rig
    _started_with_template(app)
    booted, relauncher, close = _boot(_copy_home(app, tmp_path_factory), monkeypatch)
    try:
        _restored(booted, relauncher)
        restored = relauncher.launches[0][2]
        assert relauncher.launches[0][0].model != "opus"
        at_stop: list[list[str]] = []
        stop = restored.stop

        def observed_stop() -> None:
            at_stop.append([row.status for row in _durable_rows(booted)])
            stop()

        monkeypatch.setattr(restored, "stop", observed_stop)
        booted._cell_reconcile_barrier.close()
        runtime, agent, cell = _cell(booted, "foo")
        members = dict(cell.param("cell.members") or {})
        members[agent.uri] = {**members.get(agent.uri, {}), "model": "opus"}
        current = {key: cell.param(key) for key in cell.params}
        edited = runtime.update(cell.cell_id, params={**current, "cell.members": members},
                                expect_revision=cell.revision)
        booted._cell_reconcile_barrier.open()

        allow, deny = _template_rules()
        wait_until(lambda: _settled(booted, edited.cell_id, _digest(allow, deny))
                   and len(relauncher.launches) == 2, timeout=30.0)
        assert relauncher.launches[1][0].model == "opus"
        assert restored.running is False
        assert at_stop == [["running"]]
        rows = _durable_rows(booted)
        assert len(rows) == 1 and rows[0].status == "running" and rows[0].model == "opus"
        assert len(_rows(booted, "foo")) == 1
        booted._cell_reconcile_coordinator.on_index((runtime.lookup(edited.cell_id),))
        assert len(relauncher.launches) == 2
        _manager_healthy(booted)
    finally:
        close()


def test_a_kept_launch_intent_is_restored_once_and_adopted_after_a_reboot(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    allow, deny = _template_rules()
    broken = _break(launcher, monkeypatch)
    _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    wait_until(lambda: (member := _member(app, cell.cell_id)) is not None
               and member.last_error is not None, timeout=30.0)
    assert not any(row.running for row in _rows(app, "foo"))
    assert [row.status for row in _durable_rows(app)] == ["running"]
    broken["on"] = False

    booted, relauncher, close = _boot(_copy_home(app, tmp_path_factory), monkeypatch)
    try:
        _restored(booted, relauncher)
        booted_cell = _cell(booted, "foo")[2]
        expected = _digest(allow, [*deny, _EXTRA_DENY])
        wait_until(lambda: _settled(booted, booted_cell.cell_id, expected), timeout=15.0)
        assert len(relauncher.launches) == 1
        assert relauncher.launches[0][2].running is True
        assert len(_rows(booted, "foo")) == 1
        _manager_healthy(booted)
    finally:
        close()


def _held(app: Any, cell_id: str, revision: int) -> bool:
    member = _member(app, cell_id)
    snapshot = app._cell_reconcile_coordinator.snapshot(cell_id)
    return (
        snapshot is not None
        and snapshot.revision >= revision
        and member is not None
        and member.phase is ReconcileMemberPhase.FAILED
        and member.last_error == "member-held"
    )


def test_a_held_own_member_starts_only_on_an_operator_start(
    real_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, launcher = real_rig
    cell = _started_with_template(app)
    launches = len(launcher.launches)
    broken = _break(launcher, monkeypatch)
    launcher.launches[-1][2].stop()

    def exhausted() -> bool:
        app._harnesses.reconcile()
        return [row.status for row in _durable_rows(app)] == ["failed"]

    wait_until(exhausted, timeout=15.0)
    broken["on"] = False
    allow, deny = _template_rules()
    edited = _set_rules(app, "foo", allow=allow, deny=[*deny, _EXTRA_DENY])
    wait_until(lambda: _held(app, edited.cell_id, edited.revision), timeout=15.0)
    app._harnesses.reconcile()
    assert len(launcher.launches) == launches
    assert [row.status for row in _durable_rows(app)] == ["failed"]
    assert cell.cell_id == edited.cell_id

    _start(app)
    expected = _digest(allow, [*deny, _EXTRA_DENY])
    wait_until(lambda: _settled(app, edited.cell_id, expected), timeout=15.0)
    assert len(launcher.launches) == launches + 1
    assert [row.status for row in _durable_rows(app)] == ["running"]
    _manager_healthy(app)
