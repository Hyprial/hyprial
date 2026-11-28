# Decisions:
# - Design §4.2 (revised after C3 report §11.1): concurrent edits of one
#   document converge once every node has pulled, and the repair writes stop
#   after convergence.  Broadcasts are dropped on purpose: convergence must
#   come from pulls alone, the case that diverged under the first rule.
"""Frontier repair: convergence and termination across LocalOrgFs nodes."""

from __future__ import annotations

import base64

import pytest

from hyprial.daemon.impl.orgfs.membership import decode_vector

from .support import OWNER, Cluster, Node, clone_node, doc_id_of, vector


def _cluster(count: int, *, read_only: tuple[int, ...] = ()) -> tuple[Cluster, list[Node], str]:
    origin = Node(OWNER, "node-0")
    space = origin.fs.create_space("team")
    for index in range(1, count):
        mode = "ro" if index in read_only else "rw"
        origin.fs.invite(space.space_id, f"user:n{index}", mode)
    origin.fs.write_text(space.space_id, "shared.md", "base\n")
    nodes = [origin] + [
        clone_node(origin, space.space_id, f"user:n{index}", f"node-{index}")
        for index in range(1, count)
    ]
    return Cluster(space.space_id, nodes), nodes, space.space_id


def _texts(nodes: list[Node], space_id: str) -> set[str]:
    return {node.fs.read_text(space_id, "shared.md")[0] for node in nodes}


def _close(nodes: list[Node]) -> None:
    for node in nodes:
        node.close()


@pytest.mark.parametrize("count", [2, 3])
def test_concurrent_edits_converge_by_pulls_alone_and_repairs_terminate(count: int) -> None:
    cluster, nodes, space_id = _cluster(count)
    try:
        for index, node in enumerate(nodes):
            node.fs.write_text(space_id, "shared.md", f"base\nedit {index}\n")
        cluster.drop_all()

        cluster.settle()

        texts = _texts(nodes, space_id)
        assert len(texts) == 1
        (text,) = texts
        assert all(f"edit {index}" in text for index in range(count))
        doc_id = doc_id_of(nodes[0].fs, space_id, "shared.md")
        assert len({tuple(sorted(vector(node.fs, space_id, doc_id).items())) for node in nodes}) == 1
        trees = [vector(node.fs, space_id, "tree") for node in nodes]
        assert all(tree == trees[0] for tree in trees)
        repairs = [node.fs._registry(space_id).frontier_repairs for node in nodes]
        assert sum(repairs) > 0

        for _ in range(5):
            for requester in nodes:
                for supplier in nodes:
                    if requester is not supplier:
                        assert cluster.pull(requester, supplier) == 0
        assert [vector(node.fs, space_id, "tree") for node in nodes] == trees
        assert [node.fs._registry(space_id).frontier_repairs for node in nodes] == repairs
        for node in nodes:
            assert node.fs.status(space_id).content_pending == 0
    finally:
        _close(nodes)


def test_a_lost_broadcast_after_a_concurrent_edit_is_recovered_by_pulls() -> None:
    cluster, nodes, space_id = _cluster(2)
    left, right = nodes
    try:
        left.fs.write_text(space_id, "shared.md", "left\n")
        right.fs.write_text(space_id, "shared.md", "right\n")
        frames = cluster.take()
        tree_only = [frame for frame in frames if frame[1] == "tree"]
        for sender, doc_id, update, vector_bytes in tree_only:
            target = right if sender == left.node_id else left
            cluster.host(target).apply_remote(doc_id, update, vector_bytes, sender)
        cluster.drop_all()

        cluster.settle()

        assert len(_texts(nodes, space_id)) == 1
    finally:
        _close(nodes)


def test_repair_keeps_the_row_author_and_fills_refs_only_at_the_local_vector() -> None:
    cluster, nodes, space_id = _cluster(2)
    left, right = nodes
    try:
        left.fs.write_text(space_id, "shared.md", "left\n")
        right.fs.write_text(space_id, "shared.md", "right\n")
        frames = cluster.take()
        for sender, doc_id, update, vector_bytes in frames:
            if doc_id == "tree":
                target = right if sender == left.node_id else left
                cluster.host(target).apply_remote(doc_id, update, vector_bytes, sender)
        cluster.drop_all()
        repaired = [node for node in nodes if node.fs._registry(space_id).frontier_repairs]
        assert len(repaired) == 1
        (loser,) = repaired
        winner = right if loser is left else left

        info = loser.fs.stat(space_id, "shared.md")
        raw = loser.fs._spaces[space_id].tree.row(info.node_id)
        assert raw["author"] == winner.author
        assert "refSize" not in raw and "refSha256" not in raw
        assert info.content_state == "pending"

        cluster.settle()
        for node in nodes:
            info = node.fs.stat(space_id, "shared.md")
            frontier = base64.b64decode(node.fs._spaces[space_id].tree.row(info.node_id)["contentFrontier"])
            assert decode_vector(frontier) == vector(node.fs, space_id, info.doc_id)
            assert info.content_state == "arrived"
    finally:
        _close(nodes)


def test_a_read_only_member_never_repairs() -> None:
    cluster, nodes, space_id = _cluster(3, read_only=(2,))
    try:
        nodes[0].fs.write_text(space_id, "shared.md", "base\nfrom 0\n")
        nodes[1].fs.write_text(space_id, "shared.md", "base\nfrom 1\n")
        cluster.drop_all()

        cluster.settle()

        assert nodes[2].fs._registry(space_id).frontier_repairs == 0
        assert len(_texts(nodes, space_id)) == 1
    finally:
        _close(nodes)
