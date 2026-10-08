"""Resolve an org's daemon-owned ACL space from its durable ID binding."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import org_space_name


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


__all__ = ["resolve_org_acl_space", "resolve_org_directory_space"]
