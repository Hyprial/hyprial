"""Local accepted and pending slots for owner-sovereign org context."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.document  import OrgDocument, parse_document, serialize_document, validate_document


class OrgStoreError(RuntimeError):
    """Local org-context state is corrupt or cannot be persisted."""


class OrgVersionError(OrgStoreError):
    """An adoption would move the local owner's view backwards or sideways."""


@dataclass(frozen=True, slots=True)
class AdoptionRecord:
    version: int
    publisher: str
    adopted_at: datetime
    document_sha256: str

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "publisher": self.publisher,
            "adoptedAt": self.adopted_at.isoformat().replace("+00:00", "Z"),
            "documentSha256": self.document_sha256,
        }


@dataclass(frozen=True, slots=True)
class PendingRecord:
    path: Path
    version: int
    publisher: str
    received_at: datetime
    source: str

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "version": self.version,
            "publisher": self.publisher,
            "receivedAt": self.received_at.isoformat().replace("+00:00", "Z"),
            "source": self.source,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise OrgStoreError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OrgStoreError(f"{label} must be an RFC3339 timestamp") from error
    return _aware(parsed, label)


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _document_bytes(document: OrgDocument) -> bytes:
    return serialize_document(document).encode("utf-8")


def _refuse_invalid(document: OrgDocument) -> None:
    """Refuse to persist bytes that do not re-validate.

    ``adopt`` and ``stage`` take an ``OrgDocument``, but a caller can still
    build one around corrupt source bytes.  Re-validate the exact bytes about
    to be written and name the file line on failure.
    """

    result = validate_document(serialize_document(document))
    if result.ok:
        return
    issue = result.issue
    location = "unknown" if issue is None else f"line {issue.line}, column {issue.column}"
    reason = "org-context is invalid" if issue is None else issue.reason
    raise OrgStoreError(f"refusing to write invalid org-context: {location}: {reason}")


