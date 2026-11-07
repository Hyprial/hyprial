"""Device-row owner admission over real stores and loopback replication.

These tests assume the still-unsigned ``origin.author`` is genuine.  They use
``DaemonApplication._handle_orgfs`` for mutations and reads because it is the
shared daemon endpoint used by CLI and MCP callers.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.ipc import ipc_org_remove
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.identity import (
    DirectoryDevice,
    default_role_grants_document,
    directory_device_path,
    protected_directory_doc_id,
)

from tests.support.tailnet_membership import FakeMembershipSidecar, member

OWNER = "owner@example"
ALICE = "alice@example"
BOB = "bob@example"


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
        except (AssertionError, DaemonRequestError, OrgFsError) as error:
            last_error = error
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _equal(actual, expected):
    assert actual == expected
    return actual


def _device(owner: str, device_id: str, *, address: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{device_id}",
        client_public=f"nodekey:client-{device_id}",
        address=address,
        key_generation=1,
        updated_at="2026-10-06T12:00:00+00:00",
    )


def _daemon(runtime: OrgFsRuntime) -> DaemonApplication:
    application = object.__new__(DaemonApplication)
    application._orgfs_runtime = runtime
    return application


def _read(runtime: OrgFsRuntime, space_id: str, path: str) -> str:
    return str(
        _daemon(runtime)._handle_orgfs(
            "orgfs.read", {"spaceId": space_id, "node": path}
        )["text"]
    )


def _journal_position(runtime: OrgFsRuntime, space_id: str) -> int:
    return int(
        runtime.stores[space_id]
        ._db.execute("SELECT COALESCE(MAX(rowid), 0) FROM commits")
        .fetchone()[0]
    )


def _exchange_since(
    source: OrgFsRuntime,
    targets: tuple[OrgFsRuntime, ...],
    space_id: str,
    after: int,
) -> None:
    rows = source.stores[space_id]._db.execute(
        "SELECT envelope_bytes FROM commits WHERE rowid > ? ORDER BY rowid", (after,)
    )
    for row in rows:
        envelope = bytes(row[0])
        for target in targets:
            result = target._meshes[space_id].receive(
                envelope, supplier=source.node_id
            )
            assert result.status == "applied", (result.status, result.code)
            if result.code == "duplicate":
                # Live Zenoh ingress may have durably imported this envelope
                # while its facade projection is still in flight.  A complete
                # manual exchange must also establish the idempotent projection
                # before its caller reads the target.
                target.facade.apply_envelope(space_id, envelope)


def _forged_content_envelope(
    runtime: OrgFsRuntime,
    space_id: str,
    path: str,
    content: str,
) -> bytes:
    """Build a content-only update, as a pre-fix/malicious replica could."""

    info = runtime.facade.stat(space_id, path)
    assert info.doc_id is not None
    store = runtime.stores[space_id]
    writer = store._writer_for(f"user:{BOB}", None)
    doc = Doc(client_id=store._client_id(writer))
    doc.apply_update(store.snapshot(info.doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    text = doc.get("text", type=Text)
    text.clear()
    text.insert(0, content)
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = ?",
            (writer, info.doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        info.doc_id,
        writer,
        seq,
        doc.get_update(before),
        author=f"user:{BOB}",
        actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _forged_doc_envelope(
    runtime: OrgFsRuntime,
    space_id: str,
    *,
    doc_id: str,
    author: str,
    content: str,
) -> bytes:
    """Build a content envelope for an unlinked deterministic document."""

    store = runtime.stores[space_id]
    writer = store._writer_for(f"user:{author}", None)
    doc = Doc(client_id=store._client_id(writer))
    before = doc.get_state()
    doc.get("text", type=Text).insert(0, content)
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = ?",
            (writer, doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        doc_id,
        writer,
        seq,
        doc.get_update(before),
        author=f"user:{author}",
        actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _forged_tree_envelope(
    runtime: OrgFsRuntime,
    space_id: str,
    *,
    author: str,
    mutate,
) -> bytes:
    """Build a genuine-author tree delta without passing local admission."""

    store = runtime.stores[space_id]
    doc_id = store.active_tree_doc_id()
    assert doc_id is not None
    writer = store._writer_for(f"user:{author}", None)
    doc = Doc(client_id=store._client_id(writer))
    doc.apply_update(store.snapshot(doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    mutate(doc.get("nodes", type=Map))
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = ?",
            (writer, doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        doc_id,
        writer,
        seq,
        doc.get_update(before),
        author=f"user:{author}",
        actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _rewrite_tree_node(nodes: Map, node_id: str, **changes: object) -> None:
    value = json.loads(str(nodes[node_id]))
    value.update(changes)
    nodes[node_id] = json.dumps(value, sort_keys=True, separators=(",", ":"))


def _add_tree_doc(
    nodes: Map, *, node_id: str, parent: str, name: str, doc_id: str
) -> None:
    nodes[node_id] = json.dumps(
        {
            "blob_hash": None,
            "deleted": False,
            "doc_id": doc_id,
            "kind": "doc",
            "name": name,
            "node_id": node_id,
            "parent": parent,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _register_device(
    source: OrgFsRuntime,
    targets: tuple[OrgFsRuntime, ...],
    space_id: str,
    owner: str,
    device_id: str,
) -> None:
    before = _journal_position(source, space_id)
    OrgFsDirectoryStore(source.facade).put_device(
        "group-acme", _device(owner, device_id, address=f"tcp://10.0.0.7:7447#{owner}")
    )
    _exchange_since(source, targets, space_id, before)


class _OwnerApp:
    def __init__(self, runtime: OrgFsRuntime, home: Path, space_id: str) -> None:
        self._runtime = runtime
        self.hyprial_home = home
        self.node_id = "node-owner"
        self.owner = OWNER
        self._forwarding_supervisor = FakeMembershipSidecar(owner=OWNER, device_id=self.node_id)
        self._forwarding_supervisor.seed(space_id, members=[
            self._forwarding_supervisor.local, member(ALICE, "node-alice"),
            member(BOB, "node-bob"),
        ])

    def _start_forwarding_supervisor(self) -> None:
        pass  # In-memory membership authority; no process or live endpoint.

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        return self._runtime

    def _log(self, *_args, **_fields) -> None:
        return None

    def _reconcile_forwarding_endpoints(self) -> None:
        return None

    def _await_forwarding_reconcile(self, reason: str, *, timeout: float, **fields: object) -> None:
        self._reconcile_forwarding_endpoints()

    def _request_forwarding_reconcile(self, reason: str, **fields: object) -> bool:
        return True

    def _forwarding_status_json(self) -> dict[str, object]:
        return {"policy": "off", "peers": []}


@pytest.fixture
def replicas(tmp_path: Path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    transports = [
        ZenohTransport(ZenohConfig(listen=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
    ]
    # Private-sidecar admission (§7) lets these tests reach protected author/path gates.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author=f"user:{OWNER}",
        trust_tailnet_members=True,
    )
    alice = OrgFsRuntime(
        tmp_path / "alice", node_id="node-alice", author=f"user:{ALICE}",
        trust_tailnet_members=True,
    )
    bob = OrgFsRuntime(
        tmp_path / "bob", node_id="node-bob", author=f"user:{BOB}",
        trust_tailnet_members=True,
    )
    runtimes = (owner, alice, bob)
    try:
        time.sleep(0.3)
        online = {runtime.node_id for runtime in runtimes}.__contains__
        owner.bind_transport(transports[0], supplier_online=online)
        alice.bind_transport(
            transports[1],
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        bob.bind_transport(
            transports[2],
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("group-acme")
        owner.facade.mkdir(space.space_id, "directory")
        owner.facade.mkdir(space.space_id, "directory/devices")
        owner.facade.mkdir(space.space_id, "directory/people")
        owner.facade.mkdir(space.space_id, "directory/invites")
        owner.facade.mkdir(space.space_id, "directory/leaves")
        acl = owner.facade.create_space("group-acme-acl")
        owner.facade.mkdir(acl.space_id, "policy")
        owner.facade.write_text(
            acl.space_id,
            ACL_POLICY_DOC,
            json.dumps(
                default_role_grants_document(), sort_keys=True, separators=(",", ":")
            ),
        )
        owner.facade.update_space_meta(space.space_id, {"aclSpaceId": acl.space_id})
        owner.facade.invite(space.space_id, f"user:{ALICE}", "rw")
        owner.facade.invite(space.space_id, f"user:{BOB}", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: alice.facade.join(space.space_id))
        _eventually(lambda: bob.facade.join(space.space_id))
        yield tmp_path, space.space_id, owner, alice, bob
    finally:
        for runtime in reversed(runtimes):
            runtime.close()
        for transport in reversed(transports):
            transport.close()


def test_genuine_author_content_and_tree_rules_hold_on_every_replica(
    replicas,
) -> None:
    _tmp_path, space_id, owner, alice, bob = replicas
    alice_store = OrgFsDirectoryStore(alice.facade)
    bob_store = OrgFsDirectoryStore(bob.facade)
    row = _device(ALICE, "opaque-a7", address="tcp://10.0.0.7:7447#a")
    before = _journal_position(alice, space_id)
    alice_store.put_device("group-acme", row)
    _exchange_since(alice, (owner, bob), space_id, before)
    path = directory_device_path(ALICE, row.device_id)
    expected = json.dumps(row.as_record(), sort_keys=True, separators=(",", ":"))

    for replica in (owner, alice, bob):
        _eventually(lambda replica=replica: _equal(_read(replica, space_id, path), expected))

    # Alice may update her own content row.
    own_update = _device(ALICE, row.device_id, address="tcp://10.0.0.8:7447#a2")
    before = _journal_position(alice, space_id)
    alice_store.put_device("group-acme", own_update)
    _exchange_since(alice, (owner, bob), space_id, before)
    own_text = json.dumps(
        own_update.as_record(), sort_keys=True, separators=(",", ":")
    )
    for replica in (owner, alice, bob):
        _eventually(lambda replica=replica: _equal(_read(replica, space_id, path), own_text))

    # Bob may enroll an opaque id under Bob's own subtree.
    before = _journal_position(bob, space_id)
    bob_store.put_device(
        "group-acme", _device(BOB, "random-not-hostname", address="tcp://10.0.0.9:7447#b")
    )
    _exchange_since(bob, (owner, alice), space_id, before)
    for replica in (owner, alice, bob):
        _eventually(
            lambda replica=replica: _equal(
                [item.device_id for item in OrgFsDirectoryStore(replica.facade).list_devices("group-acme")],
                ["opaque-a7", "random-not-hostname"],
            )
        )

    # The attacker-controlled record.owner variant must not turn Bob into the
    # owner of Alice's already-mapped content document.
    forged = _device(BOB, row.device_id, address="tcp://10.0.0.66:7447#hijack")
    with pytest.raises(DaemonRequestError) as content_rejected:
        _daemon(bob)._handle_orgfs(
            "orgfs.write",
            {
                "spaceId": space_id,
                "path": path,
                "text": json.dumps(
                    forged.as_record(), sort_keys=True, separators=(",", ":")
                ),
            },
        )
    assert content_rejected.value.code == "not-a-member"

    forged_text = json.dumps(
        forged.as_record(), sort_keys=True, separators=(",", ":")
    )
    envelope = _forged_content_envelope(bob, space_id, path, forged_text)
    for receiver in (owner, alice):
        result = receiver._meshes[space_id].receive(
            envelope, supplier=bob.node_id
        )
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert _read(receiver, space_id, path) == own_text

    # Tree creation and deletion under Alice's subtree are rejected too.
    forged_path = directory_device_path(ALICE, "squatted")
    with pytest.raises(DaemonRequestError) as create_rejected:
        _daemon(bob)._handle_orgfs(
            "orgfs.write",
            {"spaceId": space_id, "path": forged_path, "text": "{}"},
        )
    assert create_rejected.value.code == "not-a-member"
    with pytest.raises(DaemonRequestError) as delete_rejected:
        _daemon(bob)._handle_orgfs(
            "orgfs.remove", {"spaceId": space_id, "node": path}
        )
    assert delete_rejected.value.code == "not-a-member"

    for replica in (owner, alice, bob):
        assert _read(replica, space_id, path) == own_text
        with pytest.raises(DaemonRequestError) as absent:
            _daemon(replica)._handle_orgfs(
                "orgfs.stat", {"spaceId": space_id, "node": forged_path}
            )
        assert absent.value.code == "unknown-doc"


def test_owner_member_removal_leaves_rows_inert_on_every_replica(replicas) -> None:
    tmp_path, space_id, owner, alice, bob = replicas
    before = _journal_position(alice, space_id)
    OrgFsDirectoryStore(alice.facade).put_device(
        "group-acme", _device(ALICE, "alice-device", address="tcp://10.0.0.7:7447#a")
    )
    _exchange_since(alice, (owner, bob), space_id, before)
    path = directory_device_path(ALICE, "alice-device")
    node_id = alice.facade.stat(space_id, path).node_id
    for replica in (owner, alice, bob):
        _eventually(lambda replica=replica: _read(replica, space_id, path))

    before = _journal_position(owner, space_id)
    application = _OwnerApp(owner, tmp_path / "owner-home", space_id)
    from tests.support.tailnet_membership import confirm_ipc_removal
    result = confirm_ipc_removal(ipc_org_remove,
        application,
        {"org": "group-acme", "user": f"user:{ALICE}"},
    )
    assert result["phase"] == "complete"
    assert result["deviceIds"] == ["node-alice"]
    assert application._forwarding_supervisor.tailnets[space_id]["members"]["node-alice"]["state"] == "tombstone"
    _exchange_since(owner, (alice, bob), space_id, before)

    for replica in (owner, alice, bob):
        def gone(replica=replica):
            with pytest.raises(DaemonRequestError) as missing:
                _daemon(replica)._handle_orgfs(
                    "orgfs.stat", {"spaceId": space_id, "node": path}
                )
            assert missing.value.code == "unknown-doc"
            return True

        _eventually(gone)
        assert not replica.facade._space(space_id).nodes[node_id].deleted


def test_member_cannot_delete_directory_ancestor_on_any_replica(replicas) -> None:
    """B2: genuine Bob cannot blank the directory by deleting its ancestor."""

    _tmp_path, space_id, owner, alice, bob = replicas
    _register_device(bob, (owner, alice), space_id, BOB, "bob-device")
    directory = bob.facade.stat(space_id, "directory")
    envelope = _forged_tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda nodes: _rewrite_tree_node(
            nodes, directory.node_id, deleted=True
        ),
    )

    assert bob.stores[space_id].admission(envelope) == "not-a-member"
    for receiver in (owner, alice):
        result = receiver._meshes[space_id].receive(envelope, supplier=bob.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert receiver.facade.stat(space_id, "directory").deleted is False
        assert [
            device.device_id
            for device in OrgFsDirectoryStore(receiver.facade).list_devices("group-acme")
        ] == ["bob-device"]


def test_member_cannot_add_duplicate_devices_root_on_any_replica(replicas) -> None:
    """B3: a same-name shared root never creates permanent ambiguity."""

    _tmp_path, space_id, owner, alice, bob = replicas
    _register_device(bob, (owner, alice), space_id, BOB, "bob-device")
    directory = bob.facade.stat(space_id, "directory")

    def duplicate(nodes: Map) -> None:
        nodes["squatter-devices-root"] = json.dumps(
            {
                "blob_hash": None,
                "deleted": False,
                "doc_id": None,
                "kind": "dir",
                "name": "devices",
                "node_id": "squatter-devices-root",
                "parent": directory.node_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    envelope = _forged_tree_envelope(
        bob, space_id, author=BOB, mutate=duplicate
    )

    assert bob.stores[space_id].admission(envelope) == "not-a-member"
    for receiver in (owner, alice):
        result = receiver._meshes[space_id].receive(envelope, supplier=bob.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert receiver.facade.stat(space_id, "directory/devices").kind == "dir"


def test_link_refuses_a_non_deterministic_doc_id_on_every_replica(replicas) -> None:
    """R3-H: protected paths never adopt arbitrary physical documents."""

    _tmp_path, space_id, owner, alice, bob = replicas
    _register_device(alice, (owner, bob), space_id, ALICE, "alice-device")
    _register_device(bob, (owner, alice), space_id, BOB, "bob-device")
    polluted_doc = "doc-00000000-0000-4000-8000-000000000002"
    before = _journal_position(bob, space_id)
    bob.stores[space_id].commit(
        polluted_doc,
        lambda doc: doc.get("text", type=Text).insert(0, "polluted"),
        author=f"user:{BOB}",
        actor=None,
    )
    _exchange_since(bob, (owner, alice), space_id, before)
    parent = alice.facade.stat(space_id, f"directory/devices/{ALICE}")
    envelope = _forged_tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda nodes: _add_tree_doc(
            nodes,
            node_id="polluted-link",
            parent=parent.node_id,
            name="linked-polluted.json",
            doc_id=polluted_doc,
        ),
    )

    assert alice.stores[space_id].admission(envelope) == "not-a-member"
    for receiver in (owner, bob):
        result = receiver._meshes[space_id].receive(envelope, supplier=alice.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        with pytest.raises(OrgFsError) as missing:
            receiver.facade.stat(
                space_id, f"directory/devices/{ALICE}/linked-polluted.json"
            )
        assert missing.value.code == "unknown-doc"


def test_deterministic_device_admission_converges_in_both_arrival_orders(
    replicas,
) -> None:
    """R3-A: content/pollution/link order cannot split replicas or wedge trees."""

    _tmp_path, space_id, owner, alice, bob = replicas
    row = _device(ALICE, "arrival:device", address="tcp://10.0.0.7:7447#alice")
    path = directory_device_path(ALICE, row.device_id)
    doc_id = protected_directory_doc_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert doc_id is not None
    before_parent = _journal_position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}")
    _exchange_since(alice, (owner, bob), space_id, before_parent)
    expected = json.dumps(row.as_record(), sort_keys=True, separators=(",", ":"))
    before_write = _journal_position(alice, space_id)
    _daemon(alice)._handle_orgfs(
        "orgfs.write",
        {"spaceId": space_id, "path": path, "text": expected},
    )
    envelopes = [
        bytes(row[0])
        for row in alice.stores[space_id]._db.execute(
            "SELECT envelope_bytes FROM commits WHERE rowid > ? ORDER BY rowid",
            (before_write,),
        )
    ]
    content = next(
        envelope
        for envelope in envelopes
        if json.loads(envelope)["docId"] == doc_id
    )
    link = next(
        envelope
        for envelope in envelopes
        if str(json.loads(envelope)["docId"]).startswith("tree-")
    )
    pollution = _forged_doc_envelope(
        bob,
        space_id,
        doc_id=doc_id,
        author=BOB,
        content="polluted",
    )
    for receiver in (owner, bob):
        result = receiver._meshes[space_id].receive(content, supplier=alice.node_id)
        assert result.status == "applied"
    assert owner._meshes[space_id].receive(
        pollution, supplier=bob.node_id
    ).status == "rejected"
    assert owner._meshes[space_id].receive(link, supplier=alice.node_id).status == "applied"
    assert bob._meshes[space_id].receive(link, supplier=alice.node_id).status == "applied"
    assert bob._meshes[space_id].receive(
        pollution, supplier=bob.node_id
    ).status == "rejected"
    for receiver in (owner, bob):
        _eventually(lambda receiver=receiver: _equal(_read(receiver, space_id, path), expected))

    before_later = _journal_position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}/later")
    _exchange_since(alice, (owner, bob), space_id, before_later)
    for receiver in (owner, bob):
        assert receiver.facade.stat(
            space_id, f"directory/devices/{ALICE}/later"
        ).kind == "dir"


def test_leave_and_invite_doc_ids_enforce_their_principals_on_every_replica(
    replicas,
) -> None:
    """R3-B/C: only self may leave and only the space owner may invite."""

    _tmp_path, space_id, owner, alice, bob = replicas
    leave_path = f"directory/leaves/{ALICE}.json"
    leave_id = protected_directory_doc_id(
        space_id, leave_path, space_owner=f"user:{OWNER}"
    )
    invite_path = "directory/invites/forged.json"
    invite_id = protected_directory_doc_id(
        space_id, invite_path, space_owner=f"user:{OWNER}"
    )
    assert leave_id is not None and invite_id is not None
    with pytest.raises(OrgFsError) as leave_rejected:
        OrgFsDirectoryStore(bob.facade).put_leave_request(
            "group-acme", ALICE, {"user": ALICE, "requestedAt": "forged"}
        )
    assert leave_rejected.value.code == "not-a-member"
    with pytest.raises(OrgFsError) as invite_rejected:
        OrgFsDirectoryStore(bob.facade).put_invite(
            "group-acme", "official-forgery", {"invite": True}
        )
    assert invite_rejected.value.code == "not-a-member"
    forged_leave = _forged_doc_envelope(
        bob, space_id, doc_id=leave_id, author=BOB, content='{"user":"alice"}'
    )
    forged_invite = _forged_doc_envelope(
        bob, space_id, doc_id=invite_id, author=BOB, content='{"invite":true}'
    )
    for envelope in (forged_leave, forged_invite):
        assert bob.stores[space_id].admission(envelope) == "not-a-member"
        for receiver in (owner, alice):
            result = receiver._meshes[space_id].receive(
                envelope, supplier=bob.node_id
            )
            assert (result.status, result.code) == ("rejected", "not-a-member")

    before = _journal_position(alice, space_id)
    OrgFsDirectoryStore(alice.facade).put_leave_request(
        "group-acme", ALICE, {"user": ALICE, "requestedAt": "now"}
    )
    _exchange_since(alice, (owner, bob), space_id, before)
    for replica in (owner, alice, bob):
        assert ALICE in OrgFsDirectoryStore(replica.facade).list_leave_requests("group-acme")


def test_org_meta_is_owner_only_on_every_replica(replicas) -> None:
    """R3-I: only the owner may create, update, or move ``org.json``."""

    _tmp_path, space_id, owner, alice, bob = replicas
    path = "directory/org.json"
    owner_text = '{"state":"owner-written"}'

    with pytest.raises(DaemonRequestError) as member_create_rejected:
        _daemon(bob)._handle_orgfs(
            "orgfs.write",
            {"spaceId": space_id, "path": path, "text": '{"state":"deleted"}'},
        )
    assert member_create_rejected.value.code == "not-a-member"

    before = _journal_position(owner, space_id)
    _daemon(owner)._handle_orgfs(
        "orgfs.write", {"spaceId": space_id, "path": path, "text": owner_text}
    )
    _exchange_since(owner, (alice, bob), space_id, before)
    for replica in (owner, alice, bob):
        assert _read(replica, space_id, path) == owner_text

    with pytest.raises(DaemonRequestError) as member_update_rejected:
        _daemon(bob)._handle_orgfs(
            "orgfs.write",
            {"spaceId": space_id, "path": path, "text": '{"state":"deleted"}'},
        )
    assert member_update_rejected.value.code == "not-a-member"

    content_envelope = _forged_content_envelope(
        bob, space_id, path, '{"state":"deleted"}'
    )
    assert bob.stores[space_id].admission(content_envelope) == "not-a-member"
    for receiver in (owner, alice):
        result = receiver._meshes[space_id].receive(
            content_envelope, supplier=bob.node_id
        )
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert _read(receiver, space_id, path) == owner_text

    org_node = bob.facade.stat(space_id, path)
    tree_envelope = _forged_tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda nodes: _rewrite_tree_node(
            nodes, org_node.node_id, name="member-renamed.json"
        ),
    )
    assert bob.stores[space_id].admission(tree_envelope) == "not-a-member"
    for receiver in (owner, alice):
        result = receiver._meshes[space_id].receive(
            tree_envelope, supplier=bob.node_id
        )
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert receiver.facade.stat(space_id, path).name == "org.json"


@pytest.mark.parametrize("change", [{"deleted": True}, {"kind": "doc"}, {"name": "renamed"}])
def test_root_and_directory_ancestors_are_owner_only_on_every_replica(
    replicas, change: dict[str, object]
) -> None:
    """R3-D: members cannot delete, kind-change, or rename protected ancestors."""

    _tmp_path, space_id, owner, alice, bob = replicas
    paths = (
        "",
        "directory",
        "directory/devices",
        "directory/people",
        "directory/invites",
        "directory/leaves",
    )
    for path in paths:
        node_id = "root" if not path else bob.facade.stat(space_id, path).node_id
        envelope = _forged_tree_envelope(
            bob,
            space_id,
            author=BOB,
            mutate=lambda nodes, node_id=node_id: _rewrite_tree_node(
                nodes, node_id, **change
            ),
        )
        assert bob.stores[space_id].admission(envelope) == "not-a-member"
        for receiver in (owner, alice):
            result = receiver._meshes[space_id].receive(
                envelope, supplier=bob.node_id
            )
            assert (result.status, result.code) == ("rejected", "not-a-member")
        owner_envelope = _forged_tree_envelope(
            owner,
            space_id,
            author=OWNER,
            mutate=lambda nodes, node_id=node_id: _rewrite_tree_node(
                nodes, node_id, **change
            ),
        )
        assert owner.stores[space_id].admission(owner_envelope) == (
            "not-a-member" if path and "name" in change else "ok"
        )
