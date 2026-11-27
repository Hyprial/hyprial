"""The service-grants author rule on real replicas, adapted from a review probe.

Decision (service grant documents fix, 2026-11-26): forged tree nodes and
content from another member or from the org owner are rejected by every other
replica's admission, the org owner cannot write in a member's path, and
documents a member may legitimately write in their own path (malformed, deeply
nested, oversized) never fail another replica's load.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.service_grants import load_org_service_grants
from hyprial.identity import protected_directory_doc_id, protected_directory_node_id

from tests.daemon.orgfs.mesh.device_rows.test_round4 import (
    ALICE,
    BOB,
    OWNER,
    _add_tree_doc,
    _commits_since,
    _position,
    _receive,
    replicas,
)
from tests.daemon.orgfs.mesh.device_rows.test_round7 import _add_dir, _rewrite, _tree_envelope

_FIXTURES = (replicas,)
ORG = "group-acme"
GID_A = "sg.aaaaaaaaaaaaaaaaaaaaaaaaaa"
GID_B = "sg.bbbbbbbbbbbbbbbbbbbbbbbbbb"
GID_C = "sg.cccccccccccccccccccccccccc"
GID_E = "sg.eeeeeeeeeeeeeeeeeeeeeeeeee"


def _record(grant_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "grantId": grant_id,
        "service": {"deviceId": "device-a", "port": 8447, "name": "svc", "protocol": "http", "note": None},
        "recipient": {"owner": BOB, "deviceId": "device-b"},
        "grantedAtMs": 1,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }
    base.update(over)
    return base


def _deliver(source, targets, space_id: str, after: int) -> set[tuple[str, str | None]]:
    statuses = set()
    for envelope in _commits_since(source, space_id, after):
        for target in targets:
            result = _receive(target, space_id, envelope, source.node_id)
            statuses.add((result.status, result.code))
            if result.status == "applied" and result.code == "duplicate":
                target.facade.apply_envelope(space_id, envelope)
    return statuses


def _content_envelope(runtime, space_id: str, *, doc_id: str, author: str, content: str, existing: bool) -> bytes:
    store = runtime.stores[space_id]
    writer = store._writer_for(f"user:{author}", None)
    doc = Doc(client_id=store._client_id(writer))
    if existing:
        doc.apply_update(store.snapshot(doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    text = doc.get("text", type=Text)
    if existing:
        text.clear()
    text.insert(0, content)
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits WHERE writer = ? AND doc_id = ?",
            (writer, doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        doc_id, writer, seq, doc.get_update(before),
        author=f"user:{author}", actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _verdicts(receivers, space_id: str, envelope: bytes, source) -> set[tuple[str, str | None]]:
    for meta in _commits_since(source, space_id, 0):
        if json.loads(meta)["docId"] == "meta":
            for receiver in receivers:
                _receive(receiver, space_id, meta, source.node_id)
    return {
        (result.status, result.code)
        for result in (_receive(receiver, space_id, envelope, source.node_id) for receiver in receivers)
    }


def _listed(runtime) -> list[tuple[str, str]]:
    return [(owner, gid) for owner, gid, _ in OrgFsDirectoryStore(runtime.facade).list_service_grants(ORG)]


@pytest.fixture
def rooted(replicas):
    _tmp, space_id, owner, alice, bob, carol = replicas
    for member in (alice, bob):
        with pytest.raises(OrgFsError) as caught:
            member.facade.mkdir(space_id, "directory/service-grants")
        assert caught.value.code == "not-a-member"
    before = _position(owner, space_id)
    OrgFsDirectoryStore(owner.facade).ensure_org_directory_roots(ORG)
    _deliver(owner, (alice, bob, carol), space_id, before)
    return replicas


@pytest.fixture
def seeded(rooted):
    _tmp, space_id, owner, alice, bob, carol = rooted
    before = _position(alice, space_id)
    OrgFsDirectoryStore(alice.facade).put_service_grant(ORG, ALICE, GID_A, _record(GID_A))
    _deliver(alice, (owner, bob, carol), space_id, before)
    return rooted


def test_forged_owner_directories_are_rejected_by_every_other_replica(rooted) -> None:
    """Neither another member nor the org owner creates alice's directory."""

    _tmp, space_id, owner, alice, bob, carol = rooted
    parent = owner.facade.stat(space_id, "directory/service-grants")
    alice_dir_id = protected_directory_node_id(
        space_id, f"directory/service-grants/{ALICE}", space_owner=f"user:{OWNER}"
    )
    for author, actor in ((BOB, bob), (OWNER, owner)):
        receivers = [r for r in (owner, alice, carol) if r is not actor]
        for node_id in (alice_dir_id, "node-random-squat-1"):
            envelope = _tree_envelope(
                actor, space_id, author=author,
                mutate=lambda doc: _add_dir(doc, node_id=node_id, parent=parent.node_id, name=ALICE),
            )
            assert _verdicts(receivers, space_id, envelope, actor) == {("rejected", "not-a-member")}


