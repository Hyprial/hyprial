"""D4 read-path fetches coalesce per digest and stay inside the queue bound."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh


def _mesh(monkeypatch, limit: int = 2) -> OrgFsMesh:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.network.recovery.ORGFS_BLOB_FETCH_QUEUE_LIMIT', limit)

    def registration(*_args, **_kwargs):
        return SimpleNamespace(close=lambda: None)

    transport = SimpleNamespace(
        subscribe=registration,
        declare_query_handler=registration,
    )
    mesh = OrgFsMesh(
        transport,
        SimpleNamespace(space_id="space"),
        "reader",
        author="user:r",
        blob_store=SimpleNamespace(contains=lambda _digest: False),
        recovery_candidates=lambda: ("supplier",),
    )
    monkeypatch.setattr(mesh, "_supplier_allowed", lambda _supplier: True)
    return mesh


def test_read_fetch_coalesces_and_bounds_distinct_digests(monkeypatch):
    mesh = _mesh(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def fetch(_supplier, _digest):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(mesh, "_fetch_blob_into_store", fetch)
    try:
        first = mesh.request_blob("a" * 64)
        assert entered.wait(1)
        assert all(mesh.request_blob("a" * 64) is first for _ in range(50))
        second = mesh.request_blob("b" * 64)
        refused = mesh.request_blob("c" * 64)
        assert refused.is_set()
        assert len(mesh._blob_fetch_pending) == 2
        assert len(mesh._blob_fetch_waiters) == 2
        assert all(len(waiters) == 1 for waiters in mesh._blob_fetch_waiters.values())
        assert not first.is_set()
        release.set()
        assert first.wait(2)
        assert second.wait(2)
    finally:
        release.set()
        mesh.close()


def test_update_recovery_counts_read_batches_against_distinct_bound(monkeypatch):
    # Read-path fetches open batches with no envelopes, so the envelope count
    # alone cannot bound how many distinct digests the queue holds.
    mesh = _mesh(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(mesh, "_fetch_blob_into_store", lambda *_args: release.wait(3))
    try:
        mesh.request_blob("a" * 64)
        mesh.request_blob("b" * 64)
        assert mesh._blob_fetch_pending_count == 0
        mesh._defer_blob_recovery(b"env", supplier="supplier", digest="c" * 64)
        assert sorted(mesh._blob_fetch_pending) == ["a" * 64, "b" * 64]
        assert mesh._blob_fetch_dropped == 1
        mesh._defer_blob_recovery(b"env", supplier="supplier", digest="a" * 64)
        assert mesh._blob_fetch_pending_count == 1
    finally:
        release.set()
        mesh.close()
