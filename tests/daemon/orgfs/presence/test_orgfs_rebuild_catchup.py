# Decisions:
# - Phase D, package T2: the tests that read the journal store or the legacy
#   mesh (``stores[...]``, ``unbroadcast``, ``_meshes``, a monkeypatched
#   ``_mesh``) run on ``OrgFsRuntime`` through ``state_peers``
#   (``mesh/replication/state_support.py``) and assert only reads, device rows,
#   logged events and errors.  The other tests here construct ``OrgFsRuntime``
#   and pass under the rehearsal switch; package S points that name at the new
#   runtime.
# - A lost broadcast is no longer parked per writer: the next broadcast that
#   depends on it is ``Pending("dependency")`` and pulls from its sender
#   (design §4.2), so the old "parks until an explicit sync" test became
#   ``test_one_lost_commit_is_pulled_when_a_later_commit_arrives``.
# - ``test_parked_commits_are_reported_until_the_gap_is_pulled`` is deleted:
#   ``parkedCommits`` is removed by design §10 (diagnostic moves to
#   ``status.rejected_updates``, which counts admission rejections, not frames
#   dropped by presence); the recovery it also checked is
#   ``test_supplier_online_edge_applies_buffered_announce_before_sync``.
# - Waits for a dropped broadcast count ``StateNetwork.dropped`` rather than
#   calling ``drain_broadcasts``: that facade call is the close-time drain and
#   leaves the network lane in drain mode, so later writes are not published
#   until the next drain.
# - The rejected-supplier event of protocol 3 names the sender as ``peer``
#   (``orgfs.update.rejected`` with ``reason=supplier-offline``).
# - Package S: the bus's ``drop_updates`` hole drops protocol 3 update puts
#   (it named the deleted journal ``/log/`` keys) and counts them in ``dropped``;
#   only the conversations two-node probes use it.
# - Package S: ``anti_entropy_tick`` on the state runtime first drains the
#   verification backlog (F1 round 2) and then runs the due tick; a broken
#   ``spaces()`` makes both log ``orgfs.anti_entropy.failed``, so the test
#   asserts every failure is a warning naming the cause, not a count of one.
"""Deterministic OrgFS catch-up after transport callback replay completes."""

from __future__ import annotations

