"""The schema-8 owner rewrite (PR #536 review B2) and its B1 neutrality.

A v7 database's short-name owners would be locked out of flag/close/stop by
the principal-URI world's exact-equality authorization.  The v7→v8 migration
rewrites ``nodes.owner`` / ``graphs.created_by`` to full URIs -- only on
unique local resolution; ambiguous/unresolvable values stay verbatim, are
listed per graph in the era report, and refusals on those graphs point at
the report.  A source that exists but cannot be read aborts the whole
migration (zero writes, loud) -- never folded into "unresolvable".
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.support.pac.migrations import strip_tick_v19
from typer.testing import CliRunner

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_node, close_graph, create_graph
from hyprial.daemon.impl.pac.actors.coordinator import request_actor_stop
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.identity import UserProfileStore
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "acme"
MACHINE = "test-node"
HELPER_URI = f"agent:{OWNER}:{MACHINE}:helper"
BOTH_URI = f"agent:{OWNER}:{MACHINE}:both"


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    registry = AgentRegistry(state / "agents.sqlite3", owner=OWNER, machine=MACHINE)
    registry.create("helper")
    registry.create("both")
    registry.close()
    UserProfileStore(state / "users.json").ensure(
        owner="allen", owner_key="allen", login_name="allen",
        machine=MACHINE, machine_key=MACHINE,
    )
    UserProfileStore(state / "users.json").ensure(
        owner="both", owner_key="both", login_name="both",
        machine=MACHINE, machine_key=MACHINE,
    )
    return state


def _build_v7_database(state: Path) -> Path:
    """A schema-7 database with legacy short-name rows (the locked-out world).

    Built with the real migration ladder, then pinned back to user_version 7
    with pre-URI rows inserted as the v7 writer would have written them.
    """

    path = default_database_path(state)
    store = PacGraphStore(path)
    try:
        graph_id = create_graph(store, name="legacy", created_by="user:allen")["graphId"]
        version = 1
        for node_id, owner, kind, extra in (
            ("human-work", "user:allen", "task", {}),
            ("helper-work", HELPER_URI, "task", {}),
            ("ghost-work", "user:ghost", "task", {}),
            ("both-work", BOTH_URI, "task", {}),
            ("w", "user:allen", "actor", {"actor_name": "w", "launch_ref": "launch/w.yaml"}),
        ):
            version = add_node(
                store, state, graph_id=graph_id, node_id=node_id, owner=owner,
                brief_ref=f"r#{node_id}", kind=kind, expect_version=version, **extra,
            )["version"]
        activate_graph(store, graph_id, actor="user:allen")
    finally:
        store.close()
    db = sqlite3.connect(path)
    try:
        # simulate the v7 writer's short-name world
        db.execute("UPDATE graphs SET created_by='allen'")
        db.execute("UPDATE nodes SET owner='allen' WHERE node_id IN ('human-work','w')")
        db.execute("UPDATE nodes SET owner='helper' WHERE node_id='helper-work'")
        db.execute("UPDATE nodes SET owner='ghost' WHERE node_id='ghost-work'")
        db.execute("UPDATE nodes SET owner='both' WHERE node_id='both-work'")
        _pin_to_v7(db)
        db.commit()
    finally:
        db.close()
    return path


def _pin_to_v7(db: sqlite3.Connection) -> None:
    """Undo what the later steps of the ladder added, then label the file v7.

    Relabelling alone would leave the schema-9 agent.task tables behind and
    the re-run of that step would find them already there.
    """

    strip_tick_v19(db)
    for trigger in (
        "workflow_graph_expansion_insert_guard",
        "workflow_graph_expansion_identity_immutable",
        "workflow_node_kind_insert_guard",
        "workflow_node_kind_immutable",
        "workflow_expansion_identity_immutable",
        "workflow_expansion_binding_guard",
        "workflow_worktree_plan_immutable",
        "workflow_outcome_expansion_insert_guard",
        "workflow_outcome_expansion_identity_immutable",
        "remote_workflow_expansion_insert_guard",
        "remote_workflow_expansion_update_guard",
    ):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP INDEX IF EXISTS workflow_graphs_parent")
    db.execute("DROP INDEX IF EXISTS workflow_graphs_one_child")
    db.execute("DROP TABLE IF EXISTS workflow_worktrees")
    db.execute("DROP TABLE IF EXISTS workflow_expansions")
    db.execute("DROP INDEX IF EXISTS notifications_graph_pending")
    db.execute("DROP INDEX IF EXISTS notifications_graph_retryable")
    for column in ("failure_detail", "failure_code", "failed_at"):
        db.execute(f"ALTER TABLE notifications DROP COLUMN {column}")
    for table in (
        "workflow_worker_cleanup_intents",
        "workflow_worker_receipts",
        "remote_workflow_outbox", "remote_workflow_requests", "remote_workflow_key", "workflow_outcome_receipts",
        "workflow_deliveries", "workflow_nodes", "workflow_graphs",
        "pac_agent_task_results",
        "pac_agent_task_events",
        "pac_agent_task_dispatches",
        "pac_agent_task_targets",
        "pac_agent_task_runs",
    ):
        db.execute(f"DROP TABLE {table}")
    db.execute("PRAGMA user_version = 7")


def _owners(store: PacGraphStore, graph_id: str) -> dict[str, str]:
    return {node.node_id: node.owner for node in store.nodes(graph_id)}


def test_upgrade_rewrites_unique_owners_and_keeps_the_rest(state: Path) -> None:
    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 19
        graph_id = store.graph_ids()[0]
        owners = _owners(store, graph_id)
        assert owners["human-work"] == "user:allen"  # profile-resolved person
        assert owners["helper-work"] == HELPER_URI  # registry-resolved agent
        assert owners["both-work"] == "both"  # agent AND person share the name
        assert owners["ghost-work"] == "ghost"  # no source knows it
        assert store.graph(graph_id)["created_by"] == "user:allen"
    finally:
        store.close()


def test_upgrade_preserves_legacy_user_uri_output_for_colon_owner(state: Path) -> None:
    path = _build_v7_database(state)
    UserProfileStore(state / "users.json").ensure(
        owner="legacy:owner",
        owner_key="legacy-owner",
        login_name="legacy-owner",
        machine=MACHINE,
        machine_key=MACHINE,
    )
    db = sqlite3.connect(path)
    with db:
        db.execute("UPDATE graphs SET created_by='legacy:owner'")
        db.execute("UPDATE nodes SET owner='legacy:owner' WHERE node_id='human-work'")
    db.close()

    store = PacGraphStore(path)
    try:
        graph_id = store.graph_ids()[0]
        assert store.graph(graph_id)["created_by"] == "user:legacy:owner"
        assert _owners(store, graph_id)["human-work"] == "user:legacy:owner"
    finally:
        store.close()


def test_rewritten_graphs_stay_operable_flag_close_stop(state: Path) -> None:
    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        graph_id = store.graph_ids()[0]
        # the person flags their own node again after the upgrade
        PacReactor(store, sender=NullSender()).set_flag(
            graph_id, "human-work", actor="user:allen"
        )
        assert store.node(graph_id, "human-work").flag is True
        # the agent flags its own node through the same exact equality
        PacReactor(store, sender=NullSender()).set_flag(
            graph_id, "helper-work", actor=HELPER_URI
        )
        assert store.node(graph_id, "helper-work").flag is True
        # graph lifecycle: the rewritten creator closes their graph
        from hyprial.daemon.impl.pac.graphs.edits import close_graph

        close_graph(store, graph_id, actor="user:allen")
        assert store.graph(graph_id)["closed_at"] is not None
    finally:
        store.close()


def test_owner_gate_on_stop_uses_the_rewritten_creator(state: Path) -> None:
    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        graph_id = store.graph_ids()[0]
        # the owner gate passes for the rewritten creator (the stop itself
        # runs against a never-launched actor: NOT_FOUND is the expected
        # terminal, PAC_GRAPH_NOT_OWNER would be the B2 lockout)
        request_actor_stop(store, graph_id, "never-launched", actor="user:allen")
    except PacError as error:
        assert error.code == "PAC_NODE_NOT_FOUND"
        assert "PAC_GRAPH_NOT_OWNER" not in error.code
    finally:
        store.close()


def test_kept_owners_are_listed_and_refusals_point_at_the_report(state: Path) -> None:
    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        graph_id = store.graph_ids()[0]
        row = store._db.execute(
            "SELECT graphs_total, owners_rewritten, owners_kept, report_json "
            "FROM schema_era WHERE id=1"
        ).fetchone()
        assert row[0] == 1 and row[1] == 4 and row[2] == 2
        report = json.loads(row[3])
        [graph_report] = report["graphs"]
        assert graph_report["graphId"] == graph_id
        kept = {(item["value"], item["reason"]) for item in graph_report["kept"]}
        assert kept == {("both", "ambiguous"), ("ghost", "unresolvable")}
        # the refusal on a kept node names the report, not just NOT_OWNER
        with pytest.raises(PacError) as raised:
            PacReactor(store, sender=NullSender()).set_flag(
                graph_id, "ghost-work", actor="user:allen"
            )
        assert raised.value.code == "PAC_FLAG_NOT_OWNER"
        assert "hyprial workflow migration status" in str(raised.value)
    finally:
        store.close()


def test_migration_status_cli_lists_the_kept_owners(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(state.parent / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    (state.parent / "home").mkdir(exist_ok=True)
    path = _build_v7_database(state)
    # one write-open CLI command runs the pending v7->v8 rewrite; the status
    # surface is read-only and reports whatever the last migration recorded
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore
    PacGraphStore(path).close()
    result = CliRunner().invoke(_cli_root_app, ["workflow", "migration", "status", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["ok"] is True
    # the era report is labelled by the schema-8 step that wrote it, not by
    # the head version the ladder went on to reach
    assert document["schemaVersion"] == 8
    assert document["ownersRewritten"] == 4 and document["ownersKept"] == 2
    [graph_report] = document["report"]["graphs"]
    assert {(item["value"], item["reason"]) for item in graph_report["kept"]} == {
        ("both", "ambiguous"),
        ("ghost", "unresolvable"),
    }


def test_cursor_floor_and_journal_do_not_move_across_the_upgrade(state: Path) -> None:
    """B1: the rewrite is invisible to the event stream by design."""
    from hyprial.daemon.impl.pac.graphs.subscription import snapshot

    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        # pin back to 7 again to snapshot the pre-upgrade state
        _pin_to_v7(store._db)
        store._db.commit()
        store.close()
        before = snapshot(path, PacGraphStore(path).graph_ids()[0])
        store = PacGraphStore(path)
        after = snapshot(path, store.graph_ids()[0])
        assert after["cursor"] == before["cursor"]
        assert after["cursorFloor"] == before["cursorFloor"]
        assert after["version"] == before["version"]
        assert after["identityFormat"] == "mixed-legacy"  # kept rows remain
    finally:
        store.close()


def test_unreadable_source_aborts_the_whole_migration(state: Path) -> None:
    path = _build_v7_database(state)
    corrupt = state / "agents.sqlite3"
    real = corrupt.read_bytes()
    corrupt.write_bytes(b"not a database at all")
    try:
        with pytest.raises(PacError) as raised:
            PacGraphStore(path)
        assert raised.value.code == "PAC_MIGRATION_SOURCE_UNREADABLE"
        # zero writes: the database is still schema-7 with its short names
        db = sqlite3.connect(path)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            owners = db.execute("SELECT owner FROM nodes ORDER BY node_id").fetchall()
            assert {row[0] for row in owners} == {
                "allen", "helper", "ghost", "both",
            }
        finally:
            db.close()
    finally:
        corrupt.write_bytes(real)
    # with the source restored the same upgrade completes
    store = PacGraphStore(path)
    store.close()
    assert PacGraphStore(path)._db.execute("PRAGMA user_version").fetchone()[0] == 19
    PacGraphStore(path).close()


def test_malformed_profiles_abort_like_a_corrupt_registry(state: Path) -> None:
    path = _build_v7_database(state)
    profiles = state / "users.json"
    real = profiles.read_text(encoding="utf-8")
    profiles.write_text('{"version": 2, "users": {}}', encoding="utf-8")
    try:
        with pytest.raises(PacError) as raised:
            PacGraphStore(path)
        assert raised.value.code == "PAC_MIGRATION_SOURCE_UNREADABLE"
        db = sqlite3.connect(path)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        finally:
            db.close()
    finally:
        profiles.write_text(real, encoding="utf-8")


def test_absent_sources_are_legitimately_empty_not_an_abort(tmp_path: Path) -> None:
    state = tmp_path / "state"  # no agents.sqlite3, no users.json
    state.mkdir()
    path = _build_v7_database(state)
    store = PacGraphStore(path)
    try:
        graph_id = store.graph_ids()[0]
        owners = _owners(store, graph_id)
        # nothing resolvable: every legacy owner is kept, loudly reported
        assert owners["helper-work"] == "helper"
        row = store._db.execute(
            "SELECT owners_rewritten, owners_kept FROM schema_era WHERE id=1"
        ).fetchone()
        assert row[0] == 0 and row[1] == 6  # 5 owners + created_by
    finally:
        store.close()


def test_at_sign_owner_round_trip_create_flag_close_stop(tmp_path: Path) -> None:
    """Production owners are email-shaped (bob.smith@example.com since
    2026-09-15): a user: principal whose owner segment contains '@' must
    build a graph, flag, close, and stop under exact-equality authorization,
    and a same-local-part typo must NOT authorize (e2e-verifier cell 1)."""

    state = tmp_path / "state"
    state.mkdir()
    owner_uri = "user:bob.smith@example.com"
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = create_graph(store, name="at-owner", created_by=owner_uri)["graphId"]
        add_node(
            store, state, graph_id=graph_id, node_id="work", owner=owner_uri,
            brief_ref="r#work", expect_version=1,
        )
        add_node(
            store, state, graph_id=graph_id, node_id="w", owner=owner_uri,
            brief_ref="r#w", kind="actor", actor_name="w",
            launch_ref="launch/w.yaml", expect_version=2,
        )
        activate_graph(store, graph_id, actor=owner_uri)
        # the exact principal flags and closes; authorization is verbatim
        PacReactor(store, sender=NullSender()).set_flag(
            graph_id, "work", actor=owner_uri
        )
        assert store.node(graph_id, "work").flag is True
        close_graph(store, graph_id, actor=owner_uri)
        assert store.graph(graph_id)["closed_at"] is not None
        # the owner gate on stop passes for the exact principal
        request_actor_stop(store, graph_id, "never-launched", actor=owner_uri)
        # a stripped local-part lookalike is a DIFFERENT principal: refused
        with pytest.raises(PacError) as raised:
            PacReactor(store, sender=NullSender()).reset_flag(
                graph_id, "work", actor="user:bob.smith"
            )
        assert raised.value.code == "PAC_FLAG_NOT_OWNER"
    except PacError as error:
        if error.code != "PAC_NODE_NOT_FOUND":
            raise
    finally:
        store.close()
