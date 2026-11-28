"""The service-grants author rule on real state-host replicas.

Decision (service grant documents fix, 2026-11-26): forged tree nodes and
content from another member or from the org owner are rejected by every other
replica's admission, the org owner cannot write in a member's path, and
documents a member may legitimately write in their own path (malformed, deeply
nested, oversized) never fail another replica's load.

Decision (OrgFS protocol 3 adaptation, 2026-11-27): the journal envelopes and
replica helpers were replaced by the state host's in-process org and frame
helpers. A forged content birth whose tree row was rejected is pending for the
missing tree dependency rather than rejected as a known path; it must still
never become visible. Pending content is a temporary read failure, not an
absent grant. Soft deletion converges, and replaying pre-delete frames cannot
resurrect a grant.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import Accepted, Pending, Rejected
from hyprial.daemon.impl.service_grants import load_org_service_grants
from hyprial.identity import protected_directory_doc_id, protected_directory_node_id

from tests.daemon.orgfs.coordinator.support import clone_node
from tests.daemon.orgfs.membership.support import Frame
from tests.daemon.orgfs.mesh.device_rows.support import (
    ALICE,
    BOB,
    CAROL,
    OWNER,
    ORG,
    Org,
    new_row,
)

GID_A = "sg.aaaaaaaaaaaaaaaaaaaaaaaaaa"
GID_B = "sg.bbbbbbbbbbbbbbbbbbbbbbbbbb"
GID_C = "sg.cccccccccccccccccccccccccc"
GID_E = "sg.eeeeeeeeeeeeeeeeeeeeeeeeee"


def _record(grant_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "grantId": grant_id,
        "service": {
            "deviceId": "device-a",
            "port": 8447,
            "name": "svc",
            "protocol": "http",
            "note": None,
        },
        "recipient": {"owner": BOB, "deviceId": "device-b"},
        "grantedAtMs": 1,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }
    base.update(over)
    return base


def _put_alice_grant(org: Org) -> None:
    org.directory(ALICE).put_service_grant(ORG, ALICE, GID_A, _record(GID_A))


def _listed(org: Org, user: str) -> list[tuple[str, str]]:
    return [
        (owner, grant_id)
        for owner, grant_id, _record in org.directory(user).list_service_grants(ORG)
    ]


def _apply(org: Org, receiver: str, frame: Frame, author: str) -> object:
    return org.apply(receiver, frame, peer=f"node-{author.split('@', 1)[0]}")


def _assert_rejected(outcome: object) -> None:
    assert isinstance(outcome, Rejected)
    assert outcome.code == "protected-path"


@pytest.fixture
def rooted():
    org = Org()
    try:
        org.directory(OWNER).ensure_org_directory_roots(ORG)
        org.deliver()
        yield org
    finally:
        org.close()


@pytest.fixture
def seeded(rooted: Org):
    _put_alice_grant(rooted)
    rooted.deliver()
    return rooted


def test_forged_owner_directories_are_rejected_by_every_other_replica(rooted: Org) -> None:
    """Neither another member nor the org owner creates alice's directory."""

    space_id = rooted.space_id
    parent = rooted.fs(OWNER).stat(space_id, "directory/service-grants")
    alice_dir_id = protected_directory_node_id(
        space_id,
        f"directory/service-grants/{ALICE}",
        space_owner=f"user:{OWNER}",
    )
    for author in (BOB, OWNER):
        receivers = [user for user in (OWNER, ALICE, CAROL) if user != author]
        for node_id in (alice_dir_id, "node-random-squat-1"):
            forged = rooted.forge_rows(
                author,
                [
                    new_row(
                        node_id,
                        parent=parent.node_id,
                        name=ALICE,
                        kind="dir",
                    )
                ],
                author=author,
            )
            for receiver in receivers:
                _assert_rejected(_apply(rooted, receiver, forged, author))


