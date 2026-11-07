"""agent_entity: registry scenarios."""

from __future__ import annotations
import base64
import dataclasses
import hashlib
import re
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import (
    Agent,
    AgentExistsError,
    AgentNotFoundError,
    AgentRegistry,
    InvalidAgentNameError,
)
from hyprial.identity.impl.agents.registry import _base as registry_base
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.desired_state import (
    DesiredStateStore,
    InteractiveSession,
)

from .support import (
    NODE,
    OWNER,
    URI,
    _close_test_applications as _close_test_applications,
    application,
    register_interactive,
    start_headless,
)

# --------------------------------------------------------------------------
# §3 -- what an Agent stores
# --------------------------------------------------------------------------

def test_agent_record_stores_configuration_and_no_runtime_state() -> None:
    """The field list is the design decision, so it is asserted literally.

    `harness` is absent because a harness is a runtime binding, not
    configuration -- an agent may start on claude and swap to pi without its
    record changing. `state` is absent because there is no retired state
    (existing is active, destroy is deletion). Liveness is absent because it
    belongs to one daemon generation, not to the durable record.
    """

    fields = tuple(field.name for field in dataclasses.fields(Agent))

    assert fields == (
        "uri",
        "actor",
        "owner",
        "machine",
        # Incarnation fences homes and grants across destructive rebuilds.
        "entity_token",
        "cwd",
        "config",
        "provider",
        "model",
        "capabilities",
        "harness_args",
        "preferred_harness",
        "last_harness",
        "last_session_id",
        "last_active_at_ms",
        # Which adapters route their inbound DMs here.  On the record on
        # purpose: destroy deletes the file and every pin dies with it.
        "pinned_adapters",
        "created_at_ms",
        # Explicit hosting authority is identity metadata, not runtime state.
        "hosted_by",
    )
    assert "harness" not in fields
    assert "state" not in fields
    assert "last_seen_ms" not in fields

def test_agent_id_is_derived_from_incarnation_and_changes_on_recreate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        first = registry.create("foo")
        assert re.fullmatch(r"a\.[a-z2-7]{26}", first.agent_id)
        raw_token = bytes.fromhex(first.entity_token)
        expected = base64.b32encode(
            hashlib.sha256(b"hyprial-agent-id-v1" + raw_token).digest()[:16]
        ).decode("ascii").rstrip("=").lower()
        reversible = base64.b32encode(raw_token).decode("ascii").rstrip("=").lower()
        assert first.agent_id == f"a.{expected}"
        assert first.agent_id != f"a.{reversible}"
    finally:
        registry.close()

    reloaded = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert reloaded.require("foo").agent_id == first.agent_id
        assert reloaded.destroy("foo") is True
        replacement = reloaded.create("foo")
        assert replacement.agent_id != first.agent_id
    finally:
        reloaded.close()


