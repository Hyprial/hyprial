"""Unified actor-status enumeration and daemon projection contracts."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path
from typing import Any

import pytest

from hyprial.shell.impl.cli.commands.daemon.top import _render_top
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.status import build_actor_status_snapshot
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


OWNER = "op"
NODE = "node-1"
MANAGED = f"agent:{OWNER}:{NODE}:managed"
INTERACTIVE = f"agent:{OWNER}:{NODE}:interactive"
DORMANT = f"agent:{OWNER}:{NODE}:dormant"
REMOTE = "agent:peer:node-9:remote"


class _Harnesses:
    def __init__(self, rows: tuple[dict[str, object], ...] = ()) -> None:
        self.rows = rows

    def status(self) -> tuple[dict[str, object], ...]:
        return self.rows


class _Presence:
    def __init__(self, actors: tuple[str, ...] = ()) -> None:
        self.actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self.actors

    def actor_online(self, actor: str) -> bool:
        return actor in self.actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _application(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def test_interactive_session_is_an_independent_enumeration_source() -> None:
    """Mutation guard: deleting the interactive-session union branch is red."""

    rows = build_actor_status_snapshot(
        owner=OWNER,
        node_id=NODE,
        connectors=(),
        interactive_sessions=(
            {
                "actor": INTERACTIVE,
                "runtime": "claude_interactive",
                "source": "mcp",
            },
        ),
        registered_actors=(),
        presence_actors=(),
        status_for_actor=lambda _actor, _online: "offline",
    )

    assert [row["actor"] for row in rows] == [INTERACTIVE]
    assert rows[0]["sources"] == ["interactive_session"]
    assert rows[0]["processState"] == "not_applicable"
    assert rows[0]["state"] == "offline"


def test_top_and_targets_project_the_same_mixed_actor_snapshot(
    tmp_path: Path,
) -> None:
    """Managed, MCP, dormant registry, and remote presence actors converge.

    The equality covers both membership and the shared liveness verdict.  The
    source labels make union/dedup observable instead of accepting four names
    that two independent implementations happened to discover.
    """

    app = _application(tmp_path)
    app._harnesses = _Harnesses(  # type: ignore[assignment]
        (
            {
                "id": "pi:managed",
                "runtime": "pi",
                "name": "managed",
                "running": True,
                "pid": 4242,
            },
        )
    )
    for name in ("managed", "dormant"):
        app.agents.create(name)
    app.handle(
        "session.register",
        {
            "actor": "interactive",
            "cwd": "/work",
            "command": ["codex"],
            "source": "codex-app-server",
            "sessionRef": "session-1",
            "runtime": "codex_interactive",
        },
    )

    managed, interactive, dormant = (
        app.agents.require(name).uri for name in ("managed", "interactive", "dormant")
    )
    assert {managed, interactive, dormant}.isdisjoint({MANAGED, INTERACTIVE, DORMANT})
    app._presence = _Presence((interactive, REMOTE))  # type: ignore[assignment]

    top = app.handle("top.snapshot", {})
    targets = app.handle("targets", {"kind": "agent"})["targets"]
    top_rows = {row["actor"]: row for row in top["actors"]}
    target_rows = {row["actor"]: row for row in targets}

    assert set(top_rows) == set(target_rows) == {
        managed,
        interactive,
        dormant,
        REMOTE,
    }
    assert {actor: row["status"] for actor, row in top_rows.items()} == {
        actor: row["status"] for actor, row in target_rows.items()
    }
    assert top_rows[managed]["sources"] == ["connector", "registry"]
    assert top_rows[interactive]["sources"] == [
        "interactive_session",
        "presence",
        "registry",
    ]
    assert top_rows[dormant]["sources"] == ["registry"]
    assert top_rows[REMOTE]["sources"] == ["presence"]
    assert top_rows[interactive]["processState"] == "not_applicable"
    assert top_rows[interactive]["state"] == "online"
    assert top_rows[dormant]["processState"] == "not_applicable"
    assert top_rows[dormant]["state"] == "offline"
    assert all(
        row["state"] != "stopped"
        for row in top_rows.values()
        if row["processState"] == "not_applicable"
    )
    rendered = _render_top(top, wide=False)
    assert "interactive" in rendered
    assert "online (proc n/a)" in rendered
    assert "offline (proc n/a)" in rendered
    assert "stopped" not in rendered


def test_top_and_targets_both_depend_on_the_actor_status_snapshot_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two IPC methods must consume one seam, not parallel lookalikes."""

    app = _application(tmp_path)
    calls = 0
    actor = "agent:sentinel:node:only-from-status-layer"

    def snapshot() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return [
            {
                "actor": actor,
                "name": "only-from-status-layer",
                "runtime": None,
                "status": "offline",
                "processState": "not_applicable",
                "running": None,
                "pid": None,
                "sources": ["registry"],
                "state": "offline",
            }
        ]

    monkeypatch.setattr(app, "_actor_status_snapshot", snapshot, raising=False)

    top = app.handle("top.snapshot", {})
    targets = app.handle("targets", {"kind": "agent"})["targets"]

    assert calls == 2
    assert [(row["actor"], row["status"]) for row in top["actors"]] == [
        (actor, "offline")
    ]
    assert [(row["actor"], row["status"]) for row in targets] == [
        (actor, "offline")
    ]


def test_id_minted_registry_actor_absorbs_its_connector_and_session() -> None:
    """UUID addressing: a record stored under its ``a.`` id is one row."""

    agent_id = "a." + "b" * 26
    official = f"agent:{OWNER}:{NODE}:{agent_id}"
    rows = build_actor_status_snapshot(
        owner=OWNER,
        node_id=NODE,
        connectors=({"name": "managed", "running": True, "pid": 7},),
        interactive_sessions=({"actor": "managed", "runtime": "pi"},),
        registered_actors=(official,),
        registered_names={"managed": official},
        presence_actors=(official,),
        status_for_actor=lambda _actor, _online: "online",
    )

    assert [row["actor"] for row in rows] == [official]
    assert rows[0]["name"] == "managed"
    assert rows[0]["sources"] == [
        "connector", "interactive_session", "presence", "registry",
    ]
