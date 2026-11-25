# Decision notes (OC2 org cell actor sync & roster helpers):
# - Owner machines hold the authoritative `kind="org"` cell in `CellRuntime`
#   with writable member roster (`members` + `params["org.members"]`).
# - Regular member machines (`role != "owner"`) never create or hold `org` cells
#   in `CellRuntime`, read rosters strictly from OrgFS ACL + directory metadata,
#   and reject roster write requests with `ORG_ACTION_NOT_AUTHORIZED`.
# - User uid authority (`user:u.<sub>`) stays unchanged from OC1b; no Casdoor
#   authority switch is introduced here.
"""Owner org-cell persistence and read-only member roster projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any

from hyprial.daemon.impl.cells.model import (
    Cell,
    CellBinding,
    CellMember,
    CellPermissions,
    CellScope,
)
from hyprial.daemon.impl.cells.org.params import MAX_ORG_MEMBERS
from hyprial.daemon.impl.cells.ports import CellRuntimePort, CellSpec
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.daemon.impl.org.cell_actor.sub_org import (
    compute_sub_org_roster_status,
    read_sub_org_cell_binding,
    resolve_org_parent_param,
    verify_parent_chain_confirmed_member,
)
from hyprial.daemon.impl.org.document import is_person_name
from hyprial.daemon.impl.org.network.policy import OrgFsDerivedPolicy, roles_from_orgfs
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import (
    ORG_META_DOC,
    org_from_space_name,
    org_space_name,
    read_device_record,
)
from hyprial.kernel import (
    canonical_user_uri,
    is_device_id_segment,
    is_user_id_segment,
    keyed_cell_id,
)


@dataclass(frozen=True, slots=True)
class OrgMemberWrite:
    """Validated owner-side mutation payload for one ``org`` cell member."""

    uid_ref: str
    username: str
    role: str = "member"
    invited_by: str | None = None
    joined_at_ms: int | None = None
    devices: tuple[Mapping[str, Any], ...] = ()
    default_device_uid: str | None = None


@dataclass(frozen=True, slots=True)
class OrgRosterMember:
    """One member entry projected from an ``org`` cell or read-only OrgFS view."""

    uid_ref: str
    username: str
    role: str
    invited_by: str | None
    joined_at_ms: int | None
    devices: tuple[dict[str, Any], ...]
    in_parent: bool = True


@dataclass(frozen=True, slots=True)
class OrgCellRoster:
    """Org member roster snapshot with explicit mutability and authority source."""

    org: str
    space_id: str | None
    role: str | None
    writable: bool
    source: str
    members: tuple[OrgRosterMember, ...]
    cell_id: str | None = None
    parent_org: str | None = None
    tailnet_id: str | None = None
    out_of_parent_members: tuple[str, ...] = ()
    parent_missing: bool = False


@dataclass(frozen=True, slots=True)
class OrgOwnership:
    """Resolved local org space id, role, cell authority and write permission."""

    org: str
    space_id: str | None
    role: str | None
    authority: str | None
    writable: bool


def _now_ms() -> int:
    return int(time.time() * 1000)


def _local_authority(app: Any) -> str | None:
    user_id = getattr(app, "user_id", None)
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    uid_ref = f"u.{user_id.strip()}"
    if not is_user_id_segment(uid_ref):
        return None
    return canonical_user_uri(uid_ref)


def _person_name_of(app: Any) -> str:
    raw = str(getattr(app, "owner", "") or "").strip()
    if raw.startswith("user:"):
        return raw.removeprefix("user:").strip()
    return raw


def _orgfs_facade(app: Any) -> Any | None:
    getter = getattr(app, "_require_orgfs_runtime", None)
    if not callable(getter):
        return None
    try:
        runtime = getter()
    except Exception:  # noqa: BLE001 - fake or stopped apps may lack orgfs
        return None
    return getattr(runtime, "facade", None)


def _find_org_space(fs: Any, org: str) -> Any | None:
    try:
        target = org_space_name(org)
    except ValueError:
        return None
    try:
        spaces = fs.spaces()
    except (OrgFsError, ValueError):
        return None
    return next((space for space in spaces if space.name == target), None)


def _left_org_set(app: Any) -> set[str]:
    home = getattr(app, "hyprial_home", None)
    if not home:
        return set()
    try:
        return set(read_left_orgs(Path(home)))
    except OSError:
        return set()


def _org_meta_deleted(fs: Any, space_id: str) -> bool:
    for doc_path in (ORG_META_DOC, "org-meta.json"):
        try:
            raw = fs.read_text(space_id, doc_path)
            text = raw[0] if isinstance(raw, tuple) else raw
        except (OrgFsError, ValueError):
            continue
        if '"deleted"' in text:
            return True
    return False


def _is_org_space_name(name: str) -> bool:
    return name.startswith("group-") and not name.endswith("-acl")


def list_live_org_names(app: Any) -> list[str]:
    """Local non-left, non-deleted org names present on the node's OrgFS."""

    fs = _orgfs_facade(app)
    if fs is None:
        return []
    left = _left_org_set(app)
    orgs: list[str] = []
    try:
        spaces = fs.spaces()
    except (OrgFsError, ValueError):
        return []
    for space in spaces:
        if not _is_org_space_name(space.name):
            continue
        org = org_from_space_name(space.name)
        if org is None or org in left or _org_meta_deleted(fs, space.space_id):
            continue
        orgs.append(org)
    return sorted(orgs)


