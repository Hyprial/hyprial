# Decision notes (service grant S3, 2026-11-26):
# - Effectiveness has exactly one implementation (is_grant_effective) shared by
#   service.grants, S4-py admission projection, and S5 recipient consumption.
# - Security invariants for S5 consumers are enforced and documented here:
#   grantor is taken solely from the OrgFS path (never from document body or
#   envelope header), and credential opening requires an exact (owner, deviceId)
#   recipient match on an effective grant (never "@members").
# - S3 fix (2026-11-26, critic B1 and S1): a document that fails to parse or
#   validate for any reason, RecursionError included, is skipped alone. A revoked
#   tombstone may have null service or recipient; it is never effective, so the
#   effective-only helpers never see those nulls.
# - S5 fix (2026-11-27, critic 3): the loader takes optional ScanLimits. Without
#   them nothing changes (S4-py's call and every existing caller). With them one
#   recursive listing of the org's grants tree gives the document count and the
#   sizes before anything is read; more than max_documents documents, or more
#   than max_bytes of listed size in total, raises GrantScanLimited before the
#   first read. Documents are then read one at a time through the store's own
#   verified reader, and the deadline is checked before the listing, after it
#   and before every read. One OrgFS call is not interrupted: the deadline
#   bounds the scan, not a single call. "Limited" is never "no grant": callers
#   decide what an unknown answer means.
"""Pure service-grant query and OrgFS loading helpers shared with S4-py and S5."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import org_space_name

from .models import (
    ALL_MEMBERS,
    GrantRecipient,
    ServiceGrant,
    StoredServiceGrant,
    normalize_bare_owner,
)


MAX_ORG_GRANT_DOCUMENTS = 1024
SCAN_READ_BYTES_LIMIT = 2 * 1024 * 1024
SERVICE_GRANTS_DIR = "directory/service-grants"
SCAN_DOCUMENTS = "documents"
SCAN_BYTES = "bytes"
SCAN_DEADLINE = "deadline"


class GrantScanLimited(Exception):
    """A bounded load stopped; ``reason`` is ``documents``, ``bytes`` or ``deadline``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """The bounds of one org's grant load; ``deadline`` is a reading of ``clock``."""

    orgfs: OrgFs
    deadline: float | None = None
    max_documents: int = MAX_ORG_GRANT_DOCUMENTS
    max_bytes: int = SCAN_READ_BYTES_LIMIT
    clock: Callable[[], float] = field(default=time.monotonic)

    def check_time(self) -> None:
        if self.deadline is not None and self.clock() > self.deadline:
            raise GrantScanLimited(SCAN_DEADLINE)


def is_grant_effective(grant: ServiceGrant, *, now_ms: int) -> bool:
    """Return whether ``grant`` is neither revoked nor expired at ``now_ms``.

    This is the sole effectiveness check across S3 ``service.grants``,
    S4-py exposure admission projection, and S5 recipient consumption.
    A grant is expired once ``expires_at_ms <= now_ms``.
    """

    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if grant.revoked_at_ms is not None:
        return False
    if grant.expires_at_ms is not None and grant.expires_at_ms <= now_ms:
        return False
    return True


def effective_grants_for_service(
    grants: Iterable[StoredServiceGrant],
    *,
    owner: str,
    device_id: str,
    port: int,
    now_ms: int,
) -> tuple[StoredServiceGrant, ...]:
    """Return effective grants authored by ``owner`` for ``(device_id, port)``.

    ``owner`` may be bare or ``user:<owner>`` and is matched against each
    grant's OrgFS path-derived ``grantor``. Intended for S4-py admission
    projection so grantor-side policy never parses raw OrgFS JSON again.
    """

    bare_owner = normalize_bare_owner(owner)
    return tuple(
        item
        for item in grants
        if item.grantor == bare_owner
        and is_grant_effective(item.grant, now_ms=now_ms)
        and item.grant.service is not None
        and item.grant.service.device_id == device_id
        and item.grant.service.port == port
    )


def recipient_matches(
    grant: ServiceGrant, *, bare_owner: str, device_id: str
) -> bool:
    """Return whether ``grant`` targets ``(bare_owner, device_id)`` or ``@members``."""

    if grant.recipient == ALL_MEMBERS:
        return True
    return (
        isinstance(grant.recipient, GrantRecipient)
        and grant.recipient.owner == bare_owner
        and grant.recipient.device_id == device_id
    )


def effective_grants_for_recipient(
    grants: Iterable[StoredServiceGrant],
    *,
    owner: str,
    device_id: str,
    now_ms: int,
) -> tuple[StoredServiceGrant, ...]:
    """Return effective grants whose recipient is ``(owner, device_id)`` or ``@members``.

    Each returned :class:`StoredServiceGrant` carries ``grantor`` taken strictly
    from the OrgFS document path (``directory/service-grants/<grantor>/<grantId>.json``).

    Recipient security invariants (for S5 consumers):
    - Always trust ``stored.grantor`` (from the OrgFS path) as the grantor and
      never read grantor identity from the document body or sealed envelope header.
    - Before surfacing a service endpoint, verify that ``grant.service.device_id``
      exists under ``directory/devices/<stored.grantor>/``.
    - Never attempt to open ``grant.credential`` unless
      :func:`can_open_grant_credential` returns ``True`` (exact ``(owner, device_id)``
      match; ``@members`` grants never carry credentials).
    """

    bare_owner = normalize_bare_owner(owner)
    return tuple(
        item
        for item in grants
        if recipient_matches(item.grant, bare_owner=bare_owner, device_id=device_id)
        and is_grant_effective(item.grant, now_ms=now_ms)
    )


