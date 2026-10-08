"""The org network service: create/invite/execute/network over OrgFS.

Implements tailnet-cutover §4.3.  The service owns no transport of its
own: peer mapping, redial and the sidecar's address file all arrive as
injected callbacks (``map_peer``/``request_redial``/``read_own_address``),
so this module never imports forwarding internals and stays testable
against recording fakes.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError, require_space_owner
from hyprial.daemon.impl.org.network.binding import (
    resolve_org_acl_space,
    resolve_org_directory_space,
)
from hyprial.daemon.impl.org.network.directory import (
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC, OrgFsDerivedPolicy
from hyprial.daemon.impl.org.network.state import (
    add_bootstrap_peer,
    add_left_org,
    clear_left_org,
    read_bootstrap_peers,
    read_left_orgs,
    remove_bootstrap_peers,
)
from hyprial.kernel import canonical_user_uri, ipc_errors, parse_user_uri
from hyprial.identity import DeviceRecord, read_device_record
from hyprial.identity import (
    ACTION_INVITE,
    ACTION_LEAVE,
    ACTION_READ,
    ACTION_REMOVE,
    ACTION_WRITE,
    DirectoryDevice,
    InviteLink,
    OrgPolicy,
    RESOURCE_DIRECTORY_ALL,
    RESOURCE_ORG_MEMBERS,
    RESOURCE_ORG_SELF,
    bootstrap_yaml,
    default_role_grants_document,
    decode_invite,
    default_bootstrap,
    encode_invite,
    invite_deep_link,
    invite_https_url,
    org_space_name,
    parse_bootstrap_yaml,
)

#: One join attempt every 0.5s while waiting for a holder to come online.
JOIN_RETRY_SECONDS = 0.5
#: Default ceiling for the join-org retry loop (§4.3: “带重试，直到 holder 可见”).
DEFAULT_JOIN_TIMEOUT_SECONDS = 30.0
#: Default invite validity window.
DEFAULT_INVITE_TTL_SECONDS = 24 * 3600.0

_UNBOUND_DELETE_FALLBACK_REASONS = frozenset(
    {"missing-acl-binding", "unknown-acl-space-id", "acl-owner-mismatch"}
)

_Logger = Callable[..., None]


class OrgNetworkError(RuntimeError):
    """A typed org-network failure; ``code`` is the IPC-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_member_uri(user: str) -> str:
    """Validate IPC member input and return its canonical OrgFS key."""

    if user.startswith("user:"):
        if parse_user_uri(user) is None:
            raise ValueError("user must be a canonical user:<owner> URI")
        return user
    return canonical_user_uri(user)


