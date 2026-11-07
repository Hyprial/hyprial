"""D4 x D1 seam: the creating node can read after a peer edits its text.

Regression for docs/notes/orgfs-fix-round/d4-d1-seam.md, actor-line adaptation
(brief: docs/notes/orgfs-fix-round/d4-d1-seam-actor-brief.md).  Delivery is
hand-driven (no transport, no last-writer-wins race), so the failure is
deterministic: the peer's edit reaches the creator's live facade
incrementally, exactly as an inbound envelope does, which strands the
creator's own coverage-clock op without the absorb step.

On this tree the store sits behind OrgSpaceAuthority; the absorb delta
comes back inside the typed authority commit result and the facade folds
it into its content doc.  The kill-mutation disables
LocalOrgFs._absorb_committed_deltas (or OrgSpaceAuthority._absorb) and
both seam tests go red with content-pending.
"""

from __future__ import annotations

import time

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import StoreError


def _runtime(tmp_path, name, author):
    return OrgFsRuntime(tmp_path / name, node_id=name, author=author)


def _local_commits(runtime, space_id):
    # Only this node's own commits: re-delivering a peer its own envelopes
    # would hand it back its coverage-clock op and mask the seam.  All
    # writes settle synchronously through the space authority before the
    # writing call returns, so this read-only journal query races nothing.
    rows = runtime.stores.get(space_id)._db.execute(
        "SELECT envelope_bytes FROM commits WHERE writer LIKE ? ORDER BY rowid",
        (f"{runtime.node_id}.%",),
    )
    return [bytes(env) for (env,) in rows]


def _receive_fresh(runtime, space_id, envelopes):
    store = runtime.stores.get(space_id)
    for env in envelopes:
        store.import_envelope(env, supplier="seed")
    runtime.facade.load_space(space_id)


def _receive_live(runtime, space_id, envelopes):
    store = runtime.stores.get(space_id)
    for env in envelopes:
        store.import_envelope(env, supplier="peer")
        runtime.facade.apply_envelope(space_id, env)


def test_creator_reads_after_peer_edit_two_writers(tmp_path) -> None:
    b = _runtime(tmp_path, "node-b", "user:b")
    c = _runtime(tmp_path, "node-c", "user:c")
    try:
        space = b.facade.create_space("seam")
        sid = space.space_id
        b.facade.invite(sid, "user:c", "rw")

        created = b.facade.write_text(sid, "shared.txt", "from B 1")
        ref = f"id:{created.node_id}"

        _receive_fresh(c, sid, _local_commits(b, sid))
        assert c.facade.read_text(sid, ref)[0] == "from B 1"

        c.facade.write_text(sid, ref, "from B 1\nline-C")
        expected = "from B 1\nline-C"

        _receive_live(b, sid, _local_commits(c, sid))

        # The originating writer reads its own text through the facade...
        assert b.facade.read_text(sid, ref)[0] == expected
        # ...and through the daemon content-provisioning seam.
        deadline = time.monotonic() + 5.0
        assert b.await_content(sid, created.node_id, deadline_monotonic=deadline)
        assert b.facade.read_text(sid, ref)[0] == expected
        # The editor still reads correctly: both directions hold.
        assert c.facade.read_text(sid, ref)[0] == expected
    finally:
        c.close()
        b.close()


def test_creator_and_editor_read_after_third_writer(tmp_path) -> None:
    b = _runtime(tmp_path, "node-b", "user:b")
    c = _runtime(tmp_path, "node-c", "user:c")
    d = _runtime(tmp_path, "node-d", "user:d")
    try:
        space = b.facade.create_space("seam3")
        sid = space.space_id
        b.facade.invite(sid, "user:c", "rw")
        b.facade.invite(sid, "user:d", "rw")

        created = b.facade.write_text(sid, "shared.txt", "from B 1")
        ref = f"id:{created.node_id}"
        seed = _local_commits(b, sid)
        _receive_fresh(c, sid, seed)
        _receive_fresh(d, sid, seed)

        c.facade.write_text(sid, ref, "from B 1\nline-C")
        c_edit = _local_commits(c, sid)
        _receive_live(b, sid, c_edit)
        _receive_live(d, sid, c_edit)

        d.facade.write_text(sid, ref, d.facade.read_text(sid, ref)[0] + "\nline-D")
        d_edit = _local_commits(d, sid)
        _receive_live(b, sid, d_edit)
        _receive_live(c, sid, d_edit)

        # D's entry is the latest; every writer must cover it and read.
        texts = {
            runtime.node_id: runtime.facade.read_text(sid, ref)[0]
            for runtime in (b, c, d)
        }
        assert len(set(texts.values())) == 1, texts
        only = next(iter(texts.values()))
        assert only.startswith("from B 1")
        assert "line-C" in only and "line-D" in only
    finally:
        d.close()
        c.close()
        b.close()


def test_committed_update_refuses_a_retired_document(tmp_path) -> None:
    runtime = _runtime(tmp_path, "node-a", "user:owner")
    try:
        space = runtime.facade.create_space("retired")
        node = runtime.facade.write_text(space.space_id, "gone.md", "old")
        plan = runtime.purge_plan(
            space.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": "all",
                    "to": "all",
                }
            ],
        )
        runtime.purge(space.space_id, plan.plan_id)
        with pytest.raises(StoreError) as caught:
            runtime.stores[space.space_id].committed_update(node.doc_id, b"\x00")
        assert caught.value.code == "snapshot-barrier"
    finally:
        runtime.close()
