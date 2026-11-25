"""UUID addressing P4, external stores: each store migrates once, per row.

Every store function takes the shared mapping (stored name-form local agent
URI to official URI, None to keep) and rewrites only the binding fields it
owns, in one transaction with a durable marker, so a second run is a no-op.
Message rows in the same databases stay verbatim (ruling X).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hyprial.biz import migrate_routine_agent_uris
from hyprial.biz.impl.routine.storage.store import (
    InFlightRow,
    RoutineEffectRow,
    RoutineRow,
    RoutineStore,
)
from hyprial.daemon.impl.adapters.lark.state.agent_uris import (
    migrate_reply_route_agent_uris,
)
from hyprial.daemon.impl.adapters.lark.state.records import ReplyRoute
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
from hyprial.daemon.impl.org.network.state import (
    add_org_acl_grant,
    migrate_org_acl_grant_agents,
    read_org_acl_grants,
)
from hyprial.daemon.impl.pac.storage.agent_uris import migrate_pac_agent_uris
from hyprial.daemon.impl.pac.storage.migrations import SCHEMA_VERSION
from hyprial.daemon.impl.pac.storage.store import connect
from tests.support.pac.migrations import strip_held_v20, strip_tick_v19

NAME_URI = "agent:op:node-1:worker"
ID_URI = "agent:op:node-1:a.abcdefghijklmnopqrstuvwxyz"
OTHER = "agent:op:node-2:worker"
USER = "user:op"
MAPPING = {NAME_URI: ID_URI}.get


def _rows(path: Path, query: str) -> list[tuple[object, ...]]:
    db = sqlite3.connect(path)
    try:
        return [tuple(row) for row in db.execute(query)]
    finally:
        db.close()


def _seed_adapters(path: Path) -> None:
    store = LarkStateStore(path, adapter="lark-a")
    for message_id, actor in (("m-1", NAME_URI), ("m-2", OTHER)):
        store.record_reply(ReplyRoute(message_id, actor, actor, "h", "c", "chat"))
    store._db.execute(
        "INSERT INTO pending_inbound_submissions(adapter, message_id, event_id, request)"
        " VALUES ('lark-a', 'p-1', 'e-1', ?)",
        (json.dumps({"to": [NAME_URI]}),),
    )
    store.close()


def test_reply_routes_rewrite_once_and_keep_custody(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    _seed_adapters(path)

    assert migrate_reply_route_agent_uris(path, MAPPING) == 1
    assert _rows(path, "SELECT message_id, actor_id, actor_key FROM reply_routes ORDER BY message_id") == [
        ("m-1", ID_URI, ID_URI),
        ("m-2", OTHER, OTHER),
    ]
    assert _rows(path, "SELECT request FROM pending_inbound_submissions") == [
        (json.dumps({"to": [NAME_URI]}),)
    ]
    store = LarkStateStore(path, adapter="lark-a")
    try:
        route = store.reply("m-1")
    finally:
        store.close()
    assert route is not None and route.actor_key == ID_URI

    store = LarkStateStore(path, adapter="lark-a")
    store.record_reply(ReplyRoute("m-3", NAME_URI, NAME_URI, "h", "c", "chat"))
    store.close()
    assert migrate_reply_route_agent_uris(path, MAPPING) is None
    assert _rows(path, "SELECT actor_key FROM reply_routes WHERE message_id = 'm-3'") == [
        (NAME_URI,)
    ]


def test_reply_route_migration_leaves_a_missing_database_uncreated(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    assert migrate_reply_route_agent_uris(path, MAPPING) is None
    assert not path.exists()


def _seed_pac(path: Path) -> None:
    db = connect(path)
    with db:
        db.execute(
            "INSERT INTO graphs(graph_id, name, version, created_by, created_at)"
            " VALUES ('g-1', 'g', 1, ?, 1)",
            (NAME_URI,),
        )
        for node_id, owner in (("n-1", NAME_URI), ("n-2", USER), ("n-3", OTHER),
                               ("n-4", "agent:op:node-1:wf-1-worker")):
            db.execute(
                "INSERT INTO nodes(graph_id, node_id, owner, brief_ref, flag_set_by)"
                " VALUES ('g-1', ?, ?, 'brief', ?)",
                (node_id, owner, owner),
            )
        db.execute(
            "INSERT INTO nodes(graph_id, node_id, owner, brief_ref, kind, actor_name, launch_ref)"
            " VALUES ('g-1', 'actor-1', ?, 'brief', 'actor', 'wf-1-worker', 'launch#sha256=x')",
            (USER,),
        )
        db.execute(
            "INSERT INTO notifications(event_id, edge, kind, recipient, text, sender, at)"
            " VALUES ('ev-1', 'n-1', 'turn', ?, 'go', ?, 1)",
            (NAME_URI, NAME_URI),
        )
    db.close()


def test_pac_rewrites_authorization_owners_once_and_keeps_history(tmp_path: Path) -> None:
    path = tmp_path / "pac-graph.sqlite3"
    _seed_pac(path)
    journal = _rows(path, "SELECT COUNT(*) FROM journal")

    assert migrate_pac_agent_uris(path, MAPPING, at_ms=5) == 2
    assert _rows(path, "SELECT created_by FROM graphs") == [(ID_URI,)]
    assert dict(_rows(path, "SELECT node_id, owner FROM nodes")) == {
        "n-1": ID_URI,
        "n-2": USER,
        "n-3": OTHER,
        "n-4": "agent:op:node-1:wf-1-worker",
        "actor-1": USER,
    }
    assert _rows(path, "SELECT flag_set_by FROM nodes WHERE node_id = 'n-1'") == [(NAME_URI,)]
    assert _rows(path, "SELECT recipient, sender FROM notifications") == [(NAME_URI, NAME_URI)]
    assert _rows(path, "SELECT COUNT(*) FROM journal") == journal
    (marker,) = _rows(path, "SELECT migrated_at_ms, rewritten FROM agent_uri_migration")
    assert marker == (5, 2)

    assert migrate_pac_agent_uris(path, {USER: ID_URI}.get, at_ms=6) is None
    assert _rows(path, "SELECT owner FROM nodes WHERE node_id = 'n-2'") == [(USER,)]


def test_pac_migration_upgrades_an_older_database_and_migrates_it_in_the_same_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pac-graph.sqlite3"
    _seed_pac(path)
    db = sqlite3.connect(path)
    with db:
        # A true v18 database predates both the tick projection (v19) and the
        # held column (v20); stripping only the tick objects leaves a schema
        # that the v19→v20 upgrade cannot rebuild.
        strip_held_v20(db)
        strip_tick_v19(db)
    db.close()

    assert migrate_pac_agent_uris(path, MAPPING, at_ms=1) == 2
    assert _rows(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert _rows(path, "SELECT owner FROM nodes WHERE node_id = 'n-1'") == [(ID_URI,)]
    assert _rows(path, "SELECT rewritten FROM agent_uri_migration") == [(2,)]


def test_pac_migration_leaves_a_missing_database_uncreated(tmp_path: Path) -> None:
    path = tmp_path / "pac-graph.sqlite3"
    assert migrate_pac_agent_uris(path, MAPPING, at_ms=1) is None
    assert not path.exists()


def test_org_acl_grants_rekey_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    add_org_acl_grant(home, "group-acme", NAME_URI, granted_at_ms=10)
    add_org_acl_grant(home, "group-acme", OTHER, granted_at_ms=11)
    add_org_acl_grant(home, "group-beta", NAME_URI, granted_at_ms=12)
    add_org_acl_grant(home, "group-beta", ID_URI, granted_at_ms=13)

    assert migrate_org_acl_grant_agents(home, MAPPING, at_ms=20) == 2
    grants = read_org_acl_grants(home)
    assert set(grants["group-acme"]) == {ID_URI, OTHER}
    assert grants["group-acme"][ID_URI]["grantedAtMs"] == 10
    assert grants["group-beta"] == {ID_URI: {"grantedAtMs": 13, "untilMs": None}}

    add_org_acl_grant(home, "group-acme", NAME_URI, granted_at_ms=30)
    assert migrate_org_acl_grant_agents(home, MAPPING, at_ms=40) is None
    assert NAME_URI in read_org_acl_grants(home)["group-acme"]


def test_org_acl_migration_without_grants_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert migrate_org_acl_grant_agents(home, MAPPING, at_ms=1) is None
    assert not (home / "state").exists()


SPEC = f"""
version: 2
mode: scheduled
name: duty
schedule:
  interval: 15m
