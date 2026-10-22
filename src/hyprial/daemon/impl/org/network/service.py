"""Org lifecycle and network views over OrgFS with injected transport hooks."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError, require_space_owner
from hyprial.daemon.impl.org.network.casdoor import CasdoorGroups, CasdoorMembershipMixin
from hyprial.daemon.impl.org.network.binding import resolve_org_acl_space
from hyprial.daemon.impl.org.network.create import (
    OrgCreateResumeDenied,
    prepare_create,
    repair_candidates,
    resume_acl_space,
    serialized_create,
    space_json,
    unfinished_create_reason,
    write_default_acl_policy,
)
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.policy import OrgFsDerivedPolicy
from hyprial.daemon.impl.org.network.state import (
    add_bootstrap_peer,
    add_left_org,
    clear_left_org,
    finish_org_create,
    org_create_intent,
    read_bootstrap_peers,
    read_left_orgs,
    remove_bootstrap_peers,
    update_org_create,
)
from hyprial.daemon.impl.org.network.status.view import network_view
from hyprial.kernel import canonical_user_uri, ipc_errors, parse_user_uri
from hyprial.identity import DeviceRecord, read_device_record
from hyprial.identity import (
    ACTION_INVITE,
    ACTION_LEAVE,
    ACTION_REMOVE,
    ACTION_WRITE,
    DirectoryDevice,
    InviteLink,
    OrgPolicy,
    RESOURCE_DIRECTORY_ALL,
    RESOURCE_ORG_MEMBERS,
    RESOURCE_ORG_SELF,
    bootstrap_yaml,
    decode_invite,
    default_bootstrap,
    encode_invite,
    invite_deep_link,
    invite_https_url,
    org_space_name,
    parse_bootstrap_yaml,
)

JOIN_RETRY_SECONDS = 0.5
DEFAULT_JOIN_TIMEOUT_SECONDS = 30.0
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


class OrgNetworkService(CasdoorMembershipMixin):
    """Organization lifecycle on top of the OrgFS directory (§4.3)."""

    def __init__(
        self,
        *,
        fs: OrgFs,
        home: Path,
        node_id: str,
        owner: str,
        store: OrgFsDirectoryStore | None = None,
        invalid_devices: InvalidDeviceQuarantine | None = None,
        policy: OrgPolicy | None = None,
        map_peer: Callable[[str, str], int | None] | None = None,
        unmap_peer: Callable[[str], None] | None = None,
        request_redial: Callable[[], None] | None = None,
        read_own_address: Callable[[], str | None] | None = None,
        casdoor: CasdoorGroups | None = None,
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
        if store is None:
            if invalid_devices is None:
                raise TypeError("invalid_devices is required when store is not supplied")
            store = OrgFsDirectoryStore(
                fs,
                logger=self._log,
                invalid_devices=invalid_devices,
            )
        self._store = store
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
        self._casdoor = casdoor
        self._forwarding_status = forwarding_status
        self._invite_ttl_seconds = invite_ttl_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._sleep = sleep
        self._now = now

    @serialized_create
    def create(self, org: str) -> dict[str, Any]:
        """Create or resume the creator's org directory and ACL spaces."""

        name = org_space_name(org)
        existing = self._find_space(name)
        pending = org_create_intent(self._home, org)
        binding_reason: str | None = None
        incomplete_reason: str | None = None
        if existing is not None:
            acl_info, binding_reason = resolve_org_acl_space(self._fs, org)
            if acl_info is not None:
                incomplete_reason = unfinished_create_reason(self._fs, existing, acl_info)
                if pending is None and incomplete_reason is None:
                    self._require_allows(org=org, action="create", resource="org")
            acl_existed = acl_info is not None
        else:
            acl_info = None
            acl_existed = False
        binding_reason = binding_reason or incomplete_reason
        needs_policy = acl_info is None or incomplete_reason == "missing-acl-policy"
        needs_finalize = acl_info is None or pending is not None or incomplete_reason is not None
        if needs_finalize:
            try:
                intent = prepare_create(
                    fs=self._fs,
                    home=self._home,
                    node_id=self._node_id,
                    owner=self._owner,
                    org=org,
                    existing=existing,
                    acl_space=acl_info,
                    binding_reason=binding_reason,
                    now=self._now,
                    log=self._log,
                )
            except OrgCreateResumeDenied as error:
                raise OrgNetworkError("ORG_ACTION_NOT_AUTHORIZED", str(error)) from error
        else:
            intent = None
        casdoor = self._casdoor_require_group(org)  # before any OrgFS write
        if existing is None:
            info = self._fs.create_space(name)
            intent = update_org_create(self._home, org, directory_space_id=info.space_id)
        else:
            info = existing
        if needs_finalize:
            self._store.put_org_meta(org, {"state": "creating"})
        if acl_info is None:
            assert intent is not None
            try:
                acl_info = resume_acl_space(
                    fs=self._fs, home=self._home, org=org, directory=info, intent=intent
                )
            except OrgCreateResumeDenied as error:
                raise OrgNetworkError("ORG_ACTION_NOT_AUTHORIZED", str(error)) from error
        if needs_finalize:
            if needs_policy:
                write_default_acl_policy(self._fs, acl_info.space_id)
            self._fs.update_space_meta(info.space_id, {"aclSpaceId": acl_info.space_id})
        self._store.ensure_org_directory_roots(org)
        finish_org_create(self._home, org)
        try:
            published = self._publish_into(org)
        except OrgNetworkError as error:
            published = False
            self._log(
                "warn", "org", "org.create.publish-skipped", org=org, reason=error.code
            )
        self._store.put_org_meta(org, {"state": "active"})
        self._log("info", "org", "org.create", org=org, spaceId=info.space_id)
        return {
            "org": org,
            "spaceId": info.space_id,
            "aclSpaceId": acl_info.space_id,
            "space": space_json(info),
            "aclSpace": space_json(acl_info),
            "existed": existing is not None,
            "aclExisted": acl_existed,
            "casdoorGroup": casdoor,
            "published": published,
            "devicePublished": published,
        }

    def repair(self, org: str | None = None, *, apply: bool = False) -> dict[str, Any]:
        """Preview or resume incomplete creator-owned org creations."""

        candidates = repair_candidates(self._fs, home=self._home, node_id=self._node_id, owner=self._owner, org=org)
        repaired: list[dict[str, Any]] = []
        if apply:
            for candidate in candidates:
                if candidate["repairable"]:
                    repaired.append(self.create(str(candidate["org"])))
        return {"apply": apply, "candidates": candidates, "repaired": repaired}

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
        # The authority first (design §2); a failed OrgFS write undoes the add.
        member, acl_member = self._invite_casdoor_first(
            org, space.space_id, acl_space.space_id, member_uri
        )
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
            if step.kind == "join-org":  # a join undoes an earlier leave here,
                clear_left_org(self._home, parsed.org)  # even if a later step fails
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
        # The production sidecar launches deny-by-default (empty allow,
        # allowAny=false) until org-directory allow synchronization lands,
        # so the leaver is already refused rather than being revoked later.
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
        # The production sidecar launches deny-by-default (empty allow,
        # allowAny=false), so established Tailcat connections are not
        # admitted until an authenticated allow set is derived.
        self._log("info", "org", "org.remove", org=org, user=user)
        return {
            "org": org,
            "spaceId": space.space_id,
            "user": user,
            "removedDevices": removed_devices,
            "casdoorGroup": self._casdoor_removal_note(org),
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
                try:
                    removed_devices = self._remove_member_and_devices(org, space.space_id, user)
                except OrgNetworkError as error:  # Casdoor first: retried next pass
                    self._log("warn", "org", "org.leaves.casdoor-failed", org=org, user=user, code=error.code)
                    continue
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

        return network_view(
            fs=self._fs,
            store=self._store,
            node_id=self._node_id,
            owner=self._owner,
            orgs=[org] if org is not None else self._orgs(),
            org=org,
            require_allows=self._require_allows,
            authorization_error=OrgNetworkError,
            forwarding_status=self._forwarding_status,
            own_address=self._own_address,
            logger=self._log,
        )

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
        """Return other current member devices from one access snapshot."""

        from hyprial.daemon.impl.org.network.access.snapshot import directory_peers

        return directory_peers(self)

    def _execute_step(self, link: InviteLink, kind: str) -> str:
        if kind == "connect-inviter":
            peer = self._peer_label(link.inviter, link.inviter_device)
            slot = self._require_map_peer()(peer, link.inviter_address)
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
            where = f"slot {slot}" if slot is not None else "a fixed slot"
            return f"mapped inviter on {where}"
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
        return self._org_state_details(org)[0]

    def _org_state_details(
        self, org: str
    ) -> tuple[str, dict[str, object] | None]:
        details = getattr(self._store, "org_state_details", None)
        state_reader = details or getattr(self._store, "org_state", None)
        if state_reader is None:
            acl_space, _reason = resolve_org_acl_space(self._fs, org)
            return ("legacy-active" if acl_space is not None else "partial"), None
        try:
            result = state_reader(org)
            return result if details is not None else (result, None)
        except (OrgFsError, ValueError) as error:
            self._log("warn", "org", "org.state.unreadable", org=org, detail=str(error))
            return "partial", None

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

        self._casdoor_remove(org, user)  # the authority first (design §2)
        self._remove_orgfs_member(org, space_id, user)
        return []

    def _require_map_peer(self) -> Callable[[str, str], int | None]:
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

__all__ = ["OrgNetworkError", "OrgNetworkService", "OrgFsDirectoryStore"]
