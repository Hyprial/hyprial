"""ROUND 4 device-row admission, storage-key, and purge acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    ReplicaStore,
)
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport
from hyprial.identity import (
    DirectoryDevice,
    directory_device_path,
    parse_protected_directory_doc_id,
    protected_directory_doc_id,
)

OWNER = "owner@example"
ALICE = "alice@example"
BOB = "bob@example"
CAROL = "carol@example"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")


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
        except (AssertionError, OrgFsError) as error:
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
        server_public=f"nodekey:server-{owner}-{device_id}",
        client_public=f"nodekey:client-{owner}-{device_id}",
        address=address,
        key_generation=1,
        updated_at="2026-10-07T12:00:00+00:00",
    )


def _position(runtime: OrgFsRuntime, space_id: str) -> int:
    return int(
        runtime.stores[space_id]
        ._db.execute("SELECT COALESCE(MAX(rowid), 0) FROM commits")
        .fetchone()[0]
    )


def _commits_since(runtime: OrgFsRuntime, space_id: str, after: int) -> tuple[bytes, ...]:
    return tuple(
        bytes(row[0])
        for row in runtime.stores[space_id]._db.execute(
            "SELECT envelope_bytes FROM commits WHERE rowid > ? ORDER BY rowid",
            (after,),
        )
    )


def _doc_id(envelope: bytes) -> str:
    return str(json.loads(envelope)["docId"])


def _receive(target: OrgFsRuntime, space_id: str, envelope: bytes, supplier: str):
    return target._meshes[space_id].receive(envelope, supplier=supplier)


def _deliver_all(
    source: OrgFsRuntime,
    targets: tuple[OrgFsRuntime, ...],
    space_id: str,
    after: int,
) -> None:
    for envelope in _commits_since(source, space_id, after):
        for target in targets:
            result = _receive(target, space_id, envelope, source.node_id)
            assert result.status == "applied", (result.status, result.code)


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


def _forged_tree_envelope(
    runtime: OrgFsRuntime,
    space_id: str,
    *,
    author: str,
    parent: str,
) -> bytes:
    store = runtime.stores[space_id]
    doc_id = store.active_tree_doc_id()
    assert doc_id is not None
    writer = store._writer_for(f"user:{author}", None)
    doc = Doc(client_id=store._client_id(writer))
    doc.apply_update(store.snapshot(doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    _add_tree_doc(
        doc.get("nodes", type=Map),
        node_id="bob-forged-child",
        parent=parent,
        name="evil.json",
        doc_id="doc-00000000-0000-4000-8000-000000000099",
    )
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


@pytest.fixture
def replicas(tmp_path: Path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    transports = [
        ZenohTransport(ZenohConfig(listen=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
    ]
    # Model private sidecar admission (§7) before cold directory replication.
    # Protected path/author/commit checks below remain real, including forgeries.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author=f"user:{OWNER}", trust_tailnet_members=True
    )
    alice = OrgFsRuntime(
        tmp_path / "alice", node_id="node-alice", author=f"user:{ALICE}", trust_tailnet_members=True
    )
    bob = OrgFsRuntime(
        tmp_path / "bob", node_id="node-bob", author=f"user:{BOB}", trust_tailnet_members=True
    )
    carol = OrgFsRuntime(
        tmp_path / "carol", node_id="node-carol", author=f"user:{CAROL}", trust_tailnet_members=True
    )
    runtimes = (owner, alice, bob, carol)
    try:
        time.sleep(0.3)
        online = {runtime.node_id for runtime in runtimes}.__contains__
        for runtime, transport in zip(runtimes, transports, strict=True):
            runtime.bind_transport(transport, supplier_online=online)
        space = owner.facade.create_space("group-acme")
        for path in (
            "directory",
            "directory/devices",
            "directory/people",
            "directory/invites",
            "directory/leaves",
        ):
            owner.facade.mkdir(space.space_id, path)
        for member in (ALICE, BOB, CAROL):
            owner.facade.invite(space.space_id, f"user:{member}", "rw")
        owner._mesh(space.space_id).announce()
        for runtime in (alice, bob, carol):
            _eventually(lambda runtime=runtime: runtime.facade.join(space.space_id))
        yield tmp_path, space.space_id, owner, alice, bob, carol
    finally:
        for runtime in reversed(runtimes):
            runtime.close()
        for transport in reversed(transports):
            transport.close()


def _arrival_order_updates(replicas):
    _tmp_path, space_id, owner, alice, bob, carol = replicas
    before = _position(bob, space_id)
    OrgFsDirectoryStore(bob.facade).put_device(
        "group-acme", _device(BOB, "bob-device", address="tcp://10.0.0.8:7447#bob")
    )
    _deliver_all(bob, (owner, alice, carol), space_id, before)

    # Keep Alice's tree envelopes local so the two receivers can exercise
    # opposite deterministic delivery orders below.
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}")
    alice_updates = _commits_since(alice, space_id, before)
    for envelope in alice_updates:
        if _doc_id(envelope) == "meta":
            for target in (owner, bob, carol):
                assert _receive(target, space_id, envelope, alice.node_id).status == "applied"
    (mkdir_update,) = tuple(
        envelope for envelope in alice_updates if _doc_id(envelope).startswith("tree-")
    )
    assert _receive(bob, space_id, mkdir_update, alice.node_id).status == "applied"
    parent = bob.facade.stat(space_id, f"directory/devices/{ALICE}")
    forged = _forged_tree_envelope(
        bob, space_id, author=BOB, parent=parent.node_id
    )

    before = _position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}/later")
    (later_update,) = tuple(
        envelope
        for envelope in _commits_since(alice, space_id, before)
        if _doc_id(envelope).startswith("tree-")
    )
    return space_id, owner, alice, bob, carol, mkdir_update, forged, later_update


def _assert_alice_tree(runtime: OrgFsRuntime, space_id: str) -> None:
    assert runtime.facade.stat(
        space_id, f"directory/devices/{ALICE}/later"
    ).kind == "dir"
    with pytest.raises(OrgFsError) as missing:
        runtime.facade.stat(space_id, f"directory/devices/{ALICE}/evil.json")
    assert missing.value.code == "unknown-doc"


def test_r4_a_protected_parent_id_is_judged_before_parent_arrives(
    replicas,
) -> None:
    """R4-A: a short-path judgment cannot fork or wedge Alice's tree lane."""

    space_id, owner, alice, bob, carol, mkdir_update, forged, later_update = (
        _arrival_order_updates(replicas)
    )
    refused_first = _receive(owner, space_id, forged, bob.node_id)
    assert (refused_first.status, refused_first.code) == ("rejected", "not-a-member")
    assert owner.stores[space_id]._db.execute(
        "SELECT COUNT(*) FROM pending"
    ).fetchone()[0] == 0
    assert _receive(owner, space_id, mkdir_update, alice.node_id).status == "applied"

    assert _receive(carol, space_id, mkdir_update, alice.node_id).status == "applied"
    refused = _receive(carol, space_id, forged, bob.node_id)
    assert (refused.status, refused.code) == ("rejected", "not-a-member")

    for receiver in (owner, carol):
        assert _receive(receiver, space_id, later_update, alice.node_id).status == "applied"
        assert receiver.stores[space_id]._db.execute(
            "SELECT COUNT(*) FROM pending"
        ).fetchone()[0] == 0
        _assert_alice_tree(receiver, space_id)


