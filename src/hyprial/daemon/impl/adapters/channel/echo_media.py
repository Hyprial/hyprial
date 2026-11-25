"""Per-operation echo evidence and bounded attachment hydration."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable


class AttachmentDenied(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenEchoDecision:
    operation: str
    strategy: str
    capability_revision: str
    evidence_revision: str


@dataclass(slots=True)
class _EchoEntry:
    capability_revision: str
    strategy: str
    evidence_revision: str
    expires_at_ms: int
    revoked: bool = False


class EchoAuthority:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, str], _EchoEntry] = {}

    def install(
        self,
        *,
        instance_id: str,
        generation: int,
        capability_revision: str,
        operation: str,
        strategy: str,
        evidence_revision: str,
        expires_at_ms: int,
    ) -> None:
        self._entries[(instance_id, generation, operation)] = _EchoEntry(
            capability_revision, strategy, evidence_revision, expires_at_ms
        )

    def revoke(self, instance_id: str, generation: int, operation: str) -> None:
        entry = self._entries.get((instance_id, generation, operation))
        if entry is not None:
            entry.revoked = True

    def freeze(
        self, instance_id: str, generation: int, operation: str, *, now_ms: int
    ) -> FrozenEchoDecision:
        entry = self._entries.get((instance_id, generation, operation))
        if entry is None or entry.revoked or now_ms >= entry.expires_at_ms:
            return FrozenEchoDecision(
                operation,
                "ambiguous-self-quarantine",
                "none:v1",
                "none:v1",
            )
        return FrozenEchoDecision(
            operation,
            entry.strategy,
            entry.capability_revision,
            entry.evidence_revision,
        )


@dataclass(slots=True)
class _Attachment:
    body: bytes
    principal: str
    purpose: str
    target_ref_id: str | None
    expires_at_ms: int
    revoked: bool = False


class BoundedByteStream:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0
        self._closed = False
        self.name: str | None = None
        self.media_type: str | None = None
        self.size = len(body)
        self.sha256 = hashlib.sha256(body).hexdigest()

    async def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise ValueError("ATTACHMENT_STREAM_CLOSED")
        if max_bytes < 1 or self._offset >= len(self._body):
            return b""
        end = min(len(self._body), self._offset + max_bytes)
        chunk = bytes(self._body[self._offset : end])
        self._offset = end
        return chunk

    async def aclose(self) -> None:
        self._closed = True


class BoundedAttachmentAuthority:
    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._attachments: dict[str, _Attachment] = {}
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def register(
        self,
        *,
        handle_id: str,
        body: bytes,
        principal: str,
        purpose: str,
        target_ref_id: str | None,
        expires_at_ms: int,
    ) -> None:
        if handle_id in self._attachments:
            raise AttachmentDenied("ATTACHMENT_HANDLE_CONFLICT")
        self._attachments[handle_id] = _Attachment(
            bytes(body), principal, purpose, target_ref_id, expires_at_ms
        )

    def revoke(self, handle_id: str) -> None:
        self._attachments[handle_id].revoked = True

    async def open(
        self,
        handle_id: str,
        principal: str,
        purpose: str,
        target_ref_id: str | None,
        max_bytes: int,
    ) -> BoundedByteStream:
        record = self._attachments.get(handle_id)
        if record is None:
            raise AttachmentDenied("ATTACHMENT_NOT_FOUND")
        if record.revoked:
            raise AttachmentDenied("ATTACHMENT_REVOKED")
        if self._clock_ms() >= record.expires_at_ms:
            raise AttachmentDenied("ATTACHMENT_EXPIRED")
        if (
            record.principal != principal
            or record.purpose != purpose
            or record.target_ref_id != target_ref_id
        ):
            raise AttachmentDenied("ATTACHMENT_SCOPE_DENIED")
        if len(record.body) > max_bytes:
            raise AttachmentDenied("ATTACHMENT_TOO_LARGE")
        return BoundedByteStream(record.body)


def classify_echo(value: dict[str, object]) -> dict[str, object]:
    operation_id = str(value["operationId"])
    content_kind = str(value["contentKind"])
    evidence = value.get("evidence")
    scoped_key = f"{operation_id}:{content_kind}"
    is_echo = isinstance(evidence, dict) and evidence.get(scoped_key) == "strong"
    return {"isEcho": is_echo, "admit": not is_echo, "evidenceUpgraded": False}


__all__ = [
    "AttachmentDenied",
    "BoundedAttachmentAuthority",
    "BoundedByteStream",
    "EchoAuthority",
    "FrozenEchoDecision",
    "classify_echo",
]
