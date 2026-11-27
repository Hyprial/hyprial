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
# - S5 fix (2026-11-27, critic 3): the time budget is no longer read only
#   between orgs. The deadline goes into the loader, which also refuses an
#   org with more than MAX_ORG_GRANT_DOCUMENTS documents or more than
#   SCAN_READ_BYTES_LIMIT listed bytes before reading any of them. A limited
#   or late scan is "unknown", so the answer is "do not start", and it is
#   said once per call with the fixed event service.grant.scan-limited
#   (org, phase "startup", reason), nothing else.
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
    MAX_ORG_GRANT_DOCUMENTS,
    SCAN_READ_BYTES_LIMIT,
    GrantScanLimited,
    ScanLimits,
    effective_grants_for_recipient,
    load_org_service_grants,
)
from hyprial.identity import read_device_record

STARTUP_BUDGET_SECONDS = 0.2
SCAN_LIMITED_EVENT = "service.grant.scan-limited"


def has_effective_grant(
    *,
    orgfs: OrgFs | None,
    home: Path,
    owner: str | None,
    device_id: str | None,
    now_ms: Callable[[], int] | None = None,
    monotonic: Callable[[], float] | None = None,
    logger: Callable[..., None] | None = None,
) -> bool:
    """Whether any org this device belongs to holds an effective grant addressed to it.

    Cheap by construction, ``False`` on any failure, over a scan limit or once
    the time budget is spent, and never raises. The budget is checked between
    orgs, before and after the listing of one org's grants and before every
    document read; one call into the directory is not interrupted.
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
        limits = ScanLimits(
            orgfs,
            deadline=deadline,
            max_documents=MAX_ORG_GRANT_DOCUMENTS,
            max_bytes=SCAN_READ_BYTES_LIMIT,
            clock=clock,
        )
        for org in store.orgs():
            try:
                limits.check_time()
                if org in left or not store.is_member(org, f"user:{owner}"):
                    continue
                grants = load_org_service_grants(store, org, limits=limits)
            except GrantScanLimited as limited:
                _limited(logger, org, limited.reason)
                return False
            if effective_grants_for_recipient(grants, owner=owner, device_id=device_id, now_ms=now):
                return True
        return False
    except Exception:
        return False


def _limited(logger: Callable[..., None] | None, org: str, reason: str) -> None:
    if logger is None:
        return
    try:
        logger("warn", "service", SCAN_LIMITED_EVENT, org=org, phase="startup", reason=reason)
    except Exception:
        pass


def device_id_of(home: Path) -> str | None:
    """This home's device id, or ``None`` when there is no readable device record."""

    try:
        record = read_device_record(Path(home))
    except Exception:
        return None
    return None if record is None else record.device_id


__all__ = ["SCAN_LIMITED_EVENT", "STARTUP_BUDGET_SECONDS", "device_id_of", "has_effective_grant"]
