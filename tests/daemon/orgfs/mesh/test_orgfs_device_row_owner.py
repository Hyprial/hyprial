# Decisions:
# - Phase D T1: rewritten onto the state host.  The replicas are the in-memory
#   org of ``device_rows/support`` (owner, Alice, Bob); honest writes go
#   through the directory store or the shared daemon endpoint
#   (``DaemonApplication._handle_orgfs``), updates no honest node produces are
#   C2 forgeries delivered through ``SyncHost.apply_remote`` on every receiver.
# - Design §3.4: a member's local write to a path it does not own is
#   ``protected-path`` (was ``not-a-member``); §3.6: receivers reject the same
#   change with ``Rejected("protected-path")``.  The old "the writer's own
#   store admission says no" checks became the writer's local refusal.
# - Content that arrives before its tree row is ``Pending("unknown-doc")``
#   (design §3.6), so the arrival-order test delivers the content again once
#   the row is there, as the next pull would.
# - The protected ancestors include the root row, as before; a forged change
#   by a member to the root, and the owner's renames of the shared roots
#   (which leave a protected id at another path), are what the old receivers
#   refused.  Where the state host accepts them the test fails on purpose
#   (T1 report, defect repros).
# - F2 follow-up (design §9 admission supplement, ruling 2026-11-25): the root
#   row is not deleted, kind-changed or renamed by anyone, the owner included,
#   so the owner half now expects the root refused too.  Every owner forgery is
#   made from the same state before any is applied: an accepted soft delete of
#   ``directory`` used to make the next path lookup fail with ``unknown-doc``.
"""Device-row owner admission on the state host, on every replica."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.org.network.ipc import ipc_org_remove
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.orgfs.membership import Accepted, Rejected
from hyprial.identity import (
    default_role_grants_document,
    directory_device_path,
    protected_directory_doc_id,
    protected_directory_node_id,
)

from tests.daemon.orgfs.mesh.device_rows.support import (
    ALICE,
    BOB,
    DIRECTORY_ROOTS,
    ORG,
    OWNER,
    Org,
    device,
    new_row,
    principal,
    record_text,
)
from tests.support.tailnet_membership import (
    FakeMembershipSidecar,
    confirm_ipc_removal,
    member,
)


@pytest.fixture
def org():
    value = Org(members=(ALICE, BOB))
    try:
        yield value
    finally:
        value.close()


def _daemon(org: Org, user: str) -> DaemonApplication:
    application = object.__new__(DaemonApplication)
    application._orgfs_runtime = SimpleNamespace(facade=org.fs(user))
    return application


def _request_code(org: Org, user: str, method: str, params: dict[str, Any]) -> str:
    with pytest.raises(DaemonRequestError) as refused:
        _daemon(org, user)._handle_orgfs(method, params)
    return refused.value.code


def _read(org: Org, user: str, path: str) -> str:
    return str(
        _daemon(org, user)._handle_orgfs(
            "orgfs.read", {"spaceId": org.space_id, "node": path}
        )["text"]
    )


def _code(outcome: object) -> str | None:
    return outcome.code if isinstance(outcome, Rejected) else None


def _device_ids(org: Org, user: str) -> list[str]:
    return [item.device_id for item in org.directory(user).list_devices(ORG)]


class _OwnerApp:
    def __init__(self, org: Org, home: Path) -> None:
        self._runtime = SimpleNamespace(facade=org.fs(OWNER), node_id="node-owner")
        self.hyprial_home = home
        self.node_id = "node-owner"
        self.owner = OWNER
        self._forwarding_supervisor = FakeMembershipSidecar(owner=OWNER, device_id=self.node_id)
        self._forwarding_supervisor.seed(org.space_id, members=[
            self._forwarding_supervisor.local, member(ALICE, "node-alice"),
            member(BOB, "node-bob"),
        ])

    def _start_forwarding_supervisor(self) -> None:
        return None

    def _require_orgfs_runtime(self) -> Any:
        return self._runtime

    def _log(self, *_args, **_fields) -> None:
        return None

    def _reconcile_forwarding_endpoints(self) -> None:
        return None

    def _await_forwarding_reconcile(self, reason: str, *, timeout: float, **fields: object) -> None:
        return None

    def _request_forwarding_reconcile(self, reason: str, **fields: object) -> bool:
        return True

    def _forwarding_status_json(self) -> dict[str, object]:
        return {"policy": "off", "peers": []}


def test_genuine_author_content_and_tree_rules_hold_on_every_replica(org: Org) -> None:
    space_id = org.space_id
    row = device(ALICE, "opaque-a7", address="tcp://10.0.0.7:7447#a")
    org.directory(ALICE).put_device(ORG, row)
    org.deliver()
    path = directory_device_path(ALICE, row.device_id)
    for user in (OWNER, ALICE, BOB):
        assert _read(org, user, path) == record_text(row)

    own_update = device(ALICE, row.device_id, address="tcp://10.0.0.8:7447#a2")
    org.directory(ALICE).put_device(ORG, own_update)
    org.deliver()
    own_text = record_text(own_update)
    for user in (OWNER, ALICE, BOB):
        assert _read(org, user, path) == own_text

    org.directory(BOB).put_device(
        ORG, device(BOB, "random-not-hostname", address="tcp://10.0.0.9:7447#b")
    )
    org.deliver()
    for user in (OWNER, ALICE, BOB):
        assert _device_ids(org, user) == ["opaque-a7", "random-not-hostname"]

    forged_text = record_text(device(BOB, row.device_id, address="tcp://10.0.0.66:7447#hijack"))
    assert _request_code(
        org, BOB, "orgfs.write", {"spaceId": space_id, "path": path, "text": forged_text}
    ) == "protected-path"
    doc_id = org.fs(BOB).stat(space_id, path).doc_id
    assert doc_id is not None
    forged = org.forge_content(BOB, doc_id, forged_text, author=BOB)
    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, forged, peer="node-bob")) == "protected-path"
        assert _read(org, receiver, path) == own_text

    forged_path = directory_device_path(ALICE, "squatted")
    assert _request_code(
        org, BOB, "orgfs.write", {"spaceId": space_id, "path": forged_path, "text": "{}"}
    ) == "protected-path"
    assert _request_code(
        org, BOB, "orgfs.remove", {"spaceId": space_id, "node": path}
    ) == "protected-path"

    for user in (OWNER, ALICE, BOB):
        assert _read(org, user, path) == own_text
        assert _request_code(
            org, user, "orgfs.stat", {"spaceId": space_id, "node": forged_path}
        ) == "unknown-doc"


def test_owner_member_removal_leaves_rows_inert_on_every_replica(org: Org, tmp_path: Path) -> None:
    space_id = org.space_id
    owner_fs = org.fs(OWNER)
    acl = owner_fs.create_space("group-acme-acl")
    owner_fs.mkdir(acl.space_id, "policy")
    owner_fs.write_text(
        acl.space_id,
        ACL_POLICY_DOC,
        json.dumps(default_role_grants_document(), sort_keys=True, separators=(",", ":")),
    )
    owner_fs.update_space_meta(space_id, {"aclSpaceId": acl.space_id})
    org.directory(ALICE).put_device(ORG, device(ALICE, "alice-device"))
    org.deliver()
    path = directory_device_path(ALICE, "alice-device")
    for user in (OWNER, ALICE, BOB):
        assert _read(org, user, path)

    application = _OwnerApp(org, tmp_path / "owner-home")
    result = confirm_ipc_removal(
        ipc_org_remove, application, {"org": ORG, "user": principal(ALICE)}
    )
    assert result["phase"] == "complete"
    assert result["deviceIds"] == ["node-alice"]
    assert application._forwarding_supervisor.tailnets[space_id]["members"]["node-alice"]["state"] == "tombstone"
    org.deliver()

    for user in (OWNER, ALICE, BOB):
        assert _request_code(
            org, user, "orgfs.stat", {"spaceId": space_id, "node": path}
        ) == "unknown-doc"
        assert principal(ALICE) not in {item.user for item in org.fs(user).members(space_id)}


def test_member_cannot_delete_directory_ancestor_on_any_replica(org: Org) -> None:
    """B2: genuine Bob cannot blank the directory by deleting its ancestor."""

    org.directory(BOB).put_device(ORG, device(BOB, "bob-device"))
    org.deliver()
    directory = org.node_id(BOB, "directory")
    forged = org.forge_rewrite(BOB, directory, author=BOB, deleted=True)

    assert org.missing(BOB, "directory") is False
    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, forged, peer="node-bob")) == "protected-path"
        assert org.fs(receiver).stat(org.space_id, "directory").deleted is False
        assert _device_ids(org, receiver) == ["bob-device"]


def test_member_cannot_add_duplicate_devices_root_on_any_replica(org: Org) -> None:
    """B3: a same-name shared root never creates permanent ambiguity."""

    org.directory(BOB).put_device(ORG, device(BOB, "bob-device"))
    org.deliver()
    directory = org.node_id(BOB, "directory")
    forged = org.forge_rows(
        BOB,
        [new_row("squatter-devices-root", parent=directory, name="devices", kind="dir")],
        author=BOB,
    )

    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, forged, peer="node-bob")) == "protected-path"
        assert org.fs(receiver).stat(org.space_id, "directory/devices").kind == "dir"
        assert _device_ids(org, receiver) == ["bob-device"]


def test_link_refuses_a_non_deterministic_doc_id_on_every_replica(org: Org) -> None:
    """R3-H: protected paths never adopt arbitrary physical documents."""

    space_id = org.space_id
    org.directory(ALICE).put_device(ORG, device(ALICE, "alice-device"))
    org.directory(BOB).put_device(ORG, device(BOB, "bob-device"))
    polluted = org.fs(BOB).write_text(space_id, "polluted.txt", "polluted")
    org.deliver()
    assert polluted.doc_id is not None
    parent = org.node_id(ALICE, f"directory/devices/{ALICE}")
    forged = org.forge_rows(
        ALICE,
        [new_row("polluted-link", parent=parent, name="linked-polluted.json", doc_id=polluted.doc_id)],
        author=ALICE,
    )

    for receiver in (OWNER, BOB):
        outcome = org.apply(receiver, forged, peer="node-alice")
        assert not isinstance(outcome, Accepted), receiver
        assert org.missing(receiver, f"directory/devices/{ALICE}/linked-polluted.json")


def test_deterministic_device_admission_converges_in_both_arrival_orders(org: Org) -> None:
    """R3-A: content/pollution/link order cannot split replicas or wedge trees."""

    space_id = org.space_id
    row = device(ALICE, "arrival:device", address="tcp://10.0.0.7:7447#alice")
    path = directory_device_path(ALICE, row.device_id)
    doc_id = protected_directory_doc_id(space_id, path, space_owner=principal(OWNER))
    assert doc_id is not None
    org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}")
    org.deliver()
    expected = record_text(row)
    frames = org.capture(
        ALICE,
        lambda: _daemon(org, ALICE)._handle_orgfs(
            "orgfs.write", {"spaceId": space_id, "path": path, "text": expected}
        ),
    )
    (content,) = [frame for frame in frames if frame.doc_id == doc_id]
    (link,) = [frame for frame in frames if frame.doc_id == "tree"]
    pollution = org.forge_content(None, doc_id, "polluted", author=BOB)

    early = [org.apply(OWNER, content, peer="node-alice"), org.apply(OWNER, pollution, peer="node-bob")]
    assert not any(isinstance(outcome, Accepted) for outcome in early)
    assert org.apply(OWNER, link, peer="node-alice") == Accepted()
    assert org.apply(OWNER, content, peer="node-alice") == Accepted()
    assert _code(org.apply(OWNER, pollution, peer="node-bob")) == "protected-path"

    assert org.apply(BOB, link, peer="node-alice") == Accepted()
    assert _code(org.apply(BOB, pollution, peer="node-bob")) == "protected-path"
    assert org.apply(BOB, content, peer="node-alice") == Accepted()
    assert isinstance(org.apply(BOB, pollution, peer="node-bob"), Rejected)
    for receiver in (OWNER, BOB):
        assert _read(org, receiver, path) == expected
        assert org.fs(receiver).document_authors(space_id, path) == (principal(ALICE),)

    org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}/later")
    org.deliver()
    for receiver in (OWNER, BOB):
        assert org.fs(receiver).stat(space_id, f"directory/devices/{ALICE}/later").kind == "dir"


def test_leave_and_invite_doc_ids_enforce_their_principals_on_every_replica(org: Org) -> None:
    """R3-B/C: only self may leave and only the space owner may invite."""

    space_id = org.space_id
    with pytest.raises(Exception) as leave_rejected:
        org.directory(BOB).put_leave_request(ORG, ALICE, {"user": ALICE, "requestedAt": "forged"})
    assert getattr(leave_rejected.value, "code", None) == "protected-path"
    with pytest.raises(Exception) as invite_rejected:
        org.directory(BOB).put_invite(ORG, "official-forgery", {"invite": True})
    assert getattr(invite_rejected.value, "code", None) == "protected-path"

    for forged_path in (f"directory/leaves/{ALICE}.json", "directory/invites/forged.json"):
        parent, name = forged_path.rsplit("/", 1)
        node_id = protected_directory_node_id(space_id, forged_path, space_owner=principal(OWNER))
        doc_id = protected_directory_doc_id(space_id, forged_path, space_owner=principal(OWNER))
        assert node_id is not None and doc_id is not None
        tree = org.forge_rows(
            BOB,
            [new_row(node_id, parent=org.node_id(BOB, parent), name=name, doc_id=doc_id)],
            author=BOB,
        )
        content = org.forge_content(None, doc_id, '{"user":"alice"}', author=BOB)
        for receiver in (OWNER, ALICE):
            assert _code(org.apply(receiver, tree, peer="node-bob")) == "protected-path"
            assert not isinstance(org.apply(receiver, content, peer="node-bob"), Accepted)
            assert org.missing(receiver, forged_path)

    org.directory(ALICE).put_leave_request(ORG, ALICE, {"user": ALICE, "requestedAt": "now"})
    org.deliver()
    for user in (OWNER, ALICE, BOB):
        assert ALICE in org.directory(user).list_leave_requests(ORG)


def test_org_meta_is_owner_only_on_every_replica(org: Org) -> None:
    """R3-I: only the owner may create, update, or move ``org.json``."""

    space_id = org.space_id
    path = "directory/org.json"
    owner_text = '{"state":"owner-written"}'
    member_write = {"spaceId": space_id, "path": path, "text": '{"state":"deleted"}'}

    assert _request_code(org, BOB, "orgfs.write", member_write) == "protected-path"
    _daemon(org, OWNER)._handle_orgfs(
        "orgfs.write", {"spaceId": space_id, "path": path, "text": owner_text}
    )
    org.deliver()
    for user in (OWNER, ALICE, BOB):
        assert _read(org, user, path) == owner_text
    assert _request_code(org, BOB, "orgfs.write", member_write) == "protected-path"

    doc_id = org.fs(BOB).stat(space_id, path).doc_id
    assert doc_id is not None
    content = org.forge_content(BOB, doc_id, '{"state":"deleted"}', author=BOB)
    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, content, peer="node-bob")) == "protected-path"
        assert _read(org, receiver, path) == owner_text

    renamed = org.forge_rewrite(
        BOB, org.node_id(BOB, path), author=BOB, name="member-renamed.json"
    )
    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, renamed, peer="node-bob")) == "protected-path"
        assert org.fs(receiver).stat(space_id, path).name == "org.json"


@pytest.mark.parametrize("change", [{"deleted": True}, {"kind": "doc"}, {"name": "renamed"}])
def test_root_and_directory_ancestors_are_owner_only_on_every_replica(
    org: Org, change: dict[str, object]
) -> None:
    """R3-D: members cannot delete, kind-change, or rename protected ancestors."""

    verdicts: dict[str, list[str | None]] = {}
    for path in ("", *DIRECTORY_ROOTS):
        forged = org.forge_rewrite(BOB, org.node_id(BOB, path), author=BOB, **change)
        verdicts[path] = [_code(org.apply(receiver, forged, peer="node-bob")) for receiver in (OWNER, ALICE)]
    assert verdicts == {path: ["protected-path", "protected-path"] for path in ("", *DIRECTORY_ROOTS)}

    node_ids = {path: org.node_id(BOB, path) for path in ("", *DIRECTORY_ROOTS)}
    owner_frames = {
        path: org.forge_rewrite(BOB, node_id, author=OWNER, **change)
        for path, node_id in node_ids.items()
    }
    owner_verdicts = {
        path: isinstance(org.apply(BOB, forged, peer="node-owner"), Accepted)
        for path, forged in owner_frames.items()
    }
    assert owner_verdicts == {
        path: bool(path) and "name" not in change for path in ("", *DIRECTORY_ROOTS)
    }
    assert org.rows(BOB)["root"]["kind"] == "dir" and not org.rows(BOB)["root"].get("deleted")
    assert isinstance(org.listing(BOB), tuple)
