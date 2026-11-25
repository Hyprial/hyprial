"""Org membership records: work orgs.

Pure model. Nothing here reads storage or talks to a daemon: the org name
rules, the record shapes written into OrgFS, and the authorization questions
both sides must answer the same way.
"""

from __future__ import annotations

from hyprial.identity.impl.identity_slug import identity_slug

__all__ = [
    "MAX_ORG_NAME_LENGTH",
    "ORG_KIND_WORK",
    "OrgMembershipError",
    "org_kind",
    "user_key",
]

MAX_ORG_NAME_LENGTH = 63

ORG_KIND_WORK = "work"
ORG_KINDS = (ORG_KIND_WORK,)


class OrgMembershipError(ValueError):
    """A record or name that violates the frozen shape; the message is safe."""


def user_key(owner: str) -> str:
    """Return the normalized identity slug for ``owner``."""

    if not isinstance(owner, str):
        raise OrgMembershipError("owner must be a string")
    bare = owner.removeprefix("user:").strip()
    if not bare:
        raise OrgMembershipError("owner must not be empty")
    return identity_slug(bare)


def org_kind(org: str) -> str:
    """Return ``work`` for a valid ``group-`` org name."""

    if not isinstance(org, str) or not org:
        raise OrgMembershipError("org must be a non-empty string")
    if org.startswith("group-"):
        return ORG_KIND_WORK
    raise OrgMembershipError(f"org {org!r} is not a work org")