from fnmatch import fnmatch
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.composition import directory as directory_composition
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import TransportSample
from hyprial.daemon.impl.transport.presence_actor import ActorOnlineTransition
from hyprial.identity.impl.org_directory import DirectoryDevice
from tests.daemon.orgfs.mesh.replication.state_support import (
    StateNetwork,
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import Peer, eventually, holds, text, tree


_DIRECTORY_SYNC_INTERVAL_SECONDS = 60.0


class _Registration:
    def __init__(self, close) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class _Bus:
    """Synchronous mesh I/O with a controllable fire-and-forget update hole."""

    def __init__(self) -> None:
        self.subscribers = []
        self.handlers = []
        self.drop_updates = False
        self.dropped = 0
        self.blocked_updates: set[tuple[str, str]] = set()

    def session(self, node: str = "") -> "_Session":
        return _Session(self, node)


class _Session:
    def __init__(self, bus: _Bus, node: str = "") -> None:
        self.bus = bus
        self.node = node
        self.callbacks_complete = True
        self._completion_hooks = []

    def projection(self):
        return SimpleNamespace(callbacks_complete=self.callbacks_complete, closed=False)

    def put(self, key, payload):
        if self.bus.drop_updates and "/org/fs3/update/" in key:
            self.bus.dropped += 1
            return
        for target, expression, callback in tuple(self.bus.subscribers):
            if (self.node, target) in self.bus.blocked_updates:
                continue
            if fnmatch(key, expression.replace("**", "*")):
                callback(TransportSample(key, payload))

    def subscribe(self, key_expr, callback):
        row = (self.node, key_expr, callback)
        self.bus.subscribers.append(row)
        return _Registration(
            lambda: self.bus.subscribers.remove(row)
            if row in self.bus.subscribers
            else None
        )

    def declare_query_handler(self, key_expr, handler):
        row = (self.node, key_expr, handler)
        self.bus.handlers.append(row)
        return _Registration(
            lambda: self.bus.handlers.remove(row) if row in self.bus.handlers else None
        )

    def query(self, key_expr, payload, **_kwargs):
        replies = []
        for _target, expression, handler in tuple(self.bus.handlers):
            if fnmatch(key_expr, expression.replace("**", "*")):
                replies.extend(
                    TransportSample(key, body)
                    for key, body in handler(key_expr, payload)
                )
        return replies

    def on_rebuild_complete(self, callback):
        self._completion_hooks.append(callback)
        return lambda: self._completion_hooks.remove(callback)

    def begin_rebuild(self) -> None:
        self.callbacks_complete = False

    def finish_rebuild(self) -> None:
        self.callbacks_complete = True
        for callback in tuple(self._completion_hooks):
            callback()


class _Presence:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.online = {"node-owner"}
        self.observers = []
        self.sequence = 0

    def supplier_online(self, peer: str) -> bool:
        return self.session.callbacks_complete and peer in self.online

    def observe(self, callback):
        self.observers.append(callback)
        return lambda: self.observers.remove(callback)

    def emit_online(self, peer: str) -> None:
        self.sequence += 1
        transition = ActorOnlineTransition(1, peer, self.sequence)
        for callback in tuple(self.observers):
            callback(transition)


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch):
    yield from open_state_peers(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _ticks_to_converge(runtimes, clock: float, converged, *, bound: int) -> int | None:
    """Count rounds to convergence; each runtime must schedule exactly one pull."""

    for tick in range(1, bound + 1):
        for runtime in runtimes:
            assert runtime.anti_entropy_tick(
                clock + tick * _DIRECTORY_SYNC_INTERVAL_SECONDS
            ) == 1
        try:
            _eventually(converged, timeout=1.0)
        except (AssertionError, OrgFsError):
            continue
        return tick
    return None


def _eventually(action, *, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.005)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_text(runtime, space_id: str, node_id: str, expected: str) -> None:
    assert runtime.facade.read_text(space_id, f"id:{node_id}")[0] == expected


def _joined_pair(tmp_path, *, events=None):
    bus = _Bus()
    owner_session = bus.session()
    reader_session = bus.session()
    presence = _Presence(reader_session)
    # Catch-up fixture models private sidecar admission, not directory enrollment (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-reader",
        author="user:reader",
        trust_tailnet_members=True,
        logger=(
            None
            if events is None
            else lambda level, event, **fields: events.append((level, event, fields))
        ),
    )
    owner.bind_transport(owner_session, supplier_online=lambda _peer: True)
    reader.bind_transport(
        reader_session,
        supplier_online=presence.supplier_online,
        holder_candidates=lambda: ("node-owner",),
        observe_supplier_online=presence.observe,
    )
    space = owner.facade.create_space("rebuild catch-up")
    owner.facade.invite(space.space_id, "user:reader", "rw")
    owner._mesh(space.space_id).announce()
    reader.facade.join(space.space_id)
    return bus, owner, reader, reader_session, presence, space


def test_transition_before_complete_recovers_dropped_broadcast(state_peers: StatePeers) -> None:
    """A: an online edge seen during a rebuild is retained and pulls after it completes."""

    network = state_peers.network
    owner = state_peers.add("node-owner", "user:owner")
    reader = state_peers.add("node-reader", "user:reader")
    space_id = shared_space(state_peers, owner, reader)
    node = owner.fs.write_text(space_id, "shared.md", "before")
    eventually(lambda: _assert_state_text(reader, space_id, node.node_id, "before"))

    network.drop_updates = True
    owner.fs.write_text(space_id, f"id:{node.node_id}", "after")
    eventually(lambda: _assert_dropped(network, 2))
    network.begin_rebuild("node-reader")
    for _ in range(5):
        network.emit_online(observer="node-reader", peer="node-owner")
    holds(lambda: _assert_state_text(reader, space_id, node.node_id, "before"), duration=0.3)

    network.finish_rebuild("node-reader")
    eventually(lambda: _assert_state_text(reader, space_id, node.node_id, "after"))


