# Decisions:
# - Phase D, package T2.  This file was the production-entry (``bind_admitted``)
#   twin of ``tests/daemon/orgfs/mesh/replication/presence/test_presence.py``.
#   The twins of F5, F7 (triage "pending", kept) and F6 are deleted here: the
#   daemon-side copies now run on ``StateOrgFsRuntime`` over the same loopback
#   Zenoh, and ``bind_admitted`` only adds the admitted-peer binding, which
#   the directory tests already go through.
# - ``test_d2_restart_retries_failed_catchup_then_accepts_live_delivery`` has
#   no twin and is rewritten onto ``StateOrgFsRuntime``: the forced first
#   failure is the sync package's ``SyncError`` and the journal client-id
#   registration check is removed.  Protocol 3 may pull once more after the
#   retry (an online edge and the retried round both schedule one), so the
#   test asserts at least the failed attempt and its retry.
"""actor_preserved_orgfs_mesh_rework: announcement scenarios."""

from __future__ import annotations

import threading
import time
import pytest
from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime
from hyprial.daemon.impl.orgfs.sync import SyncError
from hyprial.daemon.impl.transport import (
    KeySpace,
    ZenohConfig,
    ZenohTransport,
)

from .support import (
    _assert_equal,
    _assert_true,
    _bind_with_daemon_presence,
    _eventually,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_d2_restart_retries_failed_catchup_then_accepts_live_delivery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: one transient restart catch-up failure cannot strand the member."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = StateOrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = StateOrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    owner_presence = reader_presence = None
    owner_token = reader_token = None
    restarted_transport = None
    restarted_presence = restarted_token = None
    restarted = None
    try:
        time.sleep(0.3)
        owner_presence, owner_token = _bind_with_daemon_presence(owner, owner_transport)
        reader_presence, reader_token = _bind_with_daemon_presence(
            reader, reader_transport
        )
        _eventually(lambda: _assert_true(owner_presence.actor_online("node-b")))
        _eventually(lambda: _assert_true(reader_presence.actor_online("node-a")))

        space = owner.facade.create_space("restart-retry")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        _eventually(lambda: reader.facade.join(space.space_id))
        created = owner.facade.write_text(space.space_id, "shared.md", "before")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "before",
            )
        )

        reader.close()
        reader_token.close()
        reader_token = None
        reader_presence.close()
        reader_presence = None
        reader_transport.close()
        owner_token.close()
        owner_token = None
        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "missed while down"
        )

        restarted_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.3)
        restarted = StateOrgFsRuntime(
            tmp_path / "reader", node_id="node-b", author="user:reader"
        )
        restarted_presence, restarted_token = _bind_with_daemon_presence(
            restarted, restarted_transport
        )
        restarted_mesh = restarted._meshes[space.space_id]
        original_sync = restarted_mesh.sync_from
        attempts = 0
        first_failed = threading.Event()

        def fail_once(supplier, *, timeout=3.0, deadline_monotonic=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_failed.set()
                raise SyncError("no-holder-online", "supplier did not answer")
            return original_sync(
                supplier,
                timeout=timeout,
                deadline_monotonic=deadline_monotonic,
            )

        monkeypatch.setattr(restarted_mesh, "sync_from", fail_once)
        owner_token = owner_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-a")
        )

        assert first_failed.wait(2.0), "restart catch-up was never attempted"
        _eventually(
            lambda: _assert_equal(
                restarted.facade.read_text(
                    space.space_id, f"id:{created.node_id}"
                )[0],
                "missed while down",
            ),
            timeout=2.0,
        )
        assert attempts >= 2

        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "live after catch-up"
        )
        _eventually(
            lambda: _assert_equal(
                restarted.facade.read_text(
                    space.space_id, f"id:{created.node_id}"
                )[0],
                "live after catch-up",
            )
        )
    finally:
        if restarted is not None:
            restarted.close()
        if reader._session is not None:
            reader.close()
        owner.close()
        for registration in (restarted_token, reader_token, owner_token):
            if registration is not None:
                registration.close()
        for presence in (restarted_presence, reader_presence, owner_presence):
            if presence is not None:
                presence.close()
        if restarted_transport is not None:
            restarted_transport.close()
        reader_transport.close()
        owner_transport.close()
