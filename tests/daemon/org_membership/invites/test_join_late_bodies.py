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
#   ``/org/fs3/sync/`` query for the target space until the test releases it
#   (superseded by the F10b gate bullet below).
# - One-time token replay remains refused with ``INVITE_CONSUMED`` on public
#   ``org.invites.accept``; ``accept_invite`` is unchanged from baseline.
# - Package F10b (owner ruling on the F10 review): the wait is best effort, so
#   a body still pending is logged and the join goes on.  The "waits for a
#   late body" tests no longer release on a timer: the call must be inside the
#   body wait while the gate still holds the body, then the test releases it,
#   which keeps them red on a non-waiting build however loaded the machine is.
#   Caps are asserted on the timeouts handed to ``await_text``, not wall time.
# - F10b: the gate (and the holder that leaves) key on what a query asks for,
#   not on its position.  Counting to the 3rd query was load dependent: on two
#   shared cores the body sometimes came with the 2nd round, failing the order
#   assertion with the fix in place (measured; trace in the F10b report).
#   Holding a body query instead would block ``join``, which waits for the
#   first resident round, so a closed gate strips the body documents from the
#   space's requests (meta and tree still sync) and opening it schedules a new
#   round from the owner.
"""Post-join late document body waits across IPC accept and CLI org join (F10)."""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
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
from hyprial.daemon.impl.orgfs.sync import FrameError, SyncRequest, decode_request, encode_request
from hyprial.daemon.impl.orgfs.sync.interface import CATALOG_DOC_IDS
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


def _sync_request(space_id: str, payload: bytes) -> SyncRequest | None:
    try:
        return decode_request(payload, space_id=space_id)
    except FrameError:
        return None


def _asks_for_body(space_id: str, payload: bytes) -> bool:
    request = _sync_request(space_id, payload)
    return request is not None and any(
        doc_id not in CATALOG_DOC_IDS for doc_id, _vector in request.vectors
    )


def _without_bodies(space_id: str, payload: bytes) -> bytes:
    request = _sync_request(space_id, payload)
    if request is None:
        return payload
    kept = tuple(row for row in request.vectors if row[0] in CATALOG_DOC_IDS)
    return encode_request(dataclasses.replace(request, vectors=kept))


class SyncGate:
    """Keep one space's document bodies from arriving until the test opens it.

    While closed, each ``/org/fs3/sync/`` request for the space goes out with
    its body documents removed, so meta and tree sync as usual and no round
    blocks on the gate.  Opening it asks the space's sync for a new round.
    """

    def __init__(self, space_id: str) -> None:
        self.space_id = space_id
        self.reached = threading.Event()
        self.release = threading.Event()
        self.on_open: list[Callable[[], None]] = []

    def open(self) -> None:
        self.release.set()
        for callback in tuple(self.on_open):
            callback()

    def release_after(self, delay_seconds: float, *, wait_timeout: float = 5.0) -> threading.Thread:
        def worker() -> None:
            if self.reached.wait(wait_timeout):
                time.sleep(delay_seconds)
            self.open()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread


def _install_gates(session: Any, gates: tuple[SyncGate, ...]) -> None:
    if not gates:
        return
    original = session.query
    by_space = {gate.space_id: gate for gate in gates}

    def query(key_expr: str, payload: bytes, **kwargs: Any) -> list[Any]:
        text = str(key_expr)
        if "/org/fs3/sync/" in text:
            for space_id, gate in by_space.items():
                if space_id in text:
                    if not gate.release.is_set() and _asks_for_body(space_id, payload):
                        gate.reached.set()
                        payload = _without_bodies(space_id, payload)
                    break
        return original(key_expr, payload, **kwargs)

    session.query = query


