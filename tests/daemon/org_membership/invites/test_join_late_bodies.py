# Decisions (package F10, real-node finding R6-1 and F9 review findings, 2026-11-26):
# - A join on the state OrgFS runtime returns once meta and tree arrive; document
#   bodies arrive through the resident sync a moment later.  Organization joins
#   (both IPC ``org.invites.accept`` and CLI ``org join``) wait once inside the
#   ``join-org`` step, within the shared ``JoinBudget`` ``join-orgfs`` leg, for
#   ``directory/org.json`` and ``policy/role-grants.json``.
# - Tests attach a real ``ActorHostRegistry`` to ``app._actor_hosts`` and invoke
#   public RPC methods through ``METHOD_REGISTRY.invoke`` so real ``OrgCellActor``
#   timeouts and wire error-code validation are exercised.  CLI joins call
#   ``OrgNetworkService.join`` directly.
# - Late bodies are deterministic: a query gate holds the Nth (3rd and later)
#   ``/org/fs3/sync/`` query for the target space until the test releases it.
# - One-time token replay remains refused with ``INVITE_CONSUMED`` on public
#   ``org.invites.accept``; ``accept_invite`` is unchanged from baseline.
"""Post-join late document body waits across IPC accept and CLI org join (F10)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.org.network import ipc
from hyprial.daemon.impl.org.cell_actor.registry import close_org_cell_actors
from hyprial.daemon.impl.org.membership.invites.journal import AcceptJournal
from hyprial.daemon.impl.org.membership.invites.recovery import run_bootstrap
from hyprial.daemon.impl.org.network import binding as org_binding
from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.identity import (
    ACTION_READ,
    ORG_META_DOC,
    RESOURCE_DIRECTORY_ALL,
    decode_invite,
    invite_identifier,
    role_grants_from_document,
)
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.kernel import DaemonRequestError, ORGFS_CONTENT_PENDING
from tests.daemon.orgfs.contracts.support.network import FakeNetwork
from tests.support.tailnet_membership import FakeMembershipSidecar

OWNER = "owner@example"
INVITEE = "x@example"
ORG = "group-acme"
SUB_ORG = "group-acme-eng"
ISSUER = "https://issuer.example"
ROSTER = {"u.x": {"username": INVITEE}}
INVITED = {"iss": ISSUER, "sub": "x", "preferred_username": INVITEE}
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _write_home(root: Path, row: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_device_record(
        root,
        DeviceRecord(
            device_id=row["deviceId"],
            owner=row["owner"],
            server_public=row["serverPublic"],
            client_public=row["masterPublic"],
        ),
    )
    return root


def _service(
    runtime: OrgFsRuntime,
    home: Path,
    membership: FakeMembershipSidecar,
    *,
    address: Callable[[], str | None] | None = None,
    join_timeout_seconds: float = 30.0,
    **extra: Any,
) -> OrgNetworkService:
    values: dict[str, Any] = dict(
        fs=runtime.facade,
        home=home,
        node_id=membership.local["deviceId"],
        owner=membership.local["owner"],
        invalid_devices=InvalidDeviceQuarantine(),
        map_peer=lambda _peer, _address: 41000,
        read_own_address=address or (lambda: membership.local["address"]),
        membership_request=membership.membership_request,
        join_timeout_seconds=join_timeout_seconds,
        sleep=time.sleep,
        now=lambda: NOW,
    )
    values.update(extra)
    return OrgNetworkService(**values)


class SyncGate:
    """Hold the Nth and later ``/org/fs3/sync/`` queries for one space."""

    def __init__(self, space_id: str, *, hold_from: int = 3) -> None:
        self.space_id = space_id
        self.hold_from = hold_from
        self.seen = 0
        self.reached = threading.Event()
        self.release = threading.Event()

    def open(self) -> None:
        self.release.set()

    def release_after(self, delay_seconds: float, *, wait_timeout: float = 5.0) -> threading.Thread:
        def worker() -> None:
            if self.reached.wait(wait_timeout):
                time.sleep(delay_seconds)
            self.release.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread


def _install_gates(session: Any, gates: tuple[SyncGate, ...]) -> None:
    if not gates:
        return
    original = session.query
    by_space = {gate.space_id: gate for gate in gates}
    lock = threading.Lock()

    def query(key_expr: str, payload: bytes, **kwargs: Any) -> list[Any]:
        text = str(key_expr)
        if "/org/fs3/sync/" in text:
            for space_id, gate in by_space.items():
                if space_id in text:
                    with lock:
                        gate.seen += 1
                        hold = gate.seen >= gate.hold_from
                    if hold:
                        gate.reached.set()
                        gate.release.wait(30.0)
                    break
        return original(key_expr, payload, **kwargs)

    session.query = query


def _holder_leaves_on_body_query(
    network: FakeNetwork, holder: str, space_id: str, *, leave_from: int = 3
) -> threading.Event:
    """Drop ``holder`` from the network when it first receives a body query."""

    left = threading.Event()
    lock = threading.Lock()
    seen = 0
    for index, (node, expression, handler) in enumerate(tuple(network.handlers)):
        if node != holder or "/org/fs3/sync/" not in expression:
            continue

        def serving(key_expr: str, payload: bytes, _handler: Any = handler) -> list[Any]:
            nonlocal seen
            if space_id in str(key_expr):
                with lock:
                    seen += 1
                    should_leave = seen >= leave_from and not left.is_set()
                if should_leave:
                    left.set()
                    network.forget(holder)
                    return []
            return _handler(key_expr, payload)

        network.handlers[index] = (node, expression, serving)
    return left


class OwnerOrg:
    """Real owner runtime hosting a root org and optional sub-org."""

    def __init__(self, root: Path, network: FakeNetwork) -> None:
        self.root = root
        self.network = network
        self.actor_hosts = ActorHostRegistry()
        self.runtime = OrgFsRuntime(
            root / "owner-state",
            node_id="node-owner",
            author=f"user:{OWNER}",
            trust_tailnet_members=True,
            actor_host=self.actor_hosts.host_for("orgfs"),
        )
        self.membership = FakeMembershipSidecar(
            owner=OWNER, device_id="node-owner", now_ms=None
        )
        self.runtime.bind_transport(
            network.add_node("node-owner"),
            supplier_online=network.online_for("node-owner"),
            holder_candidates=lambda: network.peers_of("node-owner"),
        )
        self.service = _service(
            self.runtime,
            _write_home(root / "owner-home", self.membership.local),
            self.membership,
        )
        created = self.service.create(ORG)
        self.directory_id = created["spaceId"]
        self.acl_id = created["aclSpaceId"]
        self.link = self.service.invite(ORG, INVITEE)["link"]
        self.invite_id = invite_identifier(self.link)

    def create_sub_org(self, name: str = SUB_ORG) -> dict[str, str]:
        created = self.service.create(name, parent=ORG)
        link = self.service.invite(name, INVITEE)["link"]
        return {
            "org": name,
            "directory_id": created["spaceId"],
            "acl_id": created["aclSpaceId"],
            "link": link,
            "invite_id": invite_identifier(link),
        }

    def close(self) -> None:
        self.network.forget("node-owner")
        self.runtime.close()
        self.actor_hosts.shutdown(5.0)


class InviteeNode:
    """Real invitee node wired with ``ActorHostRegistry`` and ``METHOD_REGISTRY``."""

    def __init__(
        self,
        root: Path,
        owner: OwnerOrg,
        *,
        node: str = "node-joiner",
        gates: tuple[SyncGate, ...] = (),
        join_timeout_seconds: float = 30.0,
    ) -> None:
        self.owner = owner
        self.node = node
        self.home = root / f"{node}-home"
        self.actor_hosts = ActorHostRegistry()
        self.runtime = OrgFsRuntime(
            root / f"{node}-state",
            node_id=node,
            author=f"user:{INVITEE}",
            trust_tailnet_members=True,
            actor_host=self.actor_hosts.host_for("orgfs"),
        )
        session = owner.network.add_node(node)
        _install_gates(session, gates)
        self.runtime.bind_transport(
            session,
            supplier_online=owner.network.online_for(node),
            holder_candidates=lambda: owner.network.peers_of(node),
            observe_supplier_online=owner.network.observe_for(node),
        )
        self.membership = owner.membership.for_device(INVITEE, node)
        self.service = _service(
            self.runtime,
            _write_home(self.home, self.membership.local),
            self.membership,
            join_timeout_seconds=join_timeout_seconds,
            oidc_issuer=lambda _org: ISSUER,
            oidc_roster=lambda _org: ROSTER,
        )
        self.claims = dict(INVITED)
        self.app = SimpleNamespace(
            hyprial_home=self.home,
            owner=INVITEE,
            node_id=node,
            _actor_hosts=self.actor_hosts,
            _log=lambda *_a, **_k: None,
            _require_orgfs_runtime=lambda: self.runtime,
            authenticated_oidc_claims=lambda: dict(self.claims),
        )

    def invoke(self, method: str, params: dict[str, Any]) -> Any:
        original = ipc._request_service
        ipc._request_service = lambda _app: self.service
        try:
            return METHOD_REGISTRY.invoke(self.app, method, params)
        finally:
            ipc._request_service = original

    def accept(self, *, invite_id: str | None = None, link: str | None = None) -> dict[str, Any]:
        return self.invoke(
            "org.invites.accept",
            {
                "inviteId": invite_id or self.owner.invite_id,
                "link": link or self.owner.link,
            },
        )

    def list_invites(self, links: list[str]) -> dict[str, Any]:
        rows = [{"payload": link.split("#v2.", 1)[1], "acceptedAt": None} for link in links]
        return self.invoke("org.invites.list", {"invites": rows})

    def journal(self, invite_id: str | None = None) -> dict[str, Any]:
        return AcceptJournal(self.home, invite_id or self.owner.invite_id).read()

    def own_device_rows(self, org: str = ORG) -> list[str]:
        return [
            row.device_id
            for row in self.service._store.list_devices(org)
            if row.owner.removeprefix("user:") == INVITEE
        ]

    def close(self) -> None:
        close_org_cell_actors(self.app)
        self.owner.network.forget(self.node)
        self.runtime.close()
        self.actor_hosts.shutdown(5.0)


@pytest.fixture
def owner_org(tmp_path: Path) -> Any:
    network = FakeNetwork()
    org = OwnerOrg(tmp_path, network)
    try:
        yield org
    finally:
        org.close()


def test_accept_waits_for_late_acl_policy_body(tmp_path: Path, owner_org: OwnerOrg) -> None:
    """R6-1: IPC org.invites.accept waits for a late ACL policy body (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    releaser = acl_gate.release_after(0.08)
    try:
        result = invitee.accept()
        assert acl_gate.reached.is_set()
        assert result["ok"] is True
        assert result["joined"] is True
        assert any(step["kind"] == "register-device" for step in result["steps"])
        assert invitee.journal()["state"] == "done"
        text, _version = invitee.runtime.facade.read_text(owner_org.acl_id, ACL_POLICY_DOC)
        assert role_grants_from_document(json.loads(text))
    finally:
        acl_gate.open()
        releaser.join(timeout=2.0)
        invitee.close()


def test_sub_org_accept_waits_for_late_directory_meta(tmp_path: Path, owner_org: OwnerOrg) -> None:
    """Review 9.1: sub-org accept waits for late directory/org.json (RED on baseline)."""

    sub = owner_org.create_sub_org()
    sub_dir_gate = SyncGate(sub["directory_id"])
    invitee = InviteeNode(tmp_path, owner_org, gates=(sub_dir_gate,))
    releaser: threading.Thread | None = None
    try:
        parent_res = invitee.accept()
        assert parent_res["ok"] is True
        releaser = sub_dir_gate.release_after(0.45)
        sub_res = invitee.accept(invite_id=sub["invite_id"], link=sub["link"])
        assert sub_dir_gate.reached.is_set()
        assert sub_res["ok"] is True
        assert sub_res["joined"] is True
        assert any(step["kind"] == "connect-members" for step in sub_res["steps"])
        listed = {row["org"]: row for row in invitee.service.list()["orgs"]}
        assert listed[SUB_ORG]["state"] == "active"
        assert listed[SUB_ORG]["parent"] == ORG
    finally:
        sub_dir_gate.open()
        if releaser is not None:
            releaser.join(timeout=2.0)
        invitee.close()


def test_accept_followed_immediately_by_org_network_and_org_list(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Review 9.2 & 9.3: org.network and org.list succeed immediately after accept (RED on baseline)."""

    dir_gate = SyncGate(owner_org.directory_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(dir_gate,))
    releaser = dir_gate.release_after(0.08)
    try:
        result = invitee.accept()
        assert result["ok"] is True
        assert dir_gate.reached.is_set()
        network_view = invitee.service.network(ORG)
        assert network_view["self"]["owner"] == INVITEE
        listed = {row["org"]: row for row in invitee.service.list()["orgs"]}
        assert listed[ORG]["state"] == "active"
    finally:
        dir_gate.open()
        releaser.join(timeout=2.0)
        invitee.close()


def test_parent_accept_followed_immediately_by_sub_org_accept(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Review 9.4: accepting a sub-org right after parent accept does not see partial (RED on baseline)."""

    sub = owner_org.create_sub_org()
    parent_dir_gate = SyncGate(owner_org.directory_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(parent_dir_gate,))
    releaser = parent_dir_gate.release_after(0.08)
    try:
        parent_res = invitee.accept()
        assert parent_res["ok"] is True
        assert parent_dir_gate.reached.is_set()
        sub_res = invitee.accept(invite_id=sub["invite_id"], link=sub["link"])
        assert sub_res["ok"] is True
        assert sub_res["joined"] is True
    finally:
        parent_dir_gate.open()
        releaser.join(timeout=2.0)
        invitee.close()


def test_cli_join_followed_immediately_by_policy_allows_and_network(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Review 9.5: CLI OrgNetworkService.join waits for both docs before returning (RED on baseline)."""

    dir_gate = SyncGate(owner_org.directory_id)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(dir_gate, acl_gate))
    dir_releaser = dir_gate.release_after(0.08)
    acl_releaser = acl_gate.release_after(0.08)
    try:
        joined = invitee.service.join(owner_org.link, claims=invitee.claims)
        assert joined["org"] == ORG
        assert dir_gate.reached.is_set()
        assert acl_gate.reached.is_set()
        assert invitee.service._policy.allows(
            org=ORG,
            subject=INVITEE,
            action=ACTION_READ,
            resource=RESOURCE_DIRECTORY_ALL,
        )
        network_view = invitee.service.network(ORG)
        assert network_view["self"]["owner"] == INVITEE
    finally:
        dir_gate.open()
        acl_gate.open()
        dir_releaser.join(timeout=2.0)
        acl_releaser.join(timeout=2.0)
        invitee.close()


@pytest.mark.parametrize(
    ("target", "expected_step"),
    [("acl", "acl-applied"), ("directory", "directory-committed")],
)
def test_holder_offline_after_join_returns_no_holder_online_under_actor_timeout(
    tmp_path: Path,
    owner_org: OwnerOrg,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected_step: str,
) -> None:
    """Holder drops after join: wire error is no-holder-online (never DAEMON_ERROR), under actor limit.

    ``acl`` is a guard on baseline (and pins bounded wait < actor timeout);
    ``directory`` is RED on baseline (baseline silently succeeded with pending org.json).
    """

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 0.4, raising=False)
    dir_gate = SyncGate(owner_org.directory_id) if target == "directory" else None
    gates = (dir_gate,) if dir_gate is not None else ()
    left = _holder_leaves_on_body_query(owner_org.network, "node-owner", owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=gates, join_timeout_seconds=30.0)
    try:
        actor = ipc._org_actors(invitee.app).for_org(ORG)
        actor._call_timeout_s = 1.5
        started = time.monotonic()
        with pytest.raises(DaemonRequestError) as exc_info:
            invitee.accept()
        elapsed = time.monotonic() - started
        assert left.is_set()
        if dir_gate is not None:
            assert dir_gate.reached.is_set()
        assert exc_info.value.code == "no-holder-online"
        assert exc_info.value.code != "DAEMON_ERROR"
        assert exc_info.value.data is not None
        assert exc_info.value.data["missingStep"] == expected_step
        assert exc_info.value.data["status"] == "partial"
        assert elapsed < 1.3
        assert invitee.journal()["state"] == "partial"
        assert invitee.own_device_rows() == []
        with pytest.raises(DaemonRequestError) as replay_exc:
            invitee.accept()
        assert replay_exc.value.code == "INVITE_CONSUMED"
    finally:
        if dir_gate is not None:
            dir_gate.open()
        invitee.close()


def test_invites_list_does_not_wait_for_pending_body(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 4: read-only org.invites.list never waits on a pending body (GUARD on baseline)."""

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 0.25, raising=False)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,), join_timeout_seconds=30.0)
    try:
        with pytest.raises(DaemonRequestError) as accept_exc:
            invitee.accept()
        assert accept_exc.value.code == "no-holder-online"
        assert acl_gate.reached.is_set()
        monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 5.0, raising=False)
        started = time.monotonic()
        listed = invitee.list_invites([owner_org.link])
        elapsed = time.monotonic() - started
        assert listed["ok"] is True
        assert elapsed < 0.5
        assert len(listed["pending"]) == 1
        assert listed["pending"][0]["status"] == "partial"
    finally:
        acl_gate.open()
        invitee.close()