@pytest.mark.parametrize(
    "reserved_id",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_agent_names_reject_exact_identity_id_shapes(
    tmp_path: Path, reserved_id: str
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    try:
        with pytest.raises(InvalidAgentNameError, match="identity id shape"):
            registry.create(reserved_id)
    finally:
        registry.close()


@pytest.mark.parametrize("name", ["d.lane", "a.smith", "u.zhang"])
def test_agent_names_accept_id_prefix_lookalikes(tmp_path: Path, name: str) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    try:
        assert registry.create(name).actor == name
    finally:
        registry.close()


@pytest.mark.parametrize("reserved", ["dispatch-coordinator", "_turn-hooks"])
def test_agent_names_reject_daemon_reserved_actor_names(
    tmp_path: Path, reserved: str
) -> None:
    """README ruling 63: a reserved name must not claim the link exemption.

    ``link_governed`` exempts these actor names on any node, so registering an
    ordinary agent under one would bypass every link constraint.
    """

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    try:
        with pytest.raises(InvalidAgentNameError, match="reserved"):
            registry.create(reserved)
        assert registry.get(reserved) is None
    finally:
        registry.close()


@pytest.mark.parametrize("reserved", ["dispatch-coordinator"])
def test_persisted_reserved_agent_is_still_readable_and_destroyable(
    tmp_path: Path, reserved: str
) -> None:
    """The guard is on creation only; a legacy row must remain manageable.

    ``_turn-hooks`` is excluded because the pre-existing actor-name pattern
    already refuses a leading ``_`` on every read path.
    """

    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("legacy")
    with registry._lock, registry._db:  # noqa: SLF001 - pre-ruling-63 fixture
        registry._db.execute(  # noqa: SLF001 - model an older release's row
            "UPDATE agents SET actor = ?, uri = ? WHERE actor = ?",
            (reserved, f"agent:{OWNER}:{NODE}:{reserved}", "legacy"),
        )
    registry.close()

    reloaded = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert reloaded.require(reserved).actor == reserved
        assert reloaded.destroy(reserved) is True
        assert reloaded.get(reserved) is None
    finally:
        reloaded.close()


def test_persisted_reserved_agent_can_be_required_and_destroyed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("legacy")
    with registry._lock, registry._db:  # noqa: SLF001 - pre-R3 persisted fixture
        registry._db.execute(  # noqa: SLF001 - model an older release's row
            "UPDATE agents SET actor = ?, uri = ? WHERE actor = ?",
            ("a.legacy", f"agent:{OWNER}:{NODE}:a.legacy", "legacy"),
        )
    registry.close()

    reloaded = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert reloaded.require("a.legacy").actor == "a.legacy"
        assert reloaded.destroy("a.legacy") is True
        assert reloaded.get("a.legacy") is None
    finally:
        reloaded.close()


def test_activity_timestamp_is_throttled_to_one_write_per_minute(
    tmp_path: Path,
) -> None:
    now = [1_000]
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner=OWNER,
        machine=NODE,
        clock=lambda: now[0],
    )
    try:
        registry.create("foo")
        stale = registry.require("foo")
        writes: list[str] = []
        registry._db.set_trace_callback(  # noqa: SLF001 - count the durable writes
            lambda statement: writes.append(statement)
            if statement.startswith("UPDATE agents SET last_active_at_ms")
            else None
        )

        assert registry.record_activity("foo") is True
        now[0] += 59_999
        assert registry.record_activity("foo") is False

        assert len(writes) == 1
        assert registry.require("foo").last_active_at_ms == 1_000
        registry.save(stale)
        assert registry.require("foo").last_active_at_ms == 1_000
    finally:
        registry.close()


def test_agent_start_records_activity_and_lists_honest_hints(tmp_path: Path) -> None:
    app = application(tmp_path)
    app._agent_registry._clock = lambda: 123_456  # noqa: SLF001

    start_headless(app, "pi", "foo")

    agent = app.handle("agent.list", {})["agents"][0]
    assert agent["lastActiveAtMs"] == 123_456
    assert agent["activityHints"] == {
        "createdAtMs": 123_456,
        "hasLastSessionId": False,
        "agentHomeMtimeMs": pytest.approx(
            (tmp_path / "agents" / "foo").stat().st_mtime_ns // 1_000_000,
            abs=1,
        ),
    }


def test_message_activity_is_queued_before_any_registry_write(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    app._agent_registry._clock = lambda: 123_456  # noqa: SLF001

    app._queue_agent_activity(URI)  # noqa: SLF001

    assert app.agents.require("foo").last_active_at_ms is None
    app._flush_agent_activity()  # noqa: SLF001
    assert app.agents.require("foo").last_active_at_ms == 123_456


def test_inactive_listing_uses_recorded_activity_and_excludes_workflow_agents(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    old_ms = now_ms - 10 * 86_400_000
    boundary_ms = now_ms - 7 * 86_400_000
    recent_ms = now_ms - 86_400_000
    app = application(tmp_path)
    app._agent_registry._clock = lambda: old_ms  # noqa: SLF001
    for name in (
        "stale",
        "boundary",
        "home-hint",
        "recent",
        "wf-old",
        "session-hint",
    ):
        app.agents.create(name)
        home = tmp_path / "agents" / name
        old_seconds = old_ms / 1_000
        os.utime(home, (old_seconds, old_seconds))
    recent_seconds = recent_ms / 1_000
    os.utime(
        tmp_path / "agents" / "home-hint",
        (recent_seconds, recent_seconds),
    )
    app._agent_registry._clock = lambda: boundary_ms  # noqa: SLF001
    app.agents.record_activity("boundary")
    app._agent_registry._clock = lambda: recent_ms  # noqa: SLF001
    app.agents.record_activity("recent")
    app.agents.record_session("session-hint", harness="pi", session_id="known")
    app._agent_registry._clock = lambda: now_ms  # noqa: SLF001

    result = app.handle(
        "agent.list", {"inactiveSince": "7d", "excludeWf": True}
    )

    assert [item["actor"] for item in result["agents"]] == [
        "home-hint",
        "session-hint",
        "stale",
    ]


def test_old_registry_schema_adds_nullable_activity_without_data_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agents ("
            "actor TEXT PRIMARY KEY, owner TEXT NOT NULL, machine TEXT NOT NULL, "
            "uri TEXT NOT NULL UNIQUE, entity_token TEXT NOT NULL UNIQUE, cwd TEXT, "
            "config TEXT, provider TEXT, model TEXT, capabilities TEXT NOT NULL "
            "DEFAULT '{}', harness_args TEXT NOT NULL DEFAULT '{}', "
            "preferred_harness TEXT, last_harness TEXT, last_session_id TEXT, "
            "created_at_ms INTEGER NOT NULL, hosted_by TEXT DEFAULT NULL "
            "CHECK (hosted_by IN ('transfer-receive', 'squire-container', 'host-invite')))"
        )
        connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, "
            "'{}', '{}', NULL, NULL, NULL, ?, NULL)",
            ("foo", OWNER, NODE, URI, "token-1", 7),
        )

    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert registry.require("foo").created_at_ms == 7
        assert registry.require("foo").last_active_at_ms is None
        columns = {
            row[1]: row
            for row in registry._db.execute(  # noqa: SLF001
                "PRAGMA table_info(agents)"
            )
        }
        assert columns["last_active_at_ms"][3] == 0
    finally:
        registry.close()


def test_registry_round_trips_an_agent_through_its_own_file(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)

    created = registry.create(
        "foo",
        cwd="/work",
        provider="anthropic",
        model="opus",
        harness_args={"claude": ["--model", "opus"], "pi": ("--fast",)},
        preferred_harness="claude",
    )

    assert created.uri == URI
    assert created.harness_args == {
        "claude": ("--model", "opus"),
        "pi": ("--fast",),
    }
    assert registry.get("foo") == created
    # The store is one real sqlite database -- the file name says what it is.
    database = tmp_path / "agents.sqlite3"
    assert database.is_file()
    assert database.read_bytes()[:16] == b"SQLite format 3\x00"
    # A second registry over the same database reads the identical record.
    assert (
        AgentRegistry(database, owner=OWNER, machine=NODE).get("foo") == created
    )
    # A canonical URI resolves to the same record as the bare name.
    assert registry.get(URI) == created


def test_legacy_json_records_import_once_and_are_archived(tmp_path: Path) -> None:
    """Pre-sqlite ``agents/*.json`` records are imported at construction and
    renamed to ``*.json.imported`` -- kept, not deleted, so a rollback to a
    file-reading build still finds its data.  A second construction imports
    nothing (the archive suffix is not scanned)."""

    legacy = tmp_path / "agents"
    legacy.mkdir()
    record = {
        "actor": "foo",
        "owner": OWNER,
        "machine": NODE,
        "uri": URI,
        "cwd": "/work",
        "pinnedAdapters": ["lark-a"],
        "createdAtMs": 7,
    }
    (legacy / "foo.json").write_text(json.dumps(record), encoding="utf-8")

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)

    assert registry.imported_legacy == ("foo.json",)
    imported = registry.require("foo")
    assert imported.uri == URI
    assert imported.cwd == "/work"
    assert imported.created_at_ms == 7
    # The pin rode along into the pins table.
    assert registry.pins() == {"lark-a": URI}
    # Archived, not deleted.
    assert not (legacy / "foo.json").exists()
    assert (legacy / "foo.json.imported").is_file()

    again = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    assert again.imported_legacy == ()
    assert again.require("foo").created_at_ms == 7


def test_pinned_adapters_come_from_the_pins_table(tmp_path: Path) -> None:
    """``pinnedAdapters`` is composed from the pins table at read time; the
    only writers are pin/unpin, so a stale record save cannot clobber it."""

    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo")

    assert registry.pin("lark-a", "foo") is None
    assert registry.require("foo").pinned_adapters == ("lark-a",)
    # A second registry over the same database sees the same binding.
    reloaded = AgentRegistry(database, owner=OWNER, machine=NODE)
    assert reloaded.require("foo").pinned_adapters == ("lark-a",)
    assert reloaded.pins() == {"lark-a": URI}
    # save() ignores the field: an old copy of the record cannot unpin.
    registry.save(
        dataclasses.replace(registry.require("foo"), pinned_adapters=())
    )
    assert registry.require("foo").pinned_adapters == ("lark-a",)
    # unpin is the one legitimate remover.
    assert registry.unpin("lark-a") == URI
    assert registry.require("foo").pinned_adapters == ()


def test_registry_rejects_names_that_are_not_safe_file_segments(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)

    for bad in ("../escape", "with/slash", ".hidden", ""):
        with pytest.raises(InvalidAgentNameError):
            registry.create(bad)


# --------------------------------------------------------------------------
# A1 -- duplicate names are a hard error
# --------------------------------------------------------------------------


def test_creating_a_second_agent_under_one_name_fails(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo", cwd="/first")

    with pytest.raises(AgentExistsError) as failure:
        registry.create("foo", cwd="/second")

    assert "foo" in str(failure.value)
    # The rejected create must not have touched the existing record.
    agent = registry.require("foo")
    assert agent.cwd == "/first"


def test_agent_create_rpc_refuses_a_taken_name(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "foo"})

    assert failure.value.code == "AGENT_EXISTS"


def test_the_name_is_taken_whether_or_not_anything_is_running(
    tmp_path: Path,
) -> None:
    """Uniqueness is a property of the record, not of a live connector.

    The name belongs to `agent:<owner>:<machine>:<name>` for as long as that
    record exists. Nothing has ever been started here, and the name is still
    taken -- only destroy releases it.
    """

    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    assert app._agent_liveness.binding(URI) is None
    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "offline"

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "foo"})

    assert failure.value.code == "AGENT_EXISTS"