def test_one_lost_commit_is_pulled_when_a_later_commit_arrives(state_peers: StatePeers) -> None:
    """A later broadcast that depends on a lost one makes the reader pull the gap.

    Protocol 3 replaces the journal's per-writer parking: an update whose
    predecessor is missing is ``Pending("dependency")`` and schedules a pull
    from its sender, so the reader reaches the last state without an explicit
    sync and never shows a state the writer did not have.
    """

    network = state_peers.network
    owner = state_peers.add("node-owner", "user:owner")
    reader = state_peers.add("node-reader", "user:reader")
    space_id = shared_space(state_peers, owner, reader)
    node = owner.fs.write_text(space_id, "chain.md", "v0")
    eventually(lambda: _assert_state_text(reader, space_id, node.node_id, "v0"))

    network.drop_updates = True
    owner.fs.write_text(space_id, f"id:{node.node_id}", "v1")
    eventually(lambda: _assert_dropped(network, 2))
    network.drop_updates = False
    holds(lambda: _assert_state_text(reader, space_id, node.node_id, "v0"), duration=0.2)
    owner.fs.write_text(space_id, f"id:{node.node_id}", "v2")
    owner.fs.write_text(space_id, f"id:{node.node_id}", "v3")

    eventually(lambda: _assert_state_text(reader, space_id, node.node_id, "v3"))
    assert tree(reader.fs, space_id) == tree(owner.fs, space_id)


def _assert_dropped(network: StateNetwork, count: int) -> None:
    assert network.dropped >= count


def _assert_state_text(peer: Peer, space_id: str, node_id: str, expected: str) -> None:
    assert text(peer.fs, space_id, f"id:{node_id}") == expected


def test_offline_deferred_supplier_survives_rebuild_drain(tmp_path) -> None:
    reader = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader"
    )
    try:
        reader._supplier_online = lambda _peer: False
        reader._defer_supplier_online("node-owner")

        reader._post_rebuild_effects()

        assert reader._deferred_supplier_online == {"node-owner"}
    finally:
        reader.close()


def test_deferred_supplier_rechecks_on_later_online_transition(
    tmp_path, monkeypatch
) -> None:
    """F1: a post-rebuild online edge drains the retained supplier exactly once."""

    _bus, owner, reader, session, presence, space = _joined_pair(tmp_path)
    scheduled: list[str] = []
    try:
        mesh = reader._meshes[space.space_id]
        monkeypatch.setattr(
            mesh,
            "schedule_sync_from",
            lambda peer: scheduled.append(peer) or True,
        )
        session.callbacks_complete = False
        presence.emit_online("node-owner")
        _eventually(lambda: assert_directory_drained(reader))
        assert reader._deferred_supplier_online == {"node-owner"}

        session.callbacks_complete = True
        presence.emit_online("node-owner")
        _eventually(lambda: assert_directory_drained(reader))

        assert reader._deferred_supplier_online == set()
        assert scheduled == ["node-owner"]
    finally:
        reader.close()
        owner.close()


def _device(owner: str, node: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=node,
        owner=owner,
        server_public=f"nodekey:server-{node}",
        client_public=f"nodekey:client-{node}",
        address=f"tcp://127.0.0.1:7447#{node}",
        key_generation=1,
        updated_at="2026-10-08T00:00:00+00:00",
    )


