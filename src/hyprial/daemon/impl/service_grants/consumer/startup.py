# Decision notes (service grant S5, 2026-11-26, coordinator ruling a):
# - The daemon must still start service-connect for a recipient that has
#   neither a protected record nor persisted connections, when this device
#   holds an effective grant in any org it belongs to. That question is asked
#   on the startup path, so has_effective_grant is built to be cheap and to
#   get out of the way: it reads grants only (no device rows, no keys), stops
#   at the first org that answers yes, gives up after a short time budget, and
#   answers "no" on any failure. A wrong "no" only means the worker starts at
#   the next service request instead of at startup.
# - "Effective and addressed to this device" is S3's own rule
#   (effective_grants_for_recipient) and nothing is re-implemented here. An
#   org this home left, or that no longer counts this owner as a member, is
#   skipped: "@members" is a membership claim, so it is checked the same way
#   the periodic consumer checks it.
# - Nothing here writes, opens a credential, or creates a key store.
"""The startup question: does any org hold an effective grant for this device?"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.state import read_left_orgs
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.service_grants.models import normalize_bare_owner
from hyprial.daemon.impl.service_grants.queries import (
    effective_grants_for_recipient,
    load_org_service_grants,
)
from hyprial.identity import read_device_record

STARTUP_BUDGET_SECONDS = 0.2


def has_effective_grant(
    *,
    orgfs: OrgFs | None,
    home: Path,
    owner: str | None,
    device_id: str | None,
    now_ms: Callable[[], int] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> bool:
    """Whether any org this device belongs to holds an effective grant addressed to it.

    Cheap by construction, ``False`` on any failure or once the time budget is
    spent, and never raises. The budget is checked between orgs: one call into
    the directory is bounded by the directory, not by this budget.
    """

    try:
        owner = normalize_bare_owner(owner)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    if orgfs is None or not device_id:
        return False
    clock = monotonic or time.monotonic
    deadline = clock() + STARTUP_BUDGET_SECONDS
    try:
        store = OrgFsDirectoryStore(orgfs)
        left = set(read_left_orgs(Path(home)))
        now = (now_ms or (lambda: time.time_ns() // 1_000_000))()
        for org in store.orgs():
            if clock() > deadline:
                return False
            if org in left or not store.is_member(org, f"user:{owner}"):
                continue
            grants = load_org_service_grants(store, org)
            if effective_grants_for_recipient(grants, owner=owner, device_id=device_id, now_ms=now):
                return True
        return False
    except Exception:
        return False


def device_id_of(home: Path) -> str | None:
    """This home's device id, or ``None`` when there is no readable device record."""

    try:
        record = read_device_record(Path(home))
    except Exception:
        return None
    return None if record is None else record.device_id


__all__ = ["STARTUP_BUDGET_SECONDS", "device_id_of", "has_effective_grant"]
