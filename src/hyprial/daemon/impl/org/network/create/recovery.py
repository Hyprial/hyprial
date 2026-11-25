"""Durable creator fencing and recovery for ``org.create``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
from threading import RLock
from typing import Any

from hyprial.daemon.impl.org.network.binding import resolve_org_acl_space
from hyprial.daemon.impl.org.network.create.sub_org import (
    extract_meta_sub_org_binding,
    validate_repair_sub_org_binding,
)
from hyprial.daemon.impl.org.network.state import (
    begin_org_create,
    inspect_org_create_intent,
    org_create_intent,
    update_org_create,
)
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import (
    ORG_META_DOC,
    default_role_grants_document,
    org_from_space_name,
    org_space_name,
)
from hyprial.kernel import canonical_user_uri

ACL_POLICY_DOC = "policy/role-grants.json"

_CREATE_LOCK = RLock()

_RECOVERABLE_BINDING_REASONS = frozenset(
    {
        "missing-acl-binding",
        "unknown-acl-space-id",
        "acl-owner-mismatch",
        "missing-acl-policy",
        "missing-directory-entry",
    }
)


def serialized_create(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize create transactions inside the single daemon process."""

    @wraps(operation)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _CREATE_LOCK:
            return operation(*args, **kwargs)

    return locked


class OrgCreateResumeDenied(RuntimeError):
    """The interrupted create belongs to another creator or space."""


def prepare_create(
    *,
    fs: OrgFs,
    home: Path,
    node_id: str,
    owner: str,
    org: str,
    existing: Any | None,
    acl_space: Any | None,
    binding_reason: str | None,
    now: Callable[[], datetime],
    log: Callable[..., None],
    parent: str | None = None,
    parent_space_id: str | None = None,
    tailnet_id: str | None = None,
) -> dict[str, str | None]:
    """Create or validate the local intent and bind its directory ID."""

    principal = canonical_user_uri(owner)
    intent = org_create_intent(home, org)
    if intent is not None:
        _require_matching_intent(org, intent, node_id=node_id, owner=principal)
        if intent.get("parent") != parent:
            raise OrgCreateResumeDenied(
                f"interrupted creation of org {org!r} names another parent"
            )
        if parent is not None:
            if (
                not parent_space_id
                or not tailnet_id
                or intent.get("parentSpaceId") != parent_space_id
                or intent.get("tailnetId") != tailnet_id
            ):
                raise OrgCreateResumeDenied(
                    f"interrupted creation of org {org!r} has mismatched parentSpaceId or tailnetId"
                )
        elif intent.get("parentSpaceId") is not None or intent.get("tailnetId") is not None:
            raise OrgCreateResumeDenied(
                f"interrupted creation of top-level org {org!r} carries sub-org binding"
            )
    elif existing is not None:
        if (
            existing.owner != principal
            or binding_reason not in _RECOVERABLE_BINDING_REASONS
        ):
            raise OrgCreateResumeDenied(
                f"interrupted creation of org {org!r} belongs to another creator"
            )
        try:
            recorded = extract_meta_sub_org_binding(fs, org, existing.space_id)
        except (RuntimeError, ValueError) as error:
            raise OrgCreateResumeDenied(str(error)) from error
        if parent is not None:
            if (
                not parent_space_id
                or not tailnet_id
                or recorded is None
                or recorded != (org_space_name(parent), parent_space_id, tailnet_id)
            ):
                raise OrgCreateResumeDenied(
                    f"existing top-level org {org!r} cannot be reparented to {parent!r} without matching sub-org metadata"
                )
        elif (
            recorded is not None
            or parent_space_id is not None
            or tailnet_id is not None
        ):
            raise OrgCreateResumeDenied(
                f"interrupted creation of org {org!r} has conflicting top-level and sub-org metadata"
            )
        log(
            "warn",
            "org",
            "org.create.legacy-resume",
            org=org,
            spaceId=existing.space_id,
            reason=binding_reason,
        )
        intent = _new_intent(
            home,
            org,
            node_id,
            principal,
            now,
            parent=parent,
            parent_space_id=parent_space_id,
            tailnet_id=tailnet_id,
        )
    else:
        intent = _new_intent(
            home,
            org,
            node_id,
            principal,
            now,
            parent=parent,
            parent_space_id=parent_space_id,
            tailnet_id=tailnet_id,
        )
    if acl_space is not None and intent.get("aclSpaceId") not in (
        None,
        acl_space.space_id,
    ):
        raise OrgCreateResumeDenied(
            f"interrupted creation of org {org!r} names another ACL space"
        )
    if existing is None:
        return intent
    intended_space = intent.get("directorySpaceId")
    if intended_space not in (None, existing.space_id):
        raise OrgCreateResumeDenied(
            f"interrupted creation of org {org!r} names another space"
        )
    if intended_space is None:
        return update_org_create(home, org, directory_space_id=existing.space_id)
    return intent


