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
# - S5 fix (2026-11-27, critic 3, as revised by the coordinator the same day):
#   the time budget is no longer read only between orgs. The deadline goes
#   into the bounded scan, which leaves unread any grantor directory with
#   more than MAX_GRANTOR_GRANT_DOCUMENTS documents or GRANTOR_READ_BYTES_LIMIT
#   listed bytes, and stops the org when it holds more than MAX_ORG_GRANTORS
#   grantor directories. A limited grantor is "unknown": another grantor's
#   effective grant still answers yes, but without one the answer is "do not
#   start". Too many grantors or a spent deadline is "unknown" for the whole
#   question. Each limit is said once per call with the fixed event
#   service.grant.scan_limited (org, phase "startup", reason, and grantor
#   when one grantor was limited), nothing else.
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
    GRANTOR_READ_BYTES_LIMIT,
    MAX_GRANTOR_GRANT_DOCUMENTS,
    MAX_ORG_GRANTORS,
    GrantScanLimited,
    ScanLimits,
    effective_grants_for_recipient,
    scan_org_service_grants,
)
from hyprial.identity import read_device_record

STARTUP_BUDGET_SECONDS = 0.2
SCAN_LIMITED_EVENT = "service.grant.scan_limited"


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
    document read; one call into the directory is not interrupted. A grantor
    over a limit does not hide another grantor's grant.
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
            max_documents=MAX_GRANTOR_GRANT_DOCUMENTS,
            max_bytes=GRANTOR_READ_BYTES_LIMIT,
            max_grantors=MAX_ORG_GRANTORS,
            clock=clock,
        )
        for org in store.orgs():
            try:
                limits.check_time()
                if org in left or not store.is_member(org, f"user:{owner}"):
                    continue
                scan = scan_org_service_grants(store, org, limits)
            except GrantScanLimited as limited:
                _limited(logger, org, limited.reason)
                return False
            for grantor, reason in sorted(scan.limited.items()):
                _limited(logger, org, reason, grantor)
            if effective_grants_for_recipient(scan.grants, owner=owner, device_id=device_id, now_ms=now):
                return True
        return False
    except Exception:
        return False


def _limited(logger: Callable[..., None] | None, org: str, reason: str, grantor: str | None = None) -> None:
    if logger is None:
        return
    fields = {"org": org, "phase": "startup", "reason": reason}
    if grantor is not None:
        fields["grantor"] = grantor
    try:
        logger("warn", "service", SCAN_LIMITED_EVENT, **fields)
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
