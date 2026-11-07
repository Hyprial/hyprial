from __future__ import annotations

import sqlite3
from dataclasses import replace
from tests.support.runtime.waiting_support import await_recorded
from pathlib import Path
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted, SetRestoreDispositionCommand, ClearRestoreDispositionCommand, BlockAgentCommand, UnblockAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.session_actor import SessionActor

from ..support import (
    NODE,
    OWNER,
    _SESSION_EVENT_BUDGET,
    _register,
)


def test_block_commit_and_disposition_clear_publish_atomically(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append)
    try:
        actor.submit(
            SetRestoreDispositionCommand(
                "suppress",
                "foo",
                "desired-1",
                1,
                10,
                20,
            )
        )
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "suppress"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the suppression commit is projected",
            monotonic_because="events is append-only",
        )
        assert actor.read_agent("foo").restore_disposition is not None  # type: ignore[union-attr]

        entity_token = actor.read_agent("foo").entity_token  # type: ignore[union-attr]
        actor.submit(
            SetRestoreDispositionCommand(
                "replace-suppression", "foo", "desired-2", 2, 20, 30
            )
        )
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "replace-suppression"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the replacement suppression is projected",
            monotonic_because="events is append-only",
        )
        actor.submit(
            ClearRestoreDispositionCommand(
                "late-clear", "foo", entity_token, "desired-1"
            )
        )
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "late-clear"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the stale clear is rejected without removing its successor",
            monotonic_because="events is append-only",
        )
        assert (
            actor.read_agent("foo").restore_disposition.desired_generation  # type: ignore[union-attr]
            == "desired-2"
        )

        actor.submit(BlockAgentCommand("block", "foo", "provider-quota"))
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "block"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the block commit is projected",
            monotonic_because="events is append-only",
        )
        projection = actor.read_agent("foo")
        assert projection is not None and projection.block is not None
        assert projection.restore_disposition is None

        actor.submit(UnblockAgentCommand("unblock", "foo"))
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "unblock"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the unblock commit is projected",
            monotonic_because="events is append-only",
        )
        assert actor.read_agent("foo").block is None  # type: ignore[union-attr]
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_stale_block_command_does_not_mutate_recreated_agent(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    original = registry.create("foo")
    assert registry.destroy("foo")
    successor = registry.create("foo")
    assert successor.entity_token != original.entity_token
    registry.suppress_restore(
        "foo",
        desired_generation="successor-desired",
        last_active_at_ms=1,
        idle_age_ms=10,
        restore_threshold_ms=20,
    )
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append)
    try:
        actor.submit(
            BlockAgentCommand(
                "stale-block",
                "foo",
                "provider-quota",
                original.entity_token,
            )
        )
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "stale-block"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the stale blocking observation settles as an owned no-op",
            monotonic_because="events is append-only",
        )
        event = next(
            event
            for event in events
            if isinstance(event, AgentMutationCompleted)
            and event.correlation_id == "stale-block"
        )
        assert event.changed is False
        projection = actor.read_agent("foo")
        assert projection is not None
        assert projection.entity_token == successor.entity_token
        assert projection.block is None
        assert projection.restore_disposition is not None
        assert (
            projection.restore_disposition.desired_generation
            == "successor-desired"
        )
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_restore_disposition_migration_backfills_episode_token(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo")
    registry.suppress_restore(
        "foo",
        desired_generation="desired-1",
        last_active_at_ms=1,
        idle_age_ms=10,
        restore_threshold_ms=20,
    )
    registry.close()

    database_connection = sqlite3.connect(database)
    try:
        database_connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            ALTER TABLE agent_restore_dispositions
                RENAME TO agent_restore_dispositions_with_token;
            CREATE TABLE agent_restore_dispositions (
                actor TEXT PRIMARY KEY REFERENCES agents(actor) ON DELETE CASCADE,
                entity_token TEXT NOT NULL,
                desired_generation TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status = 'idle-suppressed'),
                last_active_at_ms INTEGER,
                idle_age_ms INTEGER,
                restore_threshold_ms INTEGER NOT NULL,
                restore_override TEXT NOT NULL,
                activity_unknown INTEGER NOT NULL,
                recorded_at_ms INTEGER NOT NULL
            );
            INSERT INTO agent_restore_dispositions (
                actor, entity_token, desired_generation, status,
                last_active_at_ms, idle_age_ms, restore_threshold_ms,
                restore_override, activity_unknown, recorded_at_ms
            )
            SELECT actor, entity_token, desired_generation, status,
                   last_active_at_ms, idle_age_ms, restore_threshold_ms,
                   restore_override, activity_unknown, recorded_at_ms
              FROM agent_restore_dispositions_with_token;
            DROP TABLE agent_restore_dispositions_with_token;
            COMMIT;
            """
        )
    finally:
        database_connection.close()

    migrated = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        disposition = migrated.restore_disposition("foo")
        assert disposition is not None
        assert len(disposition.disposition_token) == 32
        assert set(disposition.disposition_token) <= set("0123456789abcdef")
    finally:
        migrated.close()


def test_lifecycle_ipc_shaped_id_remains_background(tmp_path: Path) -> None:
    events: list[object] = []
    actor = SessionActor(
        DesiredStateStore(tmp_path / "desired.json"),
        daemon_epoch="epoch-1",
        event_sink=events.append,
    )
    request = LifecycleMutationRequest(
        "session:heartbeat:not-an-ipc-command",
        "lifecycle-attempt",
        "lifecycle-operation",
        None,
        replace(_register("payload-id", "session-1"), manage_agent=False),
    )
    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, LifecycleMutationCompleted)
                and event.correlation_id == request.correlation_id
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the lifecycle completion is recorded",
            monotonic_because="events is append-only",
        )
        persistence_costs = actor.persistence_io_costs.snapshot()
        assert persistence_costs["apply_session_lifecycle/background"]["calls"] == 1
        assert "apply_session_lifecycle/ipc" not in persistence_costs
    finally:
        assert actor.drain(1.0).complete
