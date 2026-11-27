"""OC2: per-org OrgCellActor lifecycle, owner-writable vs member-readonly roster."""

from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, RLock
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.org.network import ipc
from hyprial.daemon.impl.org.cell_actor import (
    OrgCellActorRegistry,
    OrgMemberWrite,
    ensure_org_cell_actors,
    org_cell_actors,
)
from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_create,
    ipc_org_invite,
    ipc_org_join,
    ipc_org_leave,
    ipc_org_list,
    ipc_org_network,
    ipc_org_remove,
    process_leave_requests_for_app,
    publish_self_for_app,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.daemon.impl.org.network.tailnet import TailnetLifecycle
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity.impl.device import DeviceRecord, address_path, write_device_record
from hyprial.kernel import DaemonRequestError, keyed_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import uid_for
from tests.daemon.org.cell_actor.replica_support import link_replicas
from tests.support.tailnet_membership import (
    FakeMembershipSidecar,
    confirm_ipc_removal,
    key,
)

ALICE_OWNER = "alice@example.com"
BOB_OWNER = "bob@example.com"
ALICE_UID = uid_for(f"user:{ALICE_OWNER}")
BOB_UID = uid_for(f"user:{BOB_OWNER}")
ALICE_DEVICE_UID = "a" * 12 + "4" + "a" * 3 + "8" + "a" * 15
BOB_DEVICE_UID = "b" * 12 + "4" + "b" * 3 + "8" + "b" * 15