def _holder_leaves_on_body_query(
    network: FakeNetwork, holder: str, space_id: str
) -> threading.Event:
    """Drop ``holder`` from the network when it first receives a body query."""

    left = threading.Event()
    lock = threading.Lock()
    for index, (node, expression, handler) in enumerate(tuple(network.handlers)):
        if node != holder or "/org/fs3/sync/" not in expression:
            continue

        def serving(key_expr: str, payload: bytes, _handler: Any = handler) -> list[Any]:
            if space_id in str(key_expr) and _asks_for_body(space_id, payload):
                with lock:
                    should_leave = not left.is_set()
                    left.set()
                if should_leave:
                    left.set()
                    network.forget(holder)
                    return []
            return _handler(key_expr, payload)

        network.handlers[index] = (node, expression, serving)
    return left


def _call_held_in_body_wait(
    call: Callable[[], Any], runtime: OrgFsRuntime, gates: tuple[SyncGate, ...]
) -> tuple[threading.Thread, dict[str, Any]]:
    """Run ``call`` until it waits on a gated body; fail if it returns first.

    The order is structural: a call that does not wait returns while the gate
    still holds the body, however slow the machine is.
    """

    spaces = {gate.space_id for gate in gates}
    either = threading.Event()
    entered = threading.Event()
    original = runtime.await_content

    def spy(space_id: str, node_id: str, *, deadline_monotonic: float) -> bool:
        if space_id in spaces:
            entered.set()
            either.set()
        return original(space_id, node_id, deadline_monotonic=deadline_monotonic)

    runtime.await_content = spy  # type: ignore[method-assign]
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = call()
        except BaseException as error:  # noqa: BLE001
            box["error"] = error
        finally:
            box["done"] = True
            either.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert either.wait(20.0)
    assert entered.is_set() and "done" not in box, f"returned while the body was held: {box}"
    return thread, box


def _released(thread: threading.Thread, box: dict[str, Any], gates: tuple[SyncGate, ...]) -> Any:
    for gate in gates:
        gate.open()
    thread.join(timeout=20.0)
    assert box.get("done"), "the call did not return after the bodies were released"
    if "error" in box:
        raise box["error"]
    return box["result"]


def _spy_await_text(runtime: OrgFsRuntime) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []
    original = runtime.await_text

    def spy(space_id: str, ref: str, *, timeout: float) -> Any:
        calls.append((space_id, timeout))
        return original(space_id, ref, timeout=timeout)

    runtime.await_text = spy  # type: ignore[method-assign]
    return calls


class LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, _area: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for level, name, fields in self.events if name == event and level == "warn"]


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
        for gate in gates:
            gate.on_open.append(lambda space_id=gate.space_id: self._sync_again(space_id))
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

    def _sync_again(self, space_id: str) -> None:
        try:
            sync = self.runtime._mesh(space_id)
        except OrgFsError:
            return
        if sync is not None:
            sync.schedule_sync_from("node-owner")

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


@pytest.fixture
def long_body_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 20.0, raising=False)