class OrgNetworkService:
    """Organization lifecycle on top of the OrgFS directory (§4.3)."""

    def __init__(
        self,
        *,
        fs: OrgFs,
        home: Path,
        node_id: str,
        owner: str,
        store: OrgFsDirectoryStore | None = None,
        policy: OrgPolicy | None = None,
        map_peer: Callable[[str, str], int] | None = None,
        unmap_peer: Callable[[str], None] | None = None,
        request_redial: Callable[[], None] | None = None,
        read_own_address: Callable[[], str | None] | None = None,
        casdoor_create_group: Callable[[str], None] | None = None,
        forwarding_status: Callable[[], Mapping[str, Any] | None] | None = None,
        logger: _Logger | None = None,
        invite_ttl_seconds: float = DEFAULT_INVITE_TTL_SECONDS,
        join_timeout_seconds: float = DEFAULT_JOIN_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fs = fs
        self._home = Path(home)
        self._node_id = node_id
        self._owner = owner.removeprefix("user:")
        self._log = logger or (lambda *_args, **_fields: None)
        self._store = (
            store if store is not None else OrgFsDirectoryStore(fs, logger=self._log)
        )
        # Roles are derived live only from OrgFS membership (space owner →
        # owner, rw members → member) while concrete grants come from the
        # ACL-space document; callers may inject their own policy.
        self._policy: OrgPolicy = (
            policy
            if policy is not None
            else OrgFsDerivedPolicy(
                fs,
                logger=lambda level, event, **fields: self._log(
                    level, "org", event, **fields
                ),
            )
        )
        self._map_peer = map_peer
        self._unmap_peer = unmap_peer
        self._request_redial = request_redial
        self._read_own_address = read_own_address
        # LAX(tailnet-cutover): Casdoor group creation is best effort — a
        # failure only warns; the proper shape is a reconciled group
        # resource with retry and an operator surface.
        self._casdoor_create_group = casdoor_create_group
        self._forwarding_status = forwarding_status
        self._invite_ttl_seconds = invite_ttl_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._sleep = sleep
        self._now = now

    # -- org lifecycle ------------------------------------------------------

    def create(self, org: str) -> dict[str, Any]:
        """Create the org directory plus its daemon-owned ACL space."""

        name = org_space_name(org)
        existing = self._find_space(name)
        if existing is not None:
            # Re-running create on a live org is owner business; the very
            # first creation is permissionless by construction — there is
            # no membership table yet, and create_space makes the creator
            # the space owner, hence the org owner.
            self._require_allows(org=org, action="create", resource="org")
        casdoor = "not-configured"
        if self._casdoor_create_group is not None:
            try:
                self._casdoor_create_group(org)
                casdoor = "created"
            except Exception as error:  # noqa: BLE001 - best effort by ruling
                casdoor = "failed"
                self._log(
                    "warn", "org", "org.create.casdoor-failed", org=org, detail=str(error)
                )
        acl_name = f"{name}-acl"
        if existing is None:
            info = self._fs.create_space(name)
            # A same-name space has no authority: names are non-unique. Always
            # create a fresh ACL space and bind its ID into owner-only OrgFS meta.
            acl_info = self._fs.create_space(acl_name)
            self._write_default_acl_policy(acl_info.space_id)
            self._fs.update_space_meta(
                info.space_id, {"aclSpaceId": acl_info.space_id}
            )
            self._store.put_org_meta(org, {"state": "active"})
            acl_existed = False
        else:
            info = existing
            acl_info = self._require_acl_space(org)
            acl_existed = True
        # Shared roots are owner-created.  A joining member only creates the
        # leaf directory for its own principal during enrollment.
        self._store.ensure_org_directory_roots(org)
        try:
            published = self._publish_into(org)
        except OrgNetworkError as error:
            published = False
            self._log(
                "warn", "org", "org.create.publish-skipped", org=org, reason=error.code
            )
        self._log("info", "org", "org.create", org=org, spaceId=info.space_id)
        return {
            "org": org,
            "spaceId": info.space_id,
            "aclSpaceId": acl_info.space_id,
            "space": self._space_json(info),
            "aclSpace": self._space_json(acl_info),
            "existed": existing is not None,
            "aclExisted": acl_existed,
            "casdoorGroup": casdoor,
            "devicePublished": published,
        }

    def invite(self, org: str, user: str) -> dict[str, Any]:
        """Invite one user and return the encoded one-time link (§2.4)."""

        member_uri = _canonical_member_uri(user)
        self._require_allows(
            org=org, action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
        )
        space = self._require_space(org)
        record = self._require_device_record()
        address = self._own_address()
        if address is None:
            raise OrgNetworkError(
                "DEVICE_ADDRESS_UNAVAILABLE",
                "the forwarding sidecar has not written this device's "
                "Tailcat address yet; retry once the daemon is connected",
            )
        # OrgFS keys members by user URI (``user:<name>``), the same form the
        # joining daemon presents as its OrgFS author; a bare name would be a
        # member nobody can ever act as (E2E-025: "not-a-member").
        acl_space = self._require_acl_space(org)
        member = self._fs.invite(space.space_id, member_uri, "rw")
        acl_member = self._fs.invite(acl_space.space_id, member_uri, "ro")
        steps = default_bootstrap()
        workflow_yaml = bootstrap_yaml(steps)
        token = self._new_token()
        expires_at = (
            (self._now() + timedelta(seconds=self._invite_ttl_seconds))
            .isoformat()
            .replace("+00:00", "Z")
        )
        link = InviteLink(
            org=org,
            space_id=space.space_id,
            acl_space_id=acl_space.space_id,
            inviter=record.owner,
            inviter_device=record.device_id,
            inviter_address=address,
            token=token,
            workflow_yaml=workflow_yaml,
            workflow_sha256=hashlib.sha256(workflow_yaml.encode("utf-8")).hexdigest(),
            expires_at=expires_at,
        )
        # The raw token never lands in the directory — only its digest, so
        # the pending-invite document cannot leak the bearer secret.
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._store_put_invite(
            org,
            token_id,
            {
                "org": org,
                "user": user,
                "inviter": record.owner,
                "inviterDevice": record.device_id,
                "tokenDigest": token_id,
                "workflowSha256": link.workflow_sha256,
                "createdAt": self._now().isoformat(),
                "expiresAt": expires_at,
            },
        )
        # §5.1: one payload, three forms — the clickable https landing
        # page (main form), the Desktop deep link, and the bare-token
        # form kept as ``invite`` for scripts and copy-paste.  The https
        # host comes from the profile's inviteBaseUrl.
        https_link = invite_https_url(link, self._invite_base_url())
        self._log(
            "info", "org", "org.invite", org=org, user=user, spaceId=space.space_id
        )
        return {
            "org": org,
            "spaceId": space.space_id,
            "aclSpaceId": acl_space.space_id,
            "user": user,
            "link": https_link,
            "deepLink": invite_deep_link(link),
            "command": f"hyprial org join '{https_link}'",
            "invite": encode_invite(link),
            "expiresAt": expires_at,
            "member": {
                "spaceId": member.space_id,
                "user": member.user,
                "mode": member.mode,
            },
            "aclMember": {
                "spaceId": acl_member.space_id,
                "user": acl_member.user,
                "mode": acl_member.mode,
            },
        }

    def execute(self, link: str) -> dict[str, Any]:
        """Decode a link and run its declarative join workflow (§2.5)."""

        # LAX(tailnet-cutover): the bearer link itself is the join
        # credential (digest-verified, unexpired); the OrgPolicy check W-D
        # sketched here can never pass for a genuine invitee — they hold a
        # member row but no grant matching "join" — so the proper shape is
        # a signed, exactly-once invite plus a dedicated join permission.
        parsed = decode_invite(link)
        steps = parse_bootstrap_yaml(parsed.workflow_yaml)
        executed: list[dict[str, Any]] = []
        for step in steps:
            detail = self._execute_step(parsed, step.kind)
            executed.append({"kind": step.kind, "ok": True, "detail": detail})
        # An explicit join undoes an earlier leave on this node; without it
        # the rejoined org would stay hidden from list()/network().
        clear_left_org(self._home, parsed.org)
        self._log("info", "org", "org.execute", org=parsed.org, spaceId=parsed.space_id)
        return {"org": parsed.org, "spaceId": parsed.space_id, "steps": executed}

    def join(self, link: str) -> dict[str, Any]:
        """Join via any invite form — bare token, https URL, deep link (§5.1).

        Equivalent to :meth:`execute`; :func:`decode_invite` accepts all
        three link forms, so this is a named alias for the §5.1 command
        surface (``hyprial org join '<链接>'``).
        """

        return self.execute(link)

    def leave(self, org: str) -> dict[str, Any]:
        """Leave ``org``: remove this device, file a leave request, unmap.

        Three halves of leaving: the directory row goes now, the
        membership row goes when an owner/admin node runs
        :meth:`process_leave_requests`, and the local decision (this node
        stops counting the org) is recorded in ``left-orgs.json``.
        """

        self._require_allows(org=org, action=ACTION_LEAVE, resource=RESOURCE_ORG_SELF)
        space = self._require_space(org)
        peers = self._directory_peers_of(org)
        device_removed = self._store.remove_device(
            org, self._node_id, owner=self._owner
        )
        self._store.put_leave_request(
            org,
            self._owner,
            {
                "org": org,
                "user": self._owner,
                "deviceId": self._node_id,
                "requestedAt": self._now().isoformat(),
            },
        )
        left = add_left_org(self._home, org)
        unmapped: list[str] = []
        if self._unmap_peer is not None:
            for peer in sorted(peers):
                self._unmap_peer(peer)
                unmapped.append(peer)
        remove_bootstrap_peers(
            self._home,
            [
                device_id
                for device_id, entry in read_bootstrap_peers(self._home).items()
                if entry.get("org") == org
            ],
        )
        # LAX(tailnet-cutover): the owner's sidecar allow set is not
        # tightened on leave (allowAny stays on); the proper leave revokes
        # the leaver's nodekey from every member's allow set.
        self._log("info", "org", "org.leave", org=org, spaceId=space.space_id)
        return {
            "org": org,
            "spaceId": space.space_id,
            "deviceRemoved": device_removed,
            "leaveRequestFiled": True,
            "leftOrgs": left,
            "unmappedPeers": unmapped,
        }

    def remove(self, org: str, user: str) -> dict[str, Any]:
        """Remove ``user``; their protected rows remain as inert history."""

        _canonical_member_uri(user)
        self._require_allows(
            org=org, action=ACTION_REMOVE, resource=RESOURCE_ORG_MEMBERS
        )
        space = self._require_space(org)
        removed_devices = self._remove_member_and_devices(org, space.space_id, user)
        # LAX(tailnet-cutover): established Tailcat connections of the
        # removed user are not revoked here (allowAny stays on); the
        # proper removal rewrites every member's allow set.
        self._log("info", "org", "org.remove", org=org, user=user)
        return {
            "org": org,
            "spaceId": space.space_id,
            "user": user,
            "removedDevices": removed_devices,
        }

    def delete(self, org: str) -> dict[str, Any]:
        """Mark ``org`` deleted and drop every non-owner member (owner only)."""

        acl_space, binding_reason = resolve_org_acl_space(self._fs, org)
        # An unbound org has no trustworthy ACL space to keep in sync.
        unbound = (
            acl_space is None and binding_reason in _UNBOUND_DELETE_FALLBACK_REASONS
        )
        if unbound:
            space = self._require_space(org)
            require_space_owner(canonical_user_uri(self._owner), space)
            self._log(
                "warn",
                "org",
                "org.delete.unbound-owner-fallback",
                org=org,
                spaceId=space.space_id,
                reason=binding_reason,
            )
        else:
            self._require_allows(org=org, action="delete", resource="org")
            space = self._require_space(org)
        deleted_at = self._now().isoformat()
        self._store.put_org_meta(
            org, {"state": "deleted", "deletedAt": deleted_at}
        )
        removed: list[str] = []
        for member in self._fs.members(space.space_id):
            if member.user == space.owner:
                continue
            try:
                self._fs.remove_member(space.space_id, member.user)
            except OrgFsError as error:
                if error.code != "invalid-argument":
                    raise
                continue  # already gone
            removed.append(member.user)
            if not unbound:
                self._remove_acl_member(org, member.user)
        self._log("info", "org", "org.delete", org=org, spaceId=space.space_id)
        return {
            "org": org,
            "spaceId": space.space_id,
            "state": "deleted",
            "deletedAt": deleted_at,
            "removedMembers": removed,
        }

    def list(self) -> dict[str, Any]:
        """This node's orgs ({org, spaceId, role, devices, state} each) and left orgs."""

        items: list[dict[str, Any]] = []
        for org in self._orgs():
            space = self._find_space(org_space_name(org))
            try:
                devices = len(self._store.list_devices(org))
            except (OrgFsError, ValueError) as error:
                self._log(
                    "warn", "org", "org.list.directory-failed", org=org, detail=str(error)
                )
                devices = 0
            items.append(
                {
                    "org": org,
                    "spaceId": space.space_id if space is not None else None,
                    "role": self._role_of(org),
                    "devices": devices,
                    "state": self._org_state(org),
                }
            )
        # leftOrgs lets the invite pickup (shell) tell "left on purpose"
        # from "never joined" without a write to the account (2026-10-04).
        return {"orgs": items, "leftOrgs": read_left_orgs(self._home)}

    def process_leave_requests(self) -> dict[str, Any]:
        """Owner/admin reconcile: fulfill every pending leave request."""

        processed: list[dict[str, Any]] = []
        skipped: list[str] = []
        for org in self._orgs():
            if not self._policy.allows(
                org=org,
                subject=self._owner,
                action=ACTION_REMOVE,
                resource=RESOURCE_ORG_MEMBERS,
            ):
                continue  # not this node's job: only owner/admin process leaves
            try:
                requests = self._store.list_leave_requests(org)
            except (OrgFsError, ValueError) as error:
                self._log(
                    "warn",
                    "org",
                    "org.leaves.list-failed",
                    org=org,
                    detail=str(error),
                )
                skipped.append(org)
                continue
            space = self._require_space(org)
            for user, record in sorted(requests.items()):
                if not leave_request_admitted(self._store, self._log, org, user):
                    continue
                removed_devices = self._remove_member_and_devices(
                    org, space.space_id, user
                )
                processed.append(
                    {
                        "org": org,
                        "user": user,
                        "removedDevices": removed_devices,
                        "requestedAt": record.get("requestedAt"),
                    }
                )
        if processed:
            self._log("info", "org", "org.leaves.processed", count=len(processed))
        return {"processed": processed, "skipped": skipped}

    def network(self, org: str | None = None) -> dict[str, Any]:
        """The three-pane network view (§2.6), each pane with its source."""

        orgs = [org] if org is not None else self._orgs()
        if org is not None:
            directory_space, reason = resolve_org_directory_space(self._fs, org)
            if reason == "missing-directory-space":
                raise OrgFsError(
                    "unknown-space", {"org": org, "space": org_space_name(org)}
                )
            self._require_allows(
                org=org, action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
            )
            if directory_space is None:  # policy denial above is the public result
                raise OrgFsError("ambiguous-space", {"org": org})
        unavailable: list[dict[str, object]] = []
        for name in orgs:
            if name == org:
                continue
            try:
                self._require_allows(
                    org=name, action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
                )
            except OrgNetworkError:
                unavailable.append(
                    {
                        "org": name,
                        "available": False,
                        "reason": "policy.grants-unavailable",
                    }
                )
        unavailable_names = {str(item["org"]) for item in unavailable}
        directory: dict[str, list[dict[str, Any]]] = {}
        directory_pending: dict[str, Any] = dict.fromkeys(orgs)  # None: unknown
        for name in orgs:
            if name in unavailable_names:
                directory[name] = []
                continue
            try:
                devices, directory_pending[name] = self._store.scan_devices(name)
            except (OrgFsError, ValueError) as error:
                self._log(
                    "warn",
                    "org",
                    "org.network.directory-failed",
                    org=name,
                    detail=str(error),
                )
                directory[name] = []
                continue
            directory[name] = [self._device_view(device) for device in devices]
        address = self._own_address()
        forwarding = self._forwarding_status() if self._forwarding_status else None
        mapped_peers = forwarding.get("peers") if isinstance(forwarding, Mapping) else None
        sidecar = (
            {
                key: forwarding.get(key)
                for key in ("policy", "endpoints", "dialed", "restartRequired", "lastPoll")
            }
            if isinstance(forwarding, Mapping)
            else None
        )
        return {
            "orgs": orgs,
            "unavailableOrgs": unavailable,
            "directory": directory,
            "directoryPending": directory_pending,
            "self": {
                "deviceId": self._node_id,
                "owner": self._owner,
                "addressPresent": address is not None,
            },
            "mappedPeers": mapped_peers,
            "sidecar": sidecar,
            "sources": {
                "directory": "orgfs",
                "mappedPeers": "forwarding-sidecar",
                "sidecar": "forwarding-sidecar",
            },
        }

    def publish_self(self) -> dict[str, Any]:
        """Write this device into every org it belongs to (§4.3)."""

        published: list[str] = []
        skipped: list[dict[str, str]] = []
        for org in self._orgs():
            try:
                if self._publish_into(org):
                    published.append(org)
            except OrgNetworkError as error:
                skipped.append({"org": org, "reason": error.code})
        if published or skipped:
            self._log(
                "info", "org", "org.publish_self", published=published, skipped=skipped
            )
        return {"published": published, "skipped": skipped}

    def directory_peers(self) -> dict[str, str]:
        """Stable peer label → address for every other member device (§4.2).

        Rows and labels are always keyed by ``(owner, deviceId)`` and rendered
        as ``owner/deviceId``.  Labels therefore never change when a colliding
        device appears later.

        Two sources, directory first: the synced OrgFS rows, then the
        invite-authorized bootstrap peers the directory does not name yet
        (§2.5 -- the joining node's own directory is empty until sync, and
        without the bootstrap half the reconcile would unmap the inviter
        in the redial that ``connect-inviter`` triggers).  A bootstrap
        entry the directory has caught up with is pruned here: the
        directory row is authoritative from that point on.
        """

        rows: dict[tuple[str, str], str] = {}
        for org in self._orgs():
            try:
                devices = self._store.list_devices(org)
            except (OrgFsError, ValueError) as error:
                self._log(
                    "warn",
                    "org",
                    "org.directory_peers.failed",
                    org=org,
                    detail=str(error),
                )
                continue
            for device in devices:
                self._add_directory_peer(rows, device)
        peers = self._render_directory_peers(rows)
        directory_peers = {
            self._peer_label(owner, device_id) for owner, device_id in rows
        }
        bootstrap = read_bootstrap_peers(self._home)
        shadowed = [peer for peer in bootstrap if peer in directory_peers]
        if shadowed:
            remove_bootstrap_peers(self._home, shadowed)
        for peer, entry in bootstrap.items():
            identity = (entry["inviter"], entry["deviceId"])
            if peer in directory_peers or identity == (self._owner, self._node_id):
                continue
            peers[peer] = entry["address"]
        return peers

    # -- workflow steps -----------------------------------------------------

    def _execute_step(self, link: InviteLink, kind: str) -> str:
        if kind == "connect-inviter":
            peer = self._peer_label(link.inviter, link.inviter_device)
            local_port = self._require_map_peer()(
                peer, link.inviter_address
            )
            # Authorize this one peer for the directory-driven reconcile
            # before triggering it: the invite link is the credential, and
            # the directory row only exists after join + sync.
            add_bootstrap_peer(
                self._home,
                inviter=link.inviter,
                device_id=link.inviter_device,
                address=link.inviter_address,
                org=link.org,
                added_at=self._now().isoformat(),
            )
            if self._request_redial is not None:
                self._request_redial()
            return f"mapped inviter on 127.0.0.1:{local_port}"
        if kind == "join-org":
            self._join_with_retry(link.space_id)
            self._join_with_retry(link.acl_space_id)
            return "joined the org directory and ACL spaces"
        if kind == "register-device":
            try:
                self._publish_into(link.org)
            except OrgNetworkError as error:
                return f"skipped ({error.code}); publish_self will register it"
            return "registered this device in the org directory"
        if kind == "connect-members":
            peers = self._directory_peers_of(link.org)
            for peer, address in sorted(peers.items()):
                self._require_map_peer()(peer, address)
            if self._request_redial is not None:
                self._request_redial()
            return f"mapped {len(peers)} member device(s)"
        raise OrgNetworkError(
            ipc_errors.INVALID_ARGUMENT, f"unknown bootstrap step {kind!r}"
        )

    def _join_with_retry(self, space_id: str) -> None:
        deadline = time.monotonic() + self._join_timeout_seconds
        while True:
            try:
                self._fs.join(space_id)
                return
            except OrgFsError as error:
                retryable = error.code == "no-holder-online" or (
                    error.code == "not-a-member"
                    and error.details.get("reason") == "supplier-not-online"
                )
                if not retryable:
                    raise
                if time.monotonic() >= deadline:
                    raise
                self._sleep(JOIN_RETRY_SECONDS)

    # -- helpers -------------------------------------------------------------

    def _publish_into(self, org: str) -> True:
        """Write this device's row into one org's directory.

        A missing address (sidecar not up yet) or a missing device record
        is a not-ready state, not a failure of the org operation itself:
        it raises :class:`OrgNetworkError` so each caller can decide —
        create swallows it, publish_self reports it, execute names it in
        the step detail — and the refresh path republishes once the
        address appears.
        """

        record = read_device_record(self._home)
        if record is None:
            raise OrgNetworkError(
                "DEVICE_RECORD_MISSING",
                "no device record under the hyprial home; run login to "
                "provision the device key first",
            )
        address = self._own_address()
        if address is None:
            raise OrgNetworkError(
                "DEVICE_ADDRESS_UNAVAILABLE",
                "the forwarding sidecar has not written this device's "
                "Tailcat address yet",
            )
        device = DirectoryDevice(
            device_id=record.device_id,
            owner=record.owner,
            server_public=record.server_public,
            client_public=record.client_public,
            address=address,
            key_generation=record.key_generation,
            updated_at=self._now().isoformat(),
        )
        # A member writes only their *own* device row (the owner check is
        # the caller's half of "member 只能写自己的设备记录"); anything
        # beyond that needs the directory-wide write grant.
        if device.owner != self._owner and not self._policy.allows(
            org=org,
            subject=self._owner,
            action=ACTION_WRITE,
            resource=RESOURCE_DIRECTORY_ALL,
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"writing device {device.device_id!r} in org {org!r} is not allowed",
            )
        self._store.put_device(org, device)
        return True

    def _directory_peers_of(self, org: str) -> dict[str, str]:
        rows: dict[tuple[str, str], str] = {}
        for device in self._store.list_devices(org):
            self._add_directory_peer(rows, device)
        peers = self._render_directory_peers(rows)
        directory_peers = {
            self._peer_label(owner, device_id) for owner, device_id in rows
        }
        for peer, entry in read_bootstrap_peers(self._home).items():
            identity = (entry["inviter"], entry["deviceId"])
            if entry.get("org") != org or identity == (self._owner, self._node_id):
                continue
            if peer not in directory_peers:
                peers.setdefault(peer, entry["address"])
        return peers

    def _add_directory_peer(
        self,
        rows: dict[tuple[str, str], str],
        device: DirectoryDevice,
    ) -> None:
        identity = (device.owner.removeprefix("user:"), device.device_id)
        if identity == (self._owner, self._node_id) or not device.address:
            return
        rows.setdefault(identity, device.address)

    @staticmethod
    def _render_directory_peers(
        rows: Mapping[tuple[str, str], str],
    ) -> dict[str, str]:
        return {
            OrgNetworkService._peer_label(owner, device_id): address
            for (owner, device_id), address in sorted(rows.items())
        }

    @staticmethod
    def _peer_label(owner: str, device_id: str) -> str:
        return f"{owner.removeprefix('user:')}/{device_id}"

    def _require_allows(self, *, org: str, action: str, resource: str) -> None:
        if not self._policy.allows(
            org=org, subject=self._owner, action=action, resource=resource
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"{action!r} on {resource!r} in org {org!r} is not allowed",
            )

    def _orgs(self) -> list[str]:
        """This node's live orgs: store orgs minus left and deleted ones."""

        left = set(read_left_orgs(self._home))
        return [
            org
            for org in self._store.orgs()
            if org not in left and self._org_state(org) != "deleted"
        ]

    def _org_state(self, org: str) -> str:
        org_state = getattr(self._store, "org_state", None)
        if org_state is None:  # a store that implements only the §4.1 port
            return "active"
        try:
            return org_state(org)
        except (OrgFsError, ValueError) as error:
            self._log(
                "warn", "org", "org.state.unreadable", org=org, detail=str(error)
            )
            return "active"

    def _role_of(self, org: str) -> str | None:
        role_of = getattr(self._policy, "role_of", None)
        if role_of is None:
            return None
        return role_of(org, self._owner)

    def _remove_member_and_devices(
        self, org: str, space_id: str, user: str
    ) -> list[str]:
        """Drop ``user``'s membership row without owner-writing their rows.

        Membership rows key users verbatim (usually ``user:<name>``) while
        protected rows carry the bare owner name.  Both membership forms are
        tried; an already-gone membership is idempotent success.  Meta-driven
        readers make the retained protected rows inert.
        """

        candidates = (
            [user] if user.startswith("user:") else [user, canonical_user_uri(user)]
        )
        for candidate in candidates:
            try:
                self._fs.remove_member(space_id, candidate)
                self._remove_acl_member(org, candidate)
                break
            except OrgFsError as error:
                # "invalid-argument" means that form was not a member.
                if error.code != "invalid-argument":
                    raise
        return []

    def _remove_acl_member(self, org: str, user: str) -> None:
        acl_space = self._require_acl_space(org)
        try:
            self._fs.remove_member(acl_space.space_id, user)
        except OrgFsError as error:
            if error.code != "invalid-argument":
                raise

    def _write_default_acl_policy(self, space_id: str) -> None:
        try:
            self._fs.mkdir(space_id, "policy")
        except OrgFsError as error:
            if error.code != "invalid-argument" or "already exists" not in str(error):
                raise
        self._fs.write_text(
            space_id,
            ACL_POLICY_DOC,
            json.dumps(
                default_role_grants_document(),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _require_map_peer(self) -> Callable[[str, str], int]:
        if self._map_peer is None:
            raise OrgNetworkError(
                "FORWARDING_UNAVAILABLE",
                "forwarding is not configured on this daemon",
            )
        return self._map_peer

    def _require_device_record(self) -> DeviceRecord:
        record = read_device_record(self._home)
        if record is None:
            raise OrgNetworkError(
                "DEVICE_RECORD_MISSING",
                "no device record under the hyprial home; run login to "
                "provision the device key first",
            )
        return record

    def _invite_base_url(self) -> str:
        """The profile's invite landing-page host (§5.1 ``inviteBaseUrl``).

        Resolved through the daemon's own public face; a broken
        ``profile.json`` raises ValueError, which the IPC layer reports as
        INVALID_ARGUMENT — loud, never a silent fallback.
        """
        from hyprial.daemon import resolve_profile  # same-domain public face

        profile, _source = resolve_profile(hyprial_home=self._home)
        return profile.invite_base_url

    def _own_address(self) -> str | None:
        if self._read_own_address is None:
            return None
        return self._read_own_address()

    def _new_token(self) -> str:
        return secrets.token_urlsafe(24)

    def _find_space(self, name: str) -> Any | None:
        matches = [space for space in self._fs.spaces() if space.name == name]
        if len(matches) > 1:
            raise OrgFsError(
                "ambiguous-space", {"space": name, "count": len(matches)}
            )
        return matches[0] if matches else None

    def _require_space(self, org: str) -> Any:
        name = org_space_name(org)
        space = self._find_space(name)
        if space is None:
            raise OrgFsError("unknown-space", {"org": org, "space": name})
        return space

    def _require_acl_space(self, org: str) -> Any:
        space, reason = resolve_org_acl_space(self._fs, org)
        if space is None:
            self._log(
                "warn",
                "org",
                "policy.grants-unavailable",
                org=org,
                reason=reason or "missing-acl-space",
            )
            raise OrgFsError(
                "unknown-space", {"org": org, "reason": reason or "missing-acl-space"}
            )
        return space

    def _store_put_invite(self, org: str, token_id: str, record: dict[str, Any]) -> None:
        put = getattr(self._store, "put_invite", None)
        if put is None:  # a store that implements only the §4.1 port
            self._log("warn", "org", "org.invite.registration-unsupported", org=org)
            return
        put(org, token_id, record)

    @staticmethod
    def _device_view(device: DirectoryDevice) -> dict[str, Any]:
        # The address embeds the PSK; IPC answers keep it out and say only
        # whether one is recorded (the raw value never leaves the daemon).
        view = dict(device.as_record())
        view["addressPresent"] = bool(view.pop("address"))
        return view

    @staticmethod
    def _space_json(info: Any) -> dict[str, Any]:
        return {
            "spaceId": info.space_id,
            "name": info.name,
            "owner": info.owner,
            "createdAt": info.created_at,
        }


__all__ = ["OrgNetworkError", "OrgNetworkService", "OrgFsDirectoryStore"]
