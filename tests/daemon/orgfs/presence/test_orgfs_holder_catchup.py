"""Restart race (0.5.1 shard flake): a node's own announce and a supplier's
late announce must not keep it from pulling a space it holds."""

from __future__ import annotations

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

from .test_orgfs_rebuild_catchup import (
    _Bus,
    _Presence,
    _device,
    _device_ids,
    _eventually,
    _expect_devices,
    _joined_pair,
    assert_directory_drained,
)



def _scheduled_syncs(reader, space, monkeypatch) -> list[str]:
    scheduled: list[str] = []
    monkeypatch.setattr(
        reader._meshes[space.space_id],
        "schedule_sync_from",
        lambda peer: scheduled.append(peer) or True,
    )
    return scheduled


def test_own_holding_does_not_hide_a_space_from_an_online_supplier(
    tmp_path, monkeypatch
) -> None:
    """Restart race (0.5.1 shard flake): the node's own announce made holders
    {self}, so the supplier's online edge skipped the space and never pulled it."""

    _bus, owner, reader, _session, presence, space = _joined_pair(tmp_path)
    try:
        scheduled = _scheduled_syncs(reader, space, monkeypatch)
        with reader._pending_announces_lock:
            reader._holders[space.space_id] = {"node-reader"}

        presence.emit_online("node-owner")
        _eventually(lambda: assert_directory_drained(reader))

        assert scheduled == ["node-owner"]
    finally:
        reader.close()
        owner.close()


def test_announce_after_the_online_edge_pulls_the_newly_held_space(
    tmp_path, monkeypatch
) -> None:
    """The supplier's online edge ran while another peer was the only known
    holder; its own announce lands afterwards and must start the catch-up."""

    _bus, owner, reader, _session, presence, space = _joined_pair(tmp_path)
    try:
        scheduled = _scheduled_syncs(reader, space, monkeypatch)
        with reader._pending_announces_lock:
            reader._holders[space.space_id] = {"node-other"}
            reader._lively_peers.add("node-owner")

        presence.emit_online("node-owner")
        _eventually(lambda: assert_directory_drained(reader))
        assert scheduled == []  # the edge alone cannot know node-owner holds it

        owner._mesh(space.space_id).announce()  # through the real bus
        _eventually(lambda: assert_directory_drained(reader))

        assert scheduled == ["node-owner"]
    finally:
        reader.close()
        owner.close()


def test_four_member_sequential_join_converges_by_announce_catch_up_without_f2(
    tmp_path, monkeypatch
) -> None:
    """New path: the later joiners' online edges reach b while only alice is
    a known holder (skipped); their announces then start the catch-up. No
    anti-entropy tick runs."""

    bus = _Bus()
    nodes = ("alice", "bob", "carol", "dave")
    runtimes = {
        name: OrgFsRuntime(
            tmp_path / name, node_id=f"node-{name}", author=f"user:{name}@example",
            trust_tailnet_members=True,  # Holder catch-up after private admission (§7).
        )
        for name in nodes
    }
    bob_session = bus.session("node-bob")
    presence = _Presence(bob_session)
    presence.online = {"node-alice"}
    try:
        for name, runtime in runtimes.items():
            if name == "bob":
                runtime.bind_transport(
                    bob_session,
                    supplier_online=presence.supplier_online,
                    holder_candidates=lambda: ("node-alice",),
                    observe_supplier_online=presence.observe,
                )
                continue
            runtime.bind_transport(
                bus.session(f"node-{name}"),
                supplier_online=lambda _peer: True,
                holder_candidates=(
                    (lambda: ()) if name == "alice" else (lambda: ("node-alice",))
                ),
            )
        alice, bob = runtimes["alice"], runtimes["bob"]
        space = alice.facade.create_space("group-late")
        OrgFsDirectoryStore(alice.facade).ensure_org_directory_roots("group-late")
        OrgFsDirectoryStore(alice.facade).put_device(
            "group-late", _device("alice@example", "node-alice")
        )
        alice._mesh(space.space_id).announce()
        alice.facade.invite(space.space_id, "user:bob@example", "rw")
        bob.facade.join(space.space_id)
        OrgFsDirectoryStore(bob.facade).put_device(
            "group-late", _device("bob@example", "node-bob")
        )
        for name in ("carol", "dave"):
            bus.blocked_updates.add((f"node-{name}", "node-bob"))
            alice.facade.invite(space.space_id, f"user:{name}@example", "rw")
            runtimes[name].facade.join(space.space_id)
            OrgFsDirectoryStore(runtimes[name].facade).put_device(
                "group-late", _device(f"{name}@example", f"node-{name}")
            )
        _eventually(lambda: assert_directory_drained(bob))
        assert _device_ids(bob) == ["node-alice", "node-bob"]

        pulled: list[str] = []
        mesh = bob._meshes[space.space_id]
        original_sync = mesh.sync_from

        def observed_sync(peer, *args, **kwargs):
            pulled.append(peer)
            return original_sync(peer, *args, **kwargs)

        monkeypatch.setattr(mesh, "sync_from", observed_sync)
        for name in ("carol", "dave"):  # online edges before their announces
            presence.online.add(f"node-{name}")
            presence.emit_online(f"node-{name}")
        _eventually(lambda: assert_directory_drained(bob))
        assert pulled == []

        for name in ("carol", "dave"):
            bus.blocked_updates.discard((f"node-{name}", "node-bob"))
            runtimes[name]._mesh(space.space_id).announce()
        everyone = ["node-alice", "node-bob", "node-carol", "node-dave"]
        _eventually(lambda: _expect_devices(bob, everyone))
        assert set(pulled) <= {"node-carol", "node-dave"} and pulled
    finally:
        for runtime in reversed(list(runtimes.values())):
            runtime.close()
