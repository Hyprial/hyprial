"""OC7 deletion rules for OC6 sub-orgs (contract OC0 §3.8, owner ruling option A).

Decision notes:
- Deletion is leaf-first: an org is not deleted while any org that is not
  ``deleted`` declares it as parent. A child counts when its OrgFS
  ``directory/org.json``, its create intent, or the owner's org cell
  (``org.parent.directorySpace``) names the target, so a hand-edited or
  partially written marker cannot hide it. An org whose ``org.json`` exists
  but cannot be read or parsed also blocks: its parent is unknown, so the
  shared tailnet must not be shut down under it.
- An interrupted create (intent recorded, no directory space yet) whose
  ``parent`` is the target blocks too; ``OrgNetworkService.delete`` runs under
  the create lock, so a live create cannot interleave with the check.
- A sub-org is keyed on its recorded ``parent`` and resolves its tailnet with
  ``effective_tailnet_id``; a sub-org whose inherited row is missing on this
  device refuses deletion with ``AUTHORITY_REQUIRED``.
- Revocation in place of the tailnet shutdown removes every non-owner from
  both the directory and the ACL space, and drops this home's pending
  sub-org removal journals for the org.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import ORG_META_DOC, org_space_name
from hyprial.kernel import canonical_user_uri

SUB_ORG_REMOVAL_DIR = "state/sub-org-removal"


def inherited_tailnet(service: Any, org: str, space_id: str) -> str | None:
    """The tailnet a sub-org inherits, or None for a root org."""

    from ..create.sub_org import effective_tailnet_id, sub_org_binding

    parent, _tailnet = sub_org_binding(
        service._fs, service._home, org, space_id=space_id
    )
    if parent is None:
        return None
    return effective_tailnet_id(service._fs, service._home, org, space_id=space_id)


def _names(value: object, target: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return org_space_name(value) == target
    except ValueError:
        return value == target


def _meta_parent(service: Any, space_id: str) -> tuple[object, bool]:
    try:
        text, _version = service._fs.read_text(space_id, ORG_META_DOC)
    except OrgFsError as error:
        return None, error.code != "unknown-doc"
    except ValueError:
        return None, True
    try:
        meta = json.loads(text)
    except ValueError:
        return None, True
    if not isinstance(meta, dict):
        return None, True
    return meta.get("parent"), False


def _cell_parent_space(service: Any, name: str) -> object:
    reader = getattr(service, "_owner_cell_binding", None)
    if reader is None:
        return None
    try:
        binding = reader(name)
    except Exception:  # noqa: BLE001
        return None
    parent = binding.get("parent") if isinstance(binding, Mapping) else None
    return parent.get("directorySpace") if isinstance(parent, Mapping) else None


def live_sub_orgs(service: Any, org: str, space_id: str) -> list[str]:
    """Orgs that are not deleted and name *org* as parent (or cannot be read)."""

    from ..state import _read_org_create_intents

    target = org_space_name(org)
    blockers: set[str] = set()
    for name in service._store.orgs():
        if name == org or service._org_state(name) == "deleted":
            continue
        space = service._find_space(org_space_name(name))
        if space is not None:
            parent, unreadable = _meta_parent(service, space.space_id)
            if unreadable or _names(parent, target):
                blockers.add(name)
                continue
        if _cell_parent_space(service, name) == space_id:
            blockers.add(name)
    for name, intent in _read_org_create_intents(service._home).items():
        if name != org and _names(intent.get("parent"), target):
            if service._find_space(org_space_name(name)) is None or (
                service._org_state(name) != "deleted"
            ):
                blockers.add(name)
    return sorted(blockers)


def revoke_sub_org_members(service: Any, org: str, space_id: str) -> list[str]:
    """Remove every non-owner from the sub-org's directory and ACL spaces."""

    owners = {service._owner, canonical_user_uri(service._owner)}
    spaces = [space_id, service._require_acl_space(org).space_id]
    revoked = sorted(
        {
            row.user
            for space in spaces
            for row in service._fs.members(space)
            if row.user not in owners
        }
    )
    for user in revoked:
        service._remove_orgfs_member(org, space_id, user)
    return revoked


def clear_sub_org_removals(home: Path, org: str) -> None:
    """Drop this home's pending sub-org member-removal journals for *org*."""

    directory = Path(home) / SUB_ORG_REMOVAL_DIR
    if not directory.is_dir():
        return
    for path in directory.glob(f"{org}--*.json"):
        path.unlink(missing_ok=True)


__all__ = [
    "clear_sub_org_removals",
    "inherited_tailnet",
    "live_sub_orgs",
    "revoke_sub_org_members",
]