def _device_ids(runtime: OrgFsRuntime) -> list[str]:
    return [
        row.device_id
        for row in OrgFsDirectoryStore(runtime.facade).list_devices("group-late")
    ]


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_directory_anti_entropy_closes_three_member_late_join_gap(
    tmp_path, monkeypatch, offset
) -> None:
    """F2: Bob learns Carol's author-only row from Alice within one fake interval."""

    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
    monkeypatch.setattr(directory_composition.random, "randrange", lambda _stop: offset)
    bus = _Bus()
    alice_session = bus.session("node-alice")
    bob_session = bus.session("node-bob")
    carol_session = bus.session("node-carol")
    # Directory convergence follows private sidecar admission (§7), not vice versa.
    alice = OrgFsRuntime(
        tmp_path / "alice", node_id="node-alice", author="user:alice@example", trust_tailnet_members=True
    )
    bob = OrgFsRuntime(
        tmp_path / "bob", node_id="node-bob", author="user:bob@example", trust_tailnet_members=True
    )
    carol = OrgFsRuntime(
        tmp_path / "carol", node_id="node-carol", author="user:carol@example", trust_tailnet_members=True
    )
    runtimes = (alice, bob, carol)
    try:
        alice.bind_transport(alice_session, supplier_online=lambda _peer: True)
        bob.bind_transport(
            bob_session,
            supplier_online=lambda peer: peer == "node-alice",
            holder_candidates=lambda: ("node-alice",),
        )
        carol.bind_transport(
            carol_session,
            supplier_online=lambda _peer: True,
            holder_candidates=lambda: ("node-alice",),
        )
        alice_store = OrgFsDirectoryStore(alice.facade)
        space = alice.facade.create_space("group-late")
        alice_store.ensure_org_directory_roots("group-late")
        alice.facade.invite(space.space_id, "user:bob@example", "rw")
        alice.facade.invite(space.space_id, "user:carol@example", "rw")
        alice._mesh(space.space_id).announce()
        bob.facade.join(space.space_id)
        carol.facade.join(space.space_id)

        alice_store.put_device("group-late", _device("alice@example", "node-alice"))
        OrgFsDirectoryStore(bob.facade).put_device(
            "group-late", _device("bob@example", "node-bob")
        )
        expected_pair = ["node-alice", "node-bob"]
        for runtime in (alice, bob, carol):
            _eventually(lambda runtime=runtime: _expect_devices(runtime, expected_pair))

        bus.blocked_updates.add(("node-carol", "node-bob"))
        OrgFsDirectoryStore(carol.facade).put_device(
            "group-late", _device("carol@example", "node-carol")
        )
        expected_all = ["node-alice", "node-bob", "node-carol"]
        for runtime in (alice, carol):
            _eventually(lambda runtime=runtime: _expect_devices(runtime, expected_all))
        assert _device_ids(bob) == expected_pair

        mesh = bob._meshes[space.space_id]
        original_sync = mesh.sync_from
        synced = threading.Event()

        def observed_sync(*args, **kwargs):
            try:
                return original_sync(*args, **kwargs)
            finally:
                synced.set()

        monkeypatch.setattr(mesh, "sync_from", observed_sync)
        clock = 100.0
        assert bob.anti_entropy_tick(clock) == 0
        assert bob.anti_entropy_tick(
            clock + _DIRECTORY_SYNC_INTERVAL_SECONDS - 0.001
        ) == 0
        assert _device_ids(bob) == ["node-alice", "node-bob"]

        # Bob's only online holder is Alice: one round, whatever the offset.
        ticks = _ticks_to_converge(
            (bob,),
            clock,
            lambda: _expect_devices(bob, ["node-alice", "node-bob", "node-carol"]),
            bound=1,
        )
        assert synced.wait(2.0), "bounded sync worker did not settle"
        assert ticks == 1
    finally:
        for runtime in reversed(runtimes):
            runtime.close()


