from __future__ import annotations

import json
import threading
import time
import pytest
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import assert_client_ids_registered
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.transport import KeySpace, LivelinessDirectory, ZenohConfig, ZenohTransport
from hyprial.kernel.impl.primitives.uri import TARGET_KIND_HOST

from ..support import (
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
    # Private-path policy (§7) leaves the tested presence gate active.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    owner_token = None
    try:
        time.sleep(0.3)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner.bind_transport(owner_transport, supplier_online=lambda _node: True)

        live = {"node-a": False}
        received = {"count": 0, "at": time.monotonic()}
        deliver_announce = reader._on_announce

        def counting_on_announce(sample):
            received["count"] += 1
            received["at"] = time.monotonic()
            return deliver_announce(sample)

        reader._on_announce = counting_on_announce  # bound by bind_transport below
        reader.bind_transport(
            reader_transport,
            supplier_online=lambda node: live.get(node, False),
        )

        # The owner announces once per call, and a put issued before the reader's
        # subscription is routed reaches nobody.  In production the owner
        # re-announces when it sees the reader's liveliness; this reader declares
        # none, so re-announce until the reader has really buffered it.  That
        # makes the precondition "buffered, not yet a holder" observed, not vacuous.
        def announce_buffered():
            owner._announce_all()
            return _assert_true("node-a" in reader._pending_announces)

        _eventually(announce_buffered)
        # Let every retried announce land while node-a is still offline, so none
        # can arrive after the flip and make node-a a holder without the
        # buffered-announce promotion this test pins.
        _eventually(lambda: _assert_true(time.monotonic() - received["at"] >= 0.3))
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

    monkeypatch.setattr('hyprial.daemon.impl.orgfs.composition.directory.ORGFS_ANNOUNCE_BUFFER_LIMIT', 3)
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.composition.directory.ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT', 2)
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
    # Restart presence recovery after private sidecar admission (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    restarted_transport = None
    restarted = None
    owner_token = None
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online)
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
            tmp_path / "reader", node_id="node-b", author="user:reader",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        peer_live = {"node-a": False}
        restarted.bind_transport(
            restarted_transport,
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


def test_round3_r4a_announces_all_held_spaces_without_evicting_omissions(
    tmp_path,
) -> None:
    """D.3 R4a: a node holding X and Y keeps discovering and replicating both."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Holder discovery after private sidecar admission (§7), not enrollment.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    owner_presence = reader_presence = None
    owner_token = reader_token = announce_registration = None
    announcements: list[dict[str, object]] = []
    try:
        time.sleep(0.3)
        announce_registration = reader_transport.subscribe(
            KeySpace().orgfs_announce("node-a"),
            lambda sample: announcements.append(json.loads(sample.payload)),
        )
        owner_presence, owner_token = _bind_with_daemon_presence(owner, owner_transport)
        reader_presence, reader_token = _bind_with_daemon_presence(
            reader, reader_transport
        )
        _eventually(lambda: _assert_true(owner_presence.actor_online("node-b")))
        _eventually(lambda: _assert_true(reader_presence.actor_online("node-a")))

        space_x = owner.facade.create_space("x")
        owner.facade.invite(space_x.space_id, "user:reader", "rw")
        space_y = owner.facade.create_space("y")
        owner.facade.invite(space_y.space_id, "user:reader", "rw")
        held = {space_x.space_id, space_y.space_id}

        def announced_both():
            return _assert_true(
                any(
                    {
                        str(space["spaceId"])
                        for space in announce.get("spaces", [])
                        if isinstance(space, dict) and "spaceId" in space
                    }
                    == held
                    for announce in announcements
                )
            )

        _eventually(announced_both)
        _eventually(lambda: reader.facade.join(space_x.space_id))
        _eventually(lambda: reader.facade.join(space_y.space_id))

        node_x = owner.facade.write_text(space_x.space_id, "x.md", "x")
        node_y = owner.facade.write_text(space_y.space_id, "y.md", "y")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space_x.space_id, f"id:{node_x.node_id}")[0],
                "x",
            )
        )
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space_y.space_id, f"id:{node_y.node_id}")[0],
                "y",
            )
        )

        # A partial later announce is additive for discovery. It must not evict
        # an unmentioned held space.
        owner_transport.put(
            KeySpace().orgfs_announce("node-a"),
            json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "node": "node-a",
                    "durable": True,
                    "spaces": [{"spaceId": space_x.space_id}],
                }
            ).encode(),
        )
        _eventually(
            lambda: _assert_true(
                "node-a" in reader._holders.get(space_y.space_id, set())
            )
        )
        _assert_registered(space_x.space_id, owner, reader)
        _assert_registered(space_y.space_id, owner, reader)
    finally:
        reader.close()
        owner.close()
        for registration in (announce_registration, reader_token, owner_token):
            if registration is not None:
                registration.close()
        for presence in (reader_presence, owner_presence):
            if presence is not None:
                presence.close()
        reader_transport.close()
        owner_transport.close()


def test_round3_r4b_restart_accepts_live_peer_without_join_or_sync(tmp_path) -> None:
    """D.3 R4b: restart accepts live broadcasts and prompts peer re-announce."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Restart holder discovery on the private sidecar-admitted path (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    owner_presence = reader_presence = None
    owner_token = reader_token = None
    restarted_transport = None
    restarted_presence = restarted_token = announce_registration = None
    restarted = None
    announcements: list[dict[str, object]] = []
    try:
        time.sleep(0.3)
        owner_presence, owner_token = _bind_with_daemon_presence(owner, owner_transport)
        reader_presence, reader_token = _bind_with_daemon_presence(
            reader, reader_transport
        )
        _eventually(lambda: _assert_true(owner_presence.actor_online("node-b")))
        _eventually(lambda: _assert_true(reader_presence.actor_online("node-a")))

        space = owner.facade.create_space("team")
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

        restarted_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.3)
        announce_registration = restarted_transport.subscribe(
            KeySpace().orgfs_announce("node-a"),
            lambda sample: announcements.append(json.loads(sample.payload)),
        )
        restarted = OrgFsRuntime(
            tmp_path / "reader", node_id="node-b", author="user:reader",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        restarted_presence = LivelinessDirectory(restarted_transport)
        restarted.bind_transport(
            restarted_transport,
            supplier_online=lambda supplier: (
                classify_target_identity(supplier) == TARGET_KIND_HOST
                and restarted_presence.actor_online(supplier)
            ),
            holder_candidates=lambda: restarted_presence.online_actors(),
        )
        _eventually(lambda: _assert_true(restarted_presence.actor_online("node-a")))
        before_revival = len(announcements)
        restarted_token = restarted_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-b")
        )

        _eventually(
            lambda: _assert_true(
                len(announcements) > before_revival
                and any(
                    space.space_id
                    in {
                        str(item["spaceId"])
                        for item in announce.get("spaces", [])
                        if isinstance(item, dict) and "spaceId" in item
                    }
                    for announce in announcements[before_revival:]
                )
            )
        )

        # No join() and no sync_from(): this is only the established peer's
        # next live broadcast after the receiver restart.
        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "after restart"
        )
        _eventually(
            lambda: _assert_equal(
                restarted.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "after restart",
            )
        )
        _assert_registered(space.space_id, owner, restarted)
    finally:
        if restarted is not None:
            restarted.close()
        if reader._session is not None:
            reader.close()
        owner.close()
        for registration in (
            announce_registration,
            restarted_token,
            reader_token,
            owner_token,
        ):
            if registration is not None:
                registration.close()
        for presence in (restarted_presence, reader_presence, owner_presence):
            if presence is not None:
                presence.close()
        if restarted_transport is not None:
            restarted_transport.close()
        # reader_transport may already be closed; ZenohTransport.close is idempotent.
        reader_transport.close()
        owner_transport.close()


