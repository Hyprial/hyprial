# Decisions (phase D, package F3 follow-up, 2026-11-25):
# - A joining node receives content one document at a time.  Accepting one
#   content update scanned every node of the space to find the nodes of its
#   doc id, so a join of N documents cost O(N^2) in that scan.  This test pins
#   the per-update work with a counter that does not depend on the machine:
#   the nodes visited through ``space.nodes`` while ``apply_remote`` accepts
#   one content update, as a median over several updates, in a small and a
#   large space.  ``ORGFS_SCALING_JOIN_DOCS`` changes the large space.
# - Two in-process nodes with memory stores (coordinator test support): the
#   joiner opens the temporary space, pulls meta and tree, completes the
#   join, then pulls content documents in ``pending_content`` order.
# - Only ``apply_remote`` is measured.  ``pending_content`` lists one batch
#   of 128 after a scan of the nodes, which is reported, not pinned here.
"""Accepting one content update during a join costs the same in any space."""

from __future__ import annotations

import os
import statistics

from tests.daemon.orgfs.coordinator.support import OWNER, Cluster, Node

from .test_write_cost import _CountingNodes

_SMALL = 500
_SAMPLES = 20


def _join_visits(documents: int) -> int:
    origin = Node(OWNER, "node-a")
    joiner = Node("user:b", "node-b")
    try:
        space_id = origin.fs.create_space("join").space_id
        origin.fs.invite(space_id, "user:b", "rw")
        for folder in range(10):
            origin.fs.mkdir(space_id, f"d{folder}")
        for index in range(documents):
            origin.fs.write_text(space_id, f"d{index % 10}/doc{index}.md", "x" * 100)
        joiner.fs.begin_join(space_id)
        cluster = Cluster(space_id, [origin, joiner])
        mine, theirs = cluster.host(joiner), cluster.host(origin)

        def pull(doc_id: str) -> None:
            found = theirs.read_update(doc_id, mine.read_vectors([doc_id])[doc_id])
            assert found is not None
            mine.apply_remote(doc_id, found[0], found[1], origin.node_id)

        pull("meta")
        pull("tree")
        joiner.fs.complete_join(space_id)
        space = joiner.fs._spaces[space_id]  # noqa: SLF001
        space.nodes = _CountingNodes(space.nodes)
        visits = []
        for doc_id in mine.pending_content(_SAMPLES):
            _CountingNodes.visited = 0
            pull(doc_id)
            visits.append(_CountingNodes.visited)
        assert len(visits) == _SAMPLES
        return int(statistics.median(visits))
    finally:
        origin.close()
        joiner.close()


def test_accepting_one_content_update_visits_the_same_nodes_in_a_small_and_a_large_space() -> None:
    large_documents = int(os.environ.get("ORGFS_SCALING_JOIN_DOCS", "4000"))
    small = _join_visits(_SMALL)
    large = _join_visits(large_documents)
    print(f"content apply visits: {_SMALL} docs {small}; {large_documents} docs {large}")
    assert large <= small + 16, (small, large)
