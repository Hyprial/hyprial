from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import assert_client_ids_registered
from hyprial.daemon.impl.transport import KeySpace, TransportSample, ZenohConfig, ZenohTransport


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


def _operation_attribution(runtime: OrgFsRuntime, space_id: str, doc_id: str):
    rows = runtime.stores[space_id]._db.execute(
        "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
        (doc_id,),
    )
    return {
        (str(value["origin"]["writer"]), int(value["seq"])): (
            str(value["origin"]["author"]),
            value["origin"]["actor"],
            str(value["origin"]["node"]),
        )
        for row in rows
        if (value := json.loads(bytes(row[0]))).get("docId") == doc_id
    }


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def test_open_doc_concurrent_container_edits_converge_over_zenoh(tmp_path) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Shared document replication after private sidecar admission (§7).
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
        owner._mesh(space.space_id).announce()
        _eventually(lambda: peer.facade.join(space.space_id))
        node = owner.facade.write_text(space.space_id, "board.json", "")
        _eventually(lambda: peer.facade.stat(space.space_id, f"id:{node.node_id}"))
        left = owner.facade.open_doc(space.space_id, f"id:{node.node_id}")
        right = peer.facade.open_doc(space.space_id, f"id:{node.node_id}")
        left.transact(lambda root: root.ensure_map("cards"))
        _eventually(lambda: _equal(left.snapshot_json(), right.snapshot_json()))
        before_owner_operations = set(
            _operation_attribution(owner, space.space_id, node.doc_id)
        )
        left.transact(
            lambda root: root.ensure_map("cards").set(
                "a", {"done": True, "note": "owner"}
            )
        )
        owner_attribution = _operation_attribution(owner, space.space_id, node.doc_id)
        owner_operations = {
            operation
            for operation in set(owner_attribution) - before_owner_operations
            if owner_attribution[operation][2] == "node-owner"
        }
        assert len(owner_operations) == 1
        owner_operation = owner_operations.pop()
        assert owner_attribution[owner_operation] == (
            "user:owner",
            None,
            "node-owner",
        )
        before_peer_operations = set(
            _operation_attribution(peer, space.space_id, node.doc_id)
        )
        right.transact(
            lambda root: root.ensure_map("cards").set(
                "b", {"done": False, "note": "peer"}
            )
        )
        peer_attribution = _operation_attribution(peer, space.space_id, node.doc_id)
        peer_operations = {
            operation
            for operation in set(peer_attribution) - before_peer_operations
            if peer_attribution[operation][2] == "node-peer"
        }
        assert len(peer_operations) == 1
        peer_operation = peer_operations.pop()
        assert peer_attribution[peer_operation] == ("user:peer", None, "node-peer")
        expected_operations = {
            owner_operation: ("user:owner", None, "node-owner"),
            peer_operation: ("user:peer", None, "node-peer"),
        }
        _eventually(lambda: _equal(left.snapshot_json(), right.snapshot_json()))
        snapshot = json.loads(left.snapshot_json())
        assert snapshot["cards"] == {
            "a": {"done": True, "note": "owner"},
            "b": {"done": False, "note": "peer"},
        }
        for runtime in (owner, peer):
            actual_operations = _operation_attribution(
                runtime, space.space_id, node.doc_id
            )
            assert {
                operation: actual_operations[operation]
                for operation in expected_operations
            } == expected_operations
            attributed_history = {
                (entry.author, entry.actor)
                for entry in runtime.facade.history(
                    space.space_id, f"id:{node.node_id}", limit=20
                )
                if entry.changed == "content"
            }
            assert {
                (author, actor) for author, actor, _node in expected_operations.values()
            } <= attributed_history
        assert_client_ids_registered(owner.stores[space.space_id])
        assert_client_ids_registered(peer.stores[space.space_id])
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_open_doc_transact_respects_membership_gate(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "card.json", "")
        runtime.facade.invite(space.space_id, "user:reader", "ro")
        handle = runtime.facade.open_doc(space.space_id, f"id:{node.node_id}")
        before = (
            runtime.stores[space.space_id]
            ._db.execute("SELECT COUNT(*) FROM commits")
            .fetchone()[0]
        )
        runtime.facade.author = "user:reader"
        with pytest.raises(OrgFsError) as rejected:
            handle.transact(lambda root: root.set("blocked", True))
        assert rejected.value.code == "not-a-member"
        after = (
            runtime.stores[space.space_id]
            ._db.execute("SELECT COUNT(*) FROM commits")
            .fetchone()[0]
        )
        assert after == before
    finally:
        runtime.close()


def test_open_doc_handle_across_purge_rebuild(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "card.json", "body")
        old = runtime.facade.open_doc(space.space_id, f"id:{node.node_id}")
        plan = runtime.purge_plan(
            space.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": "all",
                    "to": "all",
                }
            ],
        )
        runtime.purge(space.space_id, plan.plan_id)
        with pytest.raises(OrgFsError) as blocked:
            old.transact(lambda root: root.set("stale", True))
        assert blocked.value.code == "snapshot-barrier"
        replacement = runtime.facade.open_doc(space.space_id, f"id:{node.node_id}")
        replacement.transact(lambda root: root.set("fresh", True))
        assert json.loads(replacement.snapshot_json())["fresh"] is True
    finally:
        runtime.close()


def test_reader_never_observes_partial_apply(tmp_path, monkeypatch) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "doc.md", "complete")
        envelope = bytes(
            runtime.stores[space.space_id]
            ._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid DESC LIMIT 1",
                (node.doc_id,),
            )
            .fetchone()[0]
        )
        halfway = threading.Event()
        release = threading.Event()
        read_done = threading.Event()
        original_finish = runtime.facade._finish

        def gated_finish(*args, **kwargs):
            halfway.set()
            assert release.wait(3)
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(runtime.facade, "_finish", gated_finish)
        apply_thread = threading.Thread(
            target=runtime.facade.apply_envelope, args=(space.space_id, envelope)
        )
        apply_thread.start()
        assert halfway.wait(3)

        def read() -> None:
            runtime.facade.read_text(space.space_id, f"id:{node.node_id}")
            read_done.set()

        read_thread = threading.Thread(target=read)
        read_thread.start()
        assert not read_done.wait(0.1)
        release.set()
        apply_thread.join(3)
        read_thread.join(3)
        assert read_done.is_set()
    finally:
        runtime.close()


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
