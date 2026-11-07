"""actor_preserved_orgfs_mesh_rework: announcement scenarios."""

from __future__ import annotations

from tests.support.tailnet_daemon import bind_admitted
import json
import threading
import time
import pytest
from hyprial.daemon.impl.orgfs import mesh as mesh_module
from hyprial.daemon.impl.orgfs.composition import directory as directory_module
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError
from hyprial.daemon.impl.orgfs.storage.store import assert_client_ids_registered
from hyprial.daemon.impl.transport import (
    KeySpace,
    ZenohConfig,
    ZenohTransport,
)

from .support import (
    _assert_equal,
    _assert_registered,
    _assert_true,
    _bind_with_daemon_presence,
    _eventually,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_round4_f5_buffers_announce_until_announcer_liveliness(tmp_path) -> None:
    """F5: an early announce becomes holder discovery after liveliness appears."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    owner_token = None
    announcement_probe = None
    try:
        time.sleep(0.3)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner.bind_transport(owner_transport, supplier_online=lambda _node: True)

        live = {"node-a": False}
        reader.bind_transport(
            reader_transport,
            supplier_online=lambda node: live.get(node, False),
        )
        # Observe remote subscription readiness; the owner's own wildcard
        # subscription must not satisfy this barrier. A sleep after put cannot
        # recover an announce sent before the reader was matched.
        import zenoh
        announcement_probe = owner_transport._session.declare_publisher(
            KeySpace().orgfs_announce("node-a"),
            allowed_destination=zenoh.Locality.REMOTE,
        )
        _eventually(lambda: _assert_true(announcement_probe.matching_status.matching))
        owner._announce_all()
        _eventually(lambda: _assert_true("node-a" in reader._pending_announces))
        assert "node-a" not in reader._holders.get(space.space_id, set())

        live["node-a"] = True
        owner_token = owner_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-a")
        )
        _eventually(
            lambda: _assert_true("node-a" in reader._holders.get(space.space_id, set()))
        )
        assert "node-a" not in reader._pending_announces
        assert_client_ids_registered(owner.stores.get(space.space_id))
    finally:
        if announcement_probe is not None:
            announcement_probe.undeclare()
        reader.close()
        owner.close()
        if owner_token is not None:
            owner_token.close()
        reader_transport.close()
        owner_transport.close()


def test_final_f7_pending_announces_are_bounded_counted_and_still_apply(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: inbound announce buffering is bounded in both dimensions."""

    monkeypatch.setattr(directory_module, "ORGFS_ANNOUNCE_BUFFER_LIMIT", 3)
    monkeypatch.setattr(directory_module, "ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT", 2)
    endpoint = f"tcp/127.0.0.1:{_port()}"
    publisher = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    events: list[tuple[str, str, dict[str, object]]] = []
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-reader",
        author="user:reader",
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    live: set[str] = set()
    token = None
    try:
        time.sleep(0.3)
        reader.bind_transport(
            reader_transport, supplier_online=lambda node: node in live
        )

        def announce(node: str, spaces: list[dict[str, object]]) -> None:
            publisher.put(
                KeySpace().orgfs_announce(node),
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "type": "orgfs-announce",
                        "node": node,
                        "spaces": spaces,
                    }
                ).encode(),
            )

        # Establish that the announce subscription is live with a probe, then
        # start from an empty buffer.  The four announces below are sent exactly
        # once: re-sending them on retry could re-buffer node-0 after its
        # eviction and make the oldest-evicted assertion timing-dependent
        # (a single publisher's puts arrive in order).
        def probe_is_buffered() -> bool:
            announce(
                "node-probe",
                [{"spaceId": "space-probe", "roles": ["member"], "mode": "rw"}],
            )
            return _assert_true("node-probe" in reader._pending_announces)

        _eventually(probe_is_buffered)
        # Probes may have been re-sent; a fence sent exactly once after them
        # arrives only after every probe (FIFO), so none can land post-clear.
        announce(
            "node-fence",
            [{"spaceId": "space-fence", "roles": ["member"], "mode": "rw"}],
        )
        _eventually(lambda: _assert_true("node-fence" in reader._pending_announces))
        with reader._pending_announces_lock:
            reader._pending_announces.clear()
        events.clear()
        for index in range(4):
            announce(
                f"node-{index}",
                [
                    {
                        "spaceId": f"space-{index}",
                        "roles": ["member"],
                        "mode": "rw",
                    }
                ],
            )
        _eventually(
            lambda: _assert_true(
                [item for item in events if item[1] == "orgfs.announce.buffer-dropped"]
            )
        )
        _eventually(lambda: _assert_true(len(reader._pending_announces) == 3))
        assert len(reader._pending_announces) == 3
        assert "node-0" not in reader._pending_announces
        assert any(
            event == "orgfs.announce.buffer-dropped"
            and fields["droppedCount"] == 1
            and fields["announcerLimit"] == 3
            for _level, event, fields in events
        )

        announce(
            "node-3",
            [{"spaceId": "space-kept", "roles": ["member"], "mode": "rw"}],
        )
        announce(
            "node-3",
            [{"spaceId": "space-dropped", "roles": ["member"], "mode": "rw"}],
        )
        _eventually(
            lambda: _assert_true(
                [
                    item
                    for item in events
                    if item[1] == "orgfs.announce.buffer-dropped"
                    and item[2]["droppedCount"] == 2
                ]
            )
        )
        assert len(reader._pending_announces["node-3"]["spaces"]) == 2
        assert {
            space["spaceId"] for space in reader._pending_announces["node-3"]["spaces"]
        } == {"space-3", "space-kept"}
        assert any(
            event == "orgfs.announce.buffer-dropped"
            and fields["droppedCount"] == 2
            and fields["spacesPerEntryLimit"] == 2
            for _level, event, fields in events
        )

        live.add("node-3")
        token = publisher.declare_liveliness(KeySpace().actor_liveliness("node-3"))
        _eventually(
            lambda: _assert_true("node-3" in reader._holders.get("space-kept", set()))
        )
        assert "node-3" not in reader._pending_announces
        assert "node-3" not in reader._holders.get("space-dropped", set())
    finally:
        reader.close()
        if token is not None:
            token.close()
        reader_transport.close()
        publisher.close()


