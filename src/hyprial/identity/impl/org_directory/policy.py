"""Organization authorization: pycasbin RBAC with domains (M0 §4.2).

The domain is the org: a user's role is granted *per org*, so an admin of
``alpha`` is a stranger in ``beta``.  The model text lives in this module
and policies live in memory — nothing reads or writes policy files; the
role map arrives as a constructor argument and is rebuilt wholesale by
:meth:`CasbinOrgPolicy.update_roles`.

The default permission table installed into a new ACL space is:

==========  ================================================
role        grants (action / resource)
==========  ================================================
owner       everything (``*`` / ``*``)
admin       ``invite``, ``remove`` on ``org:members``;
            ``write`` on ``directory/*``
member      ``read`` on ``directory/*``;
            ``write`` on ``directory/devices/*``;
            ``leave`` on ``org:self``
==========  ================================================
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import casbin
from casbin import persist
from casbin.persist.adapter import load_policy_line

from hyprial.identity.impl.org_directory.models import org_space_name

__all__ = [
    "ACTION_ANY",
    "ACTION_INVITE",
    "ACTION_LEAVE",
    "ACTION_READ",
    "ACTION_REMOVE",
    "ACTION_WRITE",
    "CasbinOrgPolicy",
    "RESOURCE_ANY",
    "RESOURCE_DIRECTORY_ALL",
    "RESOURCE_DIRECTORY_DEVICES",
    "RESOURCE_ORG_MEMBERS",
    "RESOURCE_ORG_SELF",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_OWNER",
    "ROLES",
    "default_role_grants_document",
    "role_grants_from_document",
]

# -- the frozen permission vocabulary ------------------------------------------

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)

ACTION_INVITE = "invite"
ACTION_REMOVE = "remove"
ACTION_READ = "read"
ACTION_WRITE = "write"
ACTION_LEAVE = "leave"
ACTION_ANY = "*"

RESOURCE_ORG_MEMBERS = "org:members"
RESOURCE_DIRECTORY_ALL = "directory/*"
RESOURCE_DIRECTORY_DEVICES = "directory/devices/*"
RESOURCE_ORG_SELF = "org:self"
RESOURCE_ANY = "*"

#: RBAC with domains: ``g(r.sub, p.sub, r.dom)`` ties the role grant to the
#: org; the ``*`` fallbacks let the owner row and the ``*`` resources match.
_MODEL_TEXT = """
[request_definition]
r = sub, dom, obj, act

[policy_definition]
p = sub, dom, obj, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && (r.dom == p.dom || p.dom == "*") && (r.obj == p.obj || keyMatch2(r.obj, p.obj) || p.obj == "*") && (r.act == p.act || p.act == "*")
"""

#: (role, resource, action) rows of the frozen permission table.  The role
#: rows are domain-independent — the *domain* enters only through the
#: ``g, <user>, <role>, <org>`` grants.
_ROLE_GRANTS: tuple[tuple[str, str, str], ...] = (
    (ROLE_OWNER, RESOURCE_ANY, ACTION_ANY),
    (ROLE_ADMIN, RESOURCE_ORG_MEMBERS, ACTION_INVITE),
    (ROLE_ADMIN, RESOURCE_ORG_MEMBERS, ACTION_REMOVE),
    (ROLE_ADMIN, RESOURCE_DIRECTORY_ALL, ACTION_WRITE),
    # admin ⊇ member (integrator correction of the M0 table, 2026-10-03): an
    # admin who can manage members must also read the directory and may leave.
    (ROLE_ADMIN, RESOURCE_DIRECTORY_ALL, ACTION_READ),
    (ROLE_ADMIN, RESOURCE_ORG_SELF, ACTION_LEAVE),
    (ROLE_MEMBER, RESOURCE_DIRECTORY_ALL, ACTION_READ),
    (ROLE_MEMBER, RESOURCE_DIRECTORY_DEVICES, ACTION_WRITE),
    (ROLE_MEMBER, RESOURCE_ORG_SELF, ACTION_LEAVE),
)

#: Policy lines are comma-separated text; a comma or whitespace inside a
#: user or org would silently split a rule, so they are rejected outright.
_CLEAN = re.compile(r"[^,\s]+")

RoleGrant = tuple[str, str, str]


def default_role_grants_document() -> dict[str, object]:
    """Build the policy document installed into a new org ACL space."""

    return {
        "version": 1,
        "grants": [
            {"role": role, "resource": resource, "action": action}
            for role, resource, action in _ROLE_GRANTS
        ],
    }


def role_grants_from_document(value: object) -> tuple[RoleGrant, ...]:
    """Validate and decode one ACL-space role-grants policy document."""

    if not isinstance(value, Mapping) or set(value) != {"version", "grants"}:
        raise ValueError("role grants policy must contain exactly version and grants")
    if value["version"] != 1:
        raise ValueError("role grants policy version must be 1")
    raw_grants = value["grants"]
    if not isinstance(raw_grants, Sequence) or isinstance(raw_grants, (str, bytes)):
        raise ValueError("role grants policy grants must be an array")
    grants: list[RoleGrant] = []
    for index, row in enumerate(raw_grants):
        if not isinstance(row, Mapping) or set(row) != {"role", "resource", "action"}:
            raise ValueError(
                f"role grants policy row {index} must contain role, resource, action"
            )
        role = row["role"]
        resource = row["resource"]
        action = row["action"]
        if role not in ROLES:
            raise ValueError(f"role grants policy row {index} has unknown role {role!r}")
        if not isinstance(resource, str) or _CLEAN.fullmatch(resource) is None:
            raise ValueError(f"role grants policy row {index} has invalid resource")
        if not isinstance(action, str) or _CLEAN.fullmatch(action) is None:
            raise ValueError(f"role grants policy row {index} has invalid action")
        grant = (role, resource, action)
        if grant in grants:
            raise ValueError(f"role grants policy row {index} is a duplicate")
        grants.append(grant)
    return tuple(grants)


class _MemoryAdapter(persist.Adapter):
    """The policy lines Casbin loads, held in memory — never a file."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def load_policy(self, model: casbin.Model) -> None:
        for line in self._lines:
            load_policy_line(line, model)