def org_ownership(app: Any, org: str) -> OrgOwnership:
    """Resolve space id, role and owner writeability for *org*."""

    authority = _local_authority(app)
    if org in _left_org_set(app):
        return OrgOwnership(org=org, space_id=None, role=None, authority=authority, writable=False)
    fs = _orgfs_facade(app)
    if fs is None:
        return OrgOwnership(org=org, space_id=None, role=None, authority=authority, writable=False)
    space = _find_org_space(fs, org)
    if space is None or _org_meta_deleted(fs, space.space_id):
        return OrgOwnership(org=org, space_id=None, role=None, authority=authority, writable=False)
    owner_name = str(getattr(app, "owner", "") or "")
    normalized_owner = owner_name.removeprefix("user:").strip()
    role: str | None = None
    try:
        policy = OrgFsDerivedPolicy(fs)
        role = policy.role_of(org, normalized_owner)
    except (OrgFsError, ValueError):
        role = None
    if role is None:
        canonical = canonical_user_uri(owner_name) if owner_name else ""
        if getattr(space, "owner", None) in {owner_name, canonical}:
            role = "owner"
    writable = role == "owner"
    return OrgOwnership(
        org=org,
        space_id=space.space_id,
        role=role,
        authority=authority,
        writable=writable,
    )


def _owner_cell_runtime(app: Any) -> CellRuntimePort | None:
    if getattr(app, "state_dir", None) is None:
        return None
    if _local_authority(app) is None:
        return None
    if _orgfs_facade(app) is None:
        return None
    from hyprial.daemon.impl.composition.cells import cell_runtime

    return cell_runtime(app)


