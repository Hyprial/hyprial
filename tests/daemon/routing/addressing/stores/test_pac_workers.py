"""UUID addressing P4, PAC store: workflow worker owners stay verbatim (HQ Q3).

Workflow workers are registered agents, so the daemon mapping does answer
their name-form URI.  The PAC migration still keeps a worker-owned node's
owner, because the immutable worker receipt (and the roster check that joins
it to the node owner) holds that exact string until the PAC graph schema
upgrade decides wf-* addressing.  A borrowed node owner is an authorization
field like any other and is rewritten; the digest-bound roster snapshot is
not.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hyprial.daemon.impl.pac.storage.agent_uris import migrate_pac_agent_uris
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.kernel import is_agent_id_segment, parse_agent_uri

USER = "user:test"
BORROWED = "agent:test:machine:helper"
WORKFLOW = f"""
version: 2
name: roster
nodes:
  - id: a
    task: first
  - id: borrowed
    owner: {BORROWED}
    task: borrowed
    after: [a]
"""


class _Sender:
    def send(self, **kwargs):
        return kwargs["idempotency_key"]


def _official(value: str) -> str | None:
    parsed = parse_agent_uri(value)
    if parsed is None or parsed[:2] != ("test", "machine"):
        return None
    if is_agent_id_segment(parsed[2]):
        return None
    letters = "".join(ch for ch in parsed[2] if ch.isalpha()).lower()
    return f"agent:test:machine:a.{(letters * 26)[:26]}"


def _rows(path: Path, query: str) -> list[tuple[object, ...]]:
    db = sqlite3.connect(path)
    try:
        return [tuple(row) for row in db.execute(query)]
    finally:
        db.close()


def test_worker_owned_nodes_keep_their_owner_and_the_roster_still_validates(
    tmp_path: Path,
) -> None:
    service = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=_Sender(),
        clock_ms=lambda: 1000,
        start_thread=False,
    )
    try:
        graph_id = service.start(
            yaml_text=WORKFLOW, sender=USER, operation_key="roster"
        )["graphId"]
    finally:
        assert service.close()
    database = service.database
    store = PacGraphStore(database)
    try:
        (receipt,) = store.workflow_worker_receipts(graph_id)
        worker_uri = str(receipt["actor_uri"])
        roster_before = store.workflow_roster(graph_id)
    finally:
        store.close()
    db = sqlite3.connect(database)
    with db:
        db.execute(
            "INSERT INTO workflow_worker_cleanup_intents(graph_id, actor_node,"
            " actor_name, actor_uri, operation_id, state, created_at, updated_at)"
            " SELECT graph_id, actor_node, actor_name, actor_uri, 'op-1', 'pending',"
            " 1, 1 FROM workflow_worker_receipts"
        )
    db.close()
    assert _official(worker_uri) is not None
    owners_before = dict(_rows(database, "SELECT node_id, owner FROM nodes"))
    assert owners_before["a"] == worker_uri

    assert migrate_pac_agent_uris(database, _official, at_ms=5) == 1

    owners = dict(_rows(database, "SELECT node_id, owner FROM nodes"))
    assert owners["a"] == worker_uri
    assert owners["borrowed"] == _official(BORROWED)
    assert {k: v for k, v in owners.items() if k != "borrowed"} == {
        k: v for k, v in owners_before.items() if k != "borrowed"
    }
    store = PacGraphStore(database)
    try:
        assert store.workflow_roster(graph_id) == roster_before
    finally:
        store.close()
