# Decision record (orgfs package F10, R6-1 and F9 review findings, 2026-11-26):
# - Protocol-3 OrgFS ``join`` returns once meta and tree arrive; document bodies
#   arrive asynchronously through the resident sync.  Organization joins (CLI
#   ``org join``, IPC ``org.join``, IPC ``org.invites.accept``, and bootstrap
#   recovery) wait once inside the ``join-org`` leg for the two known-required
#   documents: ``directory/org.json`` and ``policy/role-grants.json``.
# - Both documents share one deadline bounded by ``JOIN_BODY_WAIT_SECONDS``
#   (5 s — ample for millisecond-scale body arrival on real nodes),
#   ``service._join_timeout_seconds``, the remaining ``JoinBudget`` on the
#   ``join-orgfs`` leg, and ``JOIN_ACTOR_HEADROOM_SECONDS`` (25 s) measured
#   against ``budget.started`` so this wait never pushes an ``OrgCellActor``
#   call past its 30 s ``DEFAULT_CALL_TIMEOUT_S`` into undeclared
#   ``ORG_ACTOR_TIMEOUT`` / ``DAEMON_ERROR``.
# - A legitimately absent document (``unknown-doc``, e.g., legacy orgs without
#   ``directory/org.json``) returns immediately without waiting or failing.
#   Remaining ``content-pending`` raises ``no-holder-online`` carrying the
#   failing ``missingStep``; ``unavailable`` (closing / sync detached) propagates.
"""Resolve an org's daemon-owned ACL space from its durable ID binding."""

from __future__ import annotations

import time
from types import MappingProxyType
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import ORG_META_DOC, org_space_name
from hyprial.kernel import ORGFS_CONTENT_PENDING


INVITE_SPACE_MODES = MappingProxyType({"directory": "rw", "acl": "ro"})
JOIN_BODY_WAIT_SECONDS = 5.0
JOIN_ACTOR_HEADROOM_SECONDS = 25.0


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


def _body_wait_seconds(service: Any) -> float:
    timeout = float(getattr(service, "_join_timeout_seconds", JOIN_BODY_WAIT_SECONDS))
    limit = min(timeout, JOIN_BODY_WAIT_SECONDS) if timeout > 0 else JOIN_BODY_WAIT_SECONDS
    budget = getattr(service, "_join_budget", None)
    if budget is not None:
        budget_remaining = budget.remaining("join-orgfs")
        limit = min(limit, budget_remaining)
        started = getattr(budget, "started", None)
        if started is not None:
            elapsed = max(0.0, budget.clock() - started)
            actor_remaining = max(0.0, JOIN_ACTOR_HEADROOM_SECONDS - elapsed)
            limit = min(limit, actor_remaining)
    return max(0.0, limit)


def _await_join_doc(
    fs: Any,
    space_id: str,
    path: str,
    *,
    timeout: float,
    missing_step: str,
) -> None:
    mesh = getattr(fs, "mesh", None)
    await_text = getattr(mesh, "await_text", None)
    try:
        if callable(await_text):
            await_text(space_id, path, timeout=timeout)
        else:
            read_text = getattr(fs, "read_text", None)
            if callable(read_text):
                read_text(space_id, path)
    except OrgFsError as exc:
        if exc.code == "unknown-doc":
            return
        if exc.code == ORGFS_CONTENT_PENDING:
            raise OrgFsError("no-holder-online", {"missingStep": missing_step}) from exc
        raise


def await_org_join_bodies(
    service: Any, directory_space_id: str, acl_space_id: str
) -> None:
    """Wait once within the join budget for ``directory/org.json`` and ACL policy bodies."""

    from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC

    wait_seconds = _body_wait_seconds(service)
    deadline = time.monotonic() + wait_seconds
    for space_id, path, missing_step in (
        (directory_space_id, ORG_META_DOC, "directory-committed"),
        (acl_space_id, ACL_POLICY_DOC, "acl-applied"),
    ):
        remaining = max(0.0, deadline - time.monotonic()) if wait_seconds > 0 else 0.0
        _await_join_doc(
            service._fs,
            space_id,
            path,
            timeout=remaining,
            missing_step=missing_step,
        )


__all__ = [
    "INVITE_SPACE_MODES",
    "JOIN_ACTOR_HEADROOM_SECONDS",
    "JOIN_BODY_WAIT_SECONDS",
    "await_org_join_bodies",
    "find_org_space",
    "require_org_acl_space",
    "require_org_space",
    "resolve_org_acl_space",
    "resolve_org_directory_space",
    "store_org_invite",
]