def test_r4_b_protected_id_verdict_does_not_consume_pending_capacity(
    replicas, caplog
) -> None:
    """R4-B: a tiny hold cap drops retryable bytes, never a durable verdict."""

    space_id, owner, alice, bob, carol, mkdir_update, forged, later_update = (
        _arrival_order_updates(replicas)
    )
    owner.stores[space_id]._pending_import_limit = 0
    caplog.set_level("WARNING")
    dropped = _receive(owner, space_id, forged, bob.node_id)
    held = _receive(carol, space_id, forged, bob.node_id)
    assert (dropped.status, dropped.code) == ("rejected", "not-a-member")
    assert (held.status, held.code) == ("rejected", "not-a-member")
    assert owner.stores[space_id]._db.execute(
        "SELECT COUNT(*) FROM pending"
    ).fetchone()[0] == 0
    assert carol.stores[space_id]._db.execute(
        "SELECT COUNT(*) FROM pending"
    ).fetchone()[0] == 0
    assert "orgfs dropped pending dependency" not in caplog.text

    for receiver in (owner, carol):
        assert _receive(receiver, space_id, mkdir_update, alice.node_id).status == "applied"
    retried = _receive(owner, space_id, forged, bob.node_id)
    assert (retried.status, retried.code) == ("rejected", "not-a-member")
    for receiver in (owner, carol):
        assert _receive(receiver, space_id, later_update, alice.node_id).status == "applied"
        _assert_alice_tree(receiver, space_id)

    forged_value = json.loads(forged)
    assert owner.stores[space_id]._db.execute(
        "SELECT 1 FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
        (
            forged_value["origin"]["writer"],
            forged_value["seq"],
            forged_value["docId"],
        ),
    ).fetchone() is None