def _issuer_of(runtime: CellRuntimePort, app: Any) -> str:
    issuer = getattr(runtime.identity, "issuer", None)
    if isinstance(issuer, str) and issuer.strip():
        return issuer.strip()
    raw = getattr(app, "user_issuer", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "local"


def _local_device_entry(app: Any, *, now_ms: int, fallback_uid: str | None = None) -> dict[str, Any]:
    device_uid: str | None = None
    record = None
    home = getattr(app, "hyprial_home", None)
    if home:
        try:
            record = read_device_record(Path(home))
        except (OSError, ValueError):
            record = None
        if record is not None and isinstance(record.device_uid, str) and len(record.device_uid) == 32:
            device_uid = f"d.{record.device_uid.lower()}"
    if device_uid is None and fallback_uid is not None and is_device_id_segment(fallback_uid):
        device_uid = fallback_uid
    if device_uid is None:
        seed = f"{getattr(app, 'node_id', '')}:{_person_name_of(app)}".encode("utf-8")
        device_uid = "d." + hashlib.sha256(seed).hexdigest()[:32]
    entry = {
        "deviceUid": device_uid,
        "keyId": None,
        "publicKey": None,
        "verifiedAtMs": now_ms,
    }
    if record is not None and record.client_public and record.server_public:
        entry["masterPublic"] = record.client_public
        entry["serverPublic"] = record.server_public
    return entry


def _device_segment(raw: str) -> str:
    if is_device_id_segment(raw):
        return raw
    if len(raw) == 32:
        return f"d.{raw.lower()}"
    return raw


def _normalize_devices(
    raw_devices: tuple[Mapping[str, Any], ...],
    *,
    now_ms: int,
    fallback_seed: str,
    default_device_uid: str | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_devices:
        if not isinstance(item, Mapping):
            raise OrgNetworkError("MEMBER_CONFLICT", "device entry must be an object")
        uid = _device_segment(str(item.get("deviceUid") or ""))
        key_id = item.get("keyId")
        public_key = item.get("publicKey")
        master_public = item.get("masterPublic")
        server_public = item.get("serverPublic")
        if (master_public is None) != (server_public is None):
            raise OrgNetworkError("MEMBER_CONFLICT", "master/server public keys must be paired")
        if master_public is not None:
            if (not isinstance(master_public, str) or not master_public.strip()
                    or not isinstance(server_public, str) or not server_public.strip()
                    or master_public == server_public):
                raise OrgNetworkError("MEMBER_CONFLICT", "master/server public keys must be distinct strings")
        verified = item.get("verifiedAtMs")
        verified_ms = int(verified) if isinstance(verified, int) and not isinstance(verified, bool) else now_ms
        if not is_device_id_segment(uid):
            raise OrgNetworkError("MEMBER_CONFLICT", f"invalid device uid {uid!r}")
        if uid in seen:
            raise OrgNetworkError("MEMBER_CONFLICT", f"duplicate device uid {uid!r}")
        seen.add(uid)
        normalized.append(
            {
                "deviceUid": uid,
                "keyId": key_id,
                "publicKey": public_key,
                "verifiedAtMs": verified_ms,
                **({"masterPublic": master_public, "serverPublic": server_public}
                   if master_public is not None else {}),
            }
        )
    if normalized:
        return normalized
    if default_device_uid is not None:
        uid = _device_segment(default_device_uid)
    else:
        uid = "d." + hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()[:32]
    return [
        {
            "deviceUid": uid,
            "keyId": None,
            "publicKey": None,
            "verifiedAtMs": now_ms,
        }
    ]


def lookup_owner_org_cell(
    app: Any, *, org: str, space_id: str | None = None
) -> Cell | None:
    """Return this node's already-loaded owner ``kind='org'`` cell without initializing runtime."""

    if getattr(app, "_cell_runtime_closed", False):
        return None
    runtime = getattr(app, "_cell_runtime", None)
    if runtime is None:
        return None
    ownership = org_ownership(app, org)
    if not ownership.writable or ownership.space_id is None:
        return None
    target_space_id = ownership.space_id if space_id is None else space_id
    if target_space_id != ownership.space_id:
        return None
    identity = getattr(runtime, "identity", None)
    authority = getattr(identity, "authority", None)
    if not isinstance(authority, str) or not authority:
        return None
    cell = runtime.lookup(keyed_cell_id("org", authority, target_space_id))
    if cell is None or cell.retired:
        return None
    return cell


def ensure_owner_org_cell(
    app: Any, *, org: str, space_id: str, parent: str | None = None
) -> Cell | None:
    """Create or return this owner node's ``kind='org'`` cell for *org*."""

    ownership = org_ownership(app, org)
    if not ownership.writable or ownership.space_id != space_id:
        return None
    runtime = _owner_cell_runtime(app)
    if runtime is None:
        return None
    username = _person_name_of(app)
    if not is_person_name(username):
        return None
    authority = runtime.identity.authority
    owner_ref = authority.removeprefix("user:")
    cell_id = keyed_cell_id("org", authority, space_id)
    binding_parent, binding_tailnet = read_sub_org_cell_binding(
        app, runtime=runtime, org=org, space_id=space_id
    )
    existing = runtime.lookup(cell_id)
    if existing is not None and not existing.retired:
        existing_parent = existing.param("org.parent")
        existing_tailnet = existing.param("org.tailnet")
        if parent is not None:
            parent_param, tailnet_id = resolve_org_parent_param(
                app,
                runtime=runtime,
                org=org,
                space_id=space_id,
                parent_org=parent,
                tailnet_hint=binding_tailnet,
            )
            if existing_parent != parent_param or existing_tailnet != tailnet_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} cell topology binding is immutable and conflicts with requested parent {parent!r}",
                )
            return existing
        if existing_parent is None:
            if binding_parent is not None or (
                binding_tailnet is not None and binding_tailnet != existing_tailnet
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} top-level cell conflicts with sub-org metadata binding",
                )
            return existing
        if binding_tailnet is not None and binding_tailnet != existing_tailnet:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} sub-org cell tailnet conflicts with metadata binding",
            )
        if binding_parent is not None:
            parent_space = _find_org_space(_orgfs_facade(app), binding_parent)
            if (
                parent_space is not None
                and isinstance(existing_parent, Mapping)
                and existing_parent.get("directorySpace") != parent_space.space_id
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} sub-org cell parent conflicts with metadata binding",
                )
        return existing
    if (
        parent is not None
        and binding_parent is not None
        and org_space_name(parent) != org_space_name(binding_parent)
    ):
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"org {org!r} parent {parent!r} conflicts with recorded parent {binding_parent!r}",
        )
    effective_parent = parent if parent is not None else binding_parent
    if effective_parent is not None:
        parent_param, tailnet_id = resolve_org_parent_param(
            app,
            runtime=runtime,
            org=org,
            space_id=space_id,
            parent_org=effective_parent,
            tailnet_hint=binding_tailnet,
        )
    else:
        if binding_tailnet is not None and binding_tailnet != space_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"top-level org {org!r} cannot bind non-self tailnet {binding_tailnet!r}",
            )
        parent_param, tailnet_id = None, space_id
    now_ms = _now_ms()
    device_entry = _local_device_entry(app, now_ms=now_ms)
    params: dict[str, Any] = {
        "org.name": org,
        "org.issuer": _issuer_of(runtime, app),
        "org.directorySpace": space_id,
        "org.tailnet": tailnet_id,
        "org.parent": parent_param,
        "org.members": {
            owner_ref: {
                "username": username,
                "joinedAtMs": now_ms,
                "invitedBy": owner_ref,
                "devices": [device_entry],
            }
        },
    }
    return runtime.ensure(
        CellSpec(
            kind="org",
            binding=CellBinding.keyed(space_id),
            scope=CellScope.of_org(org),
            name=org,
            members=(CellMember(owner_ref, "owner"),),
            permissions=CellPermissions(),
            params=params,
        )
    )