def test_local_writes_in_another_members_path_are_refused(seeded: Org) -> None:
    """Neither another member nor the org owner writes in alice's path."""

    space_id = seeded.space_id
    alice_dir = f"directory/service-grants/{ALICE}"
    path_a = f"{alice_dir}/{GID_A}.json"
    for author in (BOB, OWNER):
        fs = seeded.fs(author)
        attempts = (
            lambda: fs.write_text(
                space_id,
                f"{alice_dir}/{GID_B}.json",
                json.dumps(_record(GID_B)),
            ),
            lambda: fs.write_text(
                space_id,
                path_a,
                json.dumps(_record(GID_A, recipient="@members")),
            ),
            lambda: fs.mkdir(space_id, f"{alice_dir}/sub"),
        )
        for attempt in attempts:
            with pytest.raises(OrgFsError) as caught:
                attempt()
            assert caught.value.code == "protected-path"
    for path in (path_a, alice_dir, "directory/service-grants"):
        with pytest.raises(OrgFsError) as caught:
            seeded.fs(BOB).remove(space_id, path)
        assert caught.value.code == "protected-path"


def test_forged_nodes_and_content_never_change_other_replicas(seeded: Org) -> None:
    """Remote admission enforces authorship; unknown forged births stay pending."""

    space_id = seeded.space_id
    space_owner = f"user:{OWNER}"
    alice_dir = f"directory/service-grants/{ALICE}"
    alice_dir_id = protected_directory_node_id(
        space_id, alice_dir, space_owner=space_owner
    )
    path_a = f"{alice_dir}/{GID_A}.json"
    doc_a = protected_directory_doc_id(space_id, path_a, space_owner=space_owner)
    node_a = seeded.fs(ALICE).stat(space_id, path_a)
    path_b = f"{alice_dir}/{GID_B}.json"
    node_b = protected_directory_node_id(space_id, path_b, space_owner=space_owner)
    doc_b = protected_directory_doc_id(space_id, path_b, space_owner=space_owner)

    for author in (BOB, OWNER):
        receivers = [user for user in (OWNER, ALICE, CAROL) if user != author]
        forged = (
            seeded.forge_content(
                author,
                doc_a,
                json.dumps(_record(GID_A, recipient="@members")),
                author=author,
            ),
            seeded.forge_rows(
                author,
                [
                    new_row(
                        node_b,
                        parent=alice_dir_id,
                        name=f"{GID_B}.json",
                        doc_id=doc_b,
                    )
                ],
                author=author,
            ),
            seeded.forge_rows(
                author,
                [
                    new_row(
                        "node-random-doc-1",
                        parent=alice_dir_id,
                        name=f"{GID_C}.json",
                        doc_id="doc-00000000-0000-4000-8000-000000000077",
                    )
                ],
                author=author,
            ),
            seeded.forge_rewrite(author, node_a.node_id, author=author, deleted=True),
            seeded.forge_rewrite(author, alice_dir_id, author=author, deleted=True),
            seeded.forge_rewrite(
                author, node_a.node_id, author=author, name=f"{GID_C}.json"
            ),
        )
        for frame in forged:
            for receiver in receivers:
                _assert_rejected(_apply(seeded, receiver, frame, author))

        content_birth = seeded.forge_content(
            None,
            doc_b,
            json.dumps(_record(GID_B)),
            author=author,
        )
        for receiver in receivers:
            outcome = _apply(seeded, receiver, content_birth, author)
            assert isinstance(outcome, Pending)
            assert outcome.reason == "unknown-doc"

    for receiver in (OWNER, ALICE, CAROL):
        assert _listed(seeded, receiver) == [(ALICE, GID_A)]
        [(_, _, record)] = seeded.directory(receiver).list_service_grants(ORG)
        assert record["recipient"] == {"owner": BOB, "deviceId": "device-b"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not-json"),
        pytest.param(
            json.dumps({**_record(GID_E), "owner": ALICE}), id="extra-owner-key"
        ),
        pytest.param(json.dumps(_record(GID_A)), id="grant-id-mismatch"),
        pytest.param("[" * 100000 + "]" * 100000, id="deep-array-200k"),
        pytest.param("[" * 30000 + "]" * 30000, id="deep-array-under-64k"),
    ],
)
def test_member_documents_in_own_path_never_fail_another_replicas_load(
    seeded: Org, body: str
) -> None:
    """On replicas: bob's admitted bad document is skipped by alice's load."""

    space_id = seeded.space_id
    seeded.fs(BOB).mkdir(space_id, f"directory/service-grants/{BOB}")
    path = f"directory/service-grants/{BOB}/{GID_E}.json"
    seeded.fs(BOB).write_text(space_id, path, body)
    replica = clone_node(
        seeded.nodes[BOB], space_id, f"user:{ALICE}", "node-alice-replica"
    )
    try:
        assert replica.fs.stat(space_id, path).kind == "doc"
        loaded = load_org_service_grants(OrgFsDirectoryStore(replica.fs), ORG)
        assert [(item.grantor, item.grant.grant_id) for item in loaded] == [(ALICE, GID_A)]
    finally:
        replica.close()


