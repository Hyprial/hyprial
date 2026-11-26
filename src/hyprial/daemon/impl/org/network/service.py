# Decision record (UUID addressing lane C, 2026-11-03):
# - ``deviceUid`` is an additive output field (ruling: devices only gain
#   output fields, their routing keys do not change).  The ``org network``
#   self block always names the local uid.
# - The published directory row carries it only once id-form minting is on:
#   an older reader rejects a device row with an unknown key, so the switch
#   keeps today's published document byte-identical until the flip.
# - A member writes only its own device row (the owner check is the caller's
#   half of "member 只能写自己的设备记录"); anything beyond that needs the
#   directory-wide write grant.
# - Service grant S1 (2026-11-26): the row also carries this device's published
#   encryption key; the decisions live in ``network/device_key``.  Whether the
#   device may write its row in an org is settled before the key is read.
"""Org lifecycle and network views over OrgFS with injected transport hooks."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.daemon.impl.org.network.casdoor import CasdoorGroups, CasdoorMembershipMixin, casdoor_group
from hyprial.daemon.impl.org.network.binding import (
    await_org_join_bodies,
    find_org_space,
    require_org_acl_space,
    require_org_space,
    resolve_org_acl_space,
    store_org_invite,
)
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
from hyprial.daemon.impl.org.network.create.sub_org import (
    create_meta_payload,
    effective_tailnet_id,
    link_tailnet_id,
    list_orgs_payload,
    resolve_sub_org_context,
    sub_org_binding,
    validate_join_identity_flow,
    walk_parent_chain,
)
from hyprial.daemon.impl.org.network.device_key import published_pair
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.policy import OrgFsDerivedPolicy
from hyprial.daemon.impl.org.network.state import (
    add_left_org,
    clear_left_org,
    finish_org_create,
    org_create_intent,
    read_left_orgs,
    update_org_create,
)
from hyprial.daemon.impl.org.network.status.view import network_view
from hyprial.kernel import canonical_user_uri, ipc_errors, parse_user_uri
from hyprial.identity import (
    DeviceRecord,
    id_form_minting,
    mark_first_published,
    read_device_record,
    JoinTokenLedger,
)
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

from .tailnet import TailnetLifecycle
from .tailnet.budget import JoinBudget
from .lifecycle import (
    annotate_deleted_orgs,
    annotate_unbound_org_cells,
    delete_org,
    depart_deleted_org,
    refuse_deleted_or_unbound,
    refuse_resurrected_org,
    require_bound_org_cell,
    require_roster_write_authority,
    skip_unbound_leave_processing,
)

JOIN_RETRY_SECONDS = 0.5
DEFAULT_JOIN_TIMEOUT_SECONDS = 30.0
DEFAULT_INVITE_TTL_SECONDS = 3600.0

_Logger = Callable[..., None]


class OrgNetworkError(RuntimeError):
    """A typed org-network failure; ``code`` is the IPC-facing code."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _canonical_member_uri(user: str) -> str:
    """Validate IPC member input and return its canonical OrgFS key."""

    if user.startswith("user:"):
        if parse_user_uri(user) is None:
            raise ValueError("user must be a canonical user:<owner> URI")
        return user
    return canonical_user_uri(user)