@pytest.mark.xfail(
    strict=True,
    raises=OrgFsError,
    reason=(
        "80a6e231: a lost create-time announce is never re-sent, so join "
        "stays no-holder-online; remove this mark when the fix lands"
    ),
)
def test_join_recovers_when_create_time_announce_is_lost(tmp_path) -> None:
    """A reader that missed the create-time announce can still join.

    Deterministic form of the round3_r5 race: the reader's announce
    subscription (gated at the transport) drops everything until the space
    exists, as if the owner's single create-time announce had beaten the
    reader's subscription.  The test itself never calls ``announce()``.
    """

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Late subscription recovery on the private sidecar-admitted path (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    subscribed = threading.Event()
    original_subscribe = reader_transport.subscribe

    def late_announce_subscription(key_expr, callback, *args, **kwargs):
        # Drop at the transport, below whichever runtime handler consumes
        # announces, so the pin does not depend on runtime internals.
        if "/org/fs/announce/" not in key_expr:
            return original_subscribe(key_expr, callback, *args, **kwargs)

        def gated(sample):
            if subscribed.is_set():
                callback(sample)

        return original_subscribe(key_expr, gated, *args, **kwargs)

    reader_transport.subscribe = late_announce_subscription
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        time.sleep(0.5)  # let the create-time announce arrive and be dropped
        subscribed.set()

        def join():
            try:
                return reader.facade.join(space.space_id)
            except OrgFsError as exc:
                if exc.code != "no-holder-online":
                    pytest.fail(f"unexpected join error: {exc}")
                raise

        joined = _eventually(join)
        assert joined.space_id == space.space_id
    finally:
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()
