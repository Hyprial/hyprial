# Decision notes (service grant S4-py, 2026-11-26):
# - Only this device's own grant documents are read: the owner-scoped
#   directory under directory/service-grants/<owner>/. That is one listing
#   plus one read per document, and it is the same set S3's grant and revoke
#   write, so a grant starts being pushed as soon as the immediate path runs.
# - An org whose documents cannot be read is absent from the result, so it
#   contributes no key and never widens a port: the safe direction. Nothing
#   can be reported per grant for it, because its grants are exactly what
#   could not be read.
# - One document that fails to parse never hides the others: that is S3's
#   load_org_service_grants, used as is.
"""Reading this device's service grants out of the org directories."""

from __future__ import annotations

from collections.abc import Sequence

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError

from ..models import StoredServiceGrant
from ..queries import load_org_service_grants


def own_grants(
    store: OrgFsDirectoryStore, orgs: Sequence[str], *, owner: str
) -> dict[str, tuple[StoredServiceGrant, ...]]:
    """``org`` to this owner's stored grants in it, for the orgs that could be read."""

    grants: dict[str, tuple[StoredServiceGrant, ...]] = {}
    for org in orgs:
        try:
            grants[org] = load_org_service_grants(store, org, owner=owner)
        except OrgFsError:
            continue
    return grants


def orgs(store: OrgFsDirectoryStore) -> tuple[str, ...]:
    """Every org this device's directory shows, or none when it cannot be listed."""

    try:
        return tuple(store.orgs())
    except OrgFsError:
        return ()


__all__ = ["orgs", "own_grants"]
