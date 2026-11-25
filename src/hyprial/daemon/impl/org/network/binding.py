"""Resolve an org's daemon-owned ACL space from its durable ID binding."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import org_space_name


INVITE_SPACE_MODES = MappingProxyType({"directory": "rw", "acl": "ro"})


def resolve_org_directory_space(
    fs: OrgFs, org: str
) -> tuple[Any | None, str | None]:
    """Return the one directory space for ``org``, never a first match."""

    name = org_space_name(org)
    matches = [space for space in fs.spaces() if space.name == name]
    if not matches:
        return None, "missing-directory-space"
    if len(matches) != 1:
        return None, "ambiguous-directory-space"
    return matches[0], None


def resolve_org_acl_space(fs: OrgFs, org: str) -> tuple[Any | None, str | None]:
    """Resolve the bound ACL space and verify directory/ACL ownership.

    The ACL name is intentionally irrelevant after creation: names are not
    unique and therefore cannot carry authority. The binding lives in the
    replicated owner-only OrgFS space metadata, never in directory content.
    """

    directory, reason = resolve_org_directory_space(fs, org)
    if directory is None:
        return None, reason
    try:
        record = fs.space_meta(directory.space_id)
    except OrgFsError:
        return None, "unreadable-acl-binding"
    acl_space_id = record.get("aclSpaceId")
    if not isinstance(acl_space_id, str) or not acl_space_id:
        return None, "missing-acl-binding"
    matches = [space for space in fs.spaces() if space.space_id == acl_space_id]
    if len(matches) != 1:
        return None, "unknown-acl-space-id"
    acl_space = matches[0]
    if acl_space.owner != directory.owner:
        return None, "acl-owner-mismatch"
    return acl_space, None


def store_org_invite(service: Any, org: str, token_id: str, record: dict[str, Any]) -> None:
    put = getattr(service._store, "put_invite", None)
    if put is None:
        service._log("warn", "org", "org.invite.registration-unsupported", org=org)
        return
    put(org, token_id, record)


def find_org_space(fs: OrgFs, name: str) -> Any | None:
    matches = [space for space in fs.spaces() if space.name == name]
    if len(matches) > 1:
        raise OrgFsError("ambiguous-space", {"space": name, "count": len(matches)})
    return matches[0] if matches else None


def require_org_space(service: Any, org: str) -> Any:
    name = org_space_name(org)
    space = find_org_space(service._fs, name)
    if space is None:
        raise OrgFsError("unknown-space", {"org": org, "space": name})
    return space


def require_org_acl_space(service: Any, org: str) -> Any:
    space, reason = resolve_org_acl_space(service._fs, org)
    if space is None:
        service._log("warn", "org", "policy.grants-unavailable", org=org, reason=reason or "missing-acl-space")
        raise OrgFsError("unknown-space", {"org": org, "reason": reason or "missing-acl-space"})
    return space


__all__ = ["INVITE_SPACE_MODES", "find_org_space", "require_org_acl_space", "require_org_space", "resolve_org_acl_space", "resolve_org_directory_space", "store_org_invite"]
