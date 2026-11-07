# RM3 re-serve probe (2026-11-03, brief-replica.md Round 3).
#
# Adopted from the RM2 review evidence commit 8e6a2d7701
# (docs/notes/replica-attach-window-review-2026-11-03.md): reuse of an
# already-serving replica ran its delta prime on the replica actor while the
# mesh stayed ready, so a ready mirror put blocked behind the prime.
#
# Adaptation vs the reviewer's probe: the reviewer's `mesh._replica_serving`
# assertion asserted the buggy behavior (still serving during the reuse
# prime). Round 3 routes reuse through the same NOT-READY window as first
# attach (brief: "NOT-READY + non-blocking mirror + delta backfill exactly
# like first attach"), so during the gated reuse prime the mesh must NOT be
# replica-serving (queries fall back to the store path). The latency bound
# (250 ms), the gated third core prime (the reuse delta prime), and the
# drain sequencing are the reviewer's; a post-completion no-loss check was
# added to pin the queued mirror envelope into the replica.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading

import pytest

from hyprial.daemon.impl.orgfs.storage.replica import ReplicaStore

from tests.daemon.orgfs.replication.test_orgfs_replica_mesh import (
    _await_broadcast_drained,
    _await_replica_serving,
    _await_resident_journal,
    _joined_topology,
    _log_key,
)


def test_review_re_serve_prime_does_not_block_ready_mirror(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    release = threading.Event()
    prime_started = threading.Event()
    original = ReplicaStore.prime_from
    counts: dict[int, int] = {}

    def gated(replica: ReplicaStore, store, blob_store=None):
        index = counts.get(id(replica), 0)
        counts[id(replica)] = index + 1
        if index == 2:
            prime_started.set()
            if not release.wait(10):
                raise TimeoutError("gated re-serve prime was not released")
        return original(replica, store, blob_store)

    monkeypatch.setattr(ReplicaStore, "prime_from", gated)
    try:
        node = owner.facade.write_text(space.space_id, "reuse.md", "reuse v0")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0,))
        resident.serve(space.space_id, "memory")
        _await_replica_serving(resident, space.space_id)
        mesh = resident._meshes[space.space_id]
        page = resident.stores[space.space_id].export_since(
            node.doc_id, b"", cursor=None, max_bytes=1_048_576
        )
        envelope = bytes(page.envelopes[-1])
        key = _log_key(space.space_id, envelope)
        with ThreadPoolExecutor(max_workers=2) as pool:
            serving = pool.submit(resident.serve, space.space_id, "memory")
            assert prime_started.wait(5), "re-serve delta prime never started"
            assert not mesh._replica_serving
            mirror = pool.submit(mesh._store_replica_envelope, envelope)
            try:
                mirror.result(timeout=0.25)
            except FutureTimeout:
                blocked = True
            else:
                blocked = False
            release.set()
            serving.result(timeout=30)
            mirror.result(timeout=30)
        assert not blocked, "ready mirror blocked behind the re-serve prime"
        assert resident._replicas[space.space_id].backend_get(key) == envelope
    finally:
        release.set()
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()
