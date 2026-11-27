# Decision notes (service grant S4-py, 2026-11-26):
# - Only this device's own grant documents are read: the owner-scoped
#   directory under directory/service-grants/<owner>/. That is one listing
#   plus one read per document, and it is the same set S3's grant and revoke
#   write, so a grant starts being pushed as soon as the immediate path runs.
# - An org whose documents cannot be read is absent from the result, so it
#   contributes no key and never widens a port: the safe direction.
# - A read that fails is reported, not only survived (S4-py fix, 2026-11-27,
#   coordinator ruling). It used to be silent, so a port that had just
#   tightened to nobody looked exactly like a port nobody was granted to. The
#   reader names the org and the read to its caller through ``unreadable``:
#   orgs that cannot be listed have no org to name and are reported with an
#   empty one. Nothing of the failure itself is passed on, no message and no
#   path. The caller decides when it is logged. Nothing can be reported per
#   grant for such an org, because its grants are exactly what could not be
#   read.
# - One document that fails to parse never hides the others: that is S3's
#   load_org_service_grants, used as is.
"""Reading this device's service grants out of the org directories."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError

from ..models import StoredServiceGrant
from ..queries import load_org_service_grants

Unreadable = Callable[[str, str], None]
ORGS = "orgs"
GRANTS = "grants"


def own_grants(
    store: OrgFsDirectoryStore,
    orgs: Sequence[str],
    *,
    owner: str,
    unreadable: Unreadable | None = None,
) -> dict[str, tuple[StoredServiceGrant, ...]]:
    """``org`` to this owner's stored grants in it, for the orgs that could be read.

    Each org whose documents could not be read is left out, and is named to
    ``unreadable`` with the grant read when it is given.
    """

    grants: dict[str, tuple[StoredServiceGrant, ...]] = {}
    for org in orgs:
        try:
            grants[org] = load_org_service_grants(store, org, owner=owner)
        except OrgFsError:
            if unreadable is not None:
                unreadable(org, GRANTS)
    return grants


def orgs(store: OrgFsDirectoryStore, *, unreadable: Unreadable | None = None) -> tuple[str, ...]:
    """Every org this device's directory shows, or none when it cannot be listed.

    A listing that could not be made is named to ``unreadable``, with an
    empty org: there is none to name.
    """

    try:
        return tuple(store.orgs())
    except OrgFsError:
        if unreadable is not None:
            unreadable("", ORGS)
        return ()


__all__ = ["GRANTS", "ORGS", "orgs", "own_grants"]
