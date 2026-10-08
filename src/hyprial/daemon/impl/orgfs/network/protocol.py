"""Orgfs replication over the payload-bearing transport seam.

The journal remains authoritative: this module only publishes and replays the
exact envelope bytes owned by :mod:`hyprial.orgfs.store`.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import json
import time
from typing import TYPE_CHECKING, Any, Final

from hyprial.identity import parse_protected_directory_doc_id

from hyprial.daemon.impl.orgfs.storage.store  import (
    StoreError,
    _writer_is_safe)

if TYPE_CHECKING:
    pass


ORGFS_LOG_RANGE_LIMIT: Final[int] = 256
ORGFS_WIRE_VERSION: Final[int] = 1
ORGFS_BLOB_FETCH_QUEUE_LIMIT: Final[int] = 32
ORGFS_ANNOUNCE_BUFFER_LIMIT: Final[int] = 256
ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT: Final[int] = 256
ORGFS_SYNC_QUEUE_LIMIT: Final[int] = 32
ORGFS_SYNC_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.25,)
# Registered cadence for member-side directory and ACL anti-entropy. The daemon's
# existing maintenance scheduler supplies ticks; OrgFS never owns a polling loop.
ORGFS_DIRECTORY_SYNC_INTERVAL_SECONDS: Final[float] = 60.0
# Each node spreads its anti-entropy deadline by up to ±20% so members that started
# together do not pull in lockstep.
ORGFS_DIRECTORY_SYNC_JITTER_RATIO: Final[float] = 0.2
# Projection repair shares the registered scheduled-sync retry budget: one
# initial projection plus one attempt for every admitted sync retry delay.
ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET: Final[int] = (
    1 + len(ORGFS_SYNC_RETRY_BACKOFF_SECONDS)
)
ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS: Final[float] = 2.0
ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS: Final[float] = 4.5
ORGFS_REPLICA_SYNC_SCAN_ROWS: Final[int] = 256
ORGFS_LOG_INGRESS_CAPACITY: Final[int] = 128
_READ_DIRECT = object()
# A blob query is broadcast on a key with no node segment, so every mesh
# answers and the query collects replies until this window closes.  A holder
# relayed over DERP can need several seconds for one full chunk; a window
# shorter than that drops its chunk and leaves only non-holders' answers.
ORGFS_BLOB_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
# Answers that only mean "not here": they never decide a fetch while a
# holder may still be answering.
_BLOB_ABSENCE_CODES: Final[frozenset[str]] = frozenset(
    {"unknown-blob", "unknown-space"}
)
MeshLogger = Callable[..., None]
SupplierGate = Callable[[str], bool]
AppliedHook = Callable[[bytes], Iterable[str] | None]
ReplacementHook = Callable[[str, str, bytes], None]
AnnouncementSource = Callable[[], Iterable[Mapping[str, Any]]]
RecoveryCandidates = Callable[[], Iterable[str]]
@dataclass(frozen=True, slots=True)
class _LogIngress:
    operation_id: str
    generation: int
    envelope: bytes


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _error(code: str, message: str, **details: object) -> bytes:
    return _json_bytes(
        {
            "schemaVersion": 1,
            "type": "orgfs-error",
            "code": code,
            "message": message,
            "details": details,
        }
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _reply_origin(error: Mapping[str, Any]) -> dict[str, str]:
    details = error.get("details")
    details = details if isinstance(details, Mapping) else {}
    return {
        "nodeId": str(details.get("nodeId", "")),
        "spaceId": str(details.get("spaceId", "")),
        "code": str(error.get("code", "")),
    }


def _decode_request(payload: bytes | None) -> dict[str, Any]:
    if payload is None:
        raise StoreError("invalid-argument", "orgfs query requires a JSON payload")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(
            "invalid-argument", "orgfs query payload is invalid JSON"
        ) from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise StoreError("invalid-argument", "unsupported orgfs query schema")
    return value


def _decode_update_header(envelope: bytes, *, space_id: str) -> dict[str, Any]:
    """Validate the transport-visible update schema before supplier admission."""

    try:
        value = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("invalid-argument", "orgfs update is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != ORGFS_WIRE_VERSION
        or value.get("type") != "orgfs-update"
        or value.get("spaceId") != space_id
    ):
        raise StoreError("invalid-argument", "invalid orgfs-update envelope")
    if not isinstance(value.get("docId"), str) or not value["docId"]:
        raise StoreError("invalid-argument", "orgfs update requires docId")
    try:
        parse_protected_directory_doc_id(value["docId"])
    except ValueError as exc:
        raise StoreError(
            "invalid-argument", "orgfs update has an invalid protected docId"
        ) from exc
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise StoreError("invalid-argument", "orgfs update has an invalid sequence")
    origin = value.get("origin")
    required_origin = (
        "writer",
        "node",
        "author",
        "actor",
        "committedAt",
        "metaFrontier",
    )
    if not isinstance(origin, dict) or any(
        field not in origin for field in required_origin
    ):
        raise StoreError("invalid-argument", "orgfs update has an incomplete origin")
    if not isinstance(origin.get("node"), str) or not origin["node"]:
        raise StoreError("invalid-argument", "orgfs update origin requires a node")
    if not isinstance(origin.get("writer"), str) or not _writer_is_safe(
        origin["writer"]
    ):
        raise StoreError("invalid-argument", "orgfs update has an unsafe writer")
    if ("update" in value) == ("updateBlob" in value):
        raise StoreError(
            "invalid-argument", "orgfs update requires exactly one update body"
        )
    return value


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise StoreError("invalid-argument", "version vector must be base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise StoreError(
            "invalid-argument", "version vector is not valid base64"
        ) from exc
