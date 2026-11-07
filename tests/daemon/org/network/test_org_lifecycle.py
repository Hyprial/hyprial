"""Org lifecycle against a real local OrgFS runtime: leave/remove/delete/list.

Unlike test_org_network_service.py (recording fake, service logic), these
tests run the OrgFsDirectoryStore over a real OrgFsRuntime, so the policy
derivation from the membership table, the leave-request documents, and the
local left-orgs state are exercised end to end — minus transport.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.ipc import ipc_org_delete, ipc_org_list
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.org.network.state import left_orgs_path, read_left_orgs
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory import (
    ORG_META_DOC,
    DirectoryDevice,
)

from tests.support.tailnet_membership import FakeMembershipSidecar, key, member, confirm_removal, confirmation

OWNER = "owner@example"
BOB = "bob@example"
NODE_ID = "node-a"
OWN_ADDRESS = "tcp://10.0.0.2:7447#own-psk"


@pytest.fixture
def runtime(tmp_path: Path) -> OrgFsRuntime:
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id=NODE_ID, author=f"user:{OWNER}"
    )
    yield runtime
    runtime.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    return tmp_path


class Peers:
    def __init__(self) -> None:
        self.mapped: list[tuple[str, str]] = []
        self.unmapped: list[str] = []

    def map_peer(self, peer: str, address: str) -> int:
        self.mapped.append((peer, address))
        return 41000

    def unmap_peer(self, peer: str) -> None:
        self.unmapped.append(peer)


class _IpcApp:
    """The application surface needed by the org IPC handlers in this module."""

    def __init__(self, runtime: OrgFsRuntime, home: Path, *, owner: str) -> None:
        self._runtime = runtime
        self.hyprial_home = home
        self.node_id = NODE_ID if owner == OWNER else "node-b"
        self.owner = owner
        if not hasattr(runtime, "membership_sidecar"):
            runtime.membership_sidecar = FakeMembershipSidecar(now_ms=None)
        self._forwarding_supervisor = runtime.membership_sidecar.for_device(owner, self.node_id)
        self._forwarding_supervisor.now_ms = None

    def _start_forwarding_supervisor(self) -> None:
        pass

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        return self._runtime

    def _forwarding_status_json(self) -> dict[str, Any]:
        return {"policy": "auto", "peers": []}

    def _reconcile_forwarding_endpoints(self) -> None:
        pass

    def _await_forwarding_reconcile(self, reason: str, *, timeout: float, **fields: Any) -> None:
        self._reconcile_forwarding_endpoints()

    def _request_forwarding_reconcile(self, reason: str, **fields: Any) -> bool:
        return True

    def _log(self, *_args: Any, **_fields: Any) -> None:
        pass


def _service(
    runtime: OrgFsRuntime,
    home: Path,
    *,
    owner: str = OWNER,
    node_id: str = NODE_ID,
    peers: Peers | None = None,
    **overrides: Any,
) -> OrgNetworkService:
    if not hasattr(runtime, "membership_sidecar"):
        runtime.membership_sidecar = FakeMembershipSidecar()
    sidecar = runtime.membership_sidecar
    if (owner, node_id) != (OWNER, NODE_ID):
        sidecar = sidecar.for_device(owner, node_id)
    defaults: dict[str, Any] = {
        "membership_request": sidecar.membership_request,
        "invalid_devices": InvalidDeviceQuarantine(),
        "map_peer": peers.map_peer if peers else None,
        "unmap_peer": peers.unmap_peer if peers else None,
        "read_own_address": lambda: OWN_ADDRESS,
        "now": lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return OrgNetworkService(
        fs=runtime.facade,
        home=home,
        node_id=node_id,
        owner=owner,
        **defaults,
    )


def _bob_device(device_id: str = "node-b") -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=BOB,
        server_public=f"nodekey:server-{device_id}",
        client_public=f"nodekey:client-{device_id}",
        address=f"tcp://10.0.0.9:7447#{device_id}-psk",
        key_generation=1,
        updated_at="2026-10-03T11:00:00+00:00",
    )


@contextmanager
def _as_bob(runtime: OrgFsRuntime):
    previous = runtime.facade.author
    runtime.facade.author = f"user:{BOB}"
    try:
        yield
    finally:
        runtime.facade.author = previous


def _space_id(runtime: OrgFsRuntime, org: str) -> str:
    return next(
        space.space_id for space in runtime.facade.spaces() if space.name == org
    )


def _member_users(runtime: OrgFsRuntime, org: str) -> set[str]:
    return {
        member.user for member in runtime.facade.members(_space_id(runtime, org))
    }


def _acl_space_id(runtime: OrgFsRuntime, org: str) -> str:
    return next(
        space.space_id
        for space in runtime.facade.spaces()
        if space.name == f"{org}-acl"
    )


def _acl_member_users(runtime: OrgFsRuntime, org: str) -> set[str]:
    return {
        member.user
        for member in runtime.facade.members(_acl_space_id(runtime, org))
    }


# -- leave ---------------------------------------------------------------------


def test_member_leave_requires_confirmation_then_files_request_and_revokes(
    runtime: OrgFsRuntime, home: Path
) -> None:
    peers = Peers()
    service = _service(runtime, home, peers=peers)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_device("group-acme", _bob_device("node-b"))
        store.put_device("group-acme", _bob_device("node-c"))

    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    table["members"].update({device: member(BOB, device) for device in ("node-b", "node-c")})
    bob = _service(runtime, home, owner=BOB, node_id="node-b", peers=peers)
    with _as_bob(runtime):
        pending = bob.leave("group-acme")
        assert pending["phase"] == "pending-confirmation"
        assert store.list_leave_requests("group-acme") == {}
        result = confirm_removal(bob.leave, "group-acme")
    assert result["phase"] == "complete"
    assert result["deviceIds"] == ["node-b"]
    assert result["upperLayerReceipt"] == "committed"
    assert result["zenohDeny"] == "not-implemented-0.5.5.1"
    assert table["members"]["node-b"]["state"] == "tombstone"
    assert table["members"]["node-c"]["state"] == "active"
    assert peers.unmapped == []
    assert [d.device_id for d in store.list_devices("group-acme")] == [NODE_ID, "node-c"]
    assert store.list_leave_requests("group-acme")[BOB]["deviceId"] == "node-b"
    assert read_left_orgs(home) == ["group-acme"]
    assert bob.list()["orgs"] == []


def test_leave_excludes_the_org_from_list_network_and_peers(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    service.create("group-zeta")

    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]["members"]["node-b"] = member(BOB, "node-b")
    service = _service(runtime, home, owner=BOB, node_id="node-b")
    with _as_bob(runtime):
        assert confirm_removal(service.leave, "group-acme")["phase"] == "complete"

    assert [item["org"] for item in service.list()["orgs"]] == ["group-zeta"]
    # The invite pickup reads this to never auto-rejoin a left org.
    assert service.list()["leftOrgs"] == ["group-acme"]
    assert service.network()["orgs"] == ["group-zeta"]
    assert service.directory_peers() == {}
    # Leaving twice is an error-free no-op on the local state file.
    again = service.leave("group-acme")
    assert again["phase"] == "complete"
    assert read_left_orgs(home) == ["group-acme"]


def test_leave_requires_the_leave_permission(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    # A stranger (no role in acme) cannot file a leave as anyone.
    stranger = _service(runtime, home, owner="mallory@example", node_id="node-x")
    with pytest.raises(OrgNetworkError) as raised:
        stranger.leave("group-acme")
    assert raised.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_authority_leave_is_refused_even_with_confirmation(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    with pytest.raises(OrgNetworkError) as refused:
        confirm_removal(service.leave, "group-acme")
    assert refused.value.code == "LAST_AUTHORITY_REFUSED"
    assert read_left_orgs(home) == []
    assert OrgFsDirectoryStore(runtime.facade).list_leave_requests("group-acme") == {}


# -- remove ---------------------------------------------------------------------


def test_remove_drops_membership_and_leaves_user_rows_inert(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_device("group-acme", _bob_device("node-b"))
        store.put_device("group-acme", _bob_device("node-b2"))
    row_ids = {
        runtime.facade.stat(
            _space_id(runtime, "group-acme"),
            f"directory/devices/{BOB}/{device_id}.json",
        ).node_id
        for device_id in ("node-b", "node-b2")
    }

    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    # Include a roster-only device to prove directory rows do not define the target set.
    for device in ("node-b", "node-b2", "node-b3"):
        table["members"][device] = member(BOB, device)
    pending = service.remove("group-acme", f"user:{BOB}")
    assert pending["phase"] == "pending-confirmation"
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    result = service.remove("group-acme", f"user:{BOB}", confirmed=True, **confirmation(pending))

    assert result["phase"] == "complete"
    assert result["deviceIds"] == ["node-b", "node-b2", "node-b3"]
    assert all(table["members"][device]["state"] == "tombstone" for device in result["deviceIds"])
    assert result["result"]["propagation"] == "lease-bounded"
    assert f"user:{BOB}" not in _member_users(runtime, "group-acme")
    assert [d.device_id for d in store.list_devices("group-acme")] == [NODE_ID]
    physical = runtime.facade._space(_space_id(runtime, "group-acme")).nodes
    assert all(not physical[node_id].deleted for node_id in row_ids)


def test_remove_requires_the_remove_permission(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    # bob is a plain member: no invite/remove on org:members.
    as_bob = _service(runtime, home, owner=BOB, node_id="node-b")
    with pytest.raises(OrgNetworkError) as raised:
        as_bob.remove("group-acme", f"user:{OWNER}")
    assert raised.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_remove_of_an_already_gone_member_keeps_inert_device_history(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    with _as_bob(runtime):
        store.put_device("group-acme", _bob_device("node-b"))
    row = runtime.facade.stat(
        _space_id(runtime, "group-acme"), f"directory/devices/{BOB}/node-b.json"
    )
    runtime.facade.remove_member(_space_id(runtime, "group-acme"), f"user:{BOB}")

    # Bob was removed already: owner membership cleanup remains idempotent.
    result = confirm_removal(service.remove, "group-acme", f"user:{BOB}")
    assert result["phase"] == "complete"
    assert result["deviceIds"] == []
    assert not runtime.facade._space(_space_id(runtime, "group-acme")).nodes[
        row.node_id
    ].deleted


# -- delete ---------------------------------------------------------------------


def test_delete_refuses_authority_shutdown_without_mutating_members(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")

    space_id = _space_id(runtime, "group-acme")
    before_meta = runtime.facade.space_meta(space_id)
    before_lifecycle = runtime.facade.read_text(space_id, ORG_META_DOC)
    with pytest.raises(OrgNetworkError) as refused:
        service.delete("group-acme")
    assert refused.value.code == "LAST_AUTHORITY_REFUSED"
    assert _member_users(runtime, "group-acme") == {f"user:{OWNER}", f"user:{BOB}"}
    text, _version = runtime.facade.read_text(_space_id(runtime, "group-acme"), ORG_META_DOC)
    assert json.loads(text)["state"] == "active"
    assert runtime.facade.space_meta(space_id) == before_meta
    assert runtime.facade.read_text(space_id, ORG_META_DOC) == before_lifecycle
    assert service.network()["orgs"] == ["group-acme"]


def test_delete_is_owner_only(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    as_bob = _service(runtime, home, owner=BOB, node_id="node-b")
    with pytest.raises(OrgNetworkError) as raised:
        as_bob.delete("group-acme")
    assert raised.value.code == "ORG_ACTION_NOT_AUTHORIZED"


# -- list -----------------------------------------------------------------------


def test_list_reports_role_device_count_and_state(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    with _as_bob(runtime):
        OrgFsDirectoryStore(runtime.facade).put_device(
            "group-acme", _bob_device("node-b")
        )

    items = service.list()["orgs"]
    assert items == [
        {
            "org": "group-acme",
            "spaceId": _space_id(runtime, "group-acme"),
            "role": "owner",
            "devices": 2,
            "state": "active",
        }
    ]


def test_list_reports_member_roles(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    as_bob = _service(runtime, home, owner=BOB, node_id="node-b")
    assert as_bob.list()["orgs"][0]["role"] == "member"


# -- process_leave_requests -------------------------------------------------------


def test_process_leave_requests_retains_members_until_confirmation(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    runtime.facade.invite(
        _acl_space_id(runtime, "group-acme"), f"user:{BOB}", "ro"
    )
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_device("group-acme", _bob_device("node-b"))
    # bob's node left: its leave request is in the directory.
    with _as_bob(runtime):
        store.put_leave_request(
            "group-acme", BOB, {"org": "group-acme", "user": BOB, "requestedAt": "t1"}
        )
    device = runtime.facade.stat(
        _space_id(runtime, "group-acme"), f"directory/devices/{BOB}/node-b.json"
    )
    request = runtime.facade.stat(
        _space_id(runtime, "group-acme"), f"directory/leaves/{BOB}.json"
    )

    result = service.process_leave_requests()

    assert result == {"processed": [], "skipped": ["group-acme"]}
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert f"user:{BOB}" in _acl_member_users(runtime, "group-acme")
    assert BOB in store.list_leave_requests("group-acme")
    journals = [json.loads(path.read_text()) for path in (home / "tailnet/removals").glob("*.json")]
    assert len(journals) == 1
    assert journals[0]["phase"] == "pending-confirmation"
    physical = runtime.facade._space(_space_id(runtime, "group-acme")).nodes
    assert not physical[device.node_id].deleted
    assert not physical[request.node_id].deleted
    # Reconciling again is a no-op.
    assert service.process_leave_requests()["processed"] == []


def test_process_leave_requests_skips_invalid_member_written_names(
    runtime: OrgFsRuntime, home: Path
) -> None:
    # Leave filenames are member-written: a name that cannot be a user must
    # not stop the sweep for the requests after it (review 951).
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    # Leave rows sit at protected per-principal ids, so a name that cannot be
    # a user is refused at write time; the sweep's own skip is the fallback.
    for name in (" mallory", "a:b"):
        with pytest.raises(ValueError):
            store.put_leave_request(
                "group-acme", name, {"org": "group-acme", "user": name, "requestedAt": "t1"}
            )
    with _as_bob(runtime):
        store.put_leave_request(
            "group-acme", BOB, {"org": "group-acme", "user": BOB, "requestedAt": "t1"}
        )

    result = service.process_leave_requests()

    assert result == {"processed": [], "skipped": ["group-acme"]}
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    # The valid request awaits confirmation; invalid names still never enter the store.
    assert list(store.list_leave_requests("group-acme")) == [BOB]
    assert service.process_leave_requests()["processed"] == []


def test_process_leave_requests_is_owner_admin_business_only(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    OrgFsDirectoryStore(runtime.facade).put_leave_request(
        "group-acme", OWNER, {"org": "group-acme", "user": OWNER, "requestedAt": "t1"}
    )
    # bob is a member: no remove grant, so nothing is processed.
    as_bob = _service(runtime, home, owner=BOB, node_id="node-b")
    assert as_bob.process_leave_requests() == {"processed": [], "skipped": []}
    assert OrgFsDirectoryStore(runtime.facade).list_leave_requests("group-acme") != {}


def test_process_leave_requests_ignores_a_foreign_author_set(
    runtime: OrgFsRuntime,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-B defense in depth: path user must equal every durable author."""

    victim = "alice@example"
    service = _service(runtime, home)
    service.create("group-acme")
    space_id = _space_id(runtime, "group-acme")
    runtime.facade.invite(space_id, f"user:{BOB}", "rw")
    runtime.facade.invite(space_id, f"user:{victim}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    previous = runtime.facade.author
    runtime.facade.author = f"user:{victim}"
    try:
        store.put_leave_request(
            "group-acme", victim, {"user": victim, "requestedAt": "legitimate"}
        )
    finally:
        runtime.facade.author = previous

    physical_store = runtime.stores[space_id]
    with monkeypatch.context() as bypass:
        bypass.setattr(
            physical_store,
            "_org_device_update_allowed",
            lambda *_args, **_kwargs: True,
        )
        runtime.facade.author = f"user:{BOB}"
        try:
            runtime.facade.write_text(
                space_id,
                f"directory/leaves/{victim}.json",
                json.dumps({"user": victim, "requestedAt": "forged"}),
            )
        finally:
            runtime.facade.author = previous

    assert service.process_leave_requests()["processed"] == []
    assert f"user:{victim}" in _member_users(runtime, "group-acme")


def test_r11_stale_leave_does_not_remove_a_rejoined_member_with_skewed_clocks(
    runtime: OrgFsRuntime,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leave predating the latest add is stale even with a future wall clock."""

    service = _service(runtime, home)
    service.create("group-acme")
    space_id = _space_id(runtime, "group-acme")
    runtime.facade.invite(space_id, f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_leave_request(
            "group-acme",
            BOB,
            {"user": BOB, "requestedAt": "2099-01-01T00:00:00+00:00"},
        )
    runtime.facade.remove_member(space_id, f"user:{BOB}")
    monkeypatch.setattr(
        "hyprial.daemon.impl.orgfs.document.operations.spaces._now",
        lambda: "2000-01-01T00:00:00+00:00",
    )
    runtime.facade.invite(space_id, f"user:{BOB}", "rw")

    assert service.process_leave_requests() == {"processed": [], "skipped": []}
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert BOB in store.list_leave_requests("group-acme")


def test_r11_fresh_leave_records_pending_removal_with_skewed_clocks(
    runtime: OrgFsRuntime,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leave covering the latest add is fresh even with an older wall clock."""

    service = _service(runtime, home)
    service.create("group-acme")
    space_id = _space_id(runtime, "group-acme")
    runtime.facade.invite(space_id, f"user:{BOB}", "rw")
    runtime.facade.remove_member(space_id, f"user:{BOB}")
    monkeypatch.setattr(
        "hyprial.daemon.impl.orgfs.document.operations.spaces._now",
        lambda: "2099-01-01T00:00:00+00:00",
    )
    runtime.facade.invite(space_id, f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_leave_request(
            "group-acme",
            BOB,
            {"user": BOB, "requestedAt": "2000-01-01T00:00:00+00:00"},
        )

    result = service.process_leave_requests()

    assert result == {"processed": [], "skipped": ["group-acme"]}
    journals = [json.loads(path.read_text()) for path in (home / "tailnet/removals").glob("*.json")]
    assert len(journals) == 1
    assert journals[0]["owner"] == BOB
    assert journals[0]["phase"] == "pending-confirmation"
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")


# -- policy wiring on the existing verbs -----------------------------------------


def test_invite_requires_the_invite_permission(runtime: OrgFsRuntime, home: Path) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    as_bob = _service(runtime, home, owner=BOB, node_id="node-b")
    with pytest.raises(OrgNetworkError) as raised:
        as_bob.invite("group-acme", "user:carol@example")
    assert raised.value.code == "ORG_ACTION_NOT_AUTHORIZED"


@pytest.mark.parametrize("override_role", ["owner", "admin"])
def test_roles_json_cannot_override_a_members_role_or_permissions(
    runtime: OrgFsRuntime, home: Path, override_role: str
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    space_id = _space_id(runtime, "group-acme")
    # directory/ is owner-written, so the override is planted by the owner:
    # even then the reader must not take a role from it.
    runtime.facade.write_text(
        space_id,
        "directory/roles.json",
        json.dumps({f"user:{BOB}": override_role}),
    )
    runtime.facade.author = f"user:{BOB}"
    app = _IpcApp(runtime, home, owner=BOB)

    assert ipc_org_list(app, {})["orgs"][0]["role"] == "member"
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_delete(app, {"org": "group-acme"})
    assert raised.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_left_orgs_file_is_local_state_under_the_home(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    runtime.facade.invite(_space_id(runtime, "group-acme"), f"user:{BOB}", "rw")
    runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]["members"]["node-b"] = member(BOB, "node-b")
    bob = _service(runtime, home, owner=BOB, node_id="node-b")
    with _as_bob(runtime):
        assert confirm_removal(bob.leave, "group-acme")["phase"] == "complete"
    path = left_orgs_path(home)
    assert json.loads(path.read_text(encoding="utf-8")) == ["group-acme"]
    assert path.parent == home / "state" / "tailcat"
    # A second node on the same directory is unaffected: its home has no
    # left-orgs file, so acme still counts for it.
    other_home = home / "other"
    other_home.mkdir()
    other = _service(runtime, other_home, owner=BOB, node_id="node-b")
    assert [item["org"] for item in other.list()["orgs"]] == ["group-acme"]
