# Decisions:
# - Phase D, package T2: the four tests that failed on the state runtime are
#   rewritten onto ``OrgFsRuntime``:
#   * attribution over Zenoh is read where a user sees it (``stat``'s
#     ``modified_by`` and the ``modified`` events of ``changes_since``), not
#     from journal envelope origins;
#   * "a reader never observes a partial apply" gates the receiver's state
#     store commit inside ``SyncHost.apply_remote`` (the remote path that
#     replaced ``apply_envelope``); the read waits and then sees the whole
#     update, as before;
#   * the announce-during-liveliness-flip test (triage "pending", decided:
#     keep) pins ``RuntimeDirectory``, which the state runtime composes
#     unchanged; its announces carry ``protocol: 3``.
# - ``test_text_write_respects_membership_gate`` is deleted: contract
#   ``members/test_members.py::test_read_only_member_local_writes_are_refused``
#   covers a read-only member's refused write leaving the document unchanged,
#   on both runtimes and for six write kinds (C4 report §5).
# - The anti-entropy and agent-liveliness tests pass on both runtimes and
#   keep ``OrgFsRuntime`` (package S points the name at the new runtime).
from __future__ import annotations

from collections.abc import Iterator
import json
import socket
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample, ZenohConfig, ZenohTransport
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import text


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _equal(actual, expected):
    assert actual == expected


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def _modified(runtime: OrgFsRuntime, space_id: str, node_id: str) -> set[tuple[str | None, str | None]]:
    return {
        (event.node.modified_by, event.node.modified_via)
        for event in runtime.facade.changes_since(space_id)
        if event.node.node_id == node_id and event.kind == "modified"
    }


def test_text_writes_replicate_with_attribution_over_zenoh(tmp_path) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True)
    try:
        time.sleep(0.3)
        online = {"node-owner", "node-peer"}.__contains__
        owner.bind_transport(owner_transport, supplier_online=online)
        peer.bind_transport(
            peer_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("kanban")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        _eventually(lambda: peer.facade.join(space.space_id))
        node = owner.facade.write_text(space.space_id, "board.json", "")
        ref = f"id:{node.node_id}"

        def converged():
            _equal(
                owner.facade.read_text(space.space_id, ref)[0],
                peer.facade.read_text(space.space_id, ref)[0],
            )

        _eventually(converged)
        owner.facade.write_text(space.space_id, ref, "owner")
        _eventually(lambda: _equal(peer.facade.read_text(space.space_id, ref)[0], "owner"))
        assert peer.facade.stat(space.space_id, ref).modified_by == "user:owner"
        peer.facade.write_text(space.space_id, ref, "owner peer")
        _eventually(converged)
        assert owner.facade.read_text(space.space_id, ref)[0] == "owner peer"
        for runtime in (owner, peer):
            assert runtime.facade.stat(space.space_id, ref).modified_by == "user:peer"
            assert {("user:owner", None), ("user:peer", None)} <= _modified(
                runtime, space.space_id, node.node_id
            )
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_reader_never_observes_partial_apply(state_peers: StatePeers) -> None:
    state_peers.network.drop_updates = True
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer)
    node = owner.fs.write_text(space_id, "doc.md", "old")
    state_peers.pull_all(space_id)
    state_peers.pull_all(space_id)
    assert text(peer.fs, space_id, f"id:{node.node_id}") == "old"
    owner.fs.write_text(space_id, f"id:{node.node_id}", "complete")
    peer_host = peer.fs.sync_host(space_id)
    found = owner.fs.sync_host(space_id).read_update(
        node.doc_id, peer_host.read_vectors((node.doc_id,))[node.doc_id]
    )
    assert found is not None
    assert peer.runtime is not None
    store = peer.runtime.stores.get(space_id)
    assert store is not None
    original_commit = store.commit
    halfway, release, read_done = threading.Event(), threading.Event(), threading.Event()
    seen: list[str] = []

    def gated_commit(*args, **kwargs):
        halfway.set()
        assert release.wait(3)
        return original_commit(*args, **kwargs)

    store.commit = gated_commit
    apply_thread = threading.Thread(
        target=peer_host.apply_remote, args=(node.doc_id, found[0], found[1], "node-owner")
    )
    apply_thread.start()
    try:
        assert halfway.wait(3)

        def read() -> None:
            seen.append(peer.fs.read_text(space_id, f"id:{node.node_id}")[0])
            read_done.set()

        read_thread = threading.Thread(target=read)
        read_thread.start()
        assert not read_done.wait(0.1)
    finally:
        release.set()
    apply_thread.join(3)
    read_thread.join(3)
    assert read_done.is_set()
    assert seen == ["complete"]