def test_directory_anti_entropy_deadline_is_jittered(tmp_path, monkeypatch) -> None:
    """F2: members that start together do not pull in lockstep."""

    runtime = OrgFsRuntime(
        tmp_path / "alice", node_id="node-alice", author="user:alice@example"
    )
    try:
        for spread, expected in ((0.2, 172.0), (-0.2, 148.0)):
            monkeypatch.setattr(
                directory_composition, "_directory_sync_jitter", lambda s=spread: s
            )
            runtime._directory_sync_due = None
            assert runtime.anti_entropy_tick(100.0) == 0
            assert runtime._directory_sync_due == pytest.approx(expected)
        monkeypatch.undo()
        dues = {
            directory_composition._next_directory_sync_due(100.0) for _ in range(200)
        }
        assert all(148.0 <= due <= 172.0 for due in dues)
        assert len(dues) > 1
    finally:
        runtime.close()


def _expect_devices(runtime: OrgFsRuntime, expected: list[str]) -> None:
    assert _device_ids(runtime) == expected


def _state_three_members(state_peers: StatePeers) -> tuple[Peer, Peer, Peer, str]:
    """Alice (creator) has not seen Carol online yet; Carol writes her device row."""

    network = state_peers.network
    alice = state_peers.add("node-alice", "user:alice@example")
    bob = state_peers.add("node-bob", "user:bob@example")
    carol = state_peers.add("node-carol", "user:carol@example")
    network.hide("node-alice", "node-carol")
    space_id = shared_space(state_peers, alice, bob, carol, name="group-late")
    OrgFsDirectoryStore(alice.fs).ensure_org_directory_roots("group-late")
    OrgFsDirectoryStore(alice.fs).put_device("group-late", _device("alice@example", "node-alice"))
    eventually(lambda: _expect_state_devices(carol, ["node-alice"]))
    OrgFsDirectoryStore(carol.fs).put_device("group-late", _device("carol@example", "node-carol"))
    eventually(lambda: _expect_state_devices(bob, ["node-alice", "node-carol"]))
    return alice, bob, carol, space_id


def _state_device_ids(peer: Peer) -> list[str]:
    return [row.device_id for row in OrgFsDirectoryStore(peer.fs).list_devices("group-late")]


def _expect_state_devices(peer: Peer, expected: list[str]) -> None:
    assert _state_device_ids(peer) == expected


def _offline_rejections(peer: Peer) -> set[str]:
    return {
        str(fields.get("peer"))
        for event, fields in peer.events.seen
        if event == "orgfs.update.rejected" and fields.get("reason") == "supplier-offline"
    }


def test_supplier_online_edge_applies_buffered_announce_before_sync(state_peers: StatePeers) -> None:
    """F1b: the race from the field (cr-old-3) heals on Carol's online edge, no tick."""

    alice, _bob, carol, _space_id = _state_three_members(state_peers)
    assert "node-carol" in _offline_rejections(alice)
    holds(lambda: _expect_state_devices(alice, ["node-alice"]), duration=0.2)

    carol.runtime._mesh(_space_id).announce()  # noqa: SLF001 - Carol's held-space announce
    state_peers.network.show("node-alice", "node-carol")

    eventually(lambda: _expect_state_devices(alice, ["node-alice", "node-carol"]))


