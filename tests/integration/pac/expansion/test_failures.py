"""Invalid expansion input never consumes the current planner request."""

import pytest

from hyprial.daemon.impl.pac.graphs.authority.expansion import prepare_expansion
from hyprial.identity import PAC_EXPANSION_INVALID, PacError

from .support import PLANNER, parent_graph


def test_invalid_child_leaves_parent_requested_without_publication(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(tmp_path)
    database = store.path
    store.close()

    with pytest.raises(PacError) as caught:
        prepare_expansion(
            database,
            home=home,
            raw="version: 2\nnodes: [{id: nested, task: no, kind: expansion}]\n",
            graph_id=graph_id,
            node_id="planner",
            request_id="request-1",
            actor=PLANNER,
            at=200,
            machine="machine",
            local_owner="test",
        )

    assert caught.value.code == PAC_EXPANSION_INVALID
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    read = PacGraphStore(database, read_only=True)
    try:
        row = read._db.execute(
            "SELECT state,request_id FROM workflow_nodes WHERE graph_id=? AND node_id='planner'",
            (graph_id,),
        ).fetchone()
        assert tuple(row) == ("requested", "request-1")
        for table in ("workflow_expansions", "workflow_worktrees", "workflow_outcome_receipts"):
            assert read._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        read.close()