def test_announce_landing_during_liveliness_flip_not_stranded(tmp_path) -> None:
    state = {"online": False, "injected": False}
    runtime = OrgFsRuntime(
        tmp_path / "runtime", node_id="node-local", author="user:owner"
    )
    runtime._supplier_online = lambda _node: state["online"]
    key = KeySpace().orgfs_announce("node-peer")

    def sample(space_id: str) -> TransportSample:
        return TransportSample(
            key,
            json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "protocol": 3,
                    "node": "node-peer",
                    "durable": True,
                    "spaces": [{"spaceId": space_id}],
                }
            ).encode(),
        )

    runtime._on_announce(sample("space-old"))
    original_apply = runtime._apply_announce

    def inject(node, value):
        original_apply(node, value)
        if not state["injected"]:
            state["injected"] = True
            state["online"] = False
            runtime._on_announce(sample("space-new"))
            state["online"] = True

    runtime._apply_announce = inject
    state["online"] = True
    runtime._on_peer_liveliness(
        TransportSample(KeySpace().actor_liveliness("node-peer"), b"")
    )
    assert runtime._pending_announces == {}
    assert runtime._holders["space-new"] == {"node-peer"}
    runtime.close()


def test_anti_entropy_skips_non_holder_spaces(tmp_path) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "runtime", node_id="node-local", author="user:owner"
    )
    first = runtime.facade.create_space("first")
    second = runtime.facade.create_space("second")

    class Mesh:
        def __init__(self):
            self.scheduled: list[str] = []

        def announce(self) -> None:
            return None

        def schedule_sync_from(self, peer: str) -> bool:
            self.scheduled.append(peer)
            return True

    first_mesh = Mesh()
    second_mesh = Mesh()
    runtime._session = object()
    runtime._meshes[first.space_id] = first_mesh
    runtime._meshes[second.space_id] = second_mesh
    runtime._holders[first.space_id] = {"node-peer"}
    runtime._holders[second.space_id] = {"node-other"}
    runtime._on_peer_liveliness(
        TransportSample(KeySpace().actor_liveliness("node-peer"), b"")
    )
    assert first_mesh.scheduled == ["node-peer"]
    assert second_mesh.scheduled == []
    runtime._session = None
    runtime._meshes.clear()
    runtime.close()


def test_agent_liveliness_is_not_a_peer_but_host_still_schedules_sync(
    tmp_path,
) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "runtime", node_id="node-local", author="user:owner"
    )
    space = runtime.facade.create_space("team")

    class Mesh:
        def __init__(self):
            self.scheduled: list[str] = []

        def announce(self) -> None:
            return None

        def schedule_sync_from(self, peer: str) -> bool:
            self.scheduled.append(peer)
            return True

    mesh = Mesh()
    runtime._session = object()
    runtime._meshes[space.space_id] = mesh
    agent = "agent:owner@example.com:node-peer:worker"
    agent_sample = TransportSample(KeySpace().actor_liveliness(agent), b"")
    host_sample = TransportSample(KeySpace().actor_liveliness("node-peer"), b"")

    try:
        assert runtime._apply_liveliness_command(agent_sample) == []
        runtime._on_peer_liveliness(agent_sample)
        assert mesh.scheduled == []

        host_effects = runtime._apply_liveliness_command(host_sample)
        assert any(
            effect.action == "sync" and effect.peer == "node-peer"
            for effect in host_effects
        )
        runtime._on_peer_liveliness(host_sample)
        assert mesh.scheduled == ["node-peer"]
    finally:
        runtime._session = None
        runtime._meshes.clear()
        runtime.close()