actor: "{NAME_URI}"
role: dispatch
task: work
policy:
  routes:
    - {{tag: "route:human", escalate_to: "{NAME_URI}"}}
    - {{tag: "route:user", escalate_to: "{USER}"}}
on_task_timeout:
  action: escalate
  escalate_to: "{OTHER}"
"""


PRODUCES_SPEC = f"""
version: 2
mode: scheduled
name: lead
schedule:
  interval: 15m
produces: "{NAME_URI}"
role: dispatch
task: work
on_task_timeout:
  action: escalate
  escalate_to: "{NAME_URI}"
"""


def _seed_routines(path: Path) -> None:
    store = RoutineStore(path)
    store.apply(
        routine=RoutineRow("lead", PRODUCES_SPEC, USER, True, 10**12, 0, "[]", 0, 1),
    )
    store.apply(
        routine=RoutineRow("duty", SPEC, NAME_URI, True, 10**12, 0, "[]", 0, 1),
        put_in_flight=(InFlightRow("duty", "t-1", "r-1", NAME_URI, 1),),
        add_effects=(
            RoutineEffectRow("fx-1", "c-1", "duty", 1, 1, {"kind": "RoutineAlarmEffect", "to": NAME_URI}),
        ),
    )
    store.close()


def test_routine_store_rewrites_actor_owner_and_escalations_once(tmp_path: Path) -> None:
    path = tmp_path / "routines.sqlite3"
    _seed_routines(path)

    assert migrate_routine_agent_uris(path, MAPPING, at_ms=7) == 5
    store = RoutineStore(path)
    try:
        row = store.get_routine("duty")
        lead = store.get_routine("lead")
        ledger = {
            (m.routine, m.field, m.before, m.after) for m in store.address_migrations()
        }
        in_flight = store.in_flight(routine="duty")
    finally:
        store.close()
    assert row is not None and row.owner == ID_URI
    assert f"actor: {ID_URI}" in row.yaml_text
    assert USER in row.yaml_text and OTHER in row.yaml_text
    assert NAME_URI not in row.yaml_text
    assert lead is not None and lead.owner == USER
    assert f"produces: {ID_URI}" in lead.yaml_text
    assert f"escalate_to: {ID_URI}" in lead.yaml_text
    assert NAME_URI not in lead.yaml_text
    assert ledger == {
        ("duty", "owner", NAME_URI, ID_URI),
        ("duty", "actor", NAME_URI, ID_URI),
        ("duty", "policy.routes[0].escalate_to", NAME_URI, ID_URI),
        ("lead", "produces", NAME_URI, ID_URI),
        ("lead", "on_task_timeout.escalate_to", NAME_URI, ID_URI),
    }
    assert [item.target for item in in_flight] == [NAME_URI]
    ((payload,),) = _rows(path, "SELECT payload_json FROM routine_effects")
    assert json.loads(str(payload))["to"] == NAME_URI

    assert migrate_routine_agent_uris(path, {USER: ID_URI}.get, at_ms=8) is None
    assert len(_rows(path, "SELECT * FROM routine_address_migrations")) == 5


def test_routine_migration_leaves_a_missing_database_uncreated(tmp_path: Path) -> None:
    path = tmp_path / "routines.sqlite3"
    assert migrate_routine_agent_uris(path, MAPPING, at_ms=1) is None
    assert not path.exists()