class CasbinOrgPolicy:
    """The :class:`~...ports.OrgPolicy` port backed by pycasbin.

    ``roles`` maps org → {user: role}; rebuild one org's slice with
    :meth:`update_roles`.  Everything is in memory: the enforcer is
    reconstructed from the role map on every update, which keeps the
    adapter trivially correct (no incremental policy diffing).
    """

    def __init__(
        self,
        roles: Mapping[str, Mapping[str, str]],
        grants: Sequence[RoleGrant],
    ) -> None:
        self._roles: dict[str, dict[str, str]] = {}
        for org, mapping in roles.items():
            self._roles[_checked_org(org)] = _checked_roles(org, mapping)
        self._grants = tuple(grants)
        self._enforcer = self._build_enforcer()

    @classmethod
    def from_roles(
        cls,
        roles: Mapping[str, Mapping[str, str]],
        grants: Sequence[RoleGrant],
    ) -> "CasbinOrgPolicy":
        return cls(roles, grants)

    def update_roles(self, org: str, roles: Mapping[str, str]) -> None:
        """Replace one org's whole {user: role} map and rebuild."""

        self._roles[_checked_org(org)] = _checked_roles(org, roles)
        self._enforcer = self._build_enforcer()

    def role_of(self, org: str, subject: str) -> str | None:
        """The subject's role in ``org``, or ``None`` when not a member."""

        return self._roles.get(org, {}).get(subject)

    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool:
        """May ``subject`` take ``action`` on ``resource`` inside ``org``?"""

        return bool(self._enforcer.enforce(subject, org, resource, action))

    def _build_enforcer(self) -> casbin.Enforcer:
        lines = [
            f"p, {role}, *, {resource}, {action}"
            for role, resource, action in self._grants
        ]
        for org in sorted(self._roles):
            for user, role in sorted(self._roles[org].items()):
                lines.append(f"g, {user}, {role}, {org}")
        model = casbin.Model()
        model.load_model_from_text(_MODEL_TEXT)
        return casbin.Enforcer(model, _MemoryAdapter(lines))


def _checked_org(org: object) -> str:
    if not isinstance(org, str):
        raise ValueError(f"org must be a string, got {type(org).__name__}")
    org_space_name(org)  # the same name rule the directory applies
    return org


def _checked_roles(org: str, mapping: Mapping[str, str]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for user, role in mapping.items():
        if not isinstance(user, str) or _CLEAN.fullmatch(user) is None:
            raise ValueError(
                f"policy user must be a non-empty string without whitespace "
                f"or commas (org {org!r}): {user!r}"
            )
        if role not in ROLES:
            raise ValueError(
                f"policy role must be one of {ROLES} (org {org!r}, user "
                f"{user!r}): {role!r}"
            )
        roles[user] = role
    return roles