class OrgContextStore:
    """Manage one accepted document and an unaccepted pending area."""

    def __init__(self, hyprial_home: Path) -> None:
        self.hyprial_home = Path(hyprial_home)
        self.accepted_path = self.hyprial_home / "org-context.md"
        self.org_dir = self.hyprial_home / "org"
        self.adoptions_dir = self.org_dir / "adoptions"
        self.pending_dir = self.org_dir / "pending"
        self.publish_pending_path = self.org_dir / "publish-pending.json"

    def ensure_layout(self) -> None:
        self.hyprial_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.org_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.adoptions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.org_dir, 0o700)
        os.chmod(self.adoptions_dir, 0o700)
        os.chmod(self.pending_dir, 0o700)

    def _adoption_path(self, document_sha256: str) -> Path:
        return self.adoptions_dir / f"{document_sha256}.json"

    def load_accepted(self) -> OrgDocument | None:
        try:
            source = self.accepted_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise OrgStoreError(
                f"cannot read accepted org-context {self.accepted_path}: {error}"
            ) from error
        return parse_document(source)

    def _record_for(
        self,
        document: OrgDocument,
        adopted_at: datetime,
    ) -> AdoptionRecord:
        payload = _document_bytes(document)
        return AdoptionRecord(
            version=document.meta.version,
            publisher=document.meta.publisher,
            adopted_at=_aware(adopted_at, "adopted_at"),
            document_sha256=hashlib.sha256(payload).hexdigest(),
        )

    def adopt(
        self, document: OrgDocument, *, adopted_at: datetime | None = None
    ) -> AdoptionRecord:
        """Enforce local monotonicity, then atomically replace the slot."""

        _refuse_invalid(document)
        current = self.load_accepted()
        if current is not None and document.meta.version <= current.meta.version:
            raise OrgVersionError(
                f"incoming version {document.meta.version} must be newer than "
                f"accepted version {current.meta.version}"
            )
        self.ensure_layout()
        record = self._record_for(document, adopted_at or _utc_now())
        # The accepted document is the runtime truth and its same-directory
        # os.replace is the single adoption commit point.  Write the audit
        # record first under the document digest: the old accepted document
        # continues to resolve its old record until the new document commits,
        # while a crash between these writes leaves only an unreferenced record.
        _atomic_write(
            self._adoption_path(record.document_sha256),
            _json_bytes(record.to_json()),
        )
        _atomic_write(self.accepted_path, _document_bytes(document))
        return record

    def acceptance_record(self) -> AdoptionRecord | None:
        """Read the audit record for the accepted bytes.

        Returns ``None`` when the slot or its record is absent.  Raises
        :class:`OrgStoreError` when the record exists but is unreadable or
        does not describe the accepted bytes.  The digest is over the raw
        file bytes, never newline-normalized text.
        """

        document = self.load_accepted()
        if document is None:
            return None
        try:
            payload = self.accepted_path.read_bytes()
        except OSError as error:
            raise OrgStoreError(
                f"cannot read accepted org-context {self.accepted_path}: {error}"
            ) from error
        payload_hash = hashlib.sha256(payload).hexdigest()
        metadata_path = self._adoption_path(payload_hash)
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise OrgStoreError(f"cannot read acceptance metadata: {error}") from error
        if not isinstance(raw, dict):
            raise OrgStoreError("acceptance metadata must be an object")
        if (
            raw.get("version") != document.meta.version
            or raw.get("publisher") != document.meta.publisher
            or raw.get("documentSha256") != payload_hash
        ):
            raise OrgStoreError("acceptance metadata does not match org-context.md")
        return AdoptionRecord(
            version=document.meta.version,
            publisher=document.meta.publisher,
            adopted_at=_parse_timestamp(raw.get("adoptedAt"), "adoptedAt"),
            document_sha256=payload_hash,
        )

    def accepted_slot_issue(self, document: OrgDocument, payload: bytes) -> str | None:
        """Why the accepted slot's audit state is inconsistent.

        ``payload`` must be the accepted slot's raw bytes as stored (never
        newline-normalized text).  ``None`` means the format-valid document is
        backed by a matching adoption record and, when present, an orgfs
        publish marker that names the same bytes.
        """

        digest = hashlib.sha256(payload).hexdigest()
        metadata_path = self._adoption_path(digest)
        try:
            text = metadata_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"no adoption record for the accepted org-context ({digest})"
        except OSError as error:
            return f"cannot read adoption record {metadata_path}: {error}"
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            return f"adoption record {metadata_path} is not valid JSON: {error}"
        if not isinstance(raw, dict):
            return f"adoption record {metadata_path} must be an object"
        if raw.get("documentSha256") != digest:
            return (
                f"adoption record {metadata_path} names documentSha256 "
                f"{raw.get('documentSha256')!r}, not the accepted bytes {digest}"
            )
        if raw.get("version") != document.meta.version:
            return (
                f"adoption record {metadata_path} names version "
                f"{raw.get('version')!r}, not {document.meta.version}"
            )
        if raw.get("publisher") != document.meta.publisher:
            return (
                f"adoption record {metadata_path} names publisher "
                f"{raw.get('publisher')!r}, not {document.meta.publisher!r}"
            )
        try:
            _parse_timestamp(raw.get("adoptedAt"), "adoptedAt")
        except OrgStoreError as error:
            return f"adoption record {metadata_path}: {error}"
        try:
            pending = self.orgfs_publish_pending()
        except OrgStoreError as error:
            return f"orgfs publish pending state is invalid: {error}"
        if pending is not None and pending != digest:
            return (
                f"orgfs publish pending points at {pending}, not the accepted "
                f"bytes {digest}"
            )
        return None

    def queue_orgfs_publish(self, document_sha256: str) -> None:
        """Durably request publication of the accepted bytes through orgfs."""

        if len(document_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in document_sha256
        ):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        self.ensure_layout()
        _atomic_write(
            self.publish_pending_path,
            _json_bytes({"documentSha256": document_sha256}),
        )

    def orgfs_publish_pending(self) -> str | None:
        """Return the accepted digest awaiting orgfs publication, if any."""

        try:
            raw = json.loads(self.publish_pending_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise OrgStoreError(
                f"cannot read orgfs publish pending state: {error}"
            ) from error
        digest = raw.get("documentSha256") if isinstance(raw, dict) else None
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OrgStoreError(
                "orgfs publish pending state has an invalid documentSha256"
            )
        return digest

    def clear_orgfs_publish_pending(self, document_sha256: str) -> None:
        """Clear only the request that the caller demonstrably published."""

        if self.orgfs_publish_pending() != document_sha256:
            return
        try:
            self.publish_pending_path.unlink()
        except FileNotFoundError:
            pass

    def stage(
        self,
        document: OrgDocument,
        *,
        source: str,
        received_at: datetime | None = None,
    ) -> PendingRecord:
        """Persist a candidate without changing the accepted slot."""

        _refuse_invalid(document)
        if not source or any(character in source for character in "\r\n"):
            raise ValueError("pending source must be a non-empty single line")
        received = _aware(received_at or _utc_now(), "received_at")
        payload = _document_bytes(document)
        digest = hashlib.sha256(payload).hexdigest()[:16]
        filename = f"{document.meta.publisher}-v{document.meta.version}-{digest}.md"
        path = self.pending_dir / filename
        record = PendingRecord(
            path=path,
            version=document.meta.version,
            publisher=document.meta.publisher,
            received_at=received,
            source=source,
        )
        self.ensure_layout()
        if path.exists() and path.read_bytes() != payload:
            raise OrgStoreError(f"pending slot collision at {path}")
        # A pending Markdown file is the visibility/commit point for list_pending.
        # Persist its sidecar first so readers never observe a candidate whose
        # provenance metadata is absent after an interrupted stage operation.
        _atomic_write(path.with_suffix(".json"), _json_bytes(record.to_json()))
        _atomic_write(path, payload)
        return record

    def list_pending(self) -> tuple[PendingRecord, ...]:
        self.ensure_layout()
        result: list[PendingRecord] = []
        for path in sorted(self.pending_dir.glob("*.md")):
            try:
                document = parse_document(path.read_text(encoding="utf-8"))
                raw = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise OrgStoreError(
                    f"cannot read pending org-context {path}: {error}"
                ) from error
            if not isinstance(raw, dict):
                raise OrgStoreError(f"pending metadata for {path} must be an object")
            if (
                raw.get("version") != document.meta.version
                or raw.get("publisher") != document.meta.publisher
            ):
                raise OrgStoreError(f"pending metadata does not match {path}")
            result.append(
                PendingRecord(
                    path=path,
                    version=document.meta.version,
                    publisher=document.meta.publisher,
                    received_at=_parse_timestamp(raw.get("receivedAt"), "receivedAt"),
                    source=str(raw.get("source", "")),
                )
            )
        return tuple(result)

    def status(self) -> dict[str, Any]:
        document = self.load_accepted()
        warnings: list[dict[str, str]] = []
        accepted: AdoptionRecord | None = None
        if document is not None:
            try:
                accepted = self.acceptance_record()
            except OrgStoreError as error:
                warnings.append(
                    {
                        "code": "ORG_ACCEPTANCE_METADATA_UNAVAILABLE",
                        "message": str(error),
                    }
                )
            else:
                if accepted is None:
                    # The body parses but no adoption record backs it: show the
                    # parsed view and say so instead of inventing an adoption.
                    warnings.append(
                        {
                            "code": "ORG_ACCEPTANCE_METADATA_UNAVAILABLE",
                            "message": (
                                "accepted org-context has no readable adoption "
                                "record; showing the parsed body only"
                            ),
                        }
                    )
        pending = self.list_pending()
        result: dict[str, Any] = {
            "slot": "accepted" if document is not None else "absent",
            # A parseable body backs these displayable fields on its own, so
            # status recovers them even when the adoption record is missing
            # (the same view org show derives from the parsed body).  The
            # adoption time stays absent: it is not a body fact.
            **({"meta": document.meta.to_json()} if document is not None else {}),
            **({"accepted": accepted.to_json()} if accepted is not None else {}),
            "pendingCount": len(pending),
            "pending": [item.to_json() for item in pending],
        }
        if warnings:
            result["warnings"] = warnings
        return result