def test_an_agent_name_is_a_name_not_a_harness(tmp_path: Path) -> None:
    """`pi-ds4` is a string. It says nothing about which harness runs it.

    The name occupies `agent:op:node-1:pi-ds4` regardless, and that one agent
    can run on claude just as well as on pi.
    """

    app = application(tmp_path)
    app.handle("agent.create", {"name": "pi-ds4"})
    agent = app.agents.require("pi-ds4")
    assert agent.uri == f"agent:{OWNER}:{NODE}:pi-ds4"
    assert not hasattr(agent, "harness")

    # Runs happily on a harness whose name has nothing to do with its own.
    started = start_headless(app, "claude", "pi-ds4")
    assert started["actor"] == f"agent:{OWNER}:{NODE}:pi-ds4"

    # And the name is still taken, by that one agent.
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "pi-ds4"})
    assert failure.value.code == "AGENT_EXISTS"


def test_starting_a_running_agent_is_refused_and_names_the_remedy(
    tmp_path: Path,
) -> None:
    """`hyprial agent start` does not attach to an agent that already has a connector.

    Refusing rather than stopping the incumbent is the point: a headless
    worker must not lose in-flight work silently, and an interactive session
    must not be deregistered while its window is still open.
    """

    app = application(tmp_path)
    start_headless(app, "pi", "foo")

    with pytest.raises(DaemonRequestError) as failure:
        start_headless(app, "claude", "foo")

    assert failure.value.code == "AGENT_ALREADY_RUNNING"
    message = str(failure.value)
    assert "pi" in message
    assert "hyprial agent stop foo" in message
    # It must not read as a name conflict -- foo is one agent, and stays one.
    assert "not a name conflict" in message
    # The refused start left nothing behind.
    assert [spec.harness for spec in app.desired_state.load().harnesses] == ["pi"]
    assert app._agent_liveness.binding(URI).harness == "pi"  # type: ignore[union-attr]


