"""Replica blob repairs remain accepted until the mesh close drains them."""

from __future__ import annotations

import threading
import hashlib
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh


def test_replica_blob_reconcile_keeps_close_custody() -> None:
    entered = threading.Event()
    release = threading.Event()
    stored: list[tuple[str, bytes]] = []
    pinned: list[tuple[str, str, str]] = []
    content = b"retained replica blob"
    digest = hashlib.sha256(content).hexdigest()

    def get(_space_id: str, _digest: str) -> bytes:
        entered.set()
        assert release.wait(3)
        return content

    def registration(*_args, **_kwargs):
        return SimpleNamespace(close=lambda: None)

    mesh = OrgFsMesh(
        SimpleNamespace(subscribe=registration, declare_query_handler=registration),
        SimpleNamespace(space_id="space"),
        "node-reader",
        author="user:reader",
        blob_store=SimpleNamespace(
            get=get,
            pin=lambda space, sha, reason: pinned.append((space, sha, reason)),
        ),
        replica_store=SimpleNamespace(
            pinned_reason=lambda _sha: None,
            store_blob=lambda sha, data: stored.append((sha, data)),
        ),
    )
    try:
        mesh._defer_replica_blob(digest, supplier="node-holder")
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="workers did not drain"):
            mesh.close(timeout=0.02)
        assert len(mesh._worker_futures) == 1
        assert stored == []
        release.set()
        mesh.close(timeout=2)
        assert stored == [(digest, content)]
        assert pinned == [("space", digest, "replica")]
    finally:
        release.set()
        mesh.close(timeout=2)