def test_round4_f6_liveliness_auto_sync_recovers_dropped_startup_broadcast(
    tmp_path,
) -> None:
    """F6: liveliness auto-sync repairs a startup drop without join/sync calls."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    restarted_transport = None
    restarted = None
    owner_token = None
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        # Cold/restarted replicas model already-admitted private peers.
        bind_admitted(owner, owner_transport, supplier_online=online)
        bind_admitted(reader, reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))
        created = owner.facade.write_text(space.space_id, "shared.md", "baseline")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "baseline",
            )
        )

        reader.close()
        reader_transport.close()
        restarted_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.3)
        restarted = OrgFsRuntime(
            tmp_path / "reader", node_id="node-b", author="user:reader"
        )
        peer_live = {"node-a": False}
        bind_admitted(
            restarted, restarted_transport,
            supplier_online=lambda node: peer_live.get(node, False),
        )

        # This startup-window broadcast is rejected by unchanged gate 2.
        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "missed at startup"
        )
        time.sleep(0.2)
        assert (
            restarted.facade.read_text(space.space_id, f"id:{created.node_id}")[0]
            == "baseline"
        )

        peer_live["node-a"] = True
        owner_token = owner_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-a")
        )
        # A dependent next envelope cannot mask the missing update: without
        # the liveliness trigger it parks behind the sequence gap.
        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "after liveliness"
        )
        _eventually(
            lambda: _assert_equal(
                restarted.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "after liveliness",
            )
        )
        _assert_registered(space.space_id, owner, restarted)
    finally:
        if restarted is not None:
            restarted.close()
        if reader._session is not None:
            reader.close()
        owner.close()
        if owner_token is not None:
            owner_token.close()
        if restarted_transport is not None:
            restarted_transport.close()
        reader_transport.close()
        owner_transport.close()


def test_d2_restart_retries_failed_catchup_then_accepts_live_delivery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: one transient restart catch-up failure cannot strand the member."""

    monkeypatch.setattr(
        mesh_module, "ORGFS_SYNC_RETRY_BACKOFF_SECONDS", (0.05,), raising=False
    )
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
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
        restarted = OrgFsRuntime(
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
                raise StoreError("no-holder-online", "supplier did not answer")
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
        assert attempts == 2

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
        _assert_registered(space.space_id, owner, restarted)
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
