"""A policy edit reaches the one harness ``agent start`` launched (I6-5a critic P0, P1-2).

A real ``DaemonApplication`` with its real cell runtime and member reconcile;
the harness port is the production ``HarnessPortClient`` over a
``HarnessRuntimeActor``, and only the OS process is a stand-in.  The stand-in
launcher does what the production worker-channel factory does: it asks
``member_launch_policy`` for the member's policy, records the launched
process for the ready-frame check, and acknowledges like the Agent SDK worker
(a rule named ``REFUSE_RULE`` makes it acknowledge a different digest).
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.cells.reconcile import CellReconcileState
from hyprial.daemon.impl.cells.reconcile.policy import (
    policy_digest,
    protocol_version,
    resolve_member_policy,
)
from hyprial.daemon.impl.composition.cells import agents as cell_agents
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.composition.cells.reconcile import member_launch_policy
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.harness_ports import HarnessPortClient
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.transfer.archive.session_files import claude_project_dir_name
from hyprial.daemon.impl.harnesses.turn_delivery.channel.templates import (
    load_worker_channel_template,
)
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.cells.actor.actor_support import wait_until
from tests.identity.agents.registry.entity.support import (
    _TEST_APPLICATIONS,
    _close_test_applications as _close_test_applications,
    application,
)

REFUSE_RULE = "Bash(refuse-ack:*)"


class _Process:
    def __init__(self, ready: dict[str, object], session_ref: str | None = None) -> None:
        self.running = True
        self.pid = 4242
        self.last_ready = ready
        self.session_ref = session_ref

    def wait_ready(self, *, timeout: float | None = None) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def __init__(self, app: DaemonApplication) -> None:
        self._app = app
        self._lock = threading.Lock()
        self.launches: list[tuple[HarnessLaunchSpec, dict[str, object] | None, _Process]] = []

    def start(self, spec: HarnessLaunchSpec) -> _Process:
        launch = member_launch_policy(self._app, spec)
        options = None if launch is None else dict(launch.launch_options)
        ready: dict[str, object] = {"type": "ready"}
        if launch is not None and options is not None:
            allow = list(options.get("allowedTools", []))
            deny = list(options.get("disallowedTools", []))
            echoed = allow + ["tampered"] if REFUSE_RULE in allow else allow
            ready["authority"] = {
                "enabled": True,
                "protocol": protocol_version(),
                "policyDigest": policy_digest(
                    harness="claude", headless=True, allow=echoed, deny=deny
                ),
                "version": launch.version,
            }
        process = _Process(ready, spec.session_ref)
        self._app._cell_member_policies.record_launch_process(spec, process)
        with self._lock:
            self.launches.append((spec, options, process))
        return process


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, _Launcher]]:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    app = application(tmp_path)
    launcher = _Launcher(app)
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state)
    app._harnesses = HarnessPortClient(actor, events)  # type: ignore[assignment]
    workspace = tmp_path / "work"
    workspace.mkdir()
    app.test_workspace = str(workspace)  # type: ignore[attr-defined]
    yield app, launcher
    close_cell_runtime(app)
    if app in _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.remove(app)
    app._close()
    app._close_home_lease()
    actor_port = app._harnesses
    if hasattr(actor_port, "stop"):
        actor_port.stop(5.0)
    events.close()


def _start(app: Any) -> None:
    started = app.handle(
        "agent.start", {"name": "foo", "harness": "claude", "cwd": app.test_workspace}
    )
    assert started["ok"] is True


def _cell(app: Any, actor: str) -> Any:
    runtime = cell_runtime(app)
    assert runtime is not None
    agent = app.agents.get(actor)
    cell = runtime.lookup(agent.cell_id)
    assert cell is not None
    return runtime, agent, cell


def _set_rules(app: Any, actor: str, **rules: list[str]) -> Any:
    runtime, agent, cell = _cell(app, actor)
    members = cell.param("cell.members") or {}
    entry = {key: value for key, value in members.get(agent.uri, {}).items()
             if key not in {"allow", "deny"}}
    members[agent.uri] = {**entry, **rules}
    current = {key: cell.param(key) for key in cell.params}
    return runtime.update(
        cell.cell_id, params={**current, "cell.members": members}, expect_revision=cell.revision
    )


def _member(app: Any, cell_id: str) -> Any:
    snapshot = app._cell_reconcile_coordinator.snapshot(cell_id)
    return None if snapshot is None or not snapshot.members else snapshot.members[0]


def _rows(app: Any, actor: str) -> list[Any]:
    return [row for row in app._harnesses.actor.read_harnesses() if row.name == actor]


def _digest(allow: list[str], deny: list[str]) -> str:
    policy = resolve_member_policy("claude", tuple(allow), tuple(deny))
    assert policy is not None
    return policy.digest


def test_a_policy_edit_restarts_the_one_harness_agent_start_launched(rig: Any) -> None:
    app, launcher = rig
    _start(app)
    _runtime, agent, cell = _cell(app, "foo")
    allow, deny = load_worker_channel_template("managed-worker").member_rules()
    wait_until(
        lambda: (member := _member(app, cell.cell_id)) is not None
        and member.policy_digest == _digest(list(allow), list(deny)),
        timeout=10.0,
    )
    assert len(launcher.launches) == 1

    revised = _set_rules(app, "foo", allow=list(allow), deny=[*deny, "Bash(hyprial agent stop:*)"])
    expected = _digest(list(allow), [*deny, "Bash(hyprial agent stop:*)"])
    wait_until(lambda: _member(app, cell.cell_id).policy_digest == expected, timeout=10.0)

    assert len(launcher.launches) == 2
    assert all(spec.name == "foo" and spec.member in {None, "foo"}
               for spec, _options, _process in launcher.launches)
    assert all(spec.member != agent.uri for spec, _options, _process in launcher.launches)
    assert launcher.launches[0][2].running is False
    assert launcher.launches[1][1]["disallowedTools"][-1] == "Bash(hyprial agent stop:*)"
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].harness_id == "claude:foo" and rows[0].running
    snapshot = app._cell_reconcile_coordinator.snapshot(revised.cell_id)
    assert snapshot.state is CellReconcileState.ACTIVE
    assert snapshot.effective_revision == revised.revision


def test_a_failed_apply_rolls_back_to_no_policy_without_reading_the_new_rules(
    rig: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled-for-this-test")
    app, launcher = rig
    _start(app)
    _runtime, _agent, cell = _cell(app, "foo")
    wait_until(
        lambda: (snapshot := app._cell_reconcile_coordinator.snapshot(cell.cell_id)) is not None
        and snapshot.state is CellReconcileState.EMPTY,
        timeout=10.0,
    )
    assert len(launcher.launches) == 1 and launcher.launches[0][1] is None

    _set_rules(app, "foo", allow=["Read", REFUSE_RULE])
    wait_until(
        lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.DEGRADED,
        timeout=10.0,
    )
    wait_until(lambda: len(launcher.launches) == 3, timeout=10.0)

    assert launcher.launches[1][1] == {"allowedTools": ["Read", REFUSE_RULE]}
    assert launcher.launches[2][1] is None
    member = _member(app, cell.cell_id)
    assert member.policy_digest is None
    assert member.running is True and member.admitting_work is False
    assert len(_rows(app, "foo")) == 1


def test_a_document_kind_edit_never_launches_a_second_harness(rig: Any) -> None:
    app, launcher = rig
    _start(app)
    _runtime, agent, cell = _cell(app, "foo")
    wait_until(
        lambda: (member := _member(app, cell.cell_id)) is not None and member.policy_digest,
        timeout=10.0,
    )
    runtime, agent, cell = _cell(app, "foo")
    current = {key: cell.param(key) for key in cell.params}
    current["cell.members"] = {agent.uri: {"kind": "codex"}}
    runtime.update(cell.cell_id, params=current, expect_revision=cell.revision)
    wait_until(
        lambda: "the document names codex" in (_member(app, cell.cell_id).last_error or ""),
        timeout=10.0,
    )
    assert all(spec.harness == "claude" for spec, _options, _process in launcher.launches)
    assert len(launcher.launches) == 1 and launcher.launches[0][2].running is True
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
    assert any(spec.name == "foo" for spec in app.desired_state.load().harnesses)
    member = _member(app, cell.cell_id)
    assert member.running is True and member.admitting_work is False


def _learn_session(app: Any, launcher: _Launcher) -> list[str]:
    allow, deny = load_worker_channel_template("managed-worker").member_rules()
    _start(app)
    _runtime, _agent, cell = _cell(app, "foo")
    wait_until(
        lambda: (member := _member(app, cell.cell_id)) is not None
        and member.policy_digest == _digest(list(allow), list(deny)),
        timeout=10.0,
    )
    launcher.launches[0][2].session_ref = "sess-learned"
    app._harnesses.reconcile_session_refs()
    learned = next(spec for spec in app.desired_state.load().harnesses if spec.name == "foo")
    assert learned.session_ref == "sess-learned"
    return [*deny, "Bash(hyprial agent stop:*)"]


def test_a_policy_edit_restart_resumes_the_learned_session(rig: Any, tmp_path: Path) -> None:
    app, launcher = rig
    deny = _learn_session(app, launcher)
    directory = tmp_path / "agents" / "foo" / "state"
    for part in ("sessions", "claude", claude_project_dir_name(app.test_workspace)):
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    transcript = directory / "sess-learned.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _runtime, _agent, cell = _cell(app, "foo")
    allow, _deny = load_worker_channel_template("managed-worker").member_rules()

    _set_rules(app, "foo", allow=list(allow), deny=deny)
    wait_until(lambda: _member(app, cell.cell_id).policy_digest == _digest(list(allow), deny),
               timeout=10.0)
    assert launcher.launches[-1][0].session_ref == "sess-learned"
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running


def test_a_refused_resume_stops_nothing_and_keeps_the_previous_policy(rig: Any) -> None:
    app, launcher = rig
    deny = _learn_session(app, launcher)
    _runtime, _agent, cell = _cell(app, "foo")
    allow, old_deny = load_worker_channel_template("managed-worker").member_rules()

    _set_rules(app, "foo", allow=list(allow), deny=deny)
    wait_until(
        lambda: app._cell_reconcile_coordinator.snapshot(cell.cell_id).state
        is CellReconcileState.DEGRADED,
        timeout=10.0,
    )
    member = _member(app, cell.cell_id)
    assert "cannot resume claude session" in (member.last_error or "")
    assert member.policy_digest == _digest(list(allow), list(old_deny))
    assert member.running is True
    assert len(launcher.launches) == 1 and launcher.launches[0][2].running is True
    rows = _rows(app, "foo")
    assert len(rows) == 1 and rows[0].running
