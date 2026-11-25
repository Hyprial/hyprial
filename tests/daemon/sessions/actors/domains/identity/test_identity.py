from __future__ import annotations

import threading
import sqlite3
from tests.support.runtime.waiting_support import await_recorded
from pathlib import Path
from hyprial.identity.impl.agents import AgentActor, AgentLiveness, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted, CreateAgentCommand, RecordAgentActivityCommand, UpdateAgentCommand, GrantAgentCapabilityCommand, RevokeAgentCapabilityCommand, AgentCapabilityGrantCompleted, GrantAgentSecretCommand, RevokeAgentSecretCommand, AgentSecretGrantCompleted
from hyprial.identity.impl.agents.runtime.secrets import SecretSource
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected

from ..support import (
    ACTOR,
    NODE,
    OWNER,
    _SESSION_EVENT_BUDGET,
)
from tests.support.actor_hosts import new_actor_host


def test_sqlite_is_final_duplicate_identity_constraint_across_actors(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    events_a: list[object] = []
    events_b: list[object] = []
    actor_a = AgentActor(
        AgentRegistry(database, owner=OWNER, machine=NODE),
        event_sink=events_a.append, actor_host=new_actor_host(),
    )
    actor_b = AgentActor(
        AgentRegistry(database, owner=OWNER, machine=NODE),
        event_sink=events_b.append, actor_host=new_actor_host(),
    )
    barrier = threading.Barrier(3)

    def submit(actor: AgentActor, correlation_id: str) -> None:
        barrier.wait()
        actor.submit(CreateAgentCommand(correlation_id, "foo"))

    threads = [
        threading.Thread(target=submit, args=(actor_a, "create-a")),
        threading.Thread(target=submit, args=(actor_b, "create-b")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2.0)
        assert not thread.is_alive()
    await_recorded(
        lambda: len(events_a) + len(events_b) >= 2,
        budget=_SESSION_EVENT_BUDGET,
        proves="both actors each record at least one event",
        monotonic_because="events_a is append-only in this file; a recorded both actors each record at least one entry is never removed"
    )

    all_events = [*events_a, *events_b]
    assert sum(isinstance(event, AgentMutationCompleted) for event in all_events) == 1
    rejections = [
        event for event in all_events if isinstance(event, PortCommandRejected)
    ]
    assert len(rejections) == 1
    assert rejections[0].code == "AGENT_EXISTS"
    assert len(AgentRegistry(database, owner=OWNER, machine=NODE).list()) == 1
    assert actor_a.drain(1.0).complete is True
    assert actor_b.drain(1.0).complete is True


def test_activity_version_is_visible_on_every_agent_projection(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    agent_a = registry.create("a")
    registry.create("b")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, actor_host=new_actor_host())
    try:
        actor.submit(RecordAgentActivityCommand("activity-a", agent_a.uri))
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "activity-a"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="activity completion is recorded",
            monotonic_because="events is append-only",
        )
        projection_a = actor.read_agent("a")
        projection_b = actor.read_agent("b")
        assert projection_a is not None
        assert projection_b is not None
        assert projection_b.version == projection_a.version

        actor.submit(UpdateAgentCommand(
            "update-b",
            "b",
            expected_version=projection_b.version,
            expected_entity_token=projection_b.entity_token,
        ))
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "update-b"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="the fresh projection admits an unchanged update",
            monotonic_because="events is append-only",
        )
        assert not any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "update-b"
            for event in events
        )
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_agent_identity_projection_reads_use_the_update_fence(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, actor_host=new_actor_host())
    try:
        projection = actor.read_agent("foo")
        assert projection is not None
        assert actor.read_local_actor(projection.uri) == "foo"
        assert actor.read_uri_for("foo") == projection.uri
        assert actor.submit(RecordAgentActivityCommand("activity", projection.uri)) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentMutationCompleted)
                and event.correlation_id == "activity"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="activity completion is recorded",
            monotonic_because="events is append-only",
        )
        stale = UpdateAgentCommand(
            "stale-update", "foo", expected_version=projection.version,
            expected_entity_token=projection.entity_token,
        )
        assert actor.submit(stale) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, PortCommandRejected)
                and event.correlation_id == "stale-update"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="stale update rejection is recorded",
            monotonic_because="events is append-only",
        )
        rejected = next(
            event for event in events
            if isinstance(event, PortCommandRejected)
            and event.correlation_id == "stale-update"
        )
        assert rejected.code == "AGENT_VERSION_CONFLICT"
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_agent_capability_grant_mutations_run_on_agent_authority(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, actor_host=new_actor_host())
    try:
        command = GrantAgentCapabilityCommand(
            "grant-capability", ACTOR, "grant-1", "org-context",
            "accepted", "user:op", 1,
        )
        assert actor.submit(command) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentCapabilityGrantCompleted)
                and event.correlation_id == "grant-capability"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="grant completion is recorded",
            monotonic_because="events is append-only",
        )
        granted = next(
            event for event in events
            if isinstance(event, AgentCapabilityGrantCompleted)
            and event.correlation_id == "grant-capability"
        )
        assert granted.grant is not None and granted.grant.grant_id == "grant-1"
        assert len(actor.read_capability_grants(ACTOR)) == 1
        assert actor.submit(RevokeAgentCapabilityCommand(
            "revoke-capability", ACTOR, "grant-1", "user:op"
        )) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentCapabilityGrantCompleted)
                and event.correlation_id == "revoke-capability"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="revoke completion is recorded",
            monotonic_because="events is append-only",
        )
        assert registry.capability_grants(ACTOR) == ()
        assert actor.read_capability_grants(ACTOR) == ()
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_agent_grant_read_fails_closed_after_external_incarnation_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, actor_host=new_actor_host())
    try:
        assert actor.submit(GrantAgentCapabilityCommand(
            "grant-stale", ACTOR, "grant-1", "org-context",
            "accepted", "user:op", 1,
        )) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentCapabilityGrantCompleted)
                and event.correlation_id == "grant-stale"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="grant completion is recorded",
            monotonic_because="events is append-only",
        )
        assert len(actor.read_capability_grants(ACTOR)) == 1
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE agent_capability_grants SET entity_token='stale' "
                "WHERE actor='foo' AND grant_id='grant-1'"
            )
        assert actor.read_capability_grants(ACTOR) == ()
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_synthetic_secret_grant_metadata_is_actor_owned(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, actor_host=new_actor_host())
    try:
        command = GrantAgentSecretCommand(
            "secret-grant", ACTOR, "grant-1", SecretSource.USER_PROVIDER,
            "synthetic-entry", None, ("OPENAI_API_KEY",), 1,
        )
        assert actor.submit(command) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentSecretGrantCompleted)
                and event.correlation_id == "secret-grant"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="secret grant completion is recorded",
            monotonic_because="events is append-only",
        )
        granted = next(
            event for event in events
            if isinstance(event, AgentSecretGrantCompleted)
            and event.correlation_id == "secret-grant"
        )
        assert granted.grant is not None
        assert granted.grant.entry_id == "synthetic-entry"
        assert len(actor.read_secret_inventory(ACTOR)) == 1
        assert actor.submit(RevokeAgentSecretCommand(
            "secret-revoke", ACTOR, "grant-1"
        )) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: any(
                isinstance(event, AgentSecretGrantCompleted)
                and event.correlation_id == "secret-revoke"
                for event in events
            ),
            budget=_SESSION_EVENT_BUDGET,
            proves="secret revoke completion is recorded",
            monotonic_because="events is append-only",
        )
        assert registry.secret_inventory(ACTOR) == ()
        assert actor.read_secret_inventory(ACTOR) == ()
    finally:
        assert actor.drain(1.0).complete
        registry.close()


def test_create_launch_incumbent_failure_leaves_no_partial_agent_row(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    liveness = AgentLiveness(node_id=NODE)
    liveness.bind(ACTOR, harness="claude", runtime="headless")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append, liveness=liveness, actor_host=new_actor_host())

    actor.submit(
        CreateAgentCommand(
            correlation_id="create-launch",
            name="foo",
            launch_harness="pi",
        )
    )
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "create-launch" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert registry.get("foo") is None
    assert actor.read_agent("foo") is None
    assert actor.drain(1.0).complete is True


def test_agent_actor_exposes_only_typed_surface_not_mutable_authorities(
    tmp_path: Path,
) -> None:
    actor = AgentActor(
        AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE),
        event_sink=lambda _event: None, actor_host=new_actor_host(),
    )
    assert not hasattr(actor, "registry")
    assert not hasattr(actor, "liveness")
    assert not hasattr(actor, "desired_state")
    assert actor.read_agents() == ()
    assert actor.drain(1.0).complete is True