def test_accept_waits_for_late_acl_policy_body(
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None
) -> None:
    """R6-1: IPC org.invites.accept waits for a late ACL policy body (RED on baseline)."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        thread, box = _call_held_in_body_wait(invitee.accept, invitee.runtime, (acl_gate,))
        result = _released(thread, box, (acl_gate,))
        assert result["ok"] is True
        assert result["joined"] is True
        assert any(step["kind"] == "register-device" for step in result["steps"])
        assert invitee.journal()["state"] == "done"
        text, _version = invitee.runtime.facade.read_text(owner_org.acl_id, ACL_POLICY_DOC)
        assert role_grants_from_document(json.loads(text))
    finally:
        acl_gate.open()
        invitee.close()


def test_sub_org_accept_waits_for_late_directory_meta(
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None
) -> None:
    """Review 9.1: sub-org accept waits for late directory/org.json (RED on baseline)."""

    sub = owner_org.create_sub_org()
    sub_dir_gate = SyncGate(sub["directory_id"])
    invitee = InviteeNode(tmp_path, owner_org, gates=(sub_dir_gate,))
    try:
        parent_res = invitee.accept()
        assert parent_res["ok"] is True
        thread, box = _call_held_in_body_wait(
            lambda: invitee.accept(invite_id=sub["invite_id"], link=sub["link"]),
            invitee.runtime,
            (sub_dir_gate,),
        )
        sub_res = _released(thread, box, (sub_dir_gate,))
        assert sub_res["ok"] is True
        assert sub_res["joined"] is True
        assert any(step["kind"] == "connect-members" for step in sub_res["steps"])
        listed = {row["org"]: row for row in invitee.service.list()["orgs"]}
        assert listed[SUB_ORG]["state"] == "active"
        assert listed[SUB_ORG]["parent"] == ORG
    finally:
        sub_dir_gate.open()
        invitee.close()


def test_accept_followed_immediately_by_org_network_and_org_list(
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None
) -> None:
    """Review 9.2 & 9.3: org.network and org.list succeed immediately after accept (RED on baseline).

    F10b: no timed release.  The accept must still be inside the body wait
    while the gate holds the directory body; the test then releases it.
    """

    dir_gate = SyncGate(owner_org.directory_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(dir_gate,))
    try:
        thread, box = _call_held_in_body_wait(invitee.accept, invitee.runtime, (dir_gate,))
        result = _released(thread, box, (dir_gate,))
        assert result["ok"] is True
        network_view = invitee.service.network(ORG)
        assert network_view["self"]["owner"] == INVITEE
        listed = {row["org"]: row for row in invitee.service.list()["orgs"]}
        assert listed[ORG]["state"] == "active"
    finally:
        dir_gate.open()
        invitee.close()


def test_parent_accept_followed_immediately_by_sub_org_accept(
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None
) -> None:
    """Review 9.4: accepting a sub-org right after parent accept does not see partial (RED on baseline).

    F10b: no timed release, as in the test above.
    """

    sub = owner_org.create_sub_org()
    parent_dir_gate = SyncGate(owner_org.directory_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(parent_dir_gate,))
    try:
        thread, box = _call_held_in_body_wait(invitee.accept, invitee.runtime, (parent_dir_gate,))
        parent_res = _released(thread, box, (parent_dir_gate,))
        assert parent_res["ok"] is True
        sub_res = invitee.accept(invite_id=sub["invite_id"], link=sub["link"])
        assert sub_res["ok"] is True
        assert sub_res["joined"] is True
    finally:
        parent_dir_gate.open()
        invitee.close()


def test_cli_join_followed_immediately_by_policy_allows_and_network(
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None
) -> None:
    """Review 9.5: CLI OrgNetworkService.join waits for both docs before returning (RED on baseline)."""

    dir_gate = SyncGate(owner_org.directory_id)
    acl_gate = SyncGate(owner_org.acl_id)
    gates = (dir_gate, acl_gate)
    invitee = InviteeNode(tmp_path, owner_org, gates=gates)
    try:
        thread, box = _call_held_in_body_wait(
            lambda: invitee.service.join(owner_org.link, claims=invitee.claims),
            invitee.runtime,
            gates,
        )
        joined = _released(thread, box, gates)
        assert joined["org"] == ORG
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
        invitee.close()


@pytest.mark.parametrize("directory_held", [False, True], ids=["acl-only", "directory-too"])
def test_holder_offline_after_join_returns_no_holder_online_under_actor_timeout(
    tmp_path: Path,
    owner_org: OwnerOrg,
    monkeypatch: pytest.MonkeyPatch,
    directory_held: bool,
) -> None:
    """Holder drops after join: accept fails with no-holder-online (never DAEMON_ERROR), under actor limit.

    Both cases are GUARD on baseline and give its result.  F10 had made the
    ``directory-too`` case report ``directory-committed`` from the wait; under
    the F10b ruling the wait only logs, so the accept read-back reports the
    ACL policy, as the baseline does.  In ``directory-too`` the directory body
    stays pending either because the gate holds its query or because the
    holder left before it was asked for; which happens first is not fixed, so
    the pending log is asserted, not the gate.
    """

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 0.4, raising=False)
    dir_gate = SyncGate(owner_org.directory_id) if directory_held else None
    gates = (dir_gate,) if dir_gate is not None else ()
    left = _holder_leaves_on_body_query(owner_org.network, "node-owner", owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=gates, join_timeout_seconds=30.0)
    log = LogRecorder()
    invitee.service._log = log
    try:
        actor = ipc._org_actors(invitee.app).for_org(ORG)
        actor._call_timeout_s = 1.5
        started = time.monotonic()
        with pytest.raises(DaemonRequestError) as exc_info:
            invitee.accept()
        elapsed = time.monotonic() - started
        assert left.is_set()
        assert exc_info.value.code == "no-holder-online"
        assert exc_info.value.code != "DAEMON_ERROR"
        assert exc_info.value.data is not None
        assert exc_info.value.data["missingStep"] == "acl-applied"
        assert exc_info.value.data["status"] == "partial"
        assert elapsed < 1.3
        pending = {(row["spaceId"], row["doc"]) for row in log.named("org.join.body-pending")}
        assert (owner_org.acl_id, ACL_POLICY_DOC) in pending
        assert ((owner_org.directory_id, ORG_META_DOC) in pending) is directory_held
        assert invitee.journal()["state"] == "partial"
        assert invitee.journal()["missingStep"] == "space-bound"
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
    tmp_path: Path, owner_org: OwnerOrg, long_body_wait: None, join_step_completed: bool
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

        gates = (dir_gate, acl_gate)
        thread, box = _call_held_in_body_wait(
            lambda: run_bootstrap(invitee.service, parsed, journal), invitee.runtime, gates
        )
        result = _released(thread, box, gates)
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
    """Pins M2: timeout <= 0 re-raises content-pending without await_content (RED on baseline).

    F10b: a join whose wait limit is already zero skips the wait without a read.
    """

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

        text_calls = _spy_await_text(invitee.runtime)
        log = LogRecorder()
        invitee.service._log = log
        budget = JoinBudget()
        budget.started = budget.clock() - (org_binding.JOIN_ACTOR_HEADROOM_SECONDS + 1.0)
        invitee.service._join_budget = budget
        try:
            org_binding.await_org_join_bodies(
                invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
            )
        finally:
            invitee.service._join_budget = None
        assert await_calls == []
        assert text_calls == []
        assert log.named("org.join.body-wait-skipped") == [
            {"org": ORG, "spaceIds": [owner_org.directory_id, owner_org.acl_id],
             "reason": "no-wait-budget"}
        ]
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
        text_calls = _spy_await_text(invitee.runtime)
        log = LogRecorder()
        invitee.service._log = log
        budget = JoinBudget()
        budget.deadline = budget.clock() + 0.25
        invitee.service._join_budget = budget
        started = time.monotonic()
        try:
            org_binding.await_org_join_bodies(
                invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
            )
        finally:
            invitee.service._join_budget = None
        elapsed = time.monotonic() - started
        assert [space for space, _timeout in text_calls] == [owner_org.directory_id, owner_org.acl_id]
        assert max(timeout for _space, timeout in text_calls) <= 0.25
        assert elapsed >= 0.15
        assert [row["doc"] for row in log.named("org.join.body-pending")] == [ACL_POLICY_DOC]
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
        text_calls = _spy_await_text(invitee.runtime)
        log = LogRecorder()
        invitee.service._log = log
        budget = JoinBudget()
        budget.started = budget.clock() - (org_binding.JOIN_ACTOR_HEADROOM_SECONDS - 0.25)
        invitee.service._join_budget = budget
        started = time.monotonic()
        try:
            org_binding.await_org_join_bodies(
                invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
            )
        finally:
            invitee.service._join_budget = None
        elapsed = time.monotonic() - started
        assert [space for space, _timeout in text_calls] == [owner_org.directory_id, owner_org.acl_id]
        assert max(timeout for _space, timeout in text_calls) <= 0.25
        assert elapsed >= 0.15
        assert [row["doc"] for row in log.named("org.join.body-pending")] == [ACL_POLICY_DOC]
    finally:
        acl_gate.open()
        invitee.close()
