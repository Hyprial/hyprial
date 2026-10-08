"""The default org policy: roles derived only from OrgFS membership.

The daemon's half of the §4.2 wiring: identity owns the permission model
(:class:`~hyprial.identity.impl.org_directory.policy.CasbinOrgPolicy`),
and this module derives *who has which role* from the authoritative
membership table — the OrgFS space owner is the org owner, ``rw`` members
are org members. Role assignments beyond that membership-derived mapping are
authoritative in Casdoor and are not read from OrgFS.

The derivation is recomputed on every check: roles change when invites
land and members are removed, and a stale snapshot is exactly the bug a
policy engine exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from hyprial.daemon.impl.org.network.binding import resolve_org_acl_space
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import (
    ROLE_MEMBER,
    ROLE_OWNER,
    CasbinOrgPolicy,
    org_space_name,
    role_grants_from_document,
)

__all__ = [
    "ACL_POLICY_DOC",
    "OrgFsDerivedPolicy",
    "roles_from_orgfs",
]

#: Concrete role-to-permission grants, readable by every org member.
ACL_POLICY_DOC = "policy/role-grants.json"

_Logger = Callable[..., None]


def _normalize(user: str) -> str:
    """Member-table users carry the ``user:`` prefix; policy subjects don't."""

    return user.removeprefix("user:")


def roles_from_orgfs(fs: OrgFs, org: str) -> dict[str, str]:
    """The {user: role} map for ``org`` derived from OrgFS (empty if absent)."""

    roles: dict[str, str] = {}
    space = next(
        (candidate for candidate in fs.spaces() if candidate.name == org_space_name(org)),
        None,
    )
    if space is None:
        return {}
    roles[_normalize(space.owner)] = ROLE_OWNER
    try:
        members = fs.members(space.space_id)
    except OrgFsError:
        members = ()
    for member in members:
        if member.mode == "rw":
            roles.setdefault(_normalize(member.user), ROLE_MEMBER)
    return roles


class OrgFsDerivedPolicy:
    """The OrgPolicy port, derived live from the OrgFS membership table."""

    def __init__(self, fs: OrgFs, logger: _Logger | None = None) -> None:
        self._fs = fs
        self._log = logger or (lambda *_args, **_fields: None)

    def roles(self, org: str) -> dict[str, str]:
        return roles_from_orgfs(self._fs, org)

    def role_of(self, org: str, subject: str) -> str | None:
        return roles_from_orgfs(self._fs, org).get(subject)

    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool:
        roles = roles_from_orgfs(self._fs, org)
        if subject not in roles:
            return False
        grants = self._role_grants(org)
        if grants is None:
            return False
        return CasbinOrgPolicy.from_roles({org: roles}, grants).allows(
            org=org, subject=subject, action=action, resource=resource
        )

    def _role_grants(self, org: str) -> tuple[tuple[str, str, str], ...] | None:
        acl_space, reason = resolve_org_acl_space(self._fs, org)
        if acl_space is None:
            self._unavailable(org, reason or "missing-acl-space")
            return None
        try:
            text, _version = self._fs.read_text(acl_space.space_id, ACL_POLICY_DOC)
            return role_grants_from_document(json.loads(text))
        except OrgFsError as error:
            self._unavailable(org, error.code)
        except (TypeError, ValueError) as error:
            self._unavailable(org, "invalid-policy", detail=str(error))
        return None

    def _unavailable(self, org: str, reason: str, **fields: object) -> None:
        self._log(
            "warn",
            "policy.grants-unavailable",
            org=org,
            reason=reason,
            **fields,
        )
