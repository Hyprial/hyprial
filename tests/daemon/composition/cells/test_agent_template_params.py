"""An agent made from a template carries the template's role in its agent cell (S10 item 1).

I6-5a: a claude member's launch rules are template data seeded into its cell
document by the registrar (and before a ``lifecycle.start``); every launch
reads them from the document only and fails closed without one.

The role is cell data: the agent-cell registrar seeds the template's
``cellParams`` into the agent cell, only for keys the cell does not hold, so
an owner edit of the role is never overwritten.  A plain agent gets none.
2026-11-20 (S2b): the examples use the ``user`` packaged template; the
squire template and its adoption data are deleted.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.cells.reconcile.policy import (
    MemberLaunchPolicies,
    launch_key,
    resolve_member_policy,
)
from hyprial.daemon.impl.cells.runtime.agents import agent_cell_spec
from hyprial.daemon.impl.cells.structure import parse_structure
from hyprial.daemon.impl.composition.cells import (
    cell_runtime,
    close_cell_runtime,
    seed_launch_policy,
)
from hyprial.daemon.impl.composition.cells.agents import SEED_FAILED_EVENT
from hyprial.daemon.impl.composition.cells.reconcile import (
    POLICY_UNAVAILABLE_EVENT,
    member_launch_policy,
)
from hyprial.identity.impl.agents import Agent
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.composition.cells.test_agent_cell_registration import (
    _Agents,
    _app,
    _events,
)


def _agent(actor: str, **capabilities: object) -> Agent:
    return Agent(
        uri=f"agent:alice:mac:{actor}",
        actor=actor,
        owner="alice",
        machine="mac",
        capabilities=capabilities,
        entity_token=uuid.uuid4().hex,
    )


def test_a_template_agent_cell_carries_the_template_role(tmp_path: Path) -> None:
    made = _agent("helper", template="user")
    plain = _agent("worker")
    app = _app(tmp_path, _Agents(made, plain))
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        cell = runtime.lookup(made.cell_id)
        assert cell is not None
        assert cell.param("agent.role") == "user"
        assert cell.param("agent.template") == "user"
        other = runtime.lookup(plain.cell_id)
        assert other is not None and dict(other.params) == {}
    finally:
        close_cell_runtime(app)


def test_an_owner_edited_role_is_kept_and_only_missing_keys_are_seeded(
    tmp_path: Path,
) -> None:
    agents = _Agents()
    app = _app(tmp_path, agents)
    made = _agent("helper", template="user")
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        spec = agent_cell_spec(made)
        runtime.ensure(replace(spec, params={"agent.role": "owner-chosen"}))
        agents.create(made)
        cell = runtime.lookup(made.cell_id)
        assert cell is not None
        assert cell.param("agent.role") == "owner-chosen"
        assert cell.param("agent.template") == "user"
    finally:
        close_cell_runtime(app)


def test_settle_seeds_the_template_and_answers_the_post_seed_revision(
    tmp_path: Path,
) -> None:
    """Merge of S8 onto S10: ``settle`` (the attach/detach handler path) seeds too."""

    agents = _Agents()
    app = _app(tmp_path, agents)
    made = _agent("helper", template="user")
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        agents.items.append(made)
        settled = app._agent_cell_registrar.settle("helper")
        cell = runtime.lookup(made.cell_id)
        assert cell is not None and cell.param("agent.role") == "user"
        assert settled == {"state": "recorded", "revision": cell.revision}
    finally:
        close_cell_runtime(app)


def test_registrar_does_not_overwrite_document_edited_members_on_index(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.cells.model import CellMember

    agents = _Agents()
    ext = _agent("ext", custody="external", attached=True)
    managed = _agent("worker")
    agents.items.extend([ext, managed])
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        ext_cell = runtime.lookup(ext.cell_id)
        managed_cell = runtime.lookup(managed.cell_id)
        assert ext_cell is not None and managed_cell is not None
        edited_ext = runtime.update(
            ext.cell_id,
            members=(CellMember(ext.uri, "harness"), CellMember("custom-member", "harness")),
            expect_revision=ext_cell.revision,
        )
        edited_managed = runtime.update(
            managed.cell_id,
            members=(CellMember("custom-a", "harness"), CellMember("custom-b", "harness")),
            expect_revision=managed_cell.revision,
        )
        runtime.run_once()
        reloaded_ext = runtime.lookup(ext.cell_id)
        reloaded_managed = runtime.lookup(managed.cell_id)
        assert reloaded_ext is not None and reloaded_managed is not None
        assert [m.ref for m in reloaded_ext.members] == [ext.uri, "custom-member"]
        assert reloaded_ext.revision == edited_ext.revision
        assert [m.ref for m in reloaded_managed.members] == ["custom-a", "custom-b"]
        assert reloaded_managed.revision == edited_managed.revision
    finally:
        close_cell_runtime(app)


MANAGED_ALLOW = [
    "mcp__harness-bridge__harness_ack",
    "mcp__harness-bridge__harness_delegate",
    "mcp__harness-bridge__harness_progress",
    "mcp__harness-bridge__harness_read",
    "mcp__harness-bridge__harness_read_body",
    "mcp__harness-bridge__harness_reply",
    "mcp__harness-bridge__harness_send",
    "mcp__harness-bridge__harness_targets",
    "mcp__harness-bridge__harness_whoami",
    "mcp__harness-bridge__workflow_complete",
    "mcp__harness-bridge__workflow_fail",
    "Bash(hyprial:*)",
]
MANAGED_DENY = [
    "Bash(hyprial agent destroy:*)",
    "Bash(hyprial daemon:*)",
    "Bash(hyprial init:*)",
    "Bash(hyprial org fs:*)",
]


def _launch(name: str, harness: str = "claude", **extra: object) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness=harness, name=name, headless=True, cwd="/work", **extra)


def _member_rules(cell: Any, member: str) -> dict[str, Any]:
    declared = cell.param("cell.members") or {}
    return dict(declared.get(member) or {})


def _with_desired(app: Any, *specs: HarnessLaunchSpec) -> Any:
    app.desired_state = SimpleNamespace(load=lambda: SimpleNamespace(harnesses=specs))
    return app


def test_a_fresh_claude_agent_first_start_gets_the_template_rules_from_its_document(
    tmp_path: Path,
) -> None:
    worker = _agent("worker")
    agents = _Agents()
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        agents.create(worker)
        created = runtime.lookup(worker.cell_id)
        assert created is not None and _member_rules(created, worker.uri) == {}
        assert member_launch_policy(app, _launch("worker")) is None

        assert seed_launch_policy(app, "worker", "claude") == "recorded"

        cell = runtime.lookup(worker.cell_id)
        assert cell is not None
        assert _member_rules(cell, worker.uri) == {"allow": MANAGED_ALLOW, "deny": MANAGED_DENY}
        assert not parse_structure(cell).problems
        launch = member_launch_policy(app, _launch("worker"))
        assert launch is not None
        assert dict(launch.launch_options) == {
            "allowedTools": MANAGED_ALLOW,
            "disallowedTools": MANAGED_DENY,
        }
        assert launch.version == cell.revision
        assert seed_launch_policy(app, "worker", "claude") == "recorded"
        assert runtime.lookup(worker.cell_id).revision == cell.revision
    finally:
        close_cell_runtime(app)


def test_the_registrar_seeds_a_claude_member_once_its_harness_is_known(
    tmp_path: Path,
) -> None:
    worker = _agent("worker")
    agents = _Agents(worker)
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        cell = runtime.lookup(worker.cell_id)
        assert cell is not None and _member_rules(cell, worker.uri) == {}
        _with_desired(app, _launch("worker"))
        app._agent_cell_registrar.reconcile()
        cell = runtime.lookup(worker.cell_id)
        assert cell is not None
        assert _member_rules(cell, worker.uri) == {"allow": MANAGED_ALLOW, "deny": MANAGED_DENY}
    finally:
        close_cell_runtime(app)


def test_codex_and_pi_members_are_not_seeded(tmp_path: Path) -> None:
    codex, pi = _agent("coder"), _agent("runner")
    agents = _Agents(codex, pi)
    app = _with_desired(
        _app(tmp_path, agents), _launch("coder", "codex"), _launch("runner", "pi")
    )
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        app._agent_cell_registrar.reconcile()
        assert seed_launch_policy(app, "coder", "codex") == "skipped"
        assert seed_launch_policy(app, "runner", "pi") == "skipped"
        for agent in (codex, pi):
            cell = runtime.lookup(agent.cell_id)
            assert cell is not None and _member_rules(cell, agent.uri) == {}
        assert member_launch_policy(app, _launch("coder", "codex")) is None
    finally:
        close_cell_runtime(app)


def test_a_document_kind_wins_over_the_harness_a_launch_names(tmp_path: Path) -> None:
    worker = _agent("worker")
    agents = _Agents()
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        spec = agent_cell_spec(worker)
        runtime.ensure(replace(spec, params={"cell.members": {worker.uri: {"kind": "codex"}}}))
        agents.create(worker)
        seed_launch_policy(app, "worker", "claude")
        cell = runtime.lookup(worker.cell_id)
        assert cell is not None
        assert _member_rules(cell, worker.uri) == {"kind": "codex"}
    finally:
        close_cell_runtime(app)


def test_document_rules_reach_lifecycle_and_restore_launches_and_an_edit_is_kept(
    tmp_path: Path,
) -> None:
    worker = _agent("worker")
    agents = _Agents()
    app = _app(tmp_path, agents)
    rules = {"kind": "claude", "model": "m1", "allow": ["Read"], "deny": ["Bash(rm:*)"]}
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        spec = agent_cell_spec(worker)
        runtime.ensure(replace(spec, params={"cell.members": {worker.uri: rules}}))
        agents.create(worker)
        seed_launch_policy(app, "worker", "claude")
        cell = runtime.lookup(worker.cell_id)
        assert cell is not None and _member_rules(cell, worker.uri) == rules
        restored = HarnessLaunchSpec.from_json(_launch("worker").to_json(), "harnesses[0]")
        for launch_spec in (_launch("worker"), restored):
            launch = member_launch_policy(app, launch_spec)
            assert launch is not None
            assert dict(launch.launch_options) == {
                "allowedTools": ["Read"],
                "disallowedTools": ["Bash(rm:*)"],
            }
    finally:
        close_cell_runtime(app)


def test_a_launch_fails_closed_without_a_readable_cell_index(tmp_path: Path) -> None:
    worker = _agent("worker")
    app = _app(tmp_path, _Agents(worker))
    assert member_launch_policy(app, _launch("worker")) is None
    assert [fields["reason"] for fields in _events(app, POLICY_UNAVAILABLE_EVENT)] == [
        "cell-index-unavailable"
    ]


def test_a_seed_failure_is_logged_bounded_and_never_raised(tmp_path: Path) -> None:
    worker = _agent("worker")
    agents = _Agents(worker)
    app = _app(tmp_path, agents)
    app._require_orgfs_runtime = lambda: (_ for _ in ()).throw(RuntimeError("orgfs down"))
    assert seed_launch_policy(app, "worker", "claude") == "unavailable"

    gate = threading.Event()
    blocked = _app(tmp_path / "blocked", _Agents(worker))
    try:
        assert cell_runtime(blocked) is not None
        blocked._agent_cell_registrar.seed_for_launch = lambda *_args: gate.wait(10) or "recorded"
        started = time.monotonic()
        assert seed_launch_policy(blocked, "worker", "claude", timeout_s=0.2) == "timeout"
        assert time.monotonic() - started < 5
    finally:
        gate.set()
        close_cell_runtime(blocked)
    states = [fields["state"] for fields in _events(app, SEED_FAILED_EVENT)]
    states += [fields["state"] for fields in _events(blocked, SEED_FAILED_EVENT)]
    assert states == ["unavailable", "timeout"]


def test_a_published_no_policy_wins_over_the_document_rules(tmp_path: Path) -> None:
    """Critic P1-2: a rollback to a policy-less spec never relaunches the document's rules."""

    worker = _agent("worker")
    agents = _Agents()
    app = _app(tmp_path, agents)
    rules = {"kind": "claude", "allow": ["Read"], "deny": ["Bash(rm:*)"]}
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        runtime.ensure(
            replace(agent_cell_spec(worker), params={"cell.members": {worker.uri: rules}})
        )
        agents.create(worker)
        policies = MemberLaunchPolicies()
        app._cell_member_policies = policies
        spec = _launch("worker")
        key = launch_key(spec)
        assert key is not None
        assert member_launch_policy(app, spec) is not None

        policies.publish(key, None, version=2)
        assert policies.lookup(spec) == (True, None)
        assert member_launch_policy(app, spec) is None

        policies.mark_verified(key)
        policy = resolve_member_policy("claude", ("Read",), ())
        policies.publish(key, policy, version=3)
        assert policies.restore_verified(key) is None
        assert member_launch_policy(app, spec) is None

        policies.forget(key)
        assert policies.lookup(spec) == (False, None)
        assert member_launch_policy(app, spec) is not None
    finally:
        close_cell_runtime(app)
