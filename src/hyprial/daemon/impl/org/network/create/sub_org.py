# Decision notes (OC6 sub-org network & tailnet inheritance helpers):
# - Sub-orgs keep their own directory and ACL OrgFS spaces while inheriting
#   `tailnetId` from the root ancestor org; `membership-init` is never invoked
#   for child org spaces (CONTRACT-OC0-v4 §3.8 / J19).
# - Every parent-chain validation for create, invite, and join fails closed
#   and verifies active state, acyclicity, and confirmed ancestor membership
#   without transitivity across sibling or child orgs (J24).
# - Sub-org create and repair require local owner writability across every
#   ancestor plus active local tailnet authority (`role == 'authority'` and
#   `authorityDeviceId == node_id`), and reject reparenting any existing
#   top-level directory space without prior sub-org binding.
# - Removing or leaving only a sub-org preserves the shared parent tailnet
#   without issuing `member-revoke` against the parent tailnet (O16).
"""Sub-org resolution, ancestor checks, and inherited tailnet helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
import uuid

from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.org.network.lifecycle.authority import authority_devices
from hyprial.daemon.impl.org.network.state import (
    inspect_org_create_intent,
    org_create_intent,
)
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import ORG_META_DOC, org_from_space_name, org_space_name
from hyprial.kernel import canonical_user_uri, ipc_errors


@dataclass(frozen=True, slots=True)
class SubOrgContext:
    """Resolved parent and inherited tailnet metadata for one sub-org."""

    parent_org: str
    parent_space_id: str
    tailnet_id: str
    chain: tuple[str, ...]


def read_org_meta_fields(fs: OrgFs, space_id: str) -> dict[str, Any]:
    """Read raw JSON fields from ``directory/org.json`` when present."""

    try:
        text, _version = fs.read_text(space_id, ORG_META_DOC)
    except (OrgFsError, ValueError):
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def sub_org_binding(
    fs: OrgFs, home: Path, org: str, *, space_id: str | None = None
) -> tuple[str | None, str | None]:
    """Return ``(parent_org, tailnet_id)`` stored in metadata or create intent."""

    parent: str | None = None
    tailnet_id: str | None = None
    target_space_id = space_id
    if target_space_id is None:
        space, _reason = resolve_org_directory_space(fs, org)
        if space is not None:
            target_space_id = space.space_id
    if target_space_id is not None:
        meta = read_org_meta_fields(fs, target_space_id)
        raw_parent = meta.get("parent")
        raw_tailnet = meta.get("tailnetId")
        if isinstance(raw_parent, str) and raw_parent:
            parent = raw_parent
        if isinstance(raw_tailnet, str) and raw_tailnet:
            tailnet_id = raw_tailnet
    intent = org_create_intent(home, org)
    if intent is not None:
        raw_parent = intent.get("parent")
        raw_tailnet = intent.get("tailnetId")
        if parent is None and isinstance(raw_parent, str) and raw_parent:
            parent = raw_parent
        if tailnet_id is None and isinstance(raw_tailnet, str) and raw_tailnet:
            tailnet_id = raw_tailnet
    return parent, tailnet_id


def effective_tailnet_id(
    fs: OrgFs, home: Path, org: str, *, space_id: str
) -> str:
    """Resolve the active tailnetId for ``org``, walking ancestors if inherited."""

    visited = {org}
    current_org = org
    current_space_id = space_id
    while True:
        parent, tailnet_id = sub_org_binding(
            fs, home, current_org, space_id=current_space_id
        )
        if parent is None:
            return current_space_id
        if tailnet_id is not None:
            return tailnet_id
        if parent in visited:
            return current_space_id
        visited.add(parent)
        parent_space, _reason = resolve_org_directory_space(fs, parent)
        if parent_space is None:
            return current_space_id
        current_org = parent
        current_space_id = parent_space.space_id


def _is_confirmed_in_roster(roster: Mapping[str, Any] | None, member_uri: str) -> bool:
    """Return whether ``member_uri`` appears in an owner cell or OIDC roster mapping."""

    if not isinstance(roster, Mapping):
        return False
    target_owner = member_uri.removeprefix("user:")
    if member_uri in roster or target_owner in roster:
        return True
    for key, entry in roster.items():
        if key == member_uri or key == target_owner:
            return True
        if not isinstance(entry, Mapping):
            continue
        username = entry.get("username") or entry.get("preferred_username")
        if isinstance(username, str) and username.removeprefix("user:") == target_owner:
            return True
    return False


def _canonical_org_arg(raw_parent: str) -> str:
    """Normalize and validate an explicit parent org string."""

    try:
        normalized = org_space_name(raw_parent.strip())
    except ValueError as error:
        from hyprial.daemon.impl.org.network.service import OrgNetworkError

        raise OrgNetworkError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    reversible = org_from_space_name(normalized)
    if reversible is None:
        from hyprial.daemon.impl.org.network.service import OrgNetworkError

        raise OrgNetworkError(
            ipc_errors.INVALID_ARGUMENT,
            f"invalid parent organization {raw_parent!r}",
        )
    return normalized


def _non_empty_trimmed(value: object) -> str | None:
    if isinstance(value, str) and value and value.strip() == value:
        return value
    return None


def _has_sub_org_keys(
    mapping: Mapping[str, Any] | None, *, space_id: str | None, is_meta: bool
) -> bool:
    if mapping is None:
        return False
    if "parent" in mapping or "parentSpaceId" in mapping:
        return True
    if "tailnetId" not in mapping:
        return False
    raw_tailnet = mapping.get("tailnetId")
    if is_meta and (raw_tailnet is None or (space_id is not None and raw_tailnet == space_id)):
        return False
    return True


def _extract_sub_org_record(
    org: str,
    mapping: Mapping[str, Any] | None,
    *,
    space_id: str | None,
    label: str,
    is_meta: bool,
) -> tuple[str, str, str] | None:
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    if not _has_sub_org_keys(mapping, space_id=space_id, is_meta=is_meta):
        return None
    assert mapping is not None
    raw_parent = _non_empty_trimmed(mapping.get("parent"))
    raw_parent_space = _non_empty_trimmed(mapping.get("parentSpaceId"))
    raw_tailnet = _non_empty_trimmed(mapping.get("tailnetId"))
    missing = [
        name
        for name, val in (
            ("parent", raw_parent),
            ("parentSpaceId", raw_parent_space),
            ("tailnetId", raw_tailnet),
        )
        if val is None
    ]
    if missing:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"org {org!r} {label} is missing required sub-org field(s): {', '.join(missing)}",
        )
    assert raw_parent is not None
    assert raw_parent_space is not None
    assert raw_tailnet is not None
    return _canonical_org_arg(raw_parent), raw_parent_space, raw_tailnet


def extract_meta_sub_org_binding(
    fs: OrgFs, org: str, space_id: str
) -> tuple[str, str, str] | None:
    """Extract validated sub-org metadata binding from ``directory/org.json``."""

    meta = read_org_meta_fields(fs, space_id)
    return _extract_sub_org_record(
        org, meta, space_id=space_id, label="directory metadata", is_meta=True
    )


def _resolve_recorded_sub_org_binding(
    org: str,
    pending: Mapping[str, Any] | None,
    meta: Mapping[str, Any],
    *,
    space_id: str | None,
) -> tuple[str, str, str] | None:
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    pending_rec = _extract_sub_org_record(
        org, pending, space_id=space_id, label="create intent", is_meta=False
    )
    meta_rec = _extract_sub_org_record(
        org, meta, space_id=space_id, label="directory metadata", is_meta=True
    )
    if pending is not None:
        if pending_rec is None and meta_rec is not None:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} create intent conflicts with sub-org directory metadata",
            )
        if pending_rec is not None and meta and meta_rec is None:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} sub-org create intent conflicts with top-level directory metadata",
            )
    if pending_rec is not None and meta_rec is not None and pending_rec != meta_rec:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"org {org!r} create intent conflicts with directory metadata sub-org binding",
        )
    return pending_rec or meta_rec


def resolve_sub_org_context(
    service: Any,
    org: str,
    *,
    explicit_parent: str | None,
    existing: Any | None,
    pending: Mapping[str, str | None] | None,
    existing_active: bool = False,
) -> SubOrgContext | None:
    """Validate optional sub-org creation and resolve inherited tailnet metadata."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    if pending is None:
        _intent, intent_error = inspect_org_create_intent(service._home, org)
        if intent_error is not None:
            raise OrgNetworkError("ORG_ACTION_NOT_AUTHORIZED", intent_error)

    meta = (
        read_org_meta_fields(service._fs, existing.space_id)
        if existing is not None
        else {}
    )
    recorded = _resolve_recorded_sub_org_binding(
        org,
        pending,
        meta,
        space_id=existing.space_id if existing is not None else None,
    )
    recorded_parent, recorded_parent_space, recorded_tailnet = (
        recorded if recorded is not None else (None, None, None)
    )
    cell_binding_reader = getattr(service, "_owner_cell_binding", None)
    cell_binding = (
        cell_binding_reader(org)
        if existing is not None and callable(cell_binding_reader)
        else None
    )

    normalized_org = org_space_name(org)
    parent: str | None = None
    if explicit_parent is not None:
        parent = _canonical_org_arg(explicit_parent)
        if parent == normalized_org:
            raise OrgNetworkError(
                ipc_errors.INVALID_ARGUMENT,
                f"org {org!r} cannot be its own parent",
            )
        if recorded_parent is not None:
            if recorded_parent != parent:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} already binds to parent {recorded_parent!r}",
                )
        else:
            if existing is not None:
                kind = (
                    "active"
                    if (existing_active or meta.get("state") == "active")
                    else "existing"
                )
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"{kind} top-level org {org!r} cannot be reparented to {parent!r}",
                )
            if pending is not None:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"interrupted top-level org {org!r} cannot be reparented to {parent!r}",
                )
            if isinstance(cell_binding, Mapping) and cell_binding.get("parent") is None:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"top-level org {org!r} cell cannot be reparented to {parent!r}",
                )
    else:
        parent = recorded_parent

    if parent is None:
        if isinstance(cell_binding, Mapping) and cell_binding.get("parent") is not None:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} cell is bound as a sub-org while metadata is top-level",
            )
        return None

    context = walk_parent_chain(
        service,
        child_org=org,
        parent_org=parent,
        missing_parent_is_unknown=True,
        require_owner=True,
    )
    if recorded is not None:
        if recorded_parent_space != context.parent_space_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} parentSpaceId {recorded_parent_space!r} mismatches parent {parent!r} space {context.parent_space_id!r}",
            )
        if recorded_tailnet != context.tailnet_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} tailnetId {recorded_tailnet!r} mismatches parent tailnet {context.tailnet_id!r}",
            )
    if existing is not None and context.tailnet_id == existing.space_id:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"sub-org {org!r} cannot inherit its own directorySpace as tailnetId",
        )
    verify_inherited_tailnet_ready(
        service,
        context.tailnet_id,
        existing_space_id=existing.space_id if existing is not None else None,
    )
    return context


