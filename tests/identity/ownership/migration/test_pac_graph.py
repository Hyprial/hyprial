"""owner_migration: pac graph scenarios."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from hyprial.daemon.impl.owner_migration import (
    OwnerMigrationAborted,
    migrate_owner,
    rewrite_value,
)
from hyprial.daemon.impl.owner_migration.vocabulary import ARCHIVAL_COLUMNS
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from tests.daemon.pac.expansion.engine.support import strip_v18

from .support import (
    FORMER_SELF,
    NEW,
    OLD,
    _pac_value,
    _seed_agents,
    _seed_identities,
    _seed_pac_graph,
    _state,
)


def test_v18_expansion_columns_have_exact_archival_classification(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    store = PacGraphStore(state / "pac-graph.sqlite3")
    store.close()
    columns = set(ARCHIVAL_COLUMNS)
    expected = {
        ("workflow_graphs", "parent_graph_id"),
        ("workflow_graphs", "parent_node_id"),
        ("workflow_nodes", "node_kind"),
        *(("workflow_expansions", column) for column in (
            "parent_graph_id", "parent_node_id", "child_graph_id",
            "planner_node_id", "planner_request_id", "expansion_digest",
            "limits_json",
        )),
        *(("workflow_outcome_receipts", column) for column in (
            "graph_id", "node_id", "expansion_digest",
        )),
        ("remote_workflow_outbox", "expansion_text"),
        ("remote_workflow_outbox", "expansion_digest"),
        *(("workflow_worktrees", column) for column in (
            "graph_id", "node_id", "actor_node", "plan_json", "state", "reason",
        )),
    }
    assert expected <= columns
    assert ("workflow_outcome_receipts", "result_json") not in columns
    assert ("remote_workflow_outbox", "result_json") not in columns


F5_ARCHIVAL_COLUMNS = tuple(
    item
    for item in ARCHIVAL_COLUMNS
    if item[0]
    in {
        "workflow_graphs",
        "workflow_nodes",
        "workflow_expansions",
        "workflow_outcome_receipts",
        "remote_workflow_outbox",
        "workflow_worktrees",
    }
    and item
    not in {
        ("workflow_graphs", "specification_ref"),
        ("workflow_graphs", "specification_digest"),
        ("workflow_nodes", "input_token"),
        ("workflow_nodes", "request_id"),
    }
)


def _seed_v17_then_v18_expansion_rows(state: Path) -> Path:
    """Keep real v17 rows through ALTER, then add valid v18 custody rows."""

    path = state / "pac-graph.sqlite3"
    PacGraphStore(path).close()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    strip_v18(db)
    db.execute(
        "INSERT INTO graphs(graph_id,name,version,created_by,created_at) "
        "VALUES ('v17-parent','v17',1,?,1)",
        (f"user:{OLD}",),
    )
    db.execute(
        "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind) "
        "VALUES ('v17-parent','v17-node',?,'v17','task')",
        (f"agent:{OLD}:machine:worker",),
    )
    db.execute(
        "INSERT INTO workflow_graphs"
        "(graph_id,specification_ref,specification_digest,on_failure,state) "
        "VALUES ('v17-parent','v17-ref','v17-digest','terminate','running')"
    )
    db.execute(
        "INSERT INTO workflow_nodes(graph_id,node_id,state,deadline_ms,timeout_ms) "
        "VALUES ('v17-parent','v17-node','requested',10000,5000)"
    )
    db.execute(
        "INSERT INTO workflow_outcome_receipts"
        "(request_id,actor,action,reason_ref,result_json,output_text) "
        "VALUES ('v17-receipt',?,'complete','evidence:v17','{}',NULL)",
        (f"user:{OLD}",),
    )
    db.execute(
        "INSERT INTO remote_workflow_requests"
        "(request_id,graph_id,node_id,owner,origin,message_id,deadline_ms,grant_json) "
        "VALUES ('v17-remote','v17-parent','v17-node',?,?,'v17-message',10000,'{}')",
        (f"agent:{OLD}:machine:worker", f"agent:{OLD}:origin:dispatch"),
    )
    db.execute(
        "INSERT INTO remote_workflow_outbox"
        "(request_id,action,reason_ref,result_json,attempted_at,output_text) "
        "VALUES ('v17-remote','complete','evidence:v17',NULL,0,NULL)"
    )
    db.commit()
    db.close()

    migrated = PacGraphStore(path)
    try:
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 19
        assert tuple(
            migrated._db.execute(
                "SELECT specification_ref,specification_digest,parent_graph_id,"
                "parent_node_id,expansion_deadline_ms FROM workflow_graphs "
                "WHERE graph_id='v17-parent'"
            ).fetchone()
        ) == ("v17-ref", "v17-digest", None, None, None)
        assert migrated._db.execute(
            "SELECT node_kind FROM workflow_nodes "
            "WHERE graph_id='v17-parent' AND node_id='v17-node'"
        ).fetchone()[0] == "task"
        assert tuple(
            migrated._db.execute(
                "SELECT graph_id,node_id,expansion_digest "
                "FROM workflow_outcome_receipts WHERE request_id='v17-receipt'"
            ).fetchone()
        ) == (None, None, None)
        assert tuple(
            migrated._db.execute(
                "SELECT expansion_text,expansion_digest,attempt_no "
                "FROM remote_workflow_outbox WHERE request_id='v17-remote'"
            ).fetchone()
        ) == (None, None, 0)

        parent = "wf-expansion-parent"
        child = "wf-expansion-child"
        slot = "slot"
        worker = "work"
        actor_node = f"_actor.{worker}"
        for graph_id in (parent, child):
            migrated._db.execute(
                "INSERT INTO graphs(graph_id,name,version,created_by,created_at) "
                "VALUES (?,?,1,?,1)",
                (graph_id, graph_id, f"user:{OLD}"),
            )
        for node_id, owner, kind in (
            (slot, f"agent:{OLD}:machine:pac-expansion", "task"),
        ):
            migrated._db.execute(
                "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind) "
                "VALUES (?,?,?,?,?)",
                (parent, node_id, owner, f"workflow:{parent}#{node_id}", kind),
            )
        migrated._db.execute(
            "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind) "
            "VALUES (?,?,?,?, 'task')",
            (
                child,
                worker,
                f"agent:{OLD}:machine:worker",
                f"workflow:{child}#{worker}",
            ),
        )
        migrated._db.execute(
            "INSERT INTO nodes"
            "(graph_id,node_id,owner,brief_ref,kind,actor_name,launch_ref) "
            "VALUES (?,?,?,?, 'actor',?,?)",
            (
                child,
                actor_node,
                f"agent:{OLD}:machine:worker",
                f"workflow:{child}#{actor_node}",
                actor_node,
                "artifact:worker",
            ),
        )
        migrated._db.execute(
            "INSERT INTO workflow_graphs"
            "(graph_id,specification_ref,specification_digest,on_failure,state) "
            "VALUES (?,?,?,'terminate','running')",
            (parent, "parent-ref", "parent-digest"),
        )
        migrated._db.execute(
            "INSERT INTO workflow_nodes"
            "(graph_id,node_id,state,deadline_ms,timeout_ms,node_kind) "
            "VALUES (?,?,'requested',10000,5000,'expansion')",
            (parent, slot),
        )
        migrated._db.execute(
            "INSERT INTO workflow_graphs"
            "(graph_id,specification_ref,specification_digest,on_failure,state,"
            "parent_graph_id,parent_node_id,expansion_deadline_ms) "
            "VALUES (?,?,?,'terminate','running',?,?,10000)",
            (child, "child-ref", "child-digest", parent, slot),
        )
        migrated._db.execute(
            "INSERT INTO workflow_nodes"
            "(graph_id,node_id,actor_node,state,deadline_ms,timeout_ms,node_kind) "
            "VALUES (?,?,?,'pending',10000,5000,'task')",
            (child, worker, actor_node),
        )
        migrated._db.execute(
            "INSERT INTO workflow_expansions"
            "(parent_graph_id,parent_node_id,child_graph_id,planner_node_id,"
            "planner_request_id,generation,expansion_digest,limits_json,accepted_at,deadline_ms) "
            "VALUES (?,?,?,?,?,1,?,?,1,10000)",
            (
                parent,
                slot,
                child,
                f"planner-{OLD}",
                f"request-{OLD}",
                f"digest-{OLD}",
                json.dumps({"owner": OLD}),
            ),
        )
        migrated._db.execute(
            "INSERT INTO workflow_outcome_receipts"
            "(request_id,actor,action,reason_ref,result_json,output_text,"
            "graph_id,node_id,expansion_digest) "
            "VALUES (?,?, 'complete','evidence:v18','{}',NULL,?,?,?)",
            ("receipt-v18", f"user:{OLD}", child, worker, f"digest-{OLD}"),
        )
        migrated._db.execute(
            "INSERT INTO remote_workflow_requests"
            "(request_id,graph_id,node_id,owner,origin,message_id,deadline_ms,grant_json) "
            "VALUES (?,?,?,?,?,?,10000,'{}')",
            (
                "remote-v18",
                child,
                worker,
                f"agent:{OLD}:machine:worker",
                f"agent:{OLD}:origin:dispatch",
                "message-v18",
            ),
        )
        migrated._db.execute(
            "INSERT INTO remote_workflow_outbox"
            "(request_id,action,reason_ref,result_json,attempted_at,output_text,"
            "expansion_text,expansion_digest,attempt_no) "
            "VALUES (?,'complete','evidence:v18',NULL,0,NULL,?,?,0)",
            ("remote-v18", f"expansion authored by {OLD}", f"digest-{OLD}"),
        )
        plan = json.dumps(
            {
                "path": f"/tmp/{OLD}/worktree",
                "operationId": f"pac-worktree:{child}:{worker}",
            }
        )
        migrated._db.execute(
            "INSERT INTO workflow_worktrees"
            "(graph_id,node_id,actor_node,plan_json,state,reason,updated_at) "
            "VALUES (?,?,?,?, 'planned',?,1)",
            (child, worker, actor_node, plan, f"retained-for-{OLD}"),
        )
        migrated._db.commit()
    finally:
        migrated.close()
    return path


def _archival_snapshot(path: Path) -> dict[tuple[str, str], tuple[object, ...]]:
    db = sqlite3.connect(path)
    try:
        return {
            (table, column): tuple(
                row[0]
                for row in db.execute(
                    f'SELECT "{column}" FROM "{table}" ORDER BY rowid'
                )
            )
            for table, column in F5_ARCHIVAL_COLUMNS
        }
    finally:
        db.close()


def test_v18_owner_preview_apply_preserves_f5_archive_and_rewrites_live_columns(
    tmp_path: Path,
) -> None:
    from hyprial.shell.impl.login.preview import preview_owner_migration

    state = _state(tmp_path)
    _seed_agents(state)
    path = _seed_v17_then_v18_expansion_rows(state)
    before = _archival_snapshot(path)

    preview = preview_owner_migration(
        state_dir=state, hyprial_home=tmp_path, target_owner=NEW
    )
    assert preview.ready
    assert preview.unclassified == ()
    assert all(
        any(f"{table}.{column}" in item for item in preview.skipped_archival_columns)
        for table, column in F5_ARCHIVAL_COLUMNS
    )
    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert _archival_snapshot(path) == before
    db = sqlite3.connect(path)
    try:
        assert not any(
            OLD in str(value)
            for value in db.execute("SELECT created_by FROM graphs").fetchall()
        )
        assert not any(
            OLD in str(value)
            for value in db.execute("SELECT owner FROM nodes").fetchall()
        )
        assert not any(
            OLD in str(value)
            for value in db.execute(
                "SELECT actor FROM workflow_outcome_receipts"
            ).fetchall()
        )
        assert not any(
            OLD in str(value)
            for value in db.execute(
                "SELECT owner,origin FROM remote_workflow_requests"
            ).fetchall()
        )
    finally:
        db.close()


def test_v18_owner_migration_fails_closed_on_live_principal_mismatch(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    path = _seed_v17_then_v18_expansion_rows(state)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO workflow_outcome_receipts"
        "(request_id,actor,action,reason_ref,result_json,output_text) "
        "VALUES ('mismatch','user:bobadminmismatch','complete','evidence:mismatch','{}',NULL)"
    )
    db.commit()
    db.close()
    before = path.read_bytes()

    with pytest.raises(OwnerMigrationAborted, match="workflow_outcome_receipts.actor"):
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "group",
    [
        "graphs_authorization",
        "nodes_owner",
        "nodes_flag_set_by",
        "flag_events_actor",
        "journal_records_archival",
        "notifications_routing",
    ],
)
def test_pac_graph_owner_columns_are_rewritten(group: str, tmp_path: Path) -> None:
    """Every owner-bearing PAC column moves with the rename (P1b B0).

    PAC authorizes by exact principal-URI equality; a stale spelling in any
    of these columns locks the graph out with no error.  One node per column
    group so a coverage regression reds exactly the dropped group.
    """

    state = _state(tmp_path)
    _seed_pac_graph(state)
    applied = migrate_owner(
        state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW
    )
    assert applied >= 1

    if group == "graphs_authorization":
        created = _pac_value(state, "SELECT created_by FROM graphs")
        activated = _pac_value(state, "SELECT activated_by FROM graphs")
        closed = _pac_value(state, "SELECT closed_by FROM graphs")
        assert created == f"user:{NEW}"
        assert activated == f"agent:{NEW}:node:worker"  # machine/actor intact
        assert closed == f"user:{NEW}"
    elif group == "nodes_owner":
        assert _pac_value(state, "SELECT owner FROM nodes") == f"user:{NEW}"
    elif group == "nodes_flag_set_by":
        assert (
            _pac_value(state, "SELECT flag_set_by FROM nodes") == f"user:{NEW}"
        )
    elif group == "flag_events_actor":
        assert (
            _pac_value(state, "SELECT actor FROM flag_events")
            == f"user:{NEW}"
        )
    elif group == "journal_records_archival":
        # The append-only trigger (journal_no_update) refuses any UPDATE, and
        # PAC's v8 design §2.3 already keeps journal rows untouched: the
        # dated actor strings in the log are facts, not addresses.  No
        # authorization surface compares them.
        data = json.loads(_pac_value(state, "SELECT data_json FROM journal"))
        assert data["actor"] == f"agent:{OLD}:node:worker"
        assert data["owner"] == f"user:{OLD}"
    elif group == "notifications_routing":
        assert _pac_value(state, "SELECT recipient FROM notifications") == NEW
        assert (
            _pac_value(state, "SELECT sender FROM notifications")
            == f"user:{NEW}"
        )



def test_pac_graph_record_columns_stay_byte_identical(tmp_path: Path) -> None:
    """Records of what was written/decided are not addresses (archival class).

    ``schema_era.report_json`` is the v8 completion report authorization
    refusals point at; ``notifications.text``/``plan_json`` back the
    byte-for-byte resend contract.  A rename must not falsify them.
    """

    state = _state(tmp_path)
    _seed_pac_graph(state)
    before_text = _pac_value(state, "SELECT text FROM notifications")
    before_plan = _pac_value(state, "SELECT plan_json FROM notifications")
    before_report = _pac_value(state, "SELECT report_json FROM schema_era")

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert _pac_value(state, "SELECT text FROM notifications") == before_text
    assert (
        _pac_value(state, "SELECT plan_json FROM notifications") == before_plan
    )
    assert (
        _pac_value(state, "SELECT report_json FROM schema_era") == before_report
    )
    # The routing columns of the SAME rows did move — archival is per column,
    # not per table.
    assert _pac_value(state, "SELECT sender FROM notifications") == f"user:{NEW}"


def test_pac_graph_full_seed_classifies_clean_in_preview_and_apply(
    tmp_path: Path,
) -> None:
    """The real S2 preview gains pac-graph automatically (same name list)."""

    from hyprial.shell.impl.login.preview import preview_owner_migration

    state = _state(tmp_path)
    _seed_agents(state)
    _seed_pac_graph(state)
    preview = preview_owner_migration(
        state_dir=state, hyprial_home=tmp_path, target_owner=NEW
    )
    assert preview.ready
    assert preview.unclassified == ()
    assert preview.rewritten_cells >= 8  # every rewritten sentinel cell
    # And the apply leaves no stale spelling anywhere on the rewritten
    # surface (routing columns included; the archival cells are exempt by
    # design and pinned byte-identical in their own test).
    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)
    for table, column in (
        ("graphs", "created_by"),
        ("graphs", "activated_by"),
        ("graphs", "closed_by"),
        ("nodes", "owner"),
        ("nodes", "flag_set_by"),
        ("flag_events", "actor"),
        ("notifications", "recipient"),
        ("notifications", "sender"),
    ):
        value = _pac_value(state, f'SELECT "{column}" FROM "{table}"')
        assert OLD not in str(value), f"stale owner in {table}.{column}"


def test_pac_graph_refuses_to_guess_adjacent_spellings(tmp_path: Path) -> None:
    """Anti-widening: look-alike values are neither rewritten nor skipped.

    A machine segment that merely starts with the old owner stays (the
    ``{old}mac-studio`` residual, classified clean); a user spelling only
    prefixed by the old owner is NOT rewritten — and because it is an
    unaccounted owner-looking value, the migration aborts fail-closed with
    zero writes rather than guess.
    """

    from hyprial.daemon.impl.pac.storage.store import connect as pac_connect

    state = _state(tmp_path)
    _seed_pac_graph(state)
    db = pac_connect(state / "pac-graph.sqlite3")
    with db:
        db.execute(
            "INSERT INTO nodes (graph_id, node_id, owner, brief_ref, kind)"
            " VALUES ('g1', 'resid', ?, 'r', 'task')",
            (f"agent:{OLD}mac-studio.example-tailnet.ts.net:n:a",),
        )
        db.execute(
            "INSERT INTO nodes (graph_id, node_id, owner, brief_ref, kind)"
            " VALUES ('g1', 'trap', ?, 't', 'task')",
            (f"user:{OLD}man",),
        )
    db.close()
    before = (state / "pac-graph.sqlite3").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)
    assert "nodes.owner" in str(aborted.value)
    assert (state / "pac-graph.sqlite3").read_bytes() == before  # zero writes


# -- A member's switch-account (2026-09-26): three product-made residuals ------
#
# devusr-dev switching amyws -> amy.williams@example.com aborted on 99 values
# (vs 21,543 rewritable), all from three sources that are never an owner
# segment: the squire's Lark adapter name (squire setup names it
# `<owner>-squire`, squire/setup.py), a WSL-era Linux home path
# `/home/<owner>/...`, and a manual provenance note `manual:<owner>-confirmed-`.


def test_member_squire_adapter_name_is_kept_and_its_owner_segment_rewritten(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(
        state,
        (f"lark:{OLD}-squire", f"adapter:lark:{OLD}-squire"),
        (f"lark-inbound:adapter:lark:{OLD}-squire:om_1", f"harness:lark:{OLD}-squire"),
    )

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    db = sqlite3.connect(state / "adapters.sqlite3")
    rows = db.execute("SELECT adapter, display_name, actor FROM identities").fetchall()
    db.close()
    assert [(a, d) for a, d, _ in rows] == [
        (f"lark:{OLD}-squire", f"adapter:lark:{OLD}-squire"),
        (f"lark-inbound:adapter:lark:{OLD}-squire:om_1", f"harness:lark:{OLD}-squire"),
    ]
    # the real owner segment in the same rows still moved
    assert all(actor == f"agent:{NEW}:{FORMER_SELF}:hq-adjutant" for _, _, actor in rows)


def test_member_linux_home_path_and_manual_note_are_kept(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(
        state,
        (f'{{"cwd": "/home/{OLD}/projects/dsh-talk"}}',
         f"manual:{OLD}-confirmed-2026-08-16 (self|all)"),
    )

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    db = sqlite3.connect(state / "adapters.sqlite3")
    adapter, display = db.execute("SELECT adapter, display_name FROM identities").fetchone()
    db.close()
    assert adapter == f'{{"cwd": "/home/{OLD}/projects/dsh-talk"}}'
    assert display == f"manual:{OLD}-confirmed-2026-08-16 (self|all)"


@pytest.mark.parametrize(
    "near_miss",
    [
        f"lark:{OLD}-squirrel",
        f'{{"cwd": "/homes/{OLD}/projects"}}',
        f"manual:{OLD}-approved-2026-08-16",
    ],
)
def test_near_misses_of_the_member_residual_rows_still_abort(
    tmp_path: Path, near_miss: str
) -> None:
    """The three member rows are exact forms: one character off still aborts."""
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(state, (near_miss, "unrelated"))
    before = (state / "adapters.sqlite3").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert (state / "adapters.sqlite3").read_bytes() == before
    assert "identities.adapter" in str(aborted.value)


def test_an_owner_segment_named_like_the_squire_is_still_rewritten() -> None:
    """Negative control: the `{old}-squire` residual forgives the name, never
    the owner segment of an address that carries it."""

    assert (
        rewrite_value(f"agent:{OLD}:{FORMER_SELF}:{OLD}-squire", OLD, NEW)
        == f"agent:{NEW}:{FORMER_SELF}:{OLD}-squire"
    )
