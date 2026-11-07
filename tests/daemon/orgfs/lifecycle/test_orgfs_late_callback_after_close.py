"""A log callback finishing after runtime close must not re-open stores.

On dev (pre-#976) the on-applied hook reaches the lazy store registry after
close() cleared it, re-opens journal.sqlite3/-wal/-shm and nothing closes them
again: the resident_blobs CI fd leak (80a6e231 group).  #976 refuses the late
hook with ``unavailable: orgfs is closing``; this keeps that behaviour pinned.
"""

from __future__ import annotations

import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def test_late_log_callback_after_close_does_not_reopen_store(tmp_path) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Close/callback custody after private sidecar admission (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True)
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
        peer.bind_transport(
            peer_transport,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("late callback")
        owner.facade.invite(space.space_id, "user:peer", "rw")

        def announce_and_join():
            owner._announce_all()
            return peer.facade.join(space.space_id)

        _eventually(announce_and_join)
        owner.facade.write_text(space.space_id, "late.txt", "delivered after close")
        store = owner.stores[space.space_id]
        tree = next(d for d in store.document_ids() if d.startswith("tree-"))
        envelope = store.export_since(tree, b"", cursor=None, max_bytes=1 << 20).envelopes[-1]
        mesh = peer._meshes[space.space_id]
        peer.close()
        if dict(peer.stores):
            pytest.fail("precondition: close() left space stores registered")
        # A log callback that imported its envelope just before close reaches
        # the runtime's on-applied hook after close has cleared the stores.
        try:
            mesh._on_applied(envelope)
        except OrgFsError:
            pass  # refusing late work is fine; re-opening a store is not
        assert dict(peer.stores) == {}
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()