def validate_repair_sub_org_binding(
    fs: OrgFs,
    *,
    home: Path,
    owner: str,
    org: str,
    space_id: str,
    intent: Mapping[str, str | None] | None,
    service: Any | None = None,
) -> tuple[dict[str, str | None], str | None] | None:
    """Validate and project sub-org fields for ``repair_candidates``."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    meta = read_org_meta_fields(fs, space_id)
    has_intent = _has_sub_org_keys(intent, space_id=space_id, is_meta=False)
    has_meta = _has_sub_org_keys(meta, space_id=space_id, is_meta=True)
    if not has_intent and not has_meta:
        return None

    def _pick(key: str) -> str | None:
        if intent is not None:
            val = intent.get(key)
            if isinstance(val, str) and val:
                return val
        meta_val = meta.get(key)
        if isinstance(meta_val, str) and meta_val:
            return meta_val
        return None

    fields: dict[str, str | None] = {
        "parent": _pick("parent"),
        "parentSpaceId": _pick("parentSpaceId"),
        "tailnetId": _pick("tailnetId"),
    }
    try:
        recorded = _resolve_recorded_sub_org_binding(
            org, intent, meta, space_id=space_id
        )
        if recorded is None:
            return fields, "missing sub-org binding"
        parent_org, parent_space_id, tailnet_id = recorded
        if parent_org == org_space_name(org):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} cannot be its own parent",
            )
        if tailnet_id == space_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"sub-org {org!r} cannot inherit its own directorySpace as tailnetId",
            )
        if service is not None:
            context = walk_parent_chain(
                service,
                child_org=org,
                parent_org=parent_org,
                missing_parent_is_unknown=False,
                require_owner=True,
            )
            if parent_space_id != context.parent_space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} parentSpaceId {parent_space_id!r} mismatches parent {parent_org!r} space {context.parent_space_id!r}",
                )
            if tailnet_id != context.tailnet_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"org {org!r} tailnetId {tailnet_id!r} mismatches parent tailnet {context.tailnet_id!r}",
                )
            verify_inherited_tailnet_ready(
                service, context.tailnet_id, existing_space_id=space_id
            )
        else:
            owner_uri = canonical_user_uri(owner.removeprefix("user:").strip())
            seen_anc = {org_space_name(org)}
            curr_anc = org_space_name(parent_org)
            expected_anc_space: str | None = parent_space_id
            while True:
                if curr_anc in seen_anc:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"sub-org parent cycle detected at {curr_anc!r}",
                    )
                seen_anc.add(curr_anc)
                anc_space, _reason = resolve_org_directory_space(fs, curr_anc)
                if anc_space is None:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"parent org {curr_anc!r} is missing",
                    )
                if expected_anc_space is not None and anc_space.space_id != expected_anc_space:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"org {org!r} parentSpaceId {expected_anc_space!r} mismatches parent {curr_anc!r} space {anc_space.space_id!r}",
                    )
                raw_anc_owner = getattr(anc_space, "owner", None)
                anc_owner_uri = (
                    canonical_user_uri(raw_anc_owner.removeprefix("user:").strip())
                    if isinstance(raw_anc_owner, str) and raw_anc_owner.strip()
                    else ""
                )
                if anc_owner_uri != owner_uri:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"parent org {curr_anc!r} is not owned by local user {owner_uri!r}",
                    )
                anc_meta = read_org_meta_fields(fs, anc_space.space_id)
                next_anc = anc_meta.get("parent")
                if next_anc is None:
                    break
                if not isinstance(next_anc, str) or not next_anc:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"parent org {curr_anc!r} has invalid parent metadata",
                    )
                raw_next_space = anc_meta.get("parentSpaceId")
                expected_anc_space = (
                    raw_next_space if isinstance(raw_next_space, str) and raw_next_space else None
                )
                curr_anc = org_space_name(next_anc)
    except (OrgNetworkError, OrgFsError, ValueError) as error:
        return fields, str(error)
    return fields, None


def walk_parent_chain(
    service: Any,
    *,
    child_org: str,
    parent_org: str,
    member_uri: str | None = None,
    missing_parent_is_unknown: bool = False,
    require_owner: bool = False,
) -> SubOrgContext:
    """Verify ancestor existence, acyclicity, active state, and member presence."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    target_user = (
        canonical_user_uri(member_uri.removeprefix("user:").strip())
        if member_uri is not None
        else canonical_user_uri(service._owner.removeprefix("user:").strip())
    )
    left = set(read_left_orgs(service._home))
    seen = {org_space_name(child_org)}
    chain: list[str] = []
    current = org_space_name(parent_org)
    parent_space_id: str | None = None
    root_tailnet_id: str | None = None
    expected_next_space: str | None = None
    intermediate_tailnets: list[tuple[str, str]] = []
    cell_binding_reader = getattr(service, "_owner_cell_binding", None)

    while True:
        if current in seen:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"sub-org parent cycle detected at {current!r}",
            )
        seen.add(current)
        chain.append(current)
        if current in left:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {current!r} was left on this machine",
            )
        space = service._find_space(org_space_name(current))
        if space is None:
            if missing_parent_is_unknown and len(chain) == 1:
                raise OrgNetworkError(
                    "unknown-space",
                    f"parent org {current!r} does not exist",
                    {"org": current, "space": org_space_name(current)},
                )
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {current!r} is missing",
            )
        if expected_next_space is not None and space.space_id != expected_next_space:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {current!r} space {space.space_id!r} mismatches expected parent space {expected_next_space!r}",
            )
        state = service._org_state(current)
        if state in {"deleted", "creating", "partial"}:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {current!r} is {state}",
            )
        if require_owner:
            local_owner_uri = canonical_user_uri(
                service._owner.removeprefix("user:").strip()
            )
            raw_space_owner = getattr(space, "owner", None)
            space_owner_uri = (
                canonical_user_uri(raw_space_owner.removeprefix("user:").strip())
                if isinstance(raw_space_owner, str) and raw_space_owner.strip()
                else ""
            )
            if space_owner_uri != local_owner_uri:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent org {current!r} is not owned by local user {local_owner_uri!r}",
                )
            role_reader = getattr(service, "_role_of", None)
            if callable(role_reader):
                role = role_reader(current)
                if role is not None and role != "owner":
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"parent org {current!r} requires owner role on this machine",
                    )
        if parent_space_id is None:
            parent_space_id = space.space_id
        if not service._store.is_member(current, target_user):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"user {target_user!r} is not a confirmed member of parent org {current!r}",
            )
        cell_roster_reader = getattr(service, "_read_owner_cell_roster", None)
        if callable(cell_roster_reader):
            roster = cell_roster_reader(current)
            if roster is not None and not _is_confirmed_in_roster(roster, target_user):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"user {target_user!r} is not confirmed in parent org {current!r}",
                )
        meta = read_org_meta_fields(service._fs, space.space_id)
        next_parent = meta.get("parent")
        explicit_tailnet = meta.get("tailnetId")
        explicit_parent_space = meta.get("parentSpaceId")
        cell_binding = (
            cell_binding_reader(current) if callable(cell_binding_reader) else None
        )
        if next_parent is None:
            if explicit_parent_space is not None:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"root org {current!r} metadata cannot declare parentSpaceId without parent",
                )
            if explicit_tailnet is not None and (
                not isinstance(explicit_tailnet, str)
                or explicit_tailnet != space.space_id
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"root org {current!r} tailnetId {explicit_tailnet!r} must equal directorySpace {space.space_id!r}",
                )
            if isinstance(cell_binding, Mapping):
                if cell_binding.get("parent") is not None:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"root org {current!r} cell parent conflicts with top-level metadata",
                    )
                if cell_binding.get("directorySpace") != space.space_id:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"root org {current!r} cell directorySpace mismatches space {space.space_id!r}",
                    )
                if cell_binding.get("tailnet") != space.space_id:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"root org {current!r} cell org.tailnet {cell_binding.get('tailnet')!r} must equal directorySpace {space.space_id!r}",
                    )
            root_tailnet_id = space.space_id
            break
        if not isinstance(next_parent, str) or not next_parent:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {current!r} has invalid parent metadata",
            )
        if explicit_tailnet is not None:
            if (
                not isinstance(explicit_tailnet, str)
                or not explicit_tailnet
                or explicit_tailnet == space.space_id
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} has invalid tailnetId {explicit_tailnet!r}",
                )
            intermediate_tailnets.append((current, explicit_tailnet))
        if explicit_parent_space is not None:
            if not isinstance(explicit_parent_space, str) or not explicit_parent_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} has invalid parentSpaceId",
                )
            expected_next_space = explicit_parent_space
        else:
            expected_next_space = None
        if isinstance(cell_binding, Mapping):
            if cell_binding.get("directorySpace") != space.space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} cell directorySpace mismatches space {space.space_id!r}",
                )
            cell_parent = cell_binding.get("parent")
            if not isinstance(cell_parent, Mapping):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} cell is missing org.parent binding",
                )
            cell_p_space = cell_parent.get("directorySpace")
            if not isinstance(cell_p_space, str) or not cell_p_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} cell org.parent.directorySpace is invalid",
                )
            if explicit_parent_space is not None and cell_p_space != explicit_parent_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} metadata parentSpaceId mismatches cell org.parent.directorySpace",
                )
            expected_next_space = cell_p_space
            cell_tailnet = cell_binding.get("tailnet")
            if (
                not isinstance(cell_tailnet, str)
                or not cell_tailnet
                or cell_tailnet == space.space_id
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} cell org.tailnet is invalid",
                )
            if explicit_tailnet is not None and cell_tailnet != explicit_tailnet:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {current!r} metadata tailnetId mismatches cell org.tailnet",
                )
            intermediate_tailnets.append((current, cell_tailnet))
        current = next_parent

    assert parent_space_id is not None and root_tailnet_id is not None
    for anc_org, anc_tailnet in intermediate_tailnets:
        if anc_tailnet != root_tailnet_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent sub-org {anc_org!r} tailnet {anc_tailnet!r} mismatches root tailnet {root_tailnet_id!r}",
            )
    return SubOrgContext(
        parent_org=chain[0],
        parent_space_id=parent_space_id,
        tailnet_id=root_tailnet_id,
        chain=tuple(chain),
    )


