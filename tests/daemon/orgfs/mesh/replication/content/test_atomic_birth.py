"""Cross-node document births must project as one atomic ``created`` version.

Defect archive: hq notes/defect-work-item-birth-2026-10-09.md.  A member's
``org work add`` commits creation atomically on the origin, but the owner-side
projection could split it into ``created``(empty) + ``content`` whenever the
birth tree envelope was projected before the sibling content envelope -- which
the sync pull path does by construction, because sync pages flatten documents
in ``document_ids()`` order (meta, tree, then content).  The strict work-items
chain validation then rejects the whole entry permanently.

These tests pin the materialization seam: whatever envelope order the owner
sees, the projected history must carry the birth content in the single
``created`` entry.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport

from ..support import (
    _eventually,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)

BIRTH_TEXT = "member-authored birth content\nsecond line\n"
REVISED_TEXT = "member-authored birth content\nsecond line\nedited later\n"


def _history_sizes(runtime: OrgFsRuntime, space_id: str, node_ref: str):
    return [(entry.node.size, entry.changed) for entry in runtime.facade.history(space_id, node_ref)]


def _bind_pair(tmp_path, monkeypatch: pytest.MonkeyPatch):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    member_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Atomic birth is tested after private sidecar admission (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    member = OrgFsRuntime(
        tmp_path / "member", node_id="node-member", author="user:member",
        trust_tailnet_members=True,
    )
    online = {"node-owner", "node-member"}.__contains__
    owner.bind_transport(owner_transport, supplier_online=online)
    member.bind_transport(
        member_transport,
        supplier_online=online,
        holder_candidates=lambda: ("node-owner",),
    )
    return owner, member, owner_transport, member_transport


def test_sync_pull_birth_is_one_created_version_with_content(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production trigger: anti-entropy pages deliver tree before content."""

    owner, member, owner_transport, member_transport = _bind_pair(tmp_path, monkeypatch)
    try:
        space = owner.facade.create_space("work")
        owner.facade.invite(space.space_id, "user:member", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: member.facade.join(space.space_id))

        # Hold the member's live pushes so the owner catches up exactly the
        # way a reconnecting peer does: through the sync pull, whose pages
        # flatten documents in meta/tree/content order.
        member_mesh = member._meshes[space.space_id]
        member_mesh.broadcast_records = lambda records: 0  # type: ignore[method-assign]

        member.facade.mkdir(space.space_id, "work")
        created = member.facade.write_text(space.space_id, "work/item.md", BIRTH_TEXT)
        node_ref = f"id:{created.node_id}"

        assert _history_sizes(member, space.space_id, node_ref) == [
            (len(BIRTH_TEXT.encode()), "created")
        ], "origin-side birth must stay one atomic created version"

        imported = owner._meshes[space.space_id].sync_from("node-member")
        assert imported > 0
        _eventually(
            lambda: owner.facade.read_text(space.space_id, node_ref)[0] == BIRTH_TEXT
        )
        assert _history_sizes(owner, space.space_id, node_ref) == [
            (len(BIRTH_TEXT.encode()), "created")
        ], "owner-side birth must materialize as a single created-with-content version"
        birth_version = owner.facade.history(space.space_id, node_ref)[0].node.version
        assert (
            owner.facade.read_at(space.space_id, node_ref, birth_version).decode()
            == BIRTH_TEXT
        )

        # Follow-up mutations stay ordinary content versions on top of the
        # atomic birth, so a strict chain consumer can validate them.
        member.facade.write_text(space.space_id, node_ref, REVISED_TEXT)
        owner._meshes[space.space_id].sync_from("node-member")
        _eventually(
            lambda: owner.facade.read_text(space.space_id, node_ref)[0] == REVISED_TEXT
        )
        assert _history_sizes(owner, space.space_id, node_ref) == [
            (len(REVISED_TEXT.encode()), "content"),
            (len(BIRTH_TEXT.encode()), "created"),
        ]
    finally:
        member.close()
        owner.close()
        member_transport.close()
        owner_transport.close()


def test_live_push_birth_is_one_created_version_with_content(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live broadcast order (content envelope first) must stay atomic too."""

    owner, member, owner_transport, member_transport = _bind_pair(tmp_path, monkeypatch)
    try:
        space = owner.facade.create_space("work")
        owner.facade.invite(space.space_id, "user:member", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: member.facade.join(space.space_id))

        member.facade.mkdir(space.space_id, "work")
        created = member.facade.write_text(space.space_id, "work/item.md", BIRTH_TEXT)
        node_ref = f"id:{created.node_id}"

        _eventually(
            lambda: owner.facade.read_text(space.space_id, node_ref)[0] == BIRTH_TEXT
        )
        assert _history_sizes(owner, space.space_id, node_ref) == [
            (len(BIRTH_TEXT.encode()), "created")
        ]
    finally:
        member.close()
        owner.close()
        member_transport.close()
        owner_transport.close()


def test_owner_local_birth_stays_atomic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the owner's own writes never split their birth entry."""

    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    try:
        space = owner.facade.create_space("work")
        owner.facade.mkdir(space.space_id, "work")
        created = owner.facade.write_text(space.space_id, "work/item.md", BIRTH_TEXT)
        node_ref = f"id:{created.node_id}"
        assert _history_sizes(owner, space.space_id, node_ref) == [
            (len(BIRTH_TEXT.encode()), "created")
        ]
    finally:
        owner.close()