def test_creator_restart_recovers_rejected_row_by_anti_entropy(
    state_peers: StatePeers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: Alice rejected Carol's commit, restarted, and saw no online edge since.

    This is the field shape (cr-old-3): the restart does not lose the row, the
    row was never stored; only the periodic pull brings it back.
    """

    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
    alice, _bob, carol, space_id = _state_three_members(state_peers)
    assert "node-carol" in _offline_rejections(alice)

    alice.reopen()
    state_peers.network.show("node-alice", "node-carol", edge=False)
    carol.runtime._mesh(space_id).announce()  # noqa: SLF001 - Carol's held-space announce
    holds(lambda: _expect_state_devices(alice, ["node-alice"]), duration=0.2)

    runtime = alice.runtime
    assert runtime is not None
    clock = 100.0
    assert runtime.anti_entropy_tick(clock) == 0
    assert runtime.anti_entropy_tick(clock + _DIRECTORY_SYNC_INTERVAL_SECONDS) >= 1
    eventually(lambda: _expect_state_devices(alice, ["node-alice", "node-carol"]))


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_four_member_sequential_join_earliest_joiner_converges_without_restart(
    tmp_path, monkeypatch, offset
) -> None:
    """F2: a creates, then b, c, d join in turn; b learns c and d within one interval.

    The later joiners' author-only broadcasts never reach b (the field run had
    no rejection on b either); nobody restarts.
    """

    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
    monkeypatch.setattr(directory_composition.random, "randrange", lambda _stop: offset)
    bus = _Bus()
    nodes = ("alice", "bob", "carol", "dave")
    # Sequential cold joins on the private sidecar-admitted path (§7).
    runtimes = {
        name: OrgFsRuntime(
            tmp_path / name, node_id=f"node-{name}", author=f"user:{name}@example", trust_tailnet_members=True
        )
        for name in nodes
    }
    try:
        for name, runtime in runtimes.items():
            runtime.bind_transport(
                bus.session(f"node-{name}"),
                supplier_online=lambda _peer: True,
                holder_candidates=(
                    (lambda: ()) if name == "alice" else (lambda: ("node-alice",))
                ),
            )
        alice = runtimes["alice"]
        space = alice.facade.create_space("group-late")
        OrgFsDirectoryStore(alice.facade).ensure_org_directory_roots("group-late")
        OrgFsDirectoryStore(alice.facade).put_device(
            "group-late", _device("alice@example", "node-alice")
        )
        alice._mesh(space.space_id).announce()
        for name in ("bob", "carol", "dave"):
            alice.facade.invite(space.space_id, f"user:{name}@example", "rw")
            runtimes[name].facade.join(space.space_id)
            if name != "bob":
                bus.blocked_updates.add((f"node-{name}", "node-bob"))
            OrgFsDirectoryStore(runtimes[name].facade).put_device(
                "group-late", _device(f"{name}@example", f"node-{name}")
            )
        everyone = ["node-alice", "node-bob", "node-carol", "node-dave"]
        _eventually(lambda: _expect_devices(alice, everyone))
        assert _device_ids(runtimes["bob"]) == ["node-alice", "node-bob"]

        bob = runtimes["bob"]
        mesh = bob._meshes[space.space_id]
        original_sync = mesh.sync_from
        synced = threading.Event()

        def observed_sync(*args, **kwargs):
            try:
                return original_sync(*args, **kwargs)
            finally:
                synced.set()

        monkeypatch.setattr(mesh, "sync_from", observed_sync)
        clock = 100.0
        assert bob.anti_entropy_tick(clock) == 0
        # Measured from every starting offset: within k = 3 online holders.
        ticks = _ticks_to_converge(
            (bob,), clock, lambda: _expect_devices(bob, everyone), bound=3
        )
        assert synced.wait(2.0), "bounded sync worker did not settle"
        assert ticks is not None and ticks <= 3, ticks
    finally:
        for runtime in reversed(list(runtimes.values())):
            runtime.close()


@pytest.mark.parametrize("offset", [0, 1])
def test_concurrent_joiners_converge_on_each_other_by_anti_entropy(
    tmp_path, monkeypatch, offset
) -> None:
    """F2: concurrent joiners miss each other's broadcast; periodic pulls converge."""

    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
    monkeypatch.setattr(directory_composition.random, "randrange", lambda _stop: offset)
    bus = _Bus()
    nodes = ("alice", "bob", "carol")
    # Concurrent cold joins on the private sidecar-admitted path (§7).
    runtimes = {
        name: OrgFsRuntime(
            tmp_path / name, node_id=f"node-{name}", author=f"user:{name}@example", trust_tailnet_members=True
        )
        for name in nodes
    }
    try:
        for name, runtime in runtimes.items():
            runtime.bind_transport(
                bus.session(f"node-{name}"),
                supplier_online=lambda _peer: True,
                holder_candidates=(
                    (lambda: ()) if name == "alice" else (lambda: ("node-alice",))
                ),
            )
        alice, bob, carol = (runtimes[name] for name in nodes)
        space = alice.facade.create_space("group-late")
        OrgFsDirectoryStore(alice.facade).ensure_org_directory_roots("group-late")
        OrgFsDirectoryStore(alice.facade).put_device(
            "group-late", _device("alice@example", "node-alice")
        )
        alice.facade.invite(space.space_id, "user:bob@example", "rw")
        alice.facade.invite(space.space_id, "user:carol@example", "rw")
        alice._mesh(space.space_id).announce()
        bob.facade.join(space.space_id)
        carol.facade.join(space.space_id)
        for runtime in (bob, carol):
            runtime._mesh(space.space_id).announce()
        bus.blocked_updates.update(
            {("node-bob", "node-carol"), ("node-carol", "node-bob")}
        )
        OrgFsDirectoryStore(bob.facade).put_device(
            "group-late", _device("bob@example", "node-bob")
        )
        OrgFsDirectoryStore(carol.facade).put_device(
            "group-late", _device("carol@example", "node-carol")
        )
        everyone = ["node-alice", "node-bob", "node-carol"]
        _eventually(lambda: _expect_devices(alice, everyone))
        assert _device_ids(bob) == ["node-alice", "node-bob"]
        assert _device_ids(carol) == ["node-alice", "node-carol"]

        clock = 100.0
        for runtime in (bob, carol):
            assert runtime.anti_entropy_tick(clock) == 0
        # Measured from every starting offset: within k = 2 online holders.
        ticks = _ticks_to_converge(
            (bob, carol),
            clock,
            lambda: (
                _expect_devices(bob, everyone),
                _expect_devices(carol, everyone),
            ),
            bound=2,
        )
        assert ticks is not None and ticks <= 2, ticks
    finally:
        for runtime in reversed(list(runtimes.values())):
            runtime.close()