def _owner_cell_lookup(app: Any, org: str, space_id: str) -> tuple[bool, Cell | None]:
    ownership = org_ownership(app, org)
    if ownership.role != "owner" or ownership.authority is None:
        return False, None
    runtime = _owner_cell_runtime(app)
    if runtime is None:
        return False, None
    cell = runtime.lookup(keyed_cell_id("org", ownership.authority, space_id))
    return True, cell if cell is not None and not cell.retired else None


def owner_org_cell_bound(app: Any, *, org: str, space_id: str) -> bool | None:
    """D5 binding state: ``None`` when this node cannot tell (not the owner, no runtime)."""

    known, cell = _owner_cell_lookup(app, org, space_id)
    return (cell is not None) if known else None


def upsert_owner_org_member(
    app: Any,
    *,
    org: str,
    space_id: str,
    member: OrgMemberWrite,
) -> Cell:
    """Add or update *member* on the owner node's ``org`` cell."""

    runtime = _owner_cell_runtime(app)
    if runtime is None:
        raise OrgNetworkError(
            "ORG_CELL_UNAVAILABLE",
            f"owner cell runtime unavailable for {org}",
            {"org": org},
        )
    cell = ensure_owner_org_cell(app, org=org, space_id=space_id)
    if cell is None:
        raise OrgNetworkError(
            "ORG_CELL_UNAVAILABLE",
            f"owner cell could not be initialized for {org}",
            {"org": org},
        )
    if not is_user_id_segment(member.uid_ref):
        raise OrgNetworkError(
            "ORG_CELL_INVALID_MEMBER",
            f"member uid {member.uid_ref!r} is not a valid user uid segment",
            {"org": org, "uidRef": member.uid_ref},
        )
    username = member.username.removeprefix("user:").strip()
    if not is_person_name(username):
        raise OrgNetworkError(
            "ORG_CELL_INVALID_MEMBER",
            f"member username {member.username!r} is not a valid person name",
            {"org": org, "username": member.username},
        )
    verify_parent_chain_confirmed_member(
        app,
        runtime=runtime,
        org=org,
        space_id=space_id,
        uid_ref=member.uid_ref,
        username=username,
    )
    owner_ref = runtime.identity.authority.removeprefix("user:")
    role = "owner" if member.uid_ref == owner_ref else member.role
    now_ms = _now_ms()
    joined_at = member.joined_at_ms if member.joined_at_ms is not None else now_ms
    invited_by = member.invited_by or owner_ref
    devices = _normalize_devices(
        member.devices,
        now_ms=joined_at,
        fallback_seed=f"{space_id}:{member.uid_ref}:{username}",
        default_device_uid=member.default_device_uid,
    )
    current_refs = {item.ref for item in cell.members}
    if member.uid_ref not in current_refs and len(current_refs) >= MAX_ORG_MEMBERS:
        raise OrgNetworkError(
            "MEMBERSHIP_LIMIT_EXCEEDED",
            f"org member limit {MAX_ORG_MEMBERS} exceeded",
            {"limit": MAX_ORG_MEMBERS, "current": len(current_refs)},
        )
    existing_devices = {
        str(device.get("deviceUid"))
        for payload in (cell.param("org.members") or {}).values()
        if isinstance(payload, Mapping)
        for device in (payload.get("devices") or ())
        if isinstance(device, Mapping)
    }
    existing_public_keys = {
        str(device.get("publicKey"))
        for payload in (cell.param("org.members") or {}).values()
        if isinstance(payload, Mapping)
        for device in (payload.get("devices") or ())
        if isinstance(device, Mapping)
        and device.get("publicKey")
        and not device.get("serverPublic")
    }
    existing_public_keys.update(
        str(device.get(key))
        for payload in (cell.param("org.members") or {}).values()
        if isinstance(payload, Mapping)
        for device in (payload.get("devices") or ())
        if isinstance(device, Mapping)
        for key in ("masterPublic", "serverPublic")
        if device.get(key)
    )
    replacing_devices = {
        str(device.get("deviceUid")) for device in
        ((cell.param("org.members") or {}).get(member.uid_ref, {}) or {}).get("devices", ())
        if isinstance(device, Mapping)
    }
    collisions = (existing_devices - replacing_devices) & {
        str(device.get("deviceUid")) for device in devices
    }
    if collisions:
        raise OrgNetworkError(
            "MEMBER_CONFLICT", "device uid already belongs to another member",
            {"deviceUids": sorted(collisions)},
        )
    replacing_public_key_values = [
        str(device.get(key))
        for device in devices
        for key in (("masterPublic", "serverPublic") if device.get("serverPublic") else ("publicKey",))
        if device.get(key)
    ]
    replacing_public_keys = set(replacing_public_key_values)
    if len(replacing_public_keys) != len(replacing_public_key_values):
        raise OrgNetworkError(
            "MEMBER_CONFLICT", "public key is duplicated in the replacement roster",
            {"publicKeys": sorted(replacing_public_keys)},
        )
    old_public_keys = {
        str(device.get("publicKey"))
        for device in ((cell.param("org.members") or {}).get(member.uid_ref, {}) or {}).get("devices", ())
        if isinstance(device, Mapping) and device.get("publicKey") and not device.get("serverPublic")
    }
    old_public_keys.update(
        str(device.get(key))
        for device in ((cell.param("org.members") or {}).get(member.uid_ref, {}) or {}).get("devices", ())
        if isinstance(device, Mapping)
        for key in ("masterPublic", "serverPublic")
        if device.get(key)
    )
    public_collisions = (existing_public_keys - old_public_keys) & replacing_public_keys
    if public_collisions:
        raise OrgNetworkError(
            "MEMBER_CONFLICT", "public key already belongs to another member",
            {"publicKeys": sorted(public_collisions)},
        )
    updated_members = [item for item in cell.members if item.ref != member.uid_ref]
    updated_members.append(CellMember(member.uid_ref, role))
    params = dict(cell.params)
    raw_map = dict(params.get("org.members") or {})
    raw_map[member.uid_ref] = {
        "username": username,
        "joinedAtMs": joined_at,
        "invitedBy": invited_by,
        "devices": devices,
    }
    params["org.members"] = raw_map
    return runtime.update(
        cell.cell_id,
        members=updated_members,
        params=params,
        expect_revision=cell.revision,
    )