def can_open_grant_credential(
    stored: StoredServiceGrant,
    *,
    recipient_owner: str,
    recipient_device_id: str,
    now_ms: int,
) -> bool:
    """Return whether this device may pass ``stored.grant.credential`` to ``open_service_grant``.

    Recipient security invariants:
    - The grant must be effective (not revoked and not expired at ``now_ms``).
    - ``stored.grant.credential`` must be present (non-null).
    - ``stored.grant.recipient`` must be a structured :class:`GrantRecipient`
      matching ``(bare recipient_owner, recipient_device_id)`` exactly;
      ``@members`` grants never qualify.
    - Callers must supply ``grantor=stored.grantor`` (from the OrgFS path) to
      ``EncryptionKey.Store.open_service_grant(...)``.
    """

    if stored.grant.credential is None:
        return False
    if not is_grant_effective(stored.grant, now_ms=now_ms):
        return False
    recipient = stored.grant.recipient
    if not isinstance(recipient, GrantRecipient):
        return False
    bare_owner = normalize_bare_owner(recipient_owner)
    return recipient.owner == bare_owner and recipient.device_id == recipient_device_id


def load_org_service_grants(
    store: OrgFsDirectoryStore,
    org: str,
    *,
    owner: str | None = None,
    limits: ScanLimits | None = None,
) -> tuple[StoredServiceGrant, ...]:
    """Load and validate all verified service grants from OrgFS for ``org``.

    Invalid documents or documents whose ``grantId`` disagrees with the filename
    are skipped rather than aborting observation of valid grants; any parse or
    validation failure, ``RecursionError`` included, skips only that document.
    With ``limits`` the load is bounded and raises :class:`GrantScanLimited`
    instead of exceeding them; without, it behaves exactly as before.
    """

    if limits is None:
        records = store.list_service_grants(org, owner=owner)
    else:
        records = _bounded_records(store, org, owner, limits)
    loaded: list[StoredServiceGrant] = []
    for bare_owner, filename_grant_id, record in records:
        try:
            grant = ServiceGrant.from_record(record)
            if grant.grant_id != filename_grant_id:
                continue
            loaded.append(
                StoredServiceGrant(org=org, grantor=bare_owner, grant=grant)
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
    return tuple(loaded)


def _bounded_records(
    store: OrgFsDirectoryStore, org: str, owner: str | None, limits: ScanLimits
) -> list[tuple[str, str, dict]]:
    limits.check_time()
    targets = _listed_documents(limits.orgfs, org, None if owner is None else normalize_bare_owner(owner))
    limits.check_time()
    if len(targets) > limits.max_documents:
        raise GrantScanLimited(SCAN_DOCUMENTS)
    if sum(size for _owner, _grant_id, size in targets) > limits.max_bytes:
        raise GrantScanLimited(SCAN_BYTES)
    records: list[tuple[str, str, dict]] = []
    for bare, grant_id, _size in targets:
        limits.check_time()
        try:
            record = store.read_service_grant(org, bare, grant_id)
        except ValueError:
            continue
        if record is not None:
            records.append((bare, grant_id, record))
    return records


def _listed_documents(fs: OrgFs, org: str, owner: str | None) -> list[tuple[str, str, int]]:
    """``(owner, grant id, listed size)`` of every grant document, from one listing."""

    name = org_space_name(org)
    matches = [space for space in fs.spaces() if space.name == name]
    if len(matches) != 1:
        raise OrgFsError("unknown-space", {"org": org, "space": name})
    try:
        nodes = fs.listdir(matches[0].space_id, SERVICE_GRANTS_DIR, recursive=True)
    except OrgFsError as error:
        if error.code == "unknown-doc":
            return []
        raise
    prefix = SERVICE_GRANTS_DIR + "/"
    targets: list[tuple[str, str, int]] = []
    for node in nodes:
        parts = node.path.lstrip("/").removeprefix(prefix).split("/")
        if node.kind != "doc" or node.deleted or len(parts) != 2 or not parts[1].endswith(".json"):
            continue
        try:
            if normalize_bare_owner(parts[0]) != parts[0]:
                continue
        except ValueError:
            continue
        if owner is None or parts[0] == owner:
            targets.append((parts[0], parts[1].removesuffix(".json"), node.size or 0))
    return sorted(targets)


__all__ = [
    "MAX_ORG_GRANT_DOCUMENTS",
    "SCAN_BYTES",
    "SCAN_DEADLINE",
    "SCAN_DOCUMENTS",
    "SCAN_READ_BYTES_LIMIT",
    "SERVICE_GRANTS_DIR",
    "GrantScanLimited",
    "ScanLimits",
    "can_open_grant_credential",
    "effective_grants_for_recipient",
    "effective_grants_for_service",
    "is_grant_effective",
    "load_org_service_grants",
    "recipient_matches",
]