class _OrgApp:
    """Isolated daemon-shaped application fixture with ActorHostRegistry and CellRuntime."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        owner: str,
        user_id: str,
        node_id: str,
        device_uid: str,
        runtime: Any = None,
        sidecar: FakeMembershipSidecar | None = None,
    ) -> None:
        self.hyprial_home = tmp_path / f"home-{node_id}"
        self.hyprial_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir = self.hyprial_home / "state"
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner = owner
        self.user_id = user_id
        self.user_issuer = "https://login.example.com"
        self.oidc_claims_verified = True
        self.oidc_claims = {
            "iss": self.user_issuer,
            "sub": user_id,
            "preferred_username": owner,
        }
        self.oidc_roster: dict[str, dict[str, Any]] | None = None
        self.node_id = node_id
        self._runtime = runtime or SimpleNamespace(
            facade=LocalOrgFs(author=f"user:{owner}")
        )
        self._owns_orgfs = runtime is None
        self._forwarding_supervisor = sidecar or FakeMembershipSidecar(
            owner=owner, device_id=node_id, now_ms=None
        )
        self._forwarding_supervisor.endpoints = lambda: None
        self._actor_hosts = ActorHostRegistry()
        self.agents = SimpleNamespace(list=lambda: ())
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        write_device_record(
            self.hyprial_home,
            DeviceRecord(
                device_id=node_id,
                owner=owner,
                server_public=key(f"server-{node_id}"),
                client_public=key(f"client-{node_id}"),
                device_uid=device_uid,
                name=f"machine-{node_id}",
            ),
        )
        address_path(self.hyprial_home).write_text(
            f"tcp://127.0.0.1:{7447 if node_id == 'node-a' else 7448}#psk-{node_id}",
            encoding="utf-8",
        )

    def _require_orgfs_runtime(self) -> Any:
        return self._runtime

    def authenticated_oidc_roster(self, _org: str) -> dict[str, dict[str, Any]] | None:
        return self.oidc_roster

    def authenticated_oidc_issuer(self, _org: str) -> str:
        return self.user_issuer

    def authenticated_oidc_claims(self) -> dict[str, str]:
        return dict(self.oidc_claims)

    def _start_forwarding_supervisor(self) -> None:
        pass

    def _await_forwarding_reconcile(self, _reason: str, **_kwargs: Any) -> None:
        pass

    def _request_forwarding_reconcile(self, _reason: str, **_fields: Any) -> bool:
        return True

    def _forwarding_status_json(self) -> dict[str, Any]:
        return {"policy": "auto", "peers": []}

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, f"{domain}.{event}", fields))

    def close(self) -> None:
        close_cell_runtime(self)
        self._actor_hosts.shutdown()


def test_owner_org_cell_actor_writes_and_updates_org_cell_roster(tmp_path: Path) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        created = ipc_org_create(app, {"org": "group-acme"})
        space_id = created["spaceId"]

        registry = org_cell_actors(app)
        assert isinstance(registry, OrgCellActorRegistry)
        actor = registry.for_org("group-acme")
        snapshot = actor.snapshot()
        assert snapshot.org == "group-acme"
        assert snapshot.role == "owner"
        assert snapshot.writable is True
        assert snapshot.retired is False

        engine_host = app._actor_hosts.host_for("org-cell")
        assert engine_host.find("org-cell", "group-acme") is not None

        runtime = cell_runtime(app)
        assert runtime is not None
        cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
        cell = runtime.lookup(cell_id)
        assert cell is not None
        assert cell.kind == "org"
        assert cell.scope.org == "group-acme"
        assert cell.param("org.members")[f"u.{ALICE_UID}"]["username"] == ALICE_OWNER

        updated = actor.upsert_member(
            OrgMemberWrite(
                uid_ref=f"u.{BOB_UID}",
                username=BOB_OWNER,
                role="member",
                invited_by=f"u.{ALICE_UID}",
                joined_at_ms=2_000,
                devices=(
                    {
                        "deviceUid": f"d.{BOB_DEVICE_UID}",
                        "keyId": None,
                        "publicKey": None,
                        "verifiedAtMs": 2_000,
                    },
                ),
            )
        )
        assert updated.has_member(f"u.{BOB_UID}")
        roster = actor.roster()
        assert roster.writable is True
        assert roster.source == "org-cell"
        assert {member.username for member in roster.members} == {ALICE_OWNER, BOB_OWNER}

        ipc_org_invite(app, {"org": "group-acme", "user": f"user:{BOB_OWNER}"})
        removed = confirm_ipc_removal(
            ipc_org_remove,
            app,
            {"org": "group-acme", "user": f"user:{BOB_OWNER}"},
        )
        assert removed["phase"] == "complete"
        after_remove = runtime.lookup(cell_id)
        assert after_remove is not None
        assert not after_remove.has_member(f"u.{BOB_UID}")
    finally:
        app.close()


def test_owner_roster_keys_survive_reopen_and_n1_seed(tmp_path: Path) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        created = ipc_org_create(app, {"org": "group-acme"})
        master = key("master-bob")
        server = key("server-bob")
        org_cell_actors(app).for_org("group-acme").upsert_member(
            OrgMemberWrite(
                uid_ref=f"u.{BOB_UID}",
                username=BOB_OWNER,
                devices=({
                    "deviceUid": f"d.{BOB_DEVICE_UID}",
                    "keyId": None,
                    "publicKey": None,
                    "masterPublic": master,
                    "serverPublic": server,
                    "verifiedAtMs": 1,
                },),
            )
        )
        runtime = cell_runtime(app)
        cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, created["spaceId"]))
        entry = cell.param("org.members")[f"u.{BOB_UID}"]["devices"][0]
        assert entry["masterPublic"] == master
        assert entry["serverPublic"] == server
        seeded = TailnetLifecycle._seed_members(
            {f"u.{BOB_UID}": {"username": BOB_OWNER, "devices": [entry]}},
            SimpleNamespace(owner=ALICE_OWNER, device_id="node-a", client_public=key("client-a"), server_public=key("server-a")),
            "tcp://127.0.0.1:7447",
        )
        assert seeded[0]["masterPublic"] == master
        assert seeded[0]["serverPublic"] == server
    finally:
        app.close()


def test_member_machine_org_cell_actor_is_readonly_and_does_not_hold_cells_for_others(
    tmp_path: Path,
) -> None:
    alice_app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    bob_runtime = SimpleNamespace(facade=LocalOrgFs(author=f"user:{BOB_OWNER}"))
    bob_app = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        runtime=bob_runtime,
        sidecar=alice_app._forwarding_supervisor.for_device(BOB_OWNER, "node-b"),
    )
    try:
        space_id = ipc_org_create(alice_app, {"org": "group-acme"})["spaceId"]
        invited = ipc_org_invite(alice_app, {"org": "group-acme", "user": f"user:{BOB_OWNER}"})
        bob_app.oidc_roster = {
            f"u.{ALICE_UID}": {"username": ALICE_OWNER},
            f"u.{BOB_UID}": {"username": BOB_OWNER},
        }

        link_replicas(alice_app, bob_app)

        joined = ipc_org_join(bob_app, {"link": invited["link"]})
        assert joined["org"] == "group-acme"
        bob_device = f"directory/devices/{BOB_OWNER}/node-b.json"
        assert alice_app._runtime.facade.document_authors(space_id, bob_device) == (
            f"user:{BOB_OWNER}",
        )

        bob_actors = org_cell_actors(bob_app)
        assert bob_actors is not None
        bob_actor = bob_actors.for_org("group-acme")
        snapshot = bob_actor.snapshot()
        assert snapshot.role == "member"
        assert snapshot.writable is False

        roster = bob_actor.roster()
        assert roster.writable is False
        assert roster.role == "member"
        assert any(member.username == ALICE_OWNER for member in roster.members)

        with pytest.raises(OrgNetworkError) as write_refused:
            bob_actor.upsert_member(
                OrgMemberWrite(
                    uid_ref=f"u.{BOB_UID}",
                    username=BOB_OWNER,
                    role="member",
                    invited_by=f"u.{ALICE_UID}",
                    joined_at_ms=2_000,
                    devices=(
                        {
                            "deviceUid": f"d.{BOB_DEVICE_UID}",
                            "keyId": None,
                            "publicKey": None,
                            "verifiedAtMs": 2_000,
                        },
                    ),
                )
            )
        assert write_refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        with pytest.raises(OrgNetworkError) as delete_refused:
            bob_actor.remove_member(f"u.{ALICE_UID}")
        assert delete_refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        assert getattr(bob_app, "_cell_runtime", None) is None
        bob_cell_runtime = cell_runtime(bob_app)
        assert bob_cell_runtime is not None
        assert bob_cell_runtime.index().of_kind("org") == ()

        publish_outcome = publish_self_for_app(bob_app)
        assert publish_outcome["errors"] == []
        leave_outcome = process_leave_requests_for_app(bob_app)
        assert leave_outcome == {"processed": [], "skipped": []}

        left = confirm_ipc_removal(ipc_org_leave, bob_app, {"org": "group-acme"})
        assert left["phase"] == "complete"
        assert "group-acme" not in bob_actors.snapshots()
        assert bob_app._actor_hosts.host_for("org-cell").find("org-cell", "group-acme") is None
        assert [row["org"] for row in ipc_org_list(bob_app, {})["orgs"]] == []
    finally:
        bob_app.close()
        alice_app.close()


def test_actor_registry_never_retains_unknown_or_foreign_orgs(tmp_path: Path) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        with pytest.raises(DaemonRequestError) as raised:
            ipc_org_network(app, {"org": "group-missing"})
        assert raised.value.code == "unknown-space"
        actors = ensure_org_cell_actors(app)
        assert actors.snapshots() == {}
        assert app._actor_hosts.host_for("org-cell").find("org-cell", "group-missing") is None
    finally:
        app.close()
class _Service:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.revision = 0
        self.calls: list[tuple[str, int]] = []

    def leave_request_orgs(self) -> tuple[str, ...]:
        return ("group-acme",)

    def process_leave_requests(self, org: str | None = None) -> dict[str, list[Any]]:
        if org is None:
            raise AssertionError("maintenance bypassed the org actor")
        self.revision += 1
        self.calls.append((f"leave:{org}", self.revision))
        return {"processed": [], "skipped": []}

    def repair_candidates(self) -> list[dict[str, Any]]:
        return [{"org": "group-acme", "repairable": True}]

    def repair(self, org: str | None = None, *, apply: bool = False) -> dict[str, Any]:
        if org is None:
            raise AssertionError("bulk repair bypassed the org actor")
        self.revision += 1
        self.calls.append((f"repair:{org}", self.revision))
        return {"apply": apply, "candidates": [], "repaired": [{"org": org}]}


class _Actor:
    def __init__(self, service: _Service) -> None:
        self.service = service
        self.lock = RLock()
        self.entered = Event()
        self.release = Event()
        self.barrier: Barrier | None = None

    def org_process_leave_requests(self) -> dict[str, list[Any]]:
        with self.lock:
            self.entered.set()
            if self.barrier is not None:
                self.barrier.wait()
            self.release.wait(2)
            return self.service.process_leave_requests("group-acme")

    def org_repair(self, *, apply: bool = False) -> dict[str, Any]:
        with self.lock:
            self.entered.set()
            if self.barrier is not None:
                self.barrier.wait()
            self.release.wait(2)
            return self.service.repair("group-acme", apply=apply)

    def org_invite(self, _user: str) -> dict[str, Any]:
        with self.lock:
            self.service.revision += 1
            self.service.calls.append(("invite:group-acme", self.service.revision))
            return {"org": "group-acme"}

    def org_remove(self, _user: str) -> dict[str, Any]:
        with self.lock:
            self.service.revision += 1
            self.service.calls.append(("remove:group-acme", self.service.revision))
            return {"org": "group-acme"}


class _Registry:
    def __init__(self, actor: _Actor) -> None:
        self.actor = actor

    def reconcile(self) -> dict[str, Any]:
        return {}

    def for_org(self, _org: str) -> _Actor:
        return self.actor

    def prune_if_unowned(self, _org: str) -> bool:
        return False


@pytest.fixture
def app() -> Any:
    return SimpleNamespace(_log=lambda *_args, **_kwargs: None)


def test_leave_maintenance_and_org_mutation_share_actor_barrier(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _Service(mode="leave")
    actor = _Actor(service)
    registry = _Registry(actor)
    monkeypatch.setattr(ipc, "_build_service", lambda _app: service)
    monkeypatch.setattr(ipc, "ensure_org_cell_actors", lambda _app, **_kwargs: registry)
    start = Barrier(2)
    actor.barrier = start

    def maintenance() -> dict[str, Any]:
        return ipc.process_leave_requests_for_app(app)

    def mutation() -> dict[str, Any]:
        start.wait()
        actor.org_invite("user:bob@example")
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        maintenance_future = pool.submit(maintenance)
        assert actor.entered.wait(2)
        mutation_future = pool.submit(mutation)
        actor.release.set()
        assert maintenance_future.result() == {"processed": [], "skipped": []}
        assert mutation_future.result() == {"ok": True}

    assert service.calls == [("leave:group-acme", 1), ("invite:group-acme", 2)]


@pytest.mark.parametrize("mutation", ["org_invite", "org_remove"])
def test_bulk_repair_and_org_mutation_share_actor_barrier(
    app: Any, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _Service(mode="repair")
    actor = _Actor(service)
    registry = _Registry(actor)
    monkeypatch.setattr(ipc, "_request_service", lambda _app: service)
    monkeypatch.setattr(ipc, "_org_actors", lambda _app: registry)
    start = Barrier(2)
    actor.barrier = start

    def repair() -> dict[str, Any]:
        return ipc.ipc_org_repair(app, {"apply": True})

    def org_mutation() -> None:
        start.wait()
        getattr(actor, mutation)("user:bob@example")

    with ThreadPoolExecutor(max_workers=2) as pool:
        repair_future = pool.submit(repair)
        assert actor.entered.wait(2)
        mutation_future = pool.submit(org_mutation)
        actor.release.set()
        result = repair_future.result()
        mutation_future.result()

    assert result == {
        "apply": True,
        "candidates": [{"org": "group-acme", "repairable": True}],
        "repaired": [{"org": "group-acme"}],
    }
    assert service.calls[0] == ("repair:group-acme", 1)
    assert service.calls[1][0] == f"{mutation.removeprefix('org_')}:group-acme"
    assert service.calls[1][1] == 2