def test_local_writes_in_another_members_path_are_refused(seeded) -> None:
    """Neither another member nor the org owner writes in alice's path."""

    _tmp, space_id, owner, _alice, bob, _carol = seeded
    alice_dir = f"directory/service-grants/{ALICE}"
    path_a = f"{alice_dir}/{GID_A}.json"
    attempts = (
        lambda rt: rt.facade.write_text(space_id, f"{alice_dir}/{GID_B}.json", json.dumps(_record(GID_B))),
        lambda rt: rt.facade.write_text(space_id, path_a, json.dumps(_record(GID_A, recipient="@members"))),
        lambda rt: rt.facade.mkdir(space_id, f"{alice_dir}/sub"),
    )
    for runtime in (bob, owner):
        for attempt in attempts:
            with pytest.raises(OrgFsError) as caught:
                attempt(runtime)
            assert caught.value.code == "not-a-member"
    for remove in (path_a, alice_dir, "directory/service-grants"):
        with pytest.raises(OrgFsError):
            bob.facade.remove(space_id, remove)


def test_forged_nodes_and_content_are_rejected_by_every_other_replica(seeded) -> None:
    """Remote admission, not only the local write path, enforces the author rule."""

    _tmp, space_id, owner, alice, bob, carol = seeded
    space_owner = f"user:{OWNER}"
    alice_dir = f"directory/service-grants/{ALICE}"
    alice_dir_id = protected_directory_node_id(space_id, alice_dir, space_owner=space_owner)
    path_a = f"{alice_dir}/{GID_A}.json"
    doc_a = protected_directory_doc_id(space_id, path_a, space_owner=space_owner)
    node_a = alice.facade.stat(space_id, path_a)
    path_b = f"{alice_dir}/{GID_B}.json"
    node_b = protected_directory_node_id(space_id, path_b, space_owner=space_owner)
    doc_b = protected_directory_doc_id(space_id, path_b, space_owner=space_owner)
    rejected = {("rejected", "not-a-member")}

    for author, actor in ((BOB, bob), (OWNER, owner)):
        receivers = [r for r in (owner, alice, carol) if r is not actor]
        forged = [
            _content_envelope(actor, space_id, doc_id=doc_a, author=author, content=json.dumps(_record(GID_A, recipient="@members")), existing=True),
            _tree_envelope(actor, space_id, author=author, mutate=lambda doc: _add_tree_doc(doc.get("nodes", type=Map), node_id=node_b, parent=alice_dir_id, name=f"{GID_B}.json", doc_id=doc_b)),
            _content_envelope(actor, space_id, doc_id=doc_b, author=author, content=json.dumps(_record(GID_B)), existing=False),
            _tree_envelope(actor, space_id, author=author, mutate=lambda doc: _add_tree_doc(doc.get("nodes", type=Map), node_id="node-random-doc-1", parent=alice_dir_id, name=f"{GID_C}.json", doc_id="doc-00000000-0000-4000-8000-000000000077")),
            _tree_envelope(actor, space_id, author=author, mutate=lambda doc: _rewrite(doc, node_a.node_id, deleted=True)),
            _tree_envelope(actor, space_id, author=author, mutate=lambda doc: _rewrite(doc, alice_dir_id, deleted=True)),
            _tree_envelope(actor, space_id, author=author, mutate=lambda doc: _rewrite(doc, node_a.node_id, name=f"{GID_C}.json")),
        ]
        for envelope in forged:
            assert _verdicts(receivers, space_id, envelope, actor) == rejected

    for runtime in (owner, alice, carol):
        assert _listed(runtime) == [(ALICE, GID_A)]
        [(_, _, record)] = OrgFsDirectoryStore(runtime.facade).list_service_grants(ORG)
        assert record["recipient"] == {"owner": BOB, "deviceId": "device-b"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not-json"),
        pytest.param(json.dumps({**_record(GID_E), "owner": ALICE}), id="extra-owner-key"),
        pytest.param(json.dumps(_record(GID_A)), id="grant-id-mismatch"),
        pytest.param("[" * 100000 + "]" * 100000, id="deep-array-200k"),
        pytest.param("[" * 30000 + "]" * 30000, id="deep-array-under-64k"),
    ],
)
def test_member_documents_in_own_path_never_fail_another_replicas_load(seeded, body: str) -> None:
    """On replicas: bob's admitted bad document is skipped by alice's load."""

    _tmp, space_id, owner, alice, bob, carol = seeded
    before = _position(bob, space_id)
    bob.facade.mkdir(space_id, f"directory/service-grants/{BOB}")
    path = f"directory/service-grants/{BOB}/{GID_E}.json"
    bob.facade.write_text(space_id, path, body)
    _deliver(bob, (owner, alice, carol), space_id, before)
    assert alice.facade.stat(space_id, path).kind == "doc"

    loaded = load_org_service_grants(OrgFsDirectoryStore(alice.facade), ORG)
    assert [(item.grantor, item.grant.grant_id) for item in loaded] == [(ALICE, GID_A)]


def test_nested_credential_on_replicas_is_skipped(seeded) -> None:
    """On replicas: a 700-level credential does not raise ``RecursionError``."""

    _tmp, space_id, owner, alice, bob, carol = seeded
    nested: object = {"x": 1}
    for _ in range(700):
        nested = {"n": nested}
    before = _position(bob, space_id)
    OrgFsDirectoryStore(bob.facade).put_service_grant(ORG, BOB, GID_E, _record(GID_E, credential=nested))
    _deliver(bob, (owner, alice, carol), space_id, before)
    assert alice.facade.stat(space_id, f"directory/service-grants/{BOB}/{GID_E}.json").kind == "doc"

    loaded = load_org_service_grants(OrgFsDirectoryStore(alice.facade), ORG)
    assert [(item.grantor, item.grant.grant_id) for item in loaded] == [(ALICE, GID_A)]