def test_nested_credential_on_replicas_is_skipped(seeded: Org) -> None:
    """On replicas: a 700-level credential does not raise ``RecursionError``."""

    nested: object = {"x": 1}
    for _ in range(700):
        nested = {"n": nested}
    seeded.directory(BOB).put_service_grant(
        ORG, BOB, GID_E, _record(GID_E, credential=nested)
    )
    seeded.deliver()
    assert (
        seeded.fs(ALICE).stat(
            seeded.space_id, f"directory/service-grants/{BOB}/{GID_E}.json"
        ).kind
        == "doc"
    )

    loaded = load_org_service_grants(seeded.directory(ALICE), ORG)
    assert [(item.grantor, item.grant.grant_id) for item in loaded] == [(ALICE, GID_A)]


def test_content_pending_is_not_an_absent_grant(rooted: Org) -> None:
    """A tree-only replica reports pending until the content frame arrives."""

    frames = rooted.capture(ALICE, lambda: _put_alice_grant(rooted))
    tree = [frame for frame in frames if frame.doc_id == "tree"]
    content = [frame for frame in frames if frame.doc_id != "tree"]
    assert tree and content
    for receiver in (OWNER, BOB, CAROL):
        for frame in tree:
            assert isinstance(_apply(rooted, receiver, frame, ALICE), Accepted)
        path = f"directory/service-grants/{ALICE}/{GID_A}.json"
        entry = rooted.fs(receiver).stat(rooted.space_id, path)
        assert entry.content_state == "pending"
        with pytest.raises(OrgFsError) as caught:
            rooted.directory(receiver).read_service_grant(ORG, ALICE, GID_A)
        assert caught.value.code == "content-pending"
        with pytest.raises(OrgFsError) as caught:
            rooted.directory(receiver).list_service_grants(ORG)
        assert caught.value.code == "content-pending"

        for frame in content:
            _apply(rooted, receiver, frame, ALICE)
        assert _listed(rooted, receiver) == [(ALICE, GID_A)]


def test_delete_converges_and_late_frames_do_not_resurrect(rooted: Org) -> None:
    """Cleanup deletion wins on every replica and cannot be undone by old frames."""

    creation = rooted.capture(ALICE, lambda: _put_alice_grant(rooted))
    assert creation
    for receiver in (OWNER, BOB, CAROL):
        for frame in creation:
            _apply(rooted, receiver, frame, ALICE)
        assert _listed(rooted, receiver) == [(ALICE, GID_A)]
    rooted.fs(ALICE).remove(
        rooted.space_id, f"directory/service-grants/{ALICE}/{GID_A}.json"
    )
    rooted.deliver()
    for receiver in (OWNER, ALICE, BOB, CAROL):
        assert _listed(rooted, receiver) == []
        for frame in creation:
            _apply(rooted, receiver, frame, ALICE)
        assert _listed(rooted, receiver) == []