def verify_inherited_tailnet_ready(
    service: Any, tailnet_id: str, *, existing_space_id: str | None = None
) -> None:
    """Confirm parent tailnet already exists on this device with active local authority."""

    if getattr(service, "_network_isolated", False):
        return
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    status = service._tailnet_call("membership-status", {})
    tailnets = status.get("tailnets", ()) if isinstance(status, Mapping) else ()
    if existing_space_id is not None and any(
        isinstance(item, Mapping) and item.get("tailnetId") == existing_space_id
        for item in tailnets
    ):
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"org space {existing_space_id!r} already has its own initialized tailnet and cannot be reparented",
        )
    matched = next(
        (
            item
            for item in tailnets
            if isinstance(item, Mapping) and item.get("tailnetId") == tailnet_id
        ),
        None,
    )
    if matched is None:
        raise OrgNetworkError(
            "MEMBERSHIP_NOT_INITIALIZED",
            f"inherited parent tailnet {tailnet_id!r} is not initialized on this machine",
        )
    local_node_id = getattr(service, "_node_id", None)
    authority_device_id = matched.get("authorityDeviceId")
    if (
        not isinstance(local_node_id, str)
        or not local_node_id
        or matched.get("role") != "authority"
        or not isinstance(authority_device_id, str)
        or authority_device_id != local_node_id
        or matched.get("memberReady") is False
    ):
        raise OrgNetworkError(
            "AUTHORITY_REQUIRED",
            f"this device {local_node_id!r} is not the active authority for parent tailnet {tailnet_id!r}",
        )


