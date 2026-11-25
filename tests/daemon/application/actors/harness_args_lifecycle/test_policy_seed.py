"""``lifecycle.start`` seeds a claude member's rules before it launches (I6-5a).

The hook runs after the agent record exists and before the harness launch,
and a seed that cannot happen (no cell runtime here) is logged while the
start proceeds with the document as it is.  The launch (lifecycle or a
restore of the desired-state spec) reads the member's rules from the
document, seeded or owner-edited.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.actors import lifecycle_ops
from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.composition.cells import wiring as cells_wiring
from hyprial.daemon.impl.composition.cells.reconcile import member_launch_policy
from hyprial.daemon.impl.harnesses.turn_delivery.channel.templates import (
    load_worker_channel_template,
)
from hyprial.kernel import HarnessLaunchSpec

from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    application,
)


@pytest.fixture
def app(tmp_path: Path) -> Iterator[DaemonApplication]:
    yield application(tmp_path)


def test_lifecycle_start_seeds_before_the_launch(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    order: list[tuple[str, Any]] = []

    def seed(target: Any, actor: str, harness: str) -> str:
        order.append(("seed", (actor, harness, target.agents.get(actor) is not None)))
        return "recorded"

    monkeypatch.setattr(lifecycle_ops, "seed_launch_policy", seed)
    original_start = app._harnesses.start

    def start(spec: HarnessLaunchSpec) -> Any:
        order.append(("launch", spec.name))
        return original_start(spec)

    monkeypatch.setattr(app._harnesses, "start", start)
    started = app.handle("agent.start", {"name": "foo", "harness": "claude"})

    assert started["ok"] is True
    assert order[0] == ("seed", ("foo", "claude", True))
    assert ("launch", "foo") in order[1:]


def test_a_fresh_claude_start_and_its_restore_read_the_seeded_document(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    launched: list[HarnessLaunchSpec] = []
    original_start = app._harnesses.start
    monkeypatch.setattr(
        app._harnesses, "start", lambda spec: launched.append(spec) or original_start(spec)
    )

    assert app.handle("agent.start", {"name": "foo", "harness": "claude"})["ok"] is True

    allow, deny = load_worker_channel_template("managed-worker").member_rules()
    expected = {"allowedTools": list(allow), "disallowedTools": list(deny)}
    restored = next(spec for spec in app.desired_state.load().harnesses if spec.name == "foo")
    for spec in (*launched, restored):
        launch = member_launch_policy(app, spec)
        assert launch is not None
        assert dict(launch.launch_options) == expected


def test_lifecycle_start_and_restore_apply_a_document_deny(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert app.handle("agent.create", {"name": "foo"})["ok"] is True
    agent = app.agents.get("foo")
    runtime = cell_runtime(app)
    assert runtime is not None and agent is not None
    cell = runtime.lookup(agent.cell_id)
    assert cell is not None
    members = cell.param("cell.members") or {}
    members[agent.uri] = {**members.get(agent.uri, {}), "deny": ["Bash(hyprial agent stop:*)"]}
    current = {key: cell.param(key) for key in cell.params}
    runtime.update(
        cell.cell_id, params={**current, "cell.members": members}, expect_revision=cell.revision
    )

    started = app.handle("agent.start", {"name": "foo", "harness": "claude"})
    assert started["ok"] is True
    restored = next(spec for spec in app.desired_state.load().harnesses if spec.name == "foo")
    for spec in (HarnessLaunchSpec(harness="claude", name="foo", headless=True), restored):
        launch = member_launch_policy(app, spec)
        assert launch is not None
        assert dict(launch.launch_options) == {
            "allowedTools": [],
            "disallowedTools": ["Bash(hyprial agent stop:*)"],
        }


def test_a_failed_seed_never_blocks_the_start(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    logged: list[tuple[str, dict[str, Any]]] = []
    original_log = app._log

    def log(level: str, component: str, event: str, **fields: Any) -> None:
        logged.append((event, fields))
        original_log(level, component, event, **fields)

    monkeypatch.setattr(app, "_log", log)
    monkeypatch.setattr(cells_wiring, "cell_runtime", lambda _app: None)
    launched: list[str] = []
    original_start = app._harnesses.start
    monkeypatch.setattr(
        app._harnesses, "start", lambda spec: launched.append(spec.name) or original_start(spec)
    )

    started = app.handle("agent.start", {"name": "foo", "harness": "claude"})

    assert started["ok"] is True
    assert launched == ["foo"]
    seeds = [fields for event, fields in logged if event == "cell.member_policy_seed_failed"]
    assert [(item["actor"], item["harness"]) for item in seeds] == [("foo", "claude")]
