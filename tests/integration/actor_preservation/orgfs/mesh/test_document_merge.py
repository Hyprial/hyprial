"""actor_preserved_orgfs_mesh_rework: document merge scenarios."""

from __future__ import annotations

from tests.support.tailnet_daemon import bind_admitted
import time
import pytest
from hyprial.daemon.impl.orgfs.document.model import _ContentDocument
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import (
    ZenohConfig,
    ZenohTransport,
)

from .support import (
    _assert_equal,
    _assert_registered,
    _eventually,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_concurrent_text_edits_from_same_base_do_not_duplicate_base(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: disjoint edits converge without duplicating unchanged base text."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    b_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    c_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    node_b = OrgFsRuntime(tmp_path / "node-b", node_id="node-b", author="user:b")
    node_c = OrgFsRuntime(tmp_path / "node-c", node_id="node-c", author="user:c")
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-b", "node-c"}

        # Explicit admitted-path policy for this cold replication fixture.
        bind_admitted(node_b, b_transport, supplier_online=online)
        bind_admitted(
            node_c, c_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-b",),
        )
        space = node_b.facade.create_space("concurrent-content")
        node_b.facade.invite(space.space_id, "user:c", "rw")
        node_b._mesh(space.space_id).announce()
        _eventually(lambda: node_c.facade.join(space.space_id))

        created = node_b.facade.write_text(
            space.space_id, "shared.txt", "from B 1"
        )
        node_ref = f"id:{created.node_id}"
        _eventually(
            lambda: _assert_equal(
                node_c.facade.read_text(space.space_id, node_ref)[0], "from B 1"
            )
        )
        base_version = node_b.facade.read_text(space.space_id, node_ref)[1]
        assert node_c.facade.read_text(space.space_id, node_ref)[1] == base_version

        # Hold both production broadcasts so both writes are based on the same
        # CRDT state.  Releasing them afterwards deterministically exercises a
        # real concurrent merge over loopback Zenoh.
        with monkeypatch.context() as held:
            held.setattr(node_b, "broadcast_pending", lambda _space_id: 0)
            held.setattr(node_c, "broadcast_pending", lambda _space_id: 0)
            node_b.facade.write_text(
                space.space_id,
                node_ref,
                "from B 1\nline-B",
                base_version=base_version,
            )
            node_c.facade.write_text(
                space.space_id,
                node_ref,
                "line-C\nfrom B 1",
                base_version=base_version,
            )

        assert node_b.broadcast_pending(space.space_id) >= 1
        assert node_c.broadcast_pending(space.space_id) >= 1
        expected = "line-C\nfrom B 1\nline-B"
        for runtime in (node_b, node_c):
            _eventually(
                lambda runtime=runtime: _assert_equal(
                    runtime.facade.read_text(space.space_id, node_ref)[0], expected
                )
            )
        _assert_registered(space.space_id, node_b, node_c)
    finally:
        node_c.close()
        node_b.close()
        c_transport.close()
        b_transport.close()


@pytest.mark.parametrize(
    ("initial", "updated"),
    [
        ("", "new text"),
        ("old text", ""),
        ("甲😀乙", "甲🐍中乙"),
        ("甲😀乙猫丙", "甲🐍乙犬丙"),
    ],
)
def test_content_document_set_handles_edge_edits(initial: str, updated: str) -> None:
    document = _ContentDocument(initial)
    if initial == "甲😀乙":
        assert len(document.text) == len(initial.encode())

    document.set(updated)

    assert document.value() == updated


def test_content_document_set_handles_large_text() -> None:
    prefix = "a" * 450_000
    suffix = "z" * 450_000
    document = _ContentDocument(prefix + "before" + suffix)

    document.set(prefix + "after" + suffix)

    assert document.value() == prefix + "after" + suffix


def test_content_document_set_no_op_preserves_crdt_bytes() -> None:
    document = _ContentDocument("unchanged 中文 😀")
    before = document.export()

    document.set(document.value())

    assert document.export() == before


def test_review_fix_6_fetches_missing_update_blob_and_retries_once(
    tmp_path,
) -> None:
    """Review P1 fix 6: >512000-byte public writes cross two loopback meshes."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        # Explicit admitted-path policy; document validation remains real.
        bind_admitted(owner, owner_transport, supplier_online=online)
        bind_admitted(reader, reader_transport, supplier_online=online)

        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))

        content = "mesh-update-" + "x" * 900_000
        assert len(content.encode()) > 512_000
        created = owner.facade.write_text(space.space_id, "large.md", content)

        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                content,
            )
        )
        replica_refs = reader.blobs.references(space_id=space.space_id)
        assert any(reason == "replica" for _, _, reason in replica_refs)
        _assert_registered(space.space_id, owner, reader)
    finally:
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()