def verify_sub_org_join_link(service: Any, parsed: Any) -> SubOrgContext | None:
    """Verify inherited tailnet and local parent membership before accepting a sub-org invite."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    if parsed.parent_org is None:
        return None
    tailnet = parsed.tailnet
    if not isinstance(tailnet, Mapping):
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            "sub-org invitation is missing tailnet binding",
        )
    invite_tailnet_id = tailnet.get("tailnetId")
    if not isinstance(invite_tailnet_id, str) or not invite_tailnet_id or invite_tailnet_id == parsed.space_id:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            "sub-org invitation must inherit parent tailnetId",
        )
    context = walk_parent_chain(
        service,
        child_org=parsed.org,
        parent_org=parsed.parent_org,
        member_uri=canonical_user_uri(service._owner.removeprefix("user:").strip()),
        missing_parent_is_unknown=False,
    )
    if context.tailnet_id != invite_tailnet_id:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            "sub-org invitation tailnetId does not match joined parent org tailnet",
        )
    return context


def sub_org_removal(
    *,
    home: Path,
    org: str,
    space_id: str,
    tailnet_id: str,
    user: str,
    confirmed: bool,
    upper: Any,
    self_leave: bool,
    operation_id: str | None,
    device_ids: list[str] | None,
) -> dict[str, Any]:
    """Perform confirmed sub-org member removal or leave without revoking shared parent tailnet."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    target_owner = user.removeprefix("user:")
    op_dir = Path(home) / "state" / "sub-org-removal"
    op_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{org}--{target_owner}.json".replace("/", "_")
    op_path = op_dir / safe_name

    saved: dict[str, Any] | None = None
    if op_path.exists():
        try:
            loaded = json.loads(op_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = loaded
        except (OSError, ValueError):
            saved = None

    if not confirmed:
        new_op_id = uuid.uuid4().hex
        preview = {
            "operationId": new_op_id,
            "tailnetId": tailnet_id,
            "owner": target_owner,
            "deviceIds": [] if device_ids is None else list(device_ids),
            "phase": "pending-confirmation",
            "confirmed": False,
            "requiresConfirmation": True,
            "tailnetUnchanged": True,
        }
        op_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
        return preview

    if (
        saved is None
        or saved.get("phase") != "pending-confirmation"
        or operation_id != saved.get("operationId")
    ):
        raise OrgNetworkError(
            "REMOVAL_CONFIRMATION_STALE",
            "Removal confirmation is stale or incomplete; re-run preview and confirm.",
        )

    upper()
    completed = {
        "operationId": saved["operationId"],
        "tailnetId": tailnet_id,
        "owner": target_owner,
        "deviceIds": list(saved.get("deviceIds") or []),
        "phase": "complete",
        "confirmed": True,
        "tailnetUnchanged": True,
    }
    op_path.write_text(json.dumps(completed, sort_keys=True), encoding="utf-8")
    return completed


def link_tailnet_id(link: Any) -> str:
    """Return the effective tailnetId carried by a v2 InviteLink."""

    tailnet = getattr(link, "tailnet", None)
    if isinstance(tailnet, Mapping):
        value = tailnet.get("tailnetId")
        if isinstance(value, str) and value:
            return value
    return str(link.space_id)


def create_meta_payload(state: str, sub_context: SubOrgContext | None) -> dict[str, Any]:
    """Build ``directory/org.json`` lifecycle payload including sub-org binding."""

    payload: dict[str, Any] = {"state": state}
    if sub_context is not None:
        payload["parent"] = sub_context.parent_org
        payload["parentSpaceId"] = sub_context.parent_space_id
        payload["tailnetId"] = sub_context.tailnet_id
    return payload


def validate_join_identity_flow(
    service: Any, parsed: Any, claims: Mapping[str, Any] | None
) -> None:
    """Validate OIDC roster and inherited sub-org parent membership on join."""

    import hashlib

    from hyprial.daemon.impl.org.network.service import OrgNetworkError
    from hyprial.identity import JoinIdentityError, validate_oidc_join

    sub_context: SubOrgContext | None = (
        verify_sub_org_join_link(service, parsed)
        if parsed.parent_org is not None
        else None
    )
    if service._oidc_issuer is None and service._oidc_roster is None:
        return
    if service._oidc_issuer is None or service._oidc_roster is None or claims is None:
        raise OrgNetworkError(
            ipc_errors.IDENTITY_CONFLICT,
            "authenticated OIDC identity is required",
            {"conflictField": "issuer"},
        )
    try:
        issuer = service._oidc_issuer(parsed.org)
        roster = service._oidc_roster(parsed.org)
        if not issuer or roster is None:
            raise JoinIdentityError(
                ipc_errors.IDENTITY_CONFLICT,
                "OIDC organization identity is unavailable",
                {"conflictField": "issuer"},
            )
        validate_oidc_join(claims, issuer=issuer, members=roster)
        if sub_context is not None:
            for ancestor_org in sub_context.chain:
                anc_issuer = service._oidc_issuer(ancestor_org)
                anc_roster = service._oidc_roster(ancestor_org)
                if not anc_issuer or anc_roster is None:
                    raise JoinIdentityError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"parent org {ancestor_org!r} identity roster is unavailable",
                    )
                validate_oidc_join(claims, issuer=anc_issuer, members=anc_roster)
        service._join_tokens.consume(
            hashlib.sha256(parsed.token.encode("utf-8")).hexdigest()
        )
    except JoinIdentityError as error:
        raise OrgNetworkError(error.code, str(error), error.details) from error