def test_restarting_the_very_same_connector_is_refused_too(tmp_path: Path) -> None:
    """"Already running" does not depend on which harness is asked for."""

    app = application(tmp_path)
    start_headless(app, "pi", "foo")

    with pytest.raises(DaemonRequestError) as failure:
        start_headless(app, "pi", "foo")

    assert failure.value.code == "AGENT_ALREADY_RUNNING"


def test_an_interactive_session_also_blocks_a_headless_start(
    tmp_path: Path,
) -> None:
    """The reported defect, from the interactive side.

    `claude:foo` never reaches lifecycle.start -- it registers through
    session.register -- while `pi:foo` only reaches lifecycle.start. Both mint
    agent:op:node-1:foo, which is how two connectors used to end up fighting
    over one URI. Gating one door would leave that alive.
    """

    app = application(tmp_path)
    register_interactive(app, URI)

    with pytest.raises(DaemonRequestError) as failure:
        start_headless(app, "pi", "foo")

    assert failure.value.code == "AGENT_ALREADY_RUNNING"
    assert "exit that session" in str(failure.value)
    assert app.desired_state.load().harnesses == ()


def test_a_headless_worker_blocks_an_interactive_registration(
    tmp_path: Path,
) -> None:
    """The same collision arriving in the opposite order."""

    app = application(tmp_path)
    start_headless(app, "pi", "foo")

    with pytest.raises(DaemonRequestError) as failure:
        register_interactive(app, URI)

    assert failure.value.code == "AGENT_ALREADY_RUNNING"
    assert app.desired_state.load().interactive_sessions == ()


