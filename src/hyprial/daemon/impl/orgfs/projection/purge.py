"""Frozen S5 purge value objects and canonical plan hashing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, Mapping, Sequence


ORGFS_PURGE_PLAN_TTL_SECONDS = 86_400


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


@dataclass(frozen=True, slots=True)
class PurgeDocument:
    doc_id: str
    start: str
    end: str
    update_blobs: tuple[str, ...]
    log_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "docId": self.doc_id,
            "from": self.start,
            "to": self.end,
            "updateBlobs": list(self.update_blobs),
        }


@dataclass(frozen=True, slots=True)
class PurgeBlob:
    sha: str
    referenced_elsewhere_locally: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "referencedElsewhereLocally": list(self.referenced_elsewhere_locally),
        }


@dataclass(frozen=True, slots=True)
class PurgeSnapshot:
    snapshot_id: str
    doc_id: str

    def to_dict(self) -> dict[str, str]:
        return {"snapshotId": self.snapshot_id, "docId": self.doc_id}


@dataclass(frozen=True, slots=True)
class PurgePlan:
    plan_id: str
    space_id: str
    meta_frontier: str
    docs: tuple[PurgeDocument, ...]
    blobs: tuple[PurgeBlob, ...]
    snapshots: tuple[PurgeSnapshot, ...]
    writers_affected: tuple[str, ...]
    authors_before: Mapping[str, int]
    expires_at: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "spaceId": self.space_id,
            "metaFrontier": self.meta_frontier,
            "docs": [item.to_dict() for item in self.docs],
            "blobs": [item.to_dict() for item in self.blobs],
            "snapshots": [item.to_dict() for item in self.snapshots],
            "writersAffected": list(self.writers_affected),
            "authorsBefore": dict(sorted(self.authors_before.items())),
            "expiresAt": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"planId": self.plan_id, **self.unsigned_dict()}

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def storage_dict(self) -> dict[str, Any]:
        """Return the public plan plus owner-local execution details."""

        return {
            **self.to_dict(),
            "_logKeys": {item.doc_id: list(item.log_keys) for item in self.docs},
        }

    @classmethod
    def from_storage_dict(cls, value: Mapping[str, Any]) -> "PurgePlan":
        """Restore an owner-local plan without changing its frozen wire shape."""

        log_keys = value.get("_logKeys")
        log_keys = log_keys if isinstance(log_keys, Mapping) else {}
        documents: list[PurgeDocument] = []
        for raw in value.get("docs", ()):
            if not isinstance(raw, Mapping):
                raise ValueError("invalid persisted purge document")
            doc_id = str(raw["docId"])
            documents.append(
                PurgeDocument(
                    doc_id,
                    str(raw["from"]),
                    str(raw["to"]),
                    tuple(str(item) for item in raw.get("updateBlobs", ())),
                    tuple(str(item) for item in log_keys.get(doc_id, ())),
                )
            )
        blobs = tuple(
            PurgeBlob(
                str(raw["sha"]),
                tuple(str(item) for item in raw.get("referencedElsewhereLocally", ())),
            )
            for raw in value.get("blobs", ())
            if isinstance(raw, Mapping)
        )
        snapshots = tuple(
            PurgeSnapshot(str(raw["snapshotId"]), str(raw["docId"]))
            for raw in value.get("snapshots", ())
            if isinstance(raw, Mapping)
        )
        plan = cls(
            str(value["planId"]),
            str(value["spaceId"]),
            str(value["metaFrontier"]),
            tuple(documents),
            blobs,
            snapshots,
            tuple(str(item) for item in value.get("writersAffected", ())),
            {
                str(author): int(count)
                for author, count in dict(value.get("authorsBefore", {})).items()
            },
            str(value["expiresAt"]),
        )
        if (
            hashlib.sha256(canonical_bytes(plan.unsigned_dict())).hexdigest()
            != plan.plan_id
        ):
            raise ValueError("persisted purge plan hash mismatch")
        return plan

    @classmethod
    def create(
        cls,
        *,
        space_id: str,
        meta_frontier: str,
        docs: Sequence[PurgeDocument],
        blobs: Sequence[PurgeBlob],
        snapshots: Sequence[PurgeSnapshot],
        writers_affected: Sequence[str],
        authors_before: Mapping[str, int],
        expires_at: str | None = None,
    ) -> "PurgePlan":
        expiry = expires_at or iso(
            utc_now() + timedelta(seconds=ORGFS_PURGE_PLAN_TTL_SECONDS)
        )
        provisional = cls(
            "",
            space_id,
            meta_frontier,
            tuple(docs),
            tuple(blobs),
            tuple(snapshots),
            tuple(writers_affected),
            dict(authors_before),
            expiry,
        )
        plan_id = hashlib.sha256(
            canonical_bytes(provisional.unsigned_dict())
        ).hexdigest()
        return cls(
            plan_id,
            provisional.space_id,
            provisional.meta_frontier,
            provisional.docs,
            provisional.blobs,
            provisional.snapshots,
            provisional.writers_affected,
            provisional.authors_before,
            provisional.expires_at,
        )


@dataclass(frozen=True, slots=True)
class PurgeResult:
    plan_id: str
    executed_locally: bool
    acknowledged: tuple[str, ...]
    pending: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "executedLocally": self.executed_locally,
            "acknowledged": list(self.acknowledged),
            "pending": list(self.pending),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True, slots=True)
class PurgeStatus:
    plan_id: str
    acknowledged: tuple[str, ...]
    pending: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "acknowledged": list(self.acknowledged),
            "pending": list(self.pending),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


__all__ = [
    "ORGFS_PURGE_PLAN_TTL_SECONDS",
    "PurgeBlob",
    "PurgeDocument",
    "PurgePlan",
    "PurgeResult",
    "PurgeSnapshot",
    "PurgeStatus",
]
