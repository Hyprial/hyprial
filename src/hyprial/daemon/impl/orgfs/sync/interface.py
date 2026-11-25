# Decision record (C3, 2026-11-25, design orgfs-minimal-design-2026-11-25 §4.7):
# - SyncHost, PeerObservation and SpaceSync are copied verbatim from §4.7; they
#   are the only coupling between this package and the facade coordinator.
# - The admission outcomes (Accepted / Pending / Rejected) belong to C2
#   (§3.6, orgfs/membership).  Phase D P0 (plan R16): ``outcome_kind`` checks
#   them with ``isinstance`` instead of reading ``reason`` / ``code``
#   attributes, so an exception or another object returned by mistake is not
#   mistaken for a rejection.  Anything else raises ``TypeError``: the host is
#   in-tree code bound to return one of the three, and ingress already
#   contains a failing frame.
# - SYNC_ENVELOPE_BYTES mirrors the journal's ORGFS_ENVELOPE_BYTES (2 MiB);
#   the journal module is deleted in phase E, so the bound is owned here.
# - The temporary sync blob reference reason is "sync" and lives 10 minutes
#   (§4.3).  SYNC_BLOB_MAX_BYTES bounds what a requester will fetch for one
#   update; 64 MiB is four times the 16 MiB the design names as the largest
#   expected temporary blob (§8, untested assumption).
# - 2026-11-25 §4.7 revision: SyncHost.local_generation() is the coordinator's
#   per-space local commit generation (§4.6); SpaceSync gains request_blob /
#   fetch_blob for binary file content with the journal mesh's semantics (one
#   coalesced job per digest, at most 32 waiting digests, a 10 s query window,
#   absence answers never decide, fetched bytes pinned under "replica"), and
#   the optional announcement_source / holder_discovery callbacks.
# - Design §9.2 (F1, 2026-11-25): SyncHost.requester_removed(author) is true
#   when the author's member row is still present with ``removedAt``; the
#   supplier then answers that requester with the meta document only.
# - Package F6 (real-node finding R3-1): SyncHost.local_member() is true when
#   this node's own author is a current member of the space (a row without
#   ``removedAt``).  While it is false the sync applies the meta document only
#   and asks for no tree, content or blob; a blob supplier applies the pull
#   rule to the requester named in the blob request.
"""Frozen protocols and bounds of the OrgFS protocol 3 sync package."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from threading import Event
from typing import Final, Literal, Protocol

from hyprial.daemon.impl.orgfs.membership.contract import Accepted, Pending, Rejected
from hyprial.daemon.impl.orgfs.storage.blobs import ORGFS_PAGE_BYTES

SYNC_SCHEMA_VERSION: Final[int] = 3
SYNC_PROTOCOL: Final[int] = 3
SYNC_CONTENT_DOCUMENT_LIMIT: Final[int] = 128
SYNC_INGRESS_CAPACITY: Final[int] = 128
SYNC_ENVELOPE_BYTES: Final[int] = 2 * 1024 * 1024
SYNC_MIN_PAGE_BYTES: Final[int] = 64 * 1024
SYNC_BLOB_REASON: Final[str] = "sync"
SYNC_BLOB_TTL_SECONDS: Final[float] = 600.0
SYNC_BLOB_MAX_BYTES: Final[int] = 64 * 1024 * 1024
SYNC_DOC_ID_MAX_LENGTH: Final[int] = 512
SYNC_DEPENDENCY_RETRY_LIMIT: Final[int] = 2
SYNC_BLOB_FETCH_QUEUE_LIMIT: Final[int] = 32
SYNC_BLOB_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
SYNC_FETCHED_BLOB_REASON: Final[str] = "replica"
CATALOG_DOC_IDS: Final[tuple[str, str]] = ("meta", "tree")
EMPTY_VECTOR: Final[bytes] = b"\x00"

SyncLogger = Callable[..., None]
PresenceGate = Callable[[str], bool]
OutcomeKind = Literal["accepted", "pending", "rejected"]
AnnouncementSource = Callable[[], Iterable[Mapping[str, object]]]
HolderDiscovery = set[str] | Callable[[], Iterable[str]]


class SyncHost(Protocol):
    """Implemented by the facade's commit coordinator; SpaceSync calls back."""

    def read_vectors(self, doc_ids: Sequence[str]) -> Mapping[str, bytes]: ...

    def read_update(self, doc_id: str, since: bytes) -> tuple[bytes, bytes] | None: ...

    def pending_content(self, limit: int) -> tuple[str, ...]: ...

    def apply_remote(
        self, doc_id: str, update: bytes, expected_vector: bytes, peer: str
    ) -> object: ...

    def requester_allowed(self, author: str) -> bool: ...

    def requester_removed(self, author: str) -> bool: ...

    def supplier_allowed(self, node: str) -> bool: ...

    def local_member(self) -> bool: ...

    def local_generation(self) -> int: ...


@dataclass(frozen=True, slots=True)
class PeerObservation:
    node: str
    meta_vector: bytes
    tree_vector: bytes
    local_generation: int


class SpaceSync(Protocol):
    def schedule_sync_from(self, peer: str) -> bool: ...

    def announce(self) -> None: ...

    def publish(self, doc_id: str, update: bytes, vector: bytes) -> None: ...

    def request_content(self, doc_id: str) -> None: ...

    def request_blob(self, digest: str) -> Event: ...

    def fetch_blob(self, holder: str, digest: str, *, size_hint: int | None = None) -> bytes: ...

    def maintain(self, now: float) -> None: ...

    def observations(self) -> tuple[PeerObservation, ...]: ...

    def close(self, timeout: float = 5.0) -> None: ...


class SyncError(Exception):
    """A failed sync round, shaped like the journal mesh's StoreError."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, object] = dict(details)


def outcome_kind(outcome: object) -> OutcomeKind:
    """Classify a C2 admission outcome; anything else raises ``TypeError``."""

    if isinstance(outcome, Accepted):
        return "accepted"
    if isinstance(outcome, Pending):
        return "pending"
    if isinstance(outcome, Rejected):
        return "rejected"
    raise TypeError(f"not an admission outcome: {type(outcome).__name__}")


__all__ = [
    "AnnouncementSource",
    "HolderDiscovery",
    "SYNC_BLOB_FETCH_QUEUE_LIMIT",
    "SYNC_BLOB_FETCH_TIMEOUT_SECONDS",
    "SYNC_FETCHED_BLOB_REASON",
    "CATALOG_DOC_IDS",
    "EMPTY_VECTOR",
    "ORGFS_PAGE_BYTES",
    "PeerObservation",
    "PresenceGate",
    "SYNC_BLOB_MAX_BYTES",
    "SYNC_BLOB_REASON",
    "SYNC_BLOB_TTL_SECONDS",
    "SYNC_CONTENT_DOCUMENT_LIMIT",
    "SYNC_DEPENDENCY_RETRY_LIMIT",
    "SYNC_DOC_ID_MAX_LENGTH",
    "SYNC_ENVELOPE_BYTES",
    "SYNC_INGRESS_CAPACITY",
    "SYNC_MIN_PAGE_BYTES",
    "SYNC_PROTOCOL",
    "SYNC_SCHEMA_VERSION",
    "SpaceSync",
    "SyncError",
    "SyncHost",
    "SyncLogger",
    "outcome_kind",
]