def test_down_then_start_moves_the_same_agent_to_another_harness(
    tmp_path: Path,
) -> None:
    """The supported way to change harness: down, then start. One agent.

    This is the path the refusal points at, so it has to work, carry the same
    identity, and report the previous harness and session for A9.
    """

    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    created_at = app.agents.require("foo").created_at_ms

    app.handle("down", {"target": "foo"})
    result = start_headless(app, "claude", "foo")

    assert result["ok"] is True
    assert result["actor"] == URI
    # Same agent record throughout -- not a new one.
    assert app.agents.local_actors() == (URI,)
    assert app.agents.require("foo").created_at_ms == created_at
    assert [spec.harness for spec in app.desired_state.load().harnesses] == ["claude"]
    assert app._agent_liveness.binding(URI).harness == "claude"  # type: ignore[union-attr]
    handover = result["harnessHandover"]
    assert handover["previousHarness"] == "pi"
    assert handover["nextHarness"] == "claude"


def test_at_most_one_connector_ever_speaks_for_an_actor(tmp_path: Path) -> None:
    """The property the whole change exists to guarantee.

    Now upheld by refusing rather than by stopping the incumbent, which is
    both stronger and simpler: a second connector is never created at all.
    """

    app = application(tmp_path)

    for harness in ("claude", "pi", "codex", "pi"):
        app.handle("down", {"target": "foo"})
        start_headless(app, harness, "foo")
        specs = app.desired_state.load().harnesses
        assert [spec.harness for spec in specs] == [harness]
        assert len(app._harnesses.status()) == 1  # type: ignore[union-attr]
        assert app.agents.local_actors() == (URI,)
        # And a competing start is refused for as long as it is running.
        with pytest.raises(DaemonRequestError):
            register_interactive(app, URI)


def test_a_dead_connector_does_not_block_a_start(tmp_path: Path) -> None:
    """A crash must self-heal; only a *live* connector objects."""

    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    app._harnesses.running[("pi", "foo")] = False  # type: ignore[union-attr]

    result = start_headless(app, "claude", "foo")

    assert result["ok"] is True
    # The dead connector's desired-state entry is cleared with it, so a daemon
    # restart cannot resurrect it beside the new one.
    assert [spec.harness for spec in app.desired_state.load().harnesses] == ["claude"]


def test_down_releases_the_binding_even_without_a_desired_state_entry(
    tmp_path: Path,
) -> None:
    """The remedy the refusal names must always work.

    A binding whose desired-state entry vanished without a clean stop would
    otherwise strand the agent's name behind an error whose own remedy
    ('hyprial agent stop <name>') does nothing.
    """

    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    # Simulate the spec vanishing without release: down has nothing to match.
    app.desired_state.remove_harness("pi", "foo")
    assert app._agent_liveness.binding(URI) is not None

    result = app.handle("down", {"target": "foo"})

    assert result["removed"] == []
    assert app._agent_liveness.binding(URI) is None
    # The named remedy worked: the agent can start again.
    assert start_headless(app, "claude", "foo")["ok"] is True