def remove_owner_org_member_by_uid(
    app: Any,
    *,
    org: str,
    space_id: str,
    uid_ref: str,
) -> Cell | None:
    """Remove non-owner *uid_ref* from the owner node's ``org`` cell if present."""

    runtime = _owner_cell_runtime(app)
    if runtime is None:
        return None
    cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
    cell = runtime.lookup(cell_id)
    if cell is None or cell.retired:
        return cell
    owner_ref = runtime.identity.authority.removeprefix("user:")
    if uid_ref == owner_ref:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            "cannot remove the org owner from the org cell roster",
            {"org": org, "uidRef": uid_ref},
        )
    if not cell.has_member(uid_ref):
        return cell
    updated_members = tuple(item for item in cell.members if item.ref != uid_ref)
    params = dict(cell.params)
    raw_map = dict(params.get("org.members") or {})
    raw_map.pop(uid_ref, None)
    params["org.members"] = raw_map
    return runtime.update(
        cell.cell_id,
        members=updated_members,
        params=params,
        expect_revision=cell.revision,
    )


def remove_owner_org_member_by_username(
    app: Any,
    *,
    org: str,
    space_id: str,
    username: str,
) -> Cell | None:
    """Remove non-owner member matching *username* from owner's ``org`` cell."""

    runtime = _owner_cell_runtime(app)
    if runtime is None:
        return None
    cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
    cell = runtime.lookup(cell_id)
    if cell is None or cell.retired:
        return cell
    target = username.removeprefix("user:").strip()
    owner_ref = runtime.identity.authority.removeprefix("user:")
    raw_map = cell.param("org.members") or {}
    matching = [
        uid
        for uid, entry in raw_map.items()
        if uid != owner_ref and isinstance(entry, Mapping) and entry.get("username") == target
    ]
    if not matching:
        return cell
    updated = cell
    for uid_ref in matching:
        updated = remove_owner_org_member_by_uid(app, org=org, space_id=space_id, uid_ref=uid_ref)
    return updated