def resume_acl_space(
    *,
    fs: OrgFs,
    home: Path,
    org: str,
    directory: Any,
    intent: Mapping[str, str | None],
) -> Any:
    """Return the intended ACL space, recovering either crash window."""

    intended_acl = intent.get("aclSpaceId")
    if intended_acl is not None:
        matches = [space for space in fs.spaces() if space.space_id == intended_acl]
        if len(matches) != 1 or matches[0].owner != directory.owner:
            raise OrgCreateResumeDenied(
                f"interrupted creation of org {org!r} has an invalid ACL space"
            )
        return matches[0]
    acl_name = f"{org_space_name(org)}-acl"
    started_at = str(intent["startedAt"])
    candidates = [
        space
        for space in fs.spaces()
        if space.name == acl_name
        and space.owner == directory.owner
        and space.created_at >= started_at
    ]
    if len(candidates) > 1:
        raise OrgFsError(
            "ambiguous-space", {"space": acl_name, "count": len(candidates)}
        )
    acl_space = candidates[0] if candidates else fs.create_space(acl_name)
    update_org_create(home, org, acl_space_id=acl_space.space_id)
    return acl_space


def space_json(info: Any) -> dict[str, Any]:
    return {
        "spaceId": info.space_id,
        "name": info.name,
        "owner": info.owner,
        "createdAt": info.created_at,
    }


def write_default_acl_policy(fs: OrgFs, space_id: str) -> None:
    """Idempotently install the default role grants in an ACL space."""

    try:
        fs.mkdir(space_id, "policy")
    except OrgFsError as error:
        if error.code != "invalid-argument" or "already exists" not in str(error):
            raise
    fs.write_text(
        space_id,
        ACL_POLICY_DOC,
        json.dumps(
            default_role_grants_document(), sort_keys=True, separators=(",", ":")
        ),
    )


def unfinished_create_reason(
    fs: OrgFs, directory: Any, acl_space: Any
) -> str | None:
    """Return a missing create artifact without interpreting custom content."""

    for space_id, path, reason in (
        (acl_space.space_id, ACL_POLICY_DOC, "missing-acl-policy"),
        (directory.space_id, ORG_META_DOC, "missing-directory-entry"),
    ):
        try:
            fs.read_text(space_id, path)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return reason
    return None


def repair_candidates(
    fs: OrgFs,
    *,
    home: Path,
    node_id: str,
    owner: str,
    org: str | None,
    service: Any | None = None,
) -> list[dict[str, Any]]:
    """List uniquely named directory spaces with incomplete create artifacts."""

    names: dict[str, list[Any]] = {}
    for space in fs.spaces():
        candidate_org = org_from_space_name(space.name)
        if candidate_org is not None:
            names.setdefault(candidate_org, []).append(space)
    candidates: list[dict[str, Any]] = []
    for candidate_org, spaces in sorted(names.items()):
        if org is not None and candidate_org != org or len(spaces) != 1:
            continue
        space = spaces[0]
        acl_space, reason = resolve_org_acl_space(fs, candidate_org)
        if acl_space is not None:
            reason = unfinished_create_reason(fs, space, acl_space)
        if reason not in _RECOVERABLE_BINDING_REASONS:
            continue
        intent, intent_error = inspect_org_create_intent(home, candidate_org)
        intended_acl = None if intent is None else intent.get("aclSpaceId")
        repairable = (
            intent_error is None
            and space.owner == canonical_user_uri(owner)
            and (
                intent is None
                or (
                    intent.get("nodeId") == node_id
                    and intent.get("owner") == canonical_user_uri(owner)
                    and intent.get("directorySpaceId") in (None, space.space_id)
                    and (
                        intended_acl is None
                        or acl_space is not None
                        and intended_acl == acl_space.space_id
                    )
                )
            )
        )
        candidate: dict[str, Any] = {
            "org": candidate_org,
            "spaceId": space.space_id,
            "reason": reason,
            "repairable": repairable,
        }
        if intent_error is not None:
            candidate["error"] = intent_error
        else:
            sub_check = validate_repair_sub_org_binding(
                fs,
                home=home,
                owner=owner,
                org=candidate_org,
                space_id=space.space_id,
                intent=intent,
                service=service,
            )
            if sub_check is not None:
                sub_fields, sub_error = sub_check
                candidate.update(sub_fields)
                if sub_error is not None:
                    candidate["repairable"] = False
                    candidate["error"] = sub_error
        candidates.append(candidate)
    return candidates


def _new_intent(
    home: Path,
    org: str,
    node_id: str,
    owner: str,
    now: Callable[[], datetime],
    *,
    parent: str | None = None,
    parent_space_id: str | None = None,
    tailnet_id: str | None = None,
) -> dict[str, str | None]:
    return begin_org_create(
        home,
        org=org,
        node_id=node_id,
        owner=owner,
        started_at=now().isoformat(),
        parent=parent,
        parent_space_id=parent_space_id,
        tailnet_id=tailnet_id,
    )


def _require_matching_intent(
    org: str,
    intent: Mapping[str, str | None],
    *,
    node_id: str,
    owner: str,
) -> None:
    if intent.get("nodeId") != node_id or intent.get("owner") != owner:
        raise OrgCreateResumeDenied(
            f"interrupted creation of org {org!r} belongs to another creator"
        )