def test_wait_exits_early_on_runtime_close(tmp_path: Path, owner_org: OwnerOrg) -> None:
    """Review 7a: runtime.close() aborts await_text promptly with unavailable/orgfs-closing (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        invitee.runtime.facade.join(owner_org.acl_id)
        assert acl_gate.reached.wait(2.0)

        def closer() -> None:
            time.sleep(0.06)
            invitee.runtime.close()

        thread = threading.Thread(target=closer, daemon=True)
        thread.start()
        started = time.monotonic()
        with pytest.raises(OrgFsError) as exc_info:
            invitee.runtime.await_text(owner_org.acl_id, ACL_POLICY_DOC, timeout=5.0)
        elapsed = time.monotonic() - started
        thread.join(timeout=5.0)
        assert elapsed < 1.0
        assert exc_info.value.code == "unavailable"
        assert exc_info.value.details.get("reason") == "orgfs-closing"
    finally:
        acl_gate.open()
        invitee.close()


def test_wait_exits_early_on_detach_sync(tmp_path: Path, owner_org: OwnerOrg) -> None:
    """Review 7b: detaching a space's sync aborts await_text promptly with unavailable (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        invitee.runtime.facade.join(owner_org.acl_id)
        assert acl_gate.reached.wait(2.0)

        def detacher() -> None:
            time.sleep(0.06)
            invitee.runtime.facade.detach_sync(owner_org.acl_id)

        thread = threading.Thread(target=detacher, daemon=True)
        thread.start()
        started = time.monotonic()
        with pytest.raises(OrgFsError) as exc_info:
            invitee.runtime.await_text(owner_org.acl_id, ACL_POLICY_DOC, timeout=5.0)
        elapsed = time.monotonic() - started
        thread.join(timeout=2.0)
        assert elapsed < 1.0
        assert exc_info.value.code == "unavailable"
        assert exc_info.value.details.get("reason") == "sync-detached"
    finally:
        acl_gate.open()
        invitee.close()