def retire_owner_org_cell(app: Any, *, org: str, space_id: str) -> bool:
    """Retire this owner node's ``org`` cell when present."""

    runtime = _owner_cell_runtime(app)
    if runtime is None:
        return False
    cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
    cell = runtime.lookup(cell_id)
    if cell is None or cell.retired:
        return False
    runtime.retire(cell.cell_id, expect_revision=cell.revision)
    return True


def _roster_from_cell(
    app: Any,
    cell: Cell,
    *,
    org: str,
    space_id: str,
    role: str | None,
    writable: bool,
    runtime: CellRuntimePort,
) -> OrgCellRoster:
    raw_members = cell.param("org.members") or {}
    role_map = {member.ref: member.role for member in cell.members}
    usernames = {
        member.ref: str((raw_members.get(member.ref) or {}).get("username") or member.ref)
        for member in cell.members
    }
    parent_org, tailnet_id, parent_missing, out_of_parent = (
        compute_sub_org_roster_status(
            app,
            runtime=runtime,
            org=org,
            space_id=space_id,
            member_usernames=usernames,
        )
    )
    out_set = set(out_of_parent)
    entries: list[OrgRosterMember] = []
    for member in cell.members:
        payload = raw_members.get(member.ref, {})
        username = usernames[member.ref]
        devices = tuple(dict(dev) for dev in payload.get("devices") or ())
        entries.append(
            OrgRosterMember(
                uid_ref=member.ref,
                username=username,
                role=role_map.get(member.ref, member.role),
                invited_by=payload.get("invitedBy"),
                joined_at_ms=payload.get("joinedAtMs"),
                devices=devices,
                in_parent=member.ref not in out_set,
            )
        )
    return OrgCellRoster(
        org=org,
        space_id=space_id,
        role=role,
        writable=writable,
        source="org-cell",
        members=tuple(entries),
        cell_id=cell.cell_id,
        parent_org=parent_org,
        tailnet_id=tailnet_id or str(cell.param("org.tailnet") or space_id),
        out_of_parent_members=out_of_parent,
        parent_missing=parent_missing,
    )