def list_orgs_payload(service: Any) -> dict[str, Any]:
    """Build ``org.list`` response preserving sub-org parent and inherited tailnetId."""

    items: list[dict[str, Any]] = []
    orgs = service._orgs()
    authority = (
        authority_devices(service._tailnet_call, service._log, service._node_id)
        if orgs
        else {}
    )
    for org in orgs:
        space = service._find_space(org_space_name(org))
        try:
            devices = len(service._store.list_devices(org))
        except (OrgFsError, ValueError) as error:
            service._log(
                "warn", "org", "org.list.directory-failed", org=org, detail=str(error)
            )
            devices = 0
        tailnet_id = (
            effective_tailnet_id(
                service._fs, service._home, org, space_id=space.space_id
            )
            if space is not None
            else None
        )
        parent_org, _ = sub_org_binding(
            service._fs, service._home, org, space_id=space.space_id if space else None
        )
        row: dict[str, Any] = {
            "org": org,
            "spaceId": space.space_id if space is not None else None,
            "role": service._role_of(org),
            "devices": devices,
            "state": service._org_state(org),
            "authorityDevice": (
                authority.get(tailnet_id) if tailnet_id is not None else None
            ),
        }
        if parent_org is not None:
            row["parent"] = parent_org
            row["tailnetId"] = tailnet_id
        items.append(row)
    return {"orgs": items, "leftOrgs": read_left_orgs(service._home)}