class OrgNetworkService(TailnetLifecycle, CasdoorMembershipMixin):
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
        derive_client_key: Callable[[str], str] | None = None,
        local_server_public: Callable[[], str | None] | None = None,
        request_redial: Callable[[], None] | None = None,
        read_own_address: Callable[[], str | None] | None = None,
        casdoor: CasdoorGroups | None = None,
        forwarding_status: Callable[[], Mapping[str, Any] | None] | None = None,
        membership_request: Callable[[str, dict], dict] | None = None,
        network_probe: Callable[[], dict] | None = None,
        network_isolated: bool = False,
        logger: _Logger | None = None,
        invite_ttl_seconds: float = DEFAULT_INVITE_TTL_SECONDS,
        join_timeout_seconds: float = DEFAULT_JOIN_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ensure_owner_cell: Callable[[str, str], Mapping[str, Any] | None] | None = None,
        owner_cell_roster: Callable[[str], Mapping[str, Any] | None] | None = None, oidc_issuer: Callable[[str], str | None] | None = None,
        oidc_roster: Callable[[str], Mapping[str, Mapping[str, Any]]] | None = None,
        org_cell_bound: Callable[[str, str], bool | None] | None = None,
        owner_cell_binding: Callable[[str], Mapping[str, Any] | None] | None = None,
        encryption_key: Callable[[], Mapping[str, str] | None] | None = None,
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
        self._derive_client_key = derive_client_key
        self._local_server_public = local_server_public
        self._request_redial = request_redial
        self._read_own_address = read_own_address
        self._casdoor = casdoor
        self._forwarding_status = forwarding_status
        self._membership_request = membership_request
        self._network_probe = network_probe
        self._network_isolated = network_isolated
        self._invite_ttl_seconds = invite_ttl_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._join_budget: JoinBudget | None = None
        self._sleep = sleep
        self._now = now
        self._ensure_owner_cell, self._read_owner_cell_roster = ensure_owner_cell, owner_cell_roster
        self._oidc_issuer, self._oidc_roster = oidc_issuer, oidc_roster
        self._org_cell_bound = org_cell_bound
        self._owner_cell_binding = owner_cell_binding
        self._encryption_key = encryption_key
        self._join_tokens = JoinTokenLedger(self._home / "org" / "join-consumed.json")

    @serialized_create
    def create(self, org: str, *, parent: str | None = None) -> dict[str, Any]:
        """Create or resume the creator's org directory and ACL spaces."""

        name = org_space_name(org)
        refuse_resurrected_org(self, org)
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
        sub_context = resolve_sub_org_context(
            self,
            org,
            explicit_parent=parent,
            existing=existing,
            pending=pending,
            existing_active=existing is not None and not needs_finalize,
        )
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
                    parent=sub_context.parent_org if sub_context else None,
                    parent_space_id=sub_context.parent_space_id if sub_context else None,
                    tailnet_id=sub_context.tailnet_id if sub_context else None,
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
            self._store.put_org_meta(org, create_meta_payload("creating", sub_context))
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
        if (existing is None or pending is not None) and self._ensure_owner_cell is not None:
            self._ensure_owner_cell(org, info.space_id)
        if not self._network_isolated and sub_context is None:
            self._ensure_new_tailnet(info)
        finish_org_create(self._home, org)
        if self._network_isolated:
            tailnet_initialized = False
            published = None
        else:
            tailnet_initialized = True
            try:
                self._publish_into(org)
                published = True
            except OrgNetworkError as error:
                published = False
                self._log(
                    "warn", "org", "org.create.publish-skipped", org=org, reason=error.code
                )
        self._store.put_org_meta(org, create_meta_payload("active", sub_context))
        self._log("info", "org", "org.create", org=org, spaceId=info.space_id)
        result = {
            "org": org,
            "spaceId": info.space_id,
            "aclSpaceId": acl_info.space_id,
            "space": space_json(info),
            "aclSpace": space_json(acl_info),
            "existed": existing is not None,
            "aclExisted": acl_existed,
            "casdoorGroup": casdoor,
            "tailnetInitialized": tailnet_initialized,
            "published": published,
            "devicePublished": published,
        }
        if sub_context is not None:
            result["parent"] = sub_context.parent_org
            result["parentSpaceId"] = sub_context.parent_space_id
            result["tailnetId"] = sub_context.tailnet_id
        return result

    def repair_candidates(self, org: str | None = None) -> list[dict[str, Any]]:
        """Return creator-owned org repair candidates without applying them."""

        return repair_candidates(
            self._fs,
            home=self._home,
            node_id=self._node_id,
            owner=self._owner,
            org=org,
            service=self,
        )

    def repair(self, org: str | None = None, *, apply: bool = False) -> dict[str, Any]:
        """Preview or resume incomplete creator-owned org creations."""

        candidates = self.repair_candidates(org)
        repaired: list[dict[str, Any]] = []
        if apply:
            for candidate in candidates:
                if candidate["repairable"]:
                    repaired.append(self.create(str(candidate["org"])))
        return {"apply": apply, "candidates": candidates, "repaired": repaired}

    def invite(self, org: str, user: str) -> dict[str, Any]:
        """Invite one user and return the encoded one-time link (§2.4)."""

        if self._network_isolated:
            raise OrgNetworkError(
                "MEMBERSHIP_NOT_INITIALIZED", "Tailnet sidecar unavailable."
            )
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
        refuse_deleted_or_unbound(self, org, space.space_id)
        sub_parent, _ = sub_org_binding(self._fs, self._home, org, space_id=space.space_id)
        if sub_parent is not None:
            sub_ctx = walk_parent_chain(
                self, child_org=org, parent_org=sub_parent, member_uri=member_uri
            )
            tailnet_id = sub_ctx.tailnet_id
        else:
            tailnet_id = space.space_id
        tailnet_invite = self._tailnet_call('invite-create', {
            'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
            'owner': member_uri.removeprefix('user:'),
            'expiresAtMs': int((self._now().timestamp() + min(3600, self._invite_ttl_seconds)) * 1000),
        })['invite']
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
            tailnet=tailnet_invite,
            parent_org=sub_parent,
        )
        # The raw token never lands in the directory — only its digest, so
        # the pending-invite document cannot leak the bearer secret.
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        store_org_invite(
            self,
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

    def execute(self, link: str, *, claims: Mapping[str, Any] | None = None, oidc_claims: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Decode a link and run its declarative join workflow (§2.5)."""

        parsed = decode_invite(link)
        if claims is None:
            claims = oidc_claims
        self._validate_join_identity(parsed, claims)
        self._join_budget = JoinBudget()
        try:
            self._redeem_tailnet(parsed)
            self._join_budget.remaining("member-announce")
            self._announce_tailnet(link_tailnet_id(parsed))
            steps = parse_bootstrap_yaml(parsed.workflow_yaml)
            executed: list[dict[str, Any]] = []
            for step in steps:
                self._join_budget.remaining(step.kind)
                detail = self._execute_step(parsed, step.kind)
                self._join_budget.remaining(step.kind)
                executed.append({"kind": step.kind, "ok": True, "detail": detail})
                if step.kind == "join-org":
                    clear_left_org(self._home, parsed.org)
            self._log("info", "org", "org.execute", org=parsed.org, spaceId=parsed.space_id)
            return {"org": parsed.org, "spaceId": parsed.space_id, "steps": executed}
        finally:
            self._join_budget = None

    def join(self, link: str, *, claims: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Join via any invite form — bare token, https URL, deep link (§5.1).

        Equivalent to :meth:`execute`; :func:`decode_invite` accepts all
        three link forms, so this is a named alias for the §5.1 command
        surface (``hyprial org join '<链接>'``).
        """

        return self.execute(link, claims=claims)

    def _validate_join_identity(self, parsed: Any, claims: Mapping[str, Any] | None) -> None:
        validate_join_identity_flow(self, parsed, claims)

    def invite_missing_step(self, link: str) -> str | None:
        """Read back directory, ACL membership/policy and their exact binding."""
        from ..membership.invites.recovery import device_committed, missing_membership_step

        parsed = decode_invite(link)
        missing = missing_membership_step(self, parsed)
        if missing is None:
            try:
                if not device_committed(self, parsed):
                    return "register-device"
            except OrgFsError:
                return "register-device"
        return missing

    def accept_invite(self, link: str, *, claims: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Resume the confirmed invite; the caller owns its single flight."""
        from hyprial.identity import invite_identifier
        from ..membership.invites.journal import AcceptJournal
        from ..membership.invites.recovery import run_bootstrap

        parsed = decode_invite(link)
        self._validate_join_identity(parsed, claims)
        journal = AcceptJournal(self._home, invite_identifier(link))
        journal.start()
        return run_bootstrap(self, parsed, journal)

    def leave(self, org: str, *, confirmed: bool = False,
              operation_id: str | None = None, device_ids: list[str] | None = None) -> dict[str, Any]:
        """Leave ``org``: remove this device, file a leave request, unmap.

        Three halves of leaving: the directory row goes now, the
        membership row goes when an owner/admin node runs
        :meth:`process_leave_requests`, and the local decision (this node
        stops counting the org) is recorded in ``left-orgs.json``.
        """

        self._require_allows(org=org, action=ACTION_LEAVE, resource=RESOURCE_ORG_SELF)
        space = self._require_space(org)
        departed = depart_deleted_org(self, org, space)
        if departed is not None:
            return departed
        require_bound_org_cell(self, org, space.space_id)
        operation = self._removal(org, self._owner, confirmed=confirmed, self_leave=True,
                                  operation_id=operation_id, device_ids=device_ids)
        if operation['phase'] == 'complete':
            add_left_org(self._home, org)
        return {'org': org, 'spaceId': space.space_id, **operation}

    def remove(self, org: str, user: str, *, confirmed: bool = False,
               operation_id: str | None = None, device_ids: list[str] | None = None) -> dict[str, Any]:
        """Remove ``user``; their protected rows remain as inert history."""

        _canonical_member_uri(user)
        self._require_allows(
            org=org, action=ACTION_REMOVE, resource=RESOURCE_ORG_MEMBERS
        )
        space = self._require_space(org)
        refuse_deleted_or_unbound(self, org, space.space_id)
        operation = self._removal(org, user, confirmed=confirmed,
                                  operation_id=operation_id, device_ids=device_ids)
        result = {'org': org, 'spaceId': space.space_id, 'user': user, **operation}
        if operation['phase'] == 'complete':
            if operation.get('membershipAction') == 'preserved':
                result['casdoorGroup'] = 'retained in ' + casdoor_group(org) if self._casdoor else 'not-configured'
            else:
                result['casdoorGroup'] = self._casdoor_removal_note(org)
        return result

    @serialized_create
    def delete(self, org: str, *, confirmed: bool = False,
               operation_id: str | None = None) -> dict[str, Any]:
        """Preview or confirmed-execute the org deletion (owner only, W9/D1)."""

        self._require_allows(org=org, action="delete", resource="org")
        return delete_org(self, org, confirmed=confirmed, operation_id=operation_id)

    def list(self) -> dict[str, Any]:
        """List this node's orgs and its intentionally left organizations."""

        return list_orgs_payload(self)

    def leave_request_orgs(self) -> tuple[str, ...]:
        """Return owner/admin orgs eligible for maintenance leave processing."""

        return tuple(
            org
            for org in self._orgs()
            if self._policy.allows(
                org=org,
                subject=self._owner,
                action=ACTION_REMOVE,
                resource=RESOURCE_ORG_MEMBERS,
            )
        )

    def process_leave_requests(self, org: str | None = None) -> dict[str, Any]:
        """Owner/admin reconcile: fulfill every pending leave request."""

        processed: list[dict[str, Any]] = []
        skipped: list[str] = []
        unbound: list[str] = []
        orgs = (org,) if org is not None else self._orgs()
        for org in orgs:
            if not self._policy.allows(
                org=org,
                subject=self._owner,
                action=ACTION_REMOVE,
                resource=RESOURCE_ORG_MEMBERS,
            ):
                continue  # not this node's job: only owner/admin process leaves
            space = self._require_space(org)
            if skip_unbound_leave_processing(self, org, space.space_id):
                unbound.append(org)
                continue
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
            for user, record in sorted(requests.items()):
                if not leave_request_admitted(self._store, self._log, org, user):
                    continue
                try:
                    is_sub = sub_org_binding(self._fs, self._home, org)[0] is not None
                    operation = self._removal(
                        org, user, confirmed=False, process_leave=is_sub
                    )
                    if operation['phase'] != 'complete':
                        skipped.append(org)
                        continue
                    removed_devices = operation['deviceIds']
                except OrgNetworkError as error:
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
        return {"processed": processed, "skipped": skipped, "unbound": unbound}

    def network(self, org: str | None = None) -> dict[str, Any]:
        """The three-pane network view (§2.6), each pane with its source."""

        orgs = [org] if org is not None else self._orgs()
        record = self._require_device_record()
        view = network_view(
            fs=self._fs,
            store=self._store,
            node_id=self._node_id,
            owner=self._owner,
            orgs=orgs,
            org=org,
            require_allows=self._require_allows,
            authorization_error=OrgNetworkError,
            forwarding_status=self._forwarding_status,
            own_address=self._own_address,
            server_public=record.server_public,
            logger=self._log,
            device_uid=record.device_uid,
        )
        annotate_unbound_org_cells(self, view, orgs)
        annotate_deleted_orgs(self, view, org)
        return view

    def require_roster_write_authority(self, org: str, space_id: str) -> None:
        """M2: refuse org-cell roster writes off the tailnet authority device."""

        from .create.sub_org import effective_tailnet_id

        tailnet_id = effective_tailnet_id(self._fs, self._home, org, space_id=space_id)
        if self._network_isolated and tailnet_id != space_id:
            return
        require_roster_write_authority(
            self._tailnet_call, org, tailnet_id, inherited=tailnet_id != space_id
        )

    def publish_self(self) -> dict[str, Any]:
        """Write this device into every org it belongs to (§4.3)."""

        published: list[str] = []
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        for org in self._orgs():
            try:
                if self._publish_into(org):
                    published.append(org)
            except OrgNetworkError as error:
                skipped.append({"org": org, "reason": error.code})
            except Exception as error:
                errors.append({'org': org, 'code': getattr(error, 'code', 'PUBLISH_FAILED'),
                               'errorType': type(error).__name__})
        if published or skipped or errors:
            self._log(
                "info", "org", "org.publish_self", published=published, skipped=skipped
            )
        return {"published": published, "skipped": skipped, "errors": errors}

    def directory_peers(self) -> dict[str, str]:
        """Compatibility name: sidecar roster candidates only."""
        return self._tailnet_candidates()

    def _execute_step(self, link: InviteLink, kind: str) -> str:
        if kind == "connect-inviter":
            for peer, address in sorted(self._tailnet_candidates(link_tailnet_id(link)).items()):
                self._require_map_peer()(peer, address)
            if self._request_redial is not None:
                self._request_redial()
            return "tailnet redeemed; selected roster candidates mapped"
        if kind == "join-org":
            self._join_with_retry(link.space_id)
            self._join_with_retry(link.acl_space_id)
            await_org_join_bodies(self, link.space_id, link.acl_space_id, org=link.org)
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
        leg = "join-orgfs"
        while True:
            try:
                if self._join_budget is None:
                    self._fs.join(space_id)
                else:
                    remaining = self._join_budget.remaining(leg)
                    if self._join_timeout_seconds > 0:
                        remaining = min(
                            remaining, max(0.0, deadline - time.monotonic())
                        )
                    self._fs.join(space_id, timeout=remaining)
                    self._join_budget.remaining(leg)
                return
            except OrgFsError as error:
                retryable = error.code == "no-holder-online" or (
                    error.code == "not-a-member"
                    and error.details.get("reason") == "supplier-not-online"
                )
                if self._join_budget is not None:
                    self._join_budget.remaining(leg)
                if not retryable:
                    raise
                if time.monotonic() >= deadline:
                    raise
                wait = min(JOIN_RETRY_SECONDS, deadline - time.monotonic())
                if self._join_budget is not None:
                    wait = self._join_budget.remaining(leg, wait)
                self._sleep(wait)

    def _publish_into(self, org: str) -> bool:
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
        published_at = self._now().isoformat()
        binding = mark_first_published(self._home, published_at)
        directory_devices = self._store.list_devices(org)
        own_identity = (self._owner, self._node_id)
        current = next(
            (
                candidate
                for candidate in directory_devices
                if (
                    candidate.owner.removeprefix("user:"),
                    candidate.device_id,
                )
                == own_identity
            ),
            None,
        )
        if record.owner != self._owner and not self._policy.allows(
            org=org,
            subject=self._owner,
            action=ACTION_WRITE,
            resource=RESOURCE_DIRECTORY_ALL,
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"writing device {record.device_id!r} in org {org!r} is not allowed",
            )
        # Directory publication is business metadata, never transport admission.
        client_public_by_server: dict[str, str] = {}
        device = DirectoryDevice(
            device_id=record.device_id,
            owner=record.owner,
            server_public=record.server_public,
            client_public=record.client_public,
            address=address,
            key_generation=record.key_generation,
            updated_at=published_at,
            client_public_by_server=client_public_by_server,
            machine_fingerprint=binding.machine_fingerprint,
            first_published_at=binding.first_published_at,
            device_uid=record.device_uid if id_form_minting() else None,
            **published_pair(self._encryption_key),
        )
        if current is not None and (
            current.server_public == device.server_public
            and current.client_public == device.client_public
            and current.client_public_by_server == device.client_public_by_server
            and current.address == device.address
            and current.key_generation == device.key_generation
            and current.machine_fingerprint == device.machine_fingerprint
            and current.first_published_at == device.first_published_at
            and current.device_uid == device.device_uid
            and (current.encryption_key_id, current.encryption_public_key) == (device.encryption_key_id, device.encryption_public_key)
        ):
            return False
        self._store.put_device(org, device)
        return True

    def _directory_peers_of(self, org: str) -> dict[str, str]:
        space_id = self._require_space(org).space_id
        return self._tailnet_candidates(
            effective_tailnet_id(self._fs, self._home, org, space_id=space_id)
        )

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

        operation = self._removal(org, user, confirmed=False)
        return operation['deviceIds'] if operation['phase'] == 'complete' else []

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
        return find_org_space(self._fs, name)

    def _require_space(self, org: str) -> Any:
        return require_org_space(self, org)

    def _require_acl_space(self, org: str) -> Any:
        return require_org_acl_space(self, org)

__all__ = ["OrgNetworkError", "OrgNetworkService", "OrgFsDirectoryStore"]