def _roster_from_orgfs(
    app: Any,
    *,
    org: str,
    space_id: str,
    role: str | None,
    writable: bool,
    store: Any,
) -> OrgCellRoster:
    fs = _orgfs_facade(app)
    roles: dict[str, str] = {}
    if fs is not None:
        try:
            roles = dict(roles_from_orgfs(fs, org))
        except (OrgFsError, ValueError):
            roles = {}
    devices_by_user: dict[str, list[dict[str, Any]]] = {}
    if store is not None:
        try:
            for dev in store.list_devices(org):
                user_key = str(dev.owner).removeprefix("user:").strip()
                devices_by_user.setdefault(user_key, []).append(
                    {
                        "deviceUid": f"d.{dev.device_uid}" if len(dev.device_uid) == 32 else str(dev.device_uid),
                        "keyId": None,
                        "publicKey": dev.server_public,
                        "masterPublic": dev.client_public,
                        "serverPublic": dev.server_public,
                        "verifiedAtMs": None,
                    }
                )
        except (OrgFsError, ValueError):
            devices_by_user = {}
    usernames = sorted({*roles, *devices_by_user})
    runtime = getattr(app, "_cell_runtime", None)
    parent_org, tailnet_id, parent_missing, out_of_parent = (
        compute_sub_org_roster_status(
            app,
            runtime=runtime,
            org=org,
            space_id=space_id,
            member_usernames={name: name for name in usernames},
        )
    )
    out_set = set(out_of_parent)
    entries = tuple(
        OrgRosterMember(
            uid_ref=name,
            username=name,
            role=roles.get(name, "member"),
            invited_by=None,
            joined_at_ms=None,
            devices=tuple(devices_by_user.get(name, ())),
            in_parent=name not in out_set,
        )
        for name in usernames
    )
    return OrgCellRoster(
        org=org,
        space_id=space_id,
        role=role,
        writable=writable,
        source="orgfs-readonly",
        members=entries,
        cell_id=None,
        parent_org=parent_org,
        tailnet_id=tailnet_id,
        out_of_parent_members=out_of_parent,
        parent_missing=parent_missing,
    )


def read_org_roster(
    app: Any,
    *,
    org: str,
    space_id: str | None,
    role: str | None,
    writable: bool,
    store: Any,
) -> OrgCellRoster:
    """Read the org member roster without holding cells for other authorities."""

    if space_id is None:
        return OrgCellRoster(
            org=org,
            space_id=None,
            role=role,
            writable=False,
            source="empty",
            members=(),
            cell_id=None,
        )
    if writable:
        runtime = _owner_cell_runtime(app)
        if runtime is not None:
            cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
            cell = runtime.lookup(cell_id)
            if cell is not None and not cell.retired:
                return _roster_from_cell(
                    app,
                    cell,
                    org=org,
                    space_id=space_id,
                    role=role,
                    writable=True,
                    runtime=runtime,
                )
    return _roster_from_orgfs(
        app,
        org=org,
        space_id=space_id,
        role=role,
        writable=False,
        store=store,
    )


__all__ = [
    "OrgCellRoster",
    "OrgMemberWrite",
    "OrgOwnership",
    "OrgRosterMember",
    "ensure_owner_org_cell",
    "list_live_org_names",
    "lookup_owner_org_cell",
    "org_ownership",
    "read_org_roster",
    "remove_owner_org_member_by_uid",
    "remove_owner_org_member_by_username",
    "retire_owner_org_cell",
    "upsert_owner_org_member",
]