@pytest.mark.parametrize(
    ("space_id", "owner", "path", "section", "path_principal", "leaf"),
    [
        (
            "space-at-percent",
            "name@example",
            "directory/invites/plain.json",
            "invites",
            "user:name@example",
            "plain.json",
        ),
        (
            "space-unicode",
            "owner@example",
            "directory/devices/percent% owner/slash/value.json",
            "devices",
            "user:percent% owner",
            "slash/value.json",
        ),
        (
            "space-space",
            "owner@example",
            "directory/devices/unicode雪/at@ percent% colon: space 雪.json",
            "devices",
            "user:unicode雪",
            "at@ percent% colon: space 雪.json",
        ),
        (
            "space-slash",
            "owner/segment",
            "directory/invites/device:with-slash.json",
            "invites",
            "user:owner/segment",
            "device:with-slash.json",
        ),
    ],
)
def test_r4_c_protected_ids_round_trip_through_every_real_key_validator(
    space_id: str,
    owner: str,
    path: str,
    section: str,
    path_principal: str,
    leaf: str,
) -> None:
    doc_id = protected_directory_doc_id(space_id, path, space_owner=owner)
    assert doc_id is not None
    assert _SAFE_SEGMENT.fullmatch(doc_id)
    assert parse_protected_directory_doc_id(doc_id) == (
        space_id,
        section,
        path_principal,
        leaf,
    )

    keys = KeySpace()
    keys.orgfs_log(space_id, doc_id, "writer", 0)
    snapshot = b"snapshot"
    snapshot_id = hashlib.sha256(snapshot).hexdigest()
    keys.orgfs_snapshot(space_id, doc_id, snapshot_id)
    retirement = SimpleNamespace(
        retired=lambda _doc_id: None,
        purge_listed=lambda _digest: False,
        snapshot_point=lambda _doc_id: None,
    )
    replica = ReplicaStore(space_id, MemoryReplicaBackend(space_id), retirement)
    replica.store_envelope(f"log/{space_id}/{doc_id}/writer/0", b"envelope")
    replica.store_snapshot(
        f"snapshot/{space_id}/{doc_id}/{snapshot_id}", snapshot
    )


def test_r4_c_put_broadcast_and_receiver_replica_store_the_row(replicas) -> None:
    _tmp_path, space_id, owner, alice, _bob, _carol = replicas
    alice.serve(space_id, backend="memory")
    row = _device(
        OWNER,
        "opaque-device",
        address="tcp://10.0.0.7:7447#plaintext-test-psk",
    )
    OrgFsDirectoryStore(owner.facade).put_device("group-acme", row)
    owner.broadcast_pending(space_id)
    path = directory_device_path(OWNER, row.device_id)
    doc_id = protected_directory_doc_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert doc_id is not None

    _eventually(
        lambda: _equal(
            [item.device_id for item in OrgFsDirectoryStore(alice.facade).list_devices("group-acme")],
            [row.device_id],
        )
    )
    _eventually(
        lambda: _equal(
            any(
                f"/{doc_id}/" in key
                for key in alice._replicas[space_id].backend_keys("log/")
            ),
            True,
        )
    )


def test_r4_d_protected_path_purge_is_refused_without_leaking_content(
    replicas,
) -> None:
    _tmp_path, space_id, owner, _alice, _bob, _carol = replicas
    row = _device(
        OWNER,
        "purge-guarded",
        address="tcp://10.0.0.7:7447#must-not-appear-in-error",
    )
    store = OrgFsDirectoryStore(owner.facade)
    store.put_device("group-acme", row)
    path = directory_device_path(OWNER, row.device_id)
    node = owner.facade.stat(space_id, path)

    with pytest.raises(OrgFsError) as refused:
        owner.facade.purge_plan(
            space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": node.version,
                    "to": node.version,
                }
            ],
        )
    assert refused.value.code == "protected-path"
    assert refused.value.details == {"path": path}
    assert path in str(refused.value)
    assert row.address not in str(refused.value)

    updated = _device(
        OWNER,
        row.device_id,
        address="tcp://10.0.0.9:7447#second-plaintext-test-psk",
    )
    store.put_device("group-acme", updated)
    assert store.list_devices("group-acme") == [updated]


def test_r5_long_protected_path_is_stored_not_refused(replicas) -> None:
    """A protected id past a file-name length is stored: files are named by hash.

    Until 0.5.3 this path was refused with ``protected-doc-id-too-long``
    (a 240-character cap from ``docs/<id>.loro``); real Casdoor names hit it.
    """

    _tmp_path, space_id, owner, _alice, _bob, _carol = replicas
    owner.facade.mkdir(space_id, f"directory/devices/{OWNER}")
    path = f"directory/devices/{OWNER}/{'d' * 200}.json"
    owner.facade.write_text(space_id, path, "{}")
    text, _version = owner.facade.read_text(space_id, path)
    assert text == "{}"