def test_legacy_org_without_org_meta_does_not_wait_or_fail(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Legitimate unknown-doc (legacy org without directory/org.json) never waits or fails (GUARD)."""

    owner_org.runtime.facade.remove(owner_org.directory_id, ORG_META_DOC)
    cli_link = owner_org.service.invite(ORG, INVITEE)["link"]
    invitee_ipc = InviteeNode(tmp_path, owner_org, node="node-legacy-ipc")
    invitee_cli = InviteeNode(tmp_path, owner_org, node="node-legacy-cli")
    try:
        awaited_spaces: list[str] = []
        original_await = invitee_ipc.runtime.await_content

        def spy_await(space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
            awaited_spaces.append(space_id)
            return original_await(space_id, node_id, deadline_monotonic=deadline_monotonic)

        invitee_ipc.runtime.await_content = spy_await  # type: ignore[method-assign]
        started = time.monotonic()
        res_ipc = invitee_ipc.accept()
        elapsed_ipc = time.monotonic() - started
        assert res_ipc["ok"] is True
        assert elapsed_ipc < 2.0
        assert owner_org.directory_id not in awaited_spaces
        listed = {row["org"]: row for row in invitee_ipc.service.list()["orgs"]}
        assert listed[ORG]["state"] == "legacy-active"

        res_cli = invitee_cli.service.join(cli_link, claims=invitee_cli.claims)
        assert res_cli["org"] == ORG
    finally:
        invitee_ipc.close()
        invitee_cli.close()


@pytest.mark.parametrize("join_step_completed", [False, True])
def test_recovery_reentering_bootstrap_with_committed_spaces_waits_for_late_bodies(
    tmp_path: Path, owner_org: OwnerOrg, join_step_completed: bool
) -> None:
    """Recovery path where spaces are already committed still waits for late bodies (RED on baseline)."""

    dir_gate = SyncGate(owner_org.directory_id)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(dir_gate, acl_gate))
    try:
        invitee.runtime.facade.join(owner_org.directory_id)
        invitee.runtime.facade.join(owner_org.acl_id)
        assert dir_gate.reached.wait(2.0)
        assert acl_gate.reached.wait(2.0)
        parsed = decode_invite(owner_org.link)
        journal = AcceptJournal(invitee.home, owner_org.invite_id)
        journal.start()
        if join_step_completed:
            invitee.service._redeem_tailnet(parsed)
            invitee.service._announce_tailnet(owner_org.directory_id)
            journal.complete_step("tailnet-redeemed")
            journal.complete_step("tailnet-announced")
            journal.complete_step("connect-inviter")
            journal.complete_step("directory-committed")
            journal.complete_step("acl-applied")
            journal.complete_step("space-bound")
            journal.complete_step("join-org")

        dir_releaser = dir_gate.release_after(0.08)
        acl_releaser = acl_gate.release_after(0.08)
        try:
            result = run_bootstrap(invitee.service, parsed, journal)
        finally:
            dir_gate.open()
            acl_gate.open()
            dir_releaser.join(timeout=2.0)
            acl_releaser.join(timeout=2.0)
        assert result["org"] == ORG
        text, _version = invitee.runtime.facade.read_text(owner_org.directory_id, ORG_META_DOC)
        assert json.loads(text)["state"] == "active"
        acl_text, _version = invitee.runtime.facade.read_text(owner_org.acl_id, ACL_POLICY_DOC)
        assert role_grants_from_document(json.loads(acl_text))
    finally:
        dir_gate.open()
        acl_gate.open()
        invitee.close()


def test_await_text_non_pending_error_does_not_stat_or_await(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Pins M1: non-content-pending errors re-raise directly without stat or await_content (RED on baseline)."""

    owner_org.runtime.facade.write_bytes(owner_org.directory_id, "directory/blob.bin", b"payload")
    invitee = InviteeNode(tmp_path, owner_org)
    try:
        invitee.runtime.facade.join(owner_org.directory_id)
        stat_calls: list[str] = []
        await_calls: list[str] = []
        orig_stat = invitee.runtime.facade.stat
        orig_await = invitee.runtime.await_content

        def spy_stat(space_id: str, ref: str) -> Any:
            stat_calls.append(ref)
            return orig_stat(space_id, ref)

        def spy_await(space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
            await_calls.append(node_id)
            return orig_await(space_id, node_id, deadline_monotonic=deadline_monotonic)

        invitee.runtime.facade.stat = spy_stat  # type: ignore[method-assign]
        invitee.runtime.await_content = spy_await  # type: ignore[method-assign]

        with pytest.raises(OrgFsError) as blob_exc:
            invitee.runtime.await_text(owner_org.directory_id, "directory/blob.bin", timeout=2.0)
        assert blob_exc.value.code == "invalid-argument"

        with pytest.raises(OrgFsError) as dir_exc:
            invitee.runtime.await_text(owner_org.directory_id, "directory", timeout=2.0)
        assert dir_exc.value.code == "invalid-argument"

        with pytest.raises(OrgFsError) as missing_exc:
            invitee.runtime.await_text(owner_org.directory_id, "directory/missing.json", timeout=2.0)
        assert missing_exc.value.code == "unknown-doc"

        assert stat_calls == []
        assert await_calls == []
    finally:
        invitee.close()


def test_await_text_non_positive_timeout_does_not_wait(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Pins M2: timeout <= 0 re-raises content-pending directly without calling await_content (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        invitee.runtime.facade.join(owner_org.directory_id)
        invitee.runtime.facade.join(owner_org.acl_id)
        assert acl_gate.reached.wait(2.0)
        await_calls: list[str] = []
        orig_await = invitee.runtime.await_content

        def spy_await(space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
            await_calls.append(node_id)
            return orig_await(space_id, node_id, deadline_monotonic=deadline_monotonic)

        invitee.runtime.await_content = spy_await  # type: ignore[method-assign]
        for timeout in (0.0, -1.0):
            with pytest.raises(OrgFsError) as exc_info:
                invitee.runtime.await_text(owner_org.acl_id, ACL_POLICY_DOC, timeout=timeout)
            assert exc_info.value.code == ORGFS_CONTENT_PENDING
        assert await_calls == []

        budget = JoinBudget()
        budget.started = budget.clock() - (org_binding.JOIN_ACTOR_HEADROOM_SECONDS + 1.0)
        invitee.service._join_budget = budget
        try:
            with pytest.raises(OrgFsError) as join_exc:
                org_binding.await_org_join_bodies(
                    invitee.service, owner_org.directory_id, owner_org.acl_id
                )
        finally:
            invitee.service._join_budget = None
        assert join_exc.value.code == "no-holder-online"
        assert await_calls == []
    finally:
        acl_gate.open()
        invitee.close()


def test_body_wait_is_capped_by_join_budget_remaining(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Pins M4: JoinBudget remaining time caps the body wait below JOIN_BODY_WAIT_SECONDS (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,), join_timeout_seconds=30.0)
    try:
        invitee.runtime.facade.join(owner_org.directory_id)
        invitee.runtime.facade.join(owner_org.acl_id)
        assert acl_gate.reached.wait(2.0)
        budget = JoinBudget()
        budget.deadline = budget.clock() + 0.25
        invitee.service._join_budget = budget
        started = time.monotonic()
        try:
            with pytest.raises(OrgFsError) as exc_info:
                org_binding.await_org_join_bodies(
                    invitee.service, owner_org.directory_id, owner_org.acl_id
                )
        finally:
            invitee.service._join_budget = None
        elapsed = time.monotonic() - started
        assert exc_info.value.code == "no-holder-online"
        assert exc_info.value.details.get("missingStep") == "acl-applied"
        assert 0.15 <= elapsed < 1.2
    finally:
        acl_gate.open()
        invitee.close()


def test_body_wait_is_capped_by_actor_headroom(
    tmp_path: Path, owner_org: OwnerOrg
) -> None:
    """Actor headroom caps body wait when earlier join steps already consumed most of the 30s actor window (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,), join_timeout_seconds=30.0)
    try:
        invitee.runtime.facade.join(owner_org.directory_id)
        invitee.runtime.facade.join(owner_org.acl_id)
        assert acl_gate.reached.wait(2.0)
        budget = JoinBudget()
        budget.started = budget.clock() - (org_binding.JOIN_ACTOR_HEADROOM_SECONDS - 0.25)
        invitee.service._join_budget = budget
        started = time.monotonic()
        try:
            with pytest.raises(OrgFsError) as exc_info:
                org_binding.await_org_join_bodies(
                    invitee.service, owner_org.directory_id, owner_org.acl_id
                )
        finally:
            invitee.service._join_budget = None
        elapsed = time.monotonic() - started
        assert exc_info.value.code == "no-holder-online"
        assert exc_info.value.details.get("missingStep") == "acl-applied"
        assert 0.15 <= elapsed < 1.2
    finally:
        acl_gate.open()
        invitee.close()
