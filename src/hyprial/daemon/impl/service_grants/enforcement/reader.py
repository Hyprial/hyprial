# Decision notes (service grant exposing side, 2026-11-26):
# - Only this device's own grant documents are read: the owner-scoped directory
#   under directory/service-grants/<owner>/. That is one listing plus one read
#   per document, and it is the same set the grant documents module's grant and
#   revoke write, so a grant starts being pushed as soon as the immediate path
#   runs.
# - An org whose documents cannot be read is absent from the result, so it
#   contributes no key and never widens a port: the safe direction.
# - A read that fails is reported, not only survived (exposing side fix,
#   2026-11-27). It used to be silent, so a port that had just tightened to
#   nobody looked exactly like a port nobody was granted to. The reader names
#   the org and the read to its caller through ``unreadable``: orgs that cannot
#   be listed have no org to name and are reported with an empty one. Nothing
#   of the failure itself is passed on, no message and no path. The caller
#   decides when it is logged. Nothing can be reported per grant for such an
#   org, because its grants are exactly what could not be read.
# - One document that fails to parse never hides the others.
# - Integration fix (2026-11-27): this device's own directory is read with the
#   same bounded scan the recipients use (scan_org_service_grants with its
#   default limits). A directory over a limit is unreadable here too: the org
#   contributes no key, which tightens, and is named with the read
#   GRANTS_LIMITED, so the operator sees the same event and a step that says
#   why. Recipients stop reading such a grantor, so admitting its grants would
#   open ports nobody can be told about.
"""Reading this device's service grants out of the org directories."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError

from ..models import StoredServiceGrant
from ..queries import GrantScanLimited, ScanLimits, scan_org_service_grants

Unreadable = Callable[[str, str], None]
ORGS = "orgs"
GRANTS = "grants"
GRANTS_LIMITED = "grants-limited"


def own_grants(
    store: OrgFsDirectoryStore,
    orgs: Sequence[str],
    *,
    owner: str,
    unreadable: Unreadable | None = None,
) -> dict[str, tuple[StoredServiceGrant, ...]]:
    """``org`` to this owner's stored grants in it, for the orgs that could be read.

    Each org whose documents could not be read is left out, and is named to
    ``unreadable`` with the grant read when it is given: ``GRANTS`` when a
    read failed, ``GRANTS_LIMITED`` when the directory is over a scan limit.
    """

    grants: dict[str, tuple[StoredServiceGrant, ...]] = {}
    for org in orgs:
        try:
            scan = scan_org_service_grants(store, org, ScanLimits(store.orgfs), owner=owner)
        except OrgFsError:
            step: str | None = GRANTS
        except GrantScanLimited:
            step = GRANTS_LIMITED
        else:
            step = GRANTS_LIMITED if scan.limited else None
        if step is None:
            grants[org] = scan.grants
        elif unreadable is not None:
            unreadable(org, step)
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


__all__ = ["GRANTS", "GRANTS_LIMITED", "ORGS", "orgs", "own_grants"]
