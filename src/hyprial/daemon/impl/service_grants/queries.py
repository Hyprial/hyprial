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
# - S5 fix (2026-11-27, critic 3, as revised by the coordinator the same day):
#   load_org_service_grants is unchanged (S4-py's call and every existing
#   caller). The bounded path is a separate function, scan_org_service_grants,
#   so no caller can get a bounded answer without seeing what it left out. One
#   recursive listing of the org's grants tree gives every document and its
#   listed size before anything is read. The document and byte limits apply to
#   each grantor directory on its own: a grantor over either one is left
#   unread and named in GrantScan.limited, and every other grantor is read, so
#   one member's junk only makes that member's grants unknown. More grantor
#   directories than max_grantors, or a spent deadline, still stops the whole
#   org with GrantScanLimited. Documents are read one at a time through the
#   store's own verified reader, and the deadline is checked before the
#   listing, after it and before every read. One OrgFS call is not
#   interrupted: the deadline bounds the scan, not a single call. "Limited" is
#   never "no grant": callers decide what an unknown answer means.
"""Pure service-grant query and OrgFS loading helpers shared with S4-py and S5."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from hyprial.daemon.impl.org.network.directory import SERVICE_GRANTS_DIR, OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import org_space_name

from .models import (
    ALL_MEMBERS,
    GrantRecipient,
    ServiceGrant,
    StoredServiceGrant,
    normalize_bare_owner,
)


MAX_GRANTOR_GRANT_DOCUMENTS = 256
GRANTOR_READ_BYTES_LIMIT = 512 * 1024
MAX_ORG_GRANTORS = 1024
SCAN_DOCUMENTS = "documents"
SCAN_BYTES = "bytes"
SCAN_GRANTORS = "grantors"
SCAN_DEADLINE = "deadline"


class GrantScanLimited(Exception):
    """A bounded scan stopped for the whole org; ``reason`` is ``grantors`` or ``deadline``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """The bounds of one org's grant scan; ``deadline`` is a reading of ``clock``.

    ``max_documents`` and ``max_bytes`` bound each grantor directory;
    ``max_grantors`` bounds how many grantor directories the org may hold.
    """

    orgfs: OrgFs
    deadline: float | None = None
    max_documents: int = MAX_GRANTOR_GRANT_DOCUMENTS
    max_bytes: int = GRANTOR_READ_BYTES_LIMIT
    max_grantors: int = MAX_ORG_GRANTORS
    clock: Callable[[], float] = field(default=time.monotonic)

    def check_time(self) -> None:
        if self.deadline is not None and self.clock() > self.deadline:
            raise GrantScanLimited(SCAN_DEADLINE)


@dataclass(frozen=True, slots=True)
class GrantScan:
    """What a bounded scan read, and which grantors it left unread (grantor to reason)."""

    grants: tuple[StoredServiceGrant, ...] = ()
    limited: dict[str, str] = field(default_factory=dict)


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
) -> tuple[StoredServiceGrant, ...]:
    """Load and validate all verified service grants from OrgFS for ``org``.

    Invalid documents or documents whose ``grantId`` disagrees with the filename
    are skipped rather than aborting observation of valid grants; any parse or
    validation failure, ``RecursionError`` included, skips only that document.
    """

    return _parsed(org, store.list_service_grants(org, owner=owner))


def scan_org_service_grants(
    store: OrgFsDirectoryStore,
    org: str,
    limits: ScanLimits,
    *,
    owner: str | None = None,
) -> GrantScan:
    """Load ``org``'s grants as :func:`load_org_service_grants` does, within ``limits``.

    A grantor directory over the document or byte limit is not read and is
    named in :attr:`GrantScan.limited`; every other grantor is read. Too many
    grantor directories or a spent deadline raises :class:`GrantScanLimited`.
    """

    limits.check_time()
    targets = _listed_documents(limits.orgfs, org, None if owner is None else normalize_bare_owner(owner))
    limits.check_time()
    by_grantor: dict[str, list[tuple[str, int]]] = {}
    for grantor, grant_id, size in targets:
        by_grantor.setdefault(grantor, []).append((grant_id, size))
    if len(by_grantor) > limits.max_grantors:
        raise GrantScanLimited(SCAN_GRANTORS)
    limited: dict[str, str] = {}
    records: list[tuple[str, str, dict]] = []
    for grantor, documents in sorted(by_grantor.items()):
        if len(documents) > limits.max_documents:
            limited[grantor] = SCAN_DOCUMENTS
            continue
        if sum(size for _grant_id, size in documents) > limits.max_bytes:
            limited[grantor] = SCAN_BYTES
            continue
        for grant_id, _size in documents:
            limits.check_time()
            try:
                record = store.read_service_grant(org, grantor, grant_id)
            except ValueError:
                continue
            if record is not None:
                records.append((grantor, grant_id, record))
    return GrantScan(_parsed(org, records), limited)


def _parsed(org: str, records: Iterable[tuple[str, str, dict]]) -> tuple[StoredServiceGrant, ...]:
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


def _listed_documents(fs: OrgFs, org: str, owner: str | None) -> list[tuple[str, str, int]]:
    """``(owner, grant id, listed size)`` of every grant document, from one listing."""

    name = org_space_name(org)
    matches = [space for space in fs.spaces() if space.name == name]
    if len(matches) > 1:
        raise OrgFsError("ambiguous-space", {"org": org, "space": name, "count": len(matches)})
    if not matches:
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
    "GRANTOR_READ_BYTES_LIMIT",
    "MAX_GRANTOR_GRANT_DOCUMENTS",
    "MAX_ORG_GRANTORS",
    "SCAN_BYTES",
    "SCAN_DEADLINE",
    "SCAN_DOCUMENTS",
    "SCAN_GRANTORS",
    "SERVICE_GRANTS_DIR",
    "GrantScan",
    "GrantScanLimited",
    "ScanLimits",
    "can_open_grant_credential",
    "effective_grants_for_recipient",
    "effective_grants_for_service",
    "is_grant_effective",
    "load_org_service_grants",
    "recipient_matches",
    "scan_org_service_grants",
]