def test_two_differently_named_agents_coexist(tmp_path: Path) -> None:
    app = application(tmp_path)

    start_headless(app, "claude", "foo")
    start_headless(app, "pi", "bar")

    assert sorted(app.agents.local_actors()) == [
        f"agent:{OWNER}:{NODE}:bar",
        URI,
    ]


# --------------------------------------------------------------------------
# A2 -- the invariants live in the schema (constraint layer)
# --------------------------------------------------------------------------


def test_duplicate_actor_is_refused_by_the_primary_key(tmp_path: Path) -> None:
    """A1 is a PRIMARY KEY now: the duplicate never reaches application code.

    The raw INSERT proves the constraint itself; the registry API proves the
    IntegrityError is translated into the existing AGENT_EXISTS error, message
    style intact (names the taken URI, gives the destroy command).
    """

    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo", cwd="/first")

    connection = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO agents (actor, owner, machine, uri, created_at_ms)"
                " VALUES ('foo', 'x', 'y', 'agent:x:y:foo2', 0)"
            )
    finally:
        connection.close()

    with pytest.raises(AgentExistsError) as failure:
        registry.create("foo", cwd="/second")
    assert failure.value.code == "AGENT_EXISTS"
    assert URI in str(failure.value)
    assert "hyprial agent destroy foo" in str(failure.value)
    # The loser wrote nothing.
    assert registry.require("foo").cwd == "/first"


def test_destroy_cascades_the_pin_away_in_one_transaction(
    tmp_path: Path,
) -> None:
    """ON DELETE CASCADE: no cleanup code exists to forget."""

    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo")
    registry.pin("lark-a", "foo")
    assert registry.pins() == {"lark-a": URI}

    assert registry.destroy("foo") is True

    assert registry.pins() == {}
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 0
    finally:
        connection.close()


def test_pinning_a_nonexistent_agent_is_a_foreign_key_error(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE
    )

    with pytest.raises(AgentNotFoundError):
        registry.pin("lark-a", "nobody")


def test_concurrent_processes_pinning_one_agent_admit_exactly_one(
    tmp_path: Path,
) -> None:
    """UNIQUE(agent) is enforced across real processes, not just in-process.

    Two subprocesses race to pin two different adapters to the same agent.
    Exactly one insert may win; the loser must see the typed conflict, and
    the surviving row must be the winner's.
    """

    database = tmp_path / "agents.sqlite3"
    AgentRegistry(database, owner=OWNER, machine=NODE).create("foo")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hyprial.identity.impl.agents import AgentRegistry, PinConflictError\n"
        f"registry = AgentRegistry(Path({str(database)!r}), "
        f"owner={OWNER!r}, machine={NODE!r})\n"
        "try:\n"
        "    registry.pin(sys.argv[1], 'foo')\n"
        "except PinConflictError:\n"
        "    print('CONFLICT')\n"
        "else:\n"
        "    print('PINNED')\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, adapter],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for adapter in ("lark-a", "lark-b")
    ]
    outcomes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outcomes.append(stdout.strip())

    assert sorted(outcomes) == ["CONFLICT", "PINNED"]
    pins = AgentRegistry(database, owner=OWNER, machine=NODE).pins()
    assert len(pins) == 1
    assert set(pins.values()) == {URI}


# --------------------------------------------------------------------------
# A5 -- hyprial agent start creates the agent through agent create
# --------------------------------------------------------------------------


def test_lifecycle_start_creates_the_agent_record(tmp_path: Path) -> None:
    app = application(tmp_path)

    result = start_headless(app, "pi", "foo")

    assert result["actor"] == URI
    agent = app.agents.require("foo")
    assert agent.uri == URI
    assert agent.cwd == "/work"
    assert agent.preferred_harness == "pi"
    assert (tmp_path / "agents.sqlite3").is_file()


def test_session_register_creates_the_agent_record(tmp_path: Path) -> None:
    app = application(tmp_path)

    register_interactive(app, URI)

    assert app.agents.require("foo").uri == URI


