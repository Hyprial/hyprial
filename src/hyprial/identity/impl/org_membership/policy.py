"""Who may write a membership record."""

from __future__ import annotations

from hyprial.identity.impl.org_membership.models import (
    org_kind,
)

__all__ = [
    "org_accepts_outsider",
]


def org_accepts_outsider(org: str, subject: str) -> bool:
    """Whether ``subject`` (``user:<owner>``) may be added to ``org`` at all."""

    org_kind(org)
    return True

