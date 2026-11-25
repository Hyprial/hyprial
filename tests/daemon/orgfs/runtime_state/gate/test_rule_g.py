# Decisions (phase F1, node gate ruling, 2026-11-25):
# - Each clause of rule G is pinned on one runtime with trust off, through
#   ``_node_gate`` (the callback every StateSpaceSync of the space uses):
#   global resolution over every org directory held here (a plain space
#   resolves too), the injected hint as bootstrap source, a removed member's
#   principal refused whatever the source, conflicts refused with
#   ``supplier-owner-conflict``, a failing hint refused with
#   ``supplier-owner-unresolved``, and the directory index rebuilt after a
#   tree-only update (a new device row, no member-table change).
"""Clauses of the node gate ruling (rule G) on the state runtime."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime

from ..support import Peer, Peers
from .probe import OWNER, device

MEMBER = "user:member@example"
HINT: dict[str, str] = {}


def _runtime(state_dir: Any, **kwargs: Any) -> StateOrgFsRuntime:
    return StateOrgFsRuntime(state_dir, device_owner_hint=HINT.get, **kwargs)


def _owner(peers: Peers, hint: dict[str, str] | None = None) -> Peer:
    HINT.clear()
    HINT.update(hint or {})
    return peers.add("node-owner", OWNER, trust=False, runtime_class=_runtime)


def _org(owner: Peer, name: str = "group-acme") -> str:
    space_id = owner.fs.create_space(name).space_id
    store = OrgFsDirectoryStore(owner.fs)
    store.ensure_org_directory_roots(name)
    store.put_device(name, device(OWNER, "node-owner"))
    return space_id


def _gate(owner: Peer, space_id: str, node: str) -> bool:
    assert owner.runtime is not None
    return owner.runtime._node_gate(space_id)(node)  # noqa: SLF001 - the space's node gate


def _reasons(owner: Peer) -> list[object]:
    return [fields.get("reason") for event, fields in owner.events.seen if event == "orgfs.update.rejected"]


def test_a_plain_space_resolves_through_any_org_directory_held_here(peers: Peers) -> None:
    owner = _owner(peers)
    _org(owner)
    plain = owner.fs.create_space("notes").space_id

    assert _gate(owner, plain, "node-owner")
    assert not _gate(owner, plain, "node-stranger")


def test_the_hint_admits_a_member_without_a_device_row_and_never_a_removed_one(peers: Peers) -> None:
    owner = _owner(peers, {"node-member": MEMBER, "node-stranger": "user:stranger@example"})
    directory = _org(owner)
    plain = owner.fs.create_space("notes").space_id
    for space_id in (directory, plain):
        owner.fs.invite(space_id, MEMBER, "rw")

    assert _gate(owner, directory, "node-member") and _gate(owner, plain, "node-member")
    assert not _gate(owner, plain, "node-stranger")
    assert not _gate(owner, plain, "node-unknown")
    owner.fs.remove_member(plain, MEMBER)
    assert not _gate(owner, plain, "node-member")
    assert _gate(owner, directory, "node-member")


def test_a_hint_naming_another_principal_than_the_directory_is_refused(peers: Peers) -> None:
    owner = _owner(peers, {"node-owner": MEMBER})
    plain = owner.fs.create_space("notes").space_id
    owner.fs.invite(plain, MEMBER, "rw")
    _org(owner)

    assert not _gate(owner, plain, "node-owner")
    assert "supplier-owner-conflict" in _reasons(owner)


def test_two_directories_naming_different_principals_are_refused(peers: Peers) -> None:
    owner = _owner(peers)
    first = _org(owner, "group-acme")
    second = _org(owner, "group-beta")
    plain = owner.fs.create_space("notes").space_id
    owner.fs.invite(plain, MEMBER, "rw")
    assert owner.runtime is not None
    runtime = owner.runtime
    indexes = {first: {"node-x": OWNER}, second: {"node-x": MEMBER}}
    runtime._device_owner_index = lambda directory: indexes.get(directory, {})  # type: ignore[method-assign]  # noqa: SLF001

    assert not _gate(owner, plain, "node-x")
    assert "supplier-owner-conflict" in _reasons(owner)


def test_a_failing_hint_refuses_the_node(peers: Peers) -> None:
    owner = _owner(peers)
    plain = owner.fs.create_space("notes").space_id
    assert owner.runtime is not None

    def broken(_node: str) -> str | None:
        raise RuntimeError("roster unavailable")

    owner.runtime.bind_device_owner_hint(broken)
    assert not _gate(owner, plain, "node-owner")
    assert "supplier-owner-unresolved" in _reasons(owner)


def test_a_new_device_row_is_seen_without_a_member_table_change(peers: Peers) -> None:
    owner = _owner(peers)
    _org(owner)
    plain = owner.fs.create_space("notes").space_id
    assert not _gate(owner, plain, "node-owner-laptop")

    OrgFsDirectoryStore(owner.fs).put_device("group-acme", device(OWNER, "node-owner-laptop"))

    assert _gate(owner, plain, "node-owner-laptop")