def test_join_against_an_offline_supplier_carries_the_typed_reason(tmp_path) -> None:
    """F3's producer: the retry keys on details.reason, never on the message text."""

    bus = _Bus()
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-reader", author="user:reader")
    try:
        owner.bind_transport(bus.session("node-owner"), supplier_online=lambda _peer: True)
        reader.bind_transport(
            bus.session("node-reader"),
            supplier_online=lambda _peer: False,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("offline supplier")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()

        with pytest.raises(OrgFsError) as refused:
            reader.facade.join(space.space_id)

        assert refused.value.code == "not-a-member"
        assert refused.value.details.get("reason") == "supplier-not-online"
    finally:
        reader.close()
        owner.close()


def test_anti_entropy_tick_never_raises(tmp_path, monkeypatch) -> None:
    """Maintenance runs adapter phases after the tick, so a failure is logged as 0."""

    events: list = []
    runtime = OrgFsRuntime(
        tmp_path / "alice",
        node_id="node-alice",
        author="user:alice@example",
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    try:
        monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
        assert runtime.anti_entropy_tick(100.0) == 0

        def broken_spaces():
            raise RuntimeError("fixture spaces failure")

        monkeypatch.setattr(runtime.facade, "spaces", broken_spaces)
        assert runtime.anti_entropy_tick(100.0 + _DIRECTORY_SYNC_INTERVAL_SECONDS) == 0
        failures = [row for row in events if row[1] == "orgfs.anti_entropy.failed"]
        assert failures and {level for level, _event, _fields in failures} == {"warn"}
        assert all("fixture spaces failure" in row[2]["error"] for row in failures)
    finally:
        runtime.close()


def test_join_error_message_has_one_code_prefix(state_peers: StatePeers) -> None:
    """F3: the join error names its code once and keeps a human-readable message."""

    owner = state_peers.add("node-owner", "user:owner")
    reader = state_peers.add("node-reader", "user:reader")
    space = owner.fs.create_space("offline supplier")
    owner.fs.invite(space.space_id, "user:reader", "rw")
    state_peers.network.hide("node-reader", "node-owner")

    with pytest.raises(OrgFsError) as raised:
        reader.fs.join(space.space_id)

    assert raised.value.code == "not-a-member"
    assert raised.value.details["reason"] == "supplier-not-online"
    assert str(raised.value).count("not-a-member") == 1, str(raised.value)


def test_successful_sync_logs_one_summary_line(tmp_path) -> None:
    """F4: one summary per call, never per commit; a no-op pull is only debug."""

    events: list[tuple[str, str, dict[str, object]]] = []
    bus, owner, reader, _session, _presence, space = _joined_pair(
        tmp_path, events=events
    )

    def assert_summary(imported: int, level: str) -> None:
        rows = [row for row in events if row[1] == "orgfs.sync.completed"]
        assert len(rows) == 1
        actual_level, event, fields = rows[0]
        assert (actual_level, event) == (level, "orgfs.sync.completed")
        assert {
            "spaceId": fields["spaceId"],
            "supplier": fields["supplier"],
            "importedCount": fields["importedCount"],
        } == {
            "spaceId": space.space_id,
            "supplier": "node-owner",
            "importedCount": imported,
        }
        assert all(
            isinstance(fields[name], int)
            for name in ("pages", "requestBytes", "responseBytes", "elapsedMs")
        )

    try:
        mesh = reader._meshes[space.space_id]
        mesh.sync_from("node-owner")
        events.clear()
        assert mesh.sync_from("node-owner") == 0
        assert_summary(0, "debug")

        bus.drop_updates = True
        owner.facade.write_text(space.space_id, "missed.txt", "pulled later")
        bus.drop_updates = False
        events.clear()
        imported = mesh.sync_from("node-owner")
        assert imported > 0
        assert_summary(imported, "info")
    finally:
        reader.close()
        owner.close()


def test_rebuild_catchup_is_deduplicated_and_logged_once(tmp_path, monkeypatch) -> None:
    """Presence edges deferred by one rebuild schedule one pair; every rebuild logs once."""

    events = []
    _bus, owner, reader, session, presence, space = _joined_pair(
        tmp_path, events=events
    )
    try:
        mesh = reader._meshes[space.space_id]
        scheduled = []
        monkeypatch.setattr(
            mesh,
            "schedule_sync_from",
            lambda peer: scheduled.append((space.space_id, peer)) or True,
        )

        session.begin_rebuild()
        for _ in range(8):
            presence.emit_online("node-owner")
        _eventually(lambda: assert_directory_drained(reader))
        session.finish_rebuild()
        session.begin_rebuild()
        session.finish_rebuild()
        session.begin_rebuild()
        session.finish_rebuild()

        _eventually(
            lambda: assert_rebuild_evidence(events, scheduled, space.space_id)
        )
    finally:
        reader.close()
        owner.close()


def assert_directory_drained(runtime: OrgFsRuntime) -> None:
    assert runtime.directory_ingress_pending == 0


def assert_rebuild_evidence(events, scheduled, space_id: str) -> None:
    rows = [row for row in events if row[1] == "orgfs.sync.post-rebuild"]
    assert scheduled == [(space_id, "node-owner")]
    assert len(rows) == 3
    assert all(level == "info" for level, _event, _fields in rows)
    assert all("residentSpaces" not in fields for _level, _event, fields in rows)
    assert all("onlineHolderPairs" not in fields for _level, _event, fields in rows)
    assert [fields["syncTargets"] for _level, _event, fields in rows] == [1, 0, 0]
    assert [fields["scheduledCount"] for _level, _event, fields in rows] == [1, 0, 0]