def test_retired_session_refs_prune_on_write_and_release_exactly_one(
    tmp_path: Path,
) -> None:
    now = [1_000]
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner=OWNER,
        machine=NODE,
        clock=lambda: now[0],
    )
    try:
        agent = registry.create("foo")
        assert registry.retire_session_refs(
            URI,
            agent.entity_token,
            ("session-a", "session-b"),
            reason="agent-destroyed",
            destroy_attempt="attempt-a",
        ) == 2
        assert registry.release_retired_session_ref(URI, "session-a") is True
        assert registry.is_session_ref_retired(URI, "session-a") is False
        assert registry.is_session_ref_retired(URI, "session-b") is True

        now[0] += registry_base.RETIRED_SESSION_REF_RETENTION_MS + 1
        assert registry.retire_session_refs(
            URI,
            agent.entity_token,
            ("session-c",),
            reason="agent-destroyed",
            destroy_attempt="attempt-b",
        ) == 1
        assert registry.is_session_ref_retired(URI, "session-b") is False
        assert registry.is_session_ref_retired(URI, "session-c") is True
    finally:
        registry.close()


def test_session_ref_history_is_per_incarnation_bounded_and_cascades(
    tmp_path: Path,
) -> None:
    now = [1_000]
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner=OWNER,
        machine=NODE,
        clock=lambda: now[0],
    )
    try:
        agent = registry.create("foo")
        count = 259
        for index in range(count):
            now[0] += 1
            registry.record_session_ref(
                URI, agent.entity_token, f"session-{index:04d}"
            )

        history = registry.session_ref_history(URI, agent.entity_token)
        assert len(history) == 256
        assert history[0] == "session-0003"
        assert history[-1] == f"session-{count - 1:04d}"
        # Eviction retires, never drops: an evicted ref must not escape the
        # next destroy and later resurrect the agent (hq, review 860).
        for index in range(3):
            assert registry.is_session_ref_retired(URI, f"session-{index:04d}")
        assert not registry.is_session_ref_retired(URI, "session-0003")
        assert not registry.is_session_ref_retired(URI, f"session-{count - 1:04d}")
        # A destroy-attempt rollback never releases an eviction.
        registry.rollback_retired_session_refs("some-destroy-attempt")
        assert registry.is_session_ref_retired(URI, "session-0000")

        assert registry.destroy("foo") is True
        assert registry.session_ref_history(URI, agent.entity_token) == ()
    finally:
        registry.close()


def test_retired_session_attempt_columns_upgrade_existing_table(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            "CREATE TABLE retired_session_refs ("
            "actor TEXT NOT NULL, session_ref TEXT NOT NULL, "
            "retired_at_ms INTEGER NOT NULL, reason TEXT NOT NULL, "
            "PRIMARY KEY (actor, session_ref));"
        )
        connection.execute(
            "INSERT INTO retired_session_refs VALUES (?, ?, ?, ?)",
            (URI, "legacy-ref", 1_000, "legacy-destroy"),
        )
        connection.commit()
    finally:
        connection.close()

    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        columns = {
            str(row["name"])
            for row in registry._db.execute(  # noqa: SLF001 - migration witness
                "PRAGMA table_info(retired_session_refs)"
            )
        }
        assert {"entity_token", "destroy_attempt"} <= columns
        assert registry.is_session_ref_retired(URI, "legacy-ref") is True
    finally:
        registry.close()


def test_startup_imports_pre_agent_registry_interactive_session(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.register_interactive(
        InteractiveSession(
            actor=URI,
            cwd="/legacy/work",
            command=("claude",),
            source="claude-channel",
            session_ref="legacy-session-1",
            runtime="claude_interactive",
            channel_confirmed=True,
        )
    )

    app = application(tmp_path)

    imported = app.agents.require("foo")
    assert imported.uri == URI
    assert imported.cwd == "/legacy/work"
    assert imported.preferred_harness == "claude"
    assert app._agent_session_domains.imported_legacy == (
        f"interactiveSession:{URI}",
    )


def test_agent_create_rpc_reuses_an_existing_agent_for_start(tmp_path: Path) -> None:
    """`existing: reuse` is what separates `hyprial agent start` from `hyprial agent create`."""

    app = application(tmp_path)
    first = app.handle("agent.create", {"name": "foo", "cwd": "/work"})
    assert first["created"] is True

    again = app.handle(
        "agent.create",
        {"name": "foo", "existing": "reuse", "harness": "pi", "runtime": "headless"},
    )

    assert again["created"] is False
    assert again["agent"]["uri"] == URI
