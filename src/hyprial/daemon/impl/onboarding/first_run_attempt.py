"""P3 first-run attempt: bind one send before it happens.

Decision record (web-auth-r3-p3-20261118):
- P1 ``first-run.json`` v1 keeps its exact confirmation shape.  The attempt is
  a separate strict v1 document at ``<onboarding-root>/first-run-attempt.json``
  so a P1 record is never rewritten into a P3 shape and old files stay valid.
- One attempt per ``(issuer, userId, deviceUid)`` subject.  ``begin`` runs
  before the message send and records the non-secret snapshot the server reads
  from the trusted identity, agent and credential authorities.  A repeated
  operation key returns the same attempt and never sends again; a new key
  supersedes the previous attempt.
- ``bind`` records the request message id only after the caller verified it
  through the existing message authority.  ``mark_confirmed`` follows the
  receipt write; a crash between the two leaves the attempt bound and a repeat
  confirm reconciles against the receipt.  No file pair is treated as one
  transaction.
- Nothing here stores a secret, chat body or browser-supplied proof.  A grant
  revision is metadata, never proof that the same key value is still loaded.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from hyprial.identity import OnboardingSubject
from hyprial.kernel import (
    PrivateAccessUnsupported,
    fsync_directory,
    harden_private_path,
    inspect_private_file,
    read_private_file,
    secure_private_directory,
)

from .first_run import (
    FIRST_RUN_OPERATION_KEY_CONFLICT,
    FIRST_RUN_REVISION_CONFLICT,
    FIRST_RUN_STORE_COMMIT_UNCONFIRMED,
    FIRST_RUN_STORE_CORRUPT,
    FIRST_RUN_STORE_UNAVAILABLE,
    FIRST_RUN_STORE_UNSUPPORTED_VERSION,
    FIRST_RUN_STORE_WRITE_FAILED,
    FIRST_RUN_SUBJECT_REQUIRED,
    FirstRunStoreError,
)

FILENAME = "first-run-attempt.json"
VERSION = 1
MAX_BYTES = 1_048_576
MAX_RECORDS = 32
MAX_TEXT = 2048
FIRST_RUN_ATTEMPT_CONFLICT = "FIRST_RUN_ATTEMPT_CONFLICT"
FIRST_RUN_ATTEMPT_STALE = "FIRST_RUN_ATTEMPT_STALE"
FIRST_RUN_ATTEMPT_NOT_FOUND = "FIRST_RUN_ATTEMPT_NOT_FOUND"
FIRST_RUN_ATTEMPT_NOT_BOUND = "FIRST_RUN_ATTEMPT_NOT_BOUND"

STATES = ("pending", "bound", "confirmed", "cancelled", "superseded")
CREDENTIAL_KINDS = ("key", "native")
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?Z$"
)
_DOCUMENT_FIELDS = frozenset({"version", "records"})
_RECORD_FIELDS = frozenset({"issuer", "userId", "deviceUid", "attempt"})
_ATTEMPT_FIELDS = frozenset(
    {
        "attemptId",
        "revision",
        "state",
        "operationKey",
        "sendKey",
        "receiptRevision",
        "loginEpoch",
        "agentUri",
        "agentCreatedAtMs",
        "provider",
        "model",
        "harness",
        "configRevision",
        "credential",
        "requestMessageId",
        "conversationId",
        "replyMessageId",
        "updatedAt",
    }
)
_CREDENTIAL_FIELDS = frozenset(
    {
        "kind",
        "grantId",
        "entryId",
        "entityToken",
        "revision",
        "routeRequired",
        "importId",
        "importedAtMs",
        "manifest",
    }
)


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_TEXT
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} is not valid")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("attempt timestamp is not valid")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("attempt timestamp is not a calendar time") from error
    return value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class FirstRunCredentialBinding:
    """Non-secret credential metadata selected for this attempt."""

    kind: str
    grant_id: str | None = None
    entry_id: str | None = None
    entity_token: str | None = None
    revision: int | None = None
    route_required: bool | None = None
    import_id: str | None = None
    imported_at_ms: int | None = None
    manifest: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CREDENTIAL_KINDS:
            raise ValueError("credential kind is not supported")
        if self.kind == "key":
            _text(self.grant_id, "grantId")
            _text(self.entry_id, "entryId")
            _text(self.entity_token, "entityToken")
            if type(self.revision) is not int or self.revision < 0:
                raise ValueError("grant revision is not valid")
            if type(self.route_required) is not bool:
                raise ValueError("routeRequired must be boolean")
            if (
                self.import_id is not None
                or self.imported_at_ms is not None
                or self.manifest is not None
            ):
                raise ValueError("key binding cannot carry a native import")
        else:
            _text(self.import_id, "importId")
            _text(self.manifest, "manifest")
            if type(self.imported_at_ms) is not int or self.imported_at_ms < 0:
                raise ValueError("importedAtMs is not valid")
            if (
                self.grant_id is not None
                or self.entry_id is not None
                or self.entity_token is not None
                or self.revision is not None
                or self.route_required is not None
            ):
                raise ValueError("native binding cannot carry a key grant")

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "grantId": self.grant_id,
            "entryId": self.entry_id,
            "entityToken": self.entity_token,
            "revision": self.revision,
            "routeRequired": self.route_required,
            "importId": self.import_id,
            "importedAtMs": self.imported_at_ms,
            "manifest": self.manifest,
        }

    @classmethod
    def from_json(cls, value: object) -> "FirstRunCredentialBinding":
        if not isinstance(value, Mapping) or set(value) != _CREDENTIAL_FIELDS:
            raise ValueError("credential fields do not match the attempt format")
        return cls(
            kind=value["kind"],
            grant_id=value["grantId"],
            entry_id=value["entryId"],
            entity_token=value["entityToken"],
            revision=value["revision"],
            route_required=value["routeRequired"],
            import_id=value["importId"],
            imported_at_ms=value["importedAtMs"],
            manifest=value["manifest"],
        )


@dataclass(frozen=True, slots=True)
class FirstRunAttemptSnapshot:
    """The public non-secret attempt projection for one subject."""

    attempt_id: str
    revision: int
    state: str
    operation_key: str
    send_key: str
    receipt_revision: int
    login_epoch: int
    agent_uri: str
    agent_created_at_ms: int
    provider: str | None
    model: str | None
    harness: str
    config_revision: str | None
    credential: FirstRunCredentialBinding
    request_message_id: str | None
    conversation_id: str | None
    reply_message_id: str | None
    updated_at: str

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attemptId")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("attempt revision is not valid")
        if self.state not in STATES:
            raise ValueError("attempt state is not supported")
        for label, key in (
            ("operationKey", self.operation_key),
            ("sendKey", self.send_key),
        ):
            if (
                not isinstance(key, str)
                or not 16 <= len(key) <= 128
                or _OPERATION_KEY.fullmatch(key) is None
            ):
                raise ValueError(f"{label} is outside the attempt format")
        for label, value in (
            ("receiptRevision", self.receipt_revision),
            ("loginEpoch", self.login_epoch),
            ("agentCreatedAtMs", self.agent_created_at_ms),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} is not valid")
        _text(self.agent_uri, "agentUri")
        _text(self.harness, "harness")
        for label, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("configRevision", self.config_revision),
            ("requestMessageId", self.request_message_id),
            ("conversationId", self.conversation_id),
            ("replyMessageId", self.reply_message_id),
        ):
            _optional_text(value, label)
        if not isinstance(self.credential, FirstRunCredentialBinding):
            raise ValueError("credential binding is not valid")
        _timestamp(self.updated_at)
        if self.state == "bound" and (
            self.request_message_id is None or self.conversation_id is None
        ):
            raise ValueError("a bound attempt requires its request message")
        if self.state == "pending" and (
            self.request_message_id is not None or self.conversation_id is not None
        ):
            raise ValueError("a pending attempt cannot carry a request message")
        if self.state != "confirmed" and self.reply_message_id is not None:
            raise ValueError("only a confirmed attempt carries a reply message")

    def as_status(self) -> dict[str, Any]:
        return {
            "attemptId": self.attempt_id,
            "revision": self.revision,
            "state": self.state,
            "operationKey": self.operation_key,
            "sendKey": self.send_key,
            "receiptRevision": self.receipt_revision,
            "loginEpoch": self.login_epoch,
            "agentUri": self.agent_uri,
            "agentCreatedAtMs": self.agent_created_at_ms,
            "provider": self.provider,
            "model": self.model,
            "harness": self.harness,
            "configRevision": self.config_revision,
            "credentialKind": self.credential.kind,
            "requestMessageId": self.request_message_id,
            "conversationId": self.conversation_id,
            "replyMessageId": self.reply_message_id,
        }


@dataclass(frozen=True, slots=True)
class FirstRunAttemptSelection:
    """The server-derived facts a begin call snapshots."""

    login_epoch: int
    agent_uri: str
    agent_created_at_ms: int
    provider: str | None
    model: str | None
    harness: str
    config_revision: str | None
    credential: FirstRunCredentialBinding

    def __post_init__(self) -> None:
        if type(self.login_epoch) is not int or self.login_epoch < 1:
            raise ValueError("login epoch is not valid")
        _text(self.agent_uri, "agentUri")
        if type(self.agent_created_at_ms) is not int or self.agent_created_at_ms < 0:
            raise ValueError("agentCreatedAtMs is not valid")
        for label, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("configRevision", self.config_revision),
        ):
            _optional_text(value, label)
        _text(self.harness, "harness")
        if not isinstance(self.credential, FirstRunCredentialBinding):
            raise ValueError("credential binding is not valid")


def _snapshot(value: Mapping[str, Any]) -> FirstRunAttemptSnapshot:
    return FirstRunAttemptSnapshot(
        attempt_id=value["attemptId"],
        revision=value["revision"],
        state=value["state"],
        operation_key=value["operationKey"],
        send_key=value["sendKey"],
        receipt_revision=value["receiptRevision"],
        login_epoch=value["loginEpoch"],
        agent_uri=value["agentUri"],
        agent_created_at_ms=value["agentCreatedAtMs"],
        provider=value["provider"],
        model=value["model"],
        harness=value["harness"],
        config_revision=value["configRevision"],
        credential=FirstRunCredentialBinding.from_json(value["credential"]),
        request_message_id=value["requestMessageId"],
        conversation_id=value["conversationId"],
        reply_message_id=value["replyMessageId"],
        updated_at=value["updatedAt"],
    )


def _attempt_json(attempt: FirstRunAttemptSnapshot) -> dict[str, Any]:
    return {
        "attemptId": attempt.attempt_id,
        "revision": attempt.revision,
        "state": attempt.state,
        "operationKey": attempt.operation_key,
        "sendKey": attempt.send_key,
        "receiptRevision": attempt.receipt_revision,
        "loginEpoch": attempt.login_epoch,
        "agentUri": attempt.agent_uri,
        "agentCreatedAtMs": attempt.agent_created_at_ms,
        "provider": attempt.provider,
        "model": attempt.model,
        "harness": attempt.harness,
        "configRevision": attempt.config_revision,
        "credential": attempt.credential.as_json(),
        "requestMessageId": attempt.request_message_id,
        "conversationId": attempt.conversation_id,
        "replyMessageId": attempt.reply_message_id,
        "updatedAt": attempt.updated_at,
    }


@dataclass(frozen=True, slots=True)
class _Record:
    subject: OnboardingSubject
    attempt: FirstRunAttemptSnapshot

    def as_json(self) -> dict[str, Any]:
        return {
            "issuer": self.subject.issuer,
            "userId": self.subject.user_id,
            "deviceUid": self.subject.device_uid,
            "attempt": _attempt_json(self.attempt),
        }


@dataclass(frozen=True, slots=True)
class _Loaded:
    state: str
    records: tuple[_Record, ...] = ()


def _parse(raw: bytes) -> _Loaded:
    if len(raw) > MAX_BYTES:
        raise ValueError("first-run attempt exceeds the size bound")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("first-run attempt top level is not an object")
    version = value.get("version")
    if type(version) is not int:
        raise ValueError("first-run attempt version is not an integer")
    if version != VERSION:
        return _Loaded("unsupported-version")
    if set(value) != _DOCUMENT_FIELDS:
        raise ValueError("first-run attempt fields do not match the format")
    rows = value["records"]
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise ValueError("first-run attempts are not a bounded array")
    records = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _RECORD_FIELDS:
            raise ValueError("first-run attempt record fields are not valid")
        subject = OnboardingSubject(
            issuer=_text(row["issuer"], "issuer"),
            user_id=_text(row["userId"], "userId"),
            device_uid=_text(row["deviceUid"], "deviceUid"),
        )
        attempt_value = row["attempt"]
        if not isinstance(attempt_value, Mapping) or set(attempt_value) != _ATTEMPT_FIELDS:
            raise ValueError("first-run attempt payload fields are not valid")
        records.append(_Record(subject, _snapshot(attempt_value)))
    subjects = {(r.subject.issuer, r.subject.user_id, r.subject.device_uid) for r in records}
    if len(subjects) != len(records):
        raise ValueError("first-run attempts contain duplicate subjects")
    return _Loaded("recorded", tuple(records))


class FirstRunAttemptStore:
    """Strict local attempt storage for one daemon-owned onboarding root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._path = self._root / FILENAME
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self, subject: OnboardingSubject | None) -> FirstRunAttemptSnapshot | None:
        if subject is None:
            return None
        with self._lock:
            return self._find(self._load(), subject)

    def begin(
        self,
        subject: OnboardingSubject | None,
        selection: FirstRunAttemptSelection,
        *,
        operation_key: str,
        receipt_revision: int,
        now: str | None = None,
    ) -> FirstRunAttemptSnapshot:
        if subject is None:
            raise FirstRunStoreError(FIRST_RUN_SUBJECT_REQUIRED, "a stable subject is required")
        if not isinstance(selection, FirstRunAttemptSelection):
            raise ValueError("selection is not a FirstRunAttemptSelection")
        key = self._key(operation_key)
        if type(receipt_revision) is not int or receipt_revision < 0:
            raise FirstRunStoreError(FIRST_RUN_REVISION_CONFLICT, "receipt revision is not valid")
        timestamp = self._time(now)
        with self._lock:
            loaded = self._load()
            self._refuse_states(loaded)
            records = list(loaded.records)
            index = self._index(records, subject)
            if index is not None:
                current = records[index].attempt
                if current.operation_key == key:
                    if self._same_selection(current, selection, receipt_revision):
                        return current
                    raise FirstRunStoreError(
                        FIRST_RUN_ATTEMPT_CONFLICT,
                        "the operation key belongs to another attempt selection",
                    )
            attempt = FirstRunAttemptSnapshot(
                attempt_id=f"fr-{uuid4().hex}",
                revision=1,
                state="pending",
                operation_key=key,
                send_key=f"frs-{uuid4().hex}",
                receipt_revision=receipt_revision,
                login_epoch=selection.login_epoch,
                agent_uri=selection.agent_uri,
                agent_created_at_ms=selection.agent_created_at_ms,
                provider=selection.provider,
                model=selection.model,
                harness=selection.harness,
                config_revision=selection.config_revision,
                credential=selection.credential,
                request_message_id=None,
                conversation_id=None,
                reply_message_id=None,
                updated_at=timestamp,
            )
            record = _Record(subject, attempt)
            if index is None:
                if len(records) >= MAX_RECORDS:
                    raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "attempt limit reached")
                records.append(record)
            else:
                records[index] = record
            self._write(records)
            return attempt

    def bind(
        self,
        subject: OnboardingSubject | None,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
        request_message_id: str,
        conversation_id: str,
        now: str | None = None,
    ) -> FirstRunAttemptSnapshot:
        key = self._key(operation_key)
        _text(request_message_id, "requestMessageId")
        _text(conversation_id, "conversationId")
        timestamp = self._time(now)
        with self._lock:
            loaded = self._load()
            self._refuse_states(loaded)
            records = list(loaded.records)
            index = self._index(records, subject)
            if index is None:
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_FOUND, "no attempt exists")
            current = records[index].attempt
            self._match(current, attempt_id, expected_revision, key)
            if current.state == "bound":
                if current.request_message_id == request_message_id and current.conversation_id == conversation_id:
                    return current
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt is bound to another request")
            if current.state != "pending":
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt cannot be bound")
            attempt = self._replace(
                current,
                state="bound",
                revision=current.revision + 1,
                request_message_id=request_message_id,
                conversation_id=conversation_id,
                updated_at=timestamp,
            )
            records[index] = _Record(records[index].subject, attempt)
            self._write(records)
            return attempt

    def mark_confirmed(
        self,
        subject: OnboardingSubject | None,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
        reply_message_id: str | None = None,
        now: str | None = None,
    ) -> FirstRunAttemptSnapshot:
        key = self._key(operation_key)
        if reply_message_id is not None:
            _text(reply_message_id, "replyMessageId")
        timestamp = self._time(now)
        with self._lock:
            loaded = self._load()
            self._refuse_states(loaded)
            records = list(loaded.records)
            index = self._index(records, subject)
            if index is None:
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_FOUND, "no attempt exists")
            current = records[index].attempt
            self._match_identity(current, attempt_id, key)
            if current.state == "confirmed":
                return current
            self._match_revision(current, expected_revision)
            if current.state != "bound":
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_BOUND, "the attempt is not bound")
            attempt = self._replace(
                current,
                state="confirmed",
                revision=current.revision + 1,
                reply_message_id=reply_message_id or current.reply_message_id,
                updated_at=timestamp,
            )
            records[index] = _Record(records[index].subject, attempt)
            self._write(records)
            return attempt

    def cancel(
        self,
        subject: OnboardingSubject | None,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
        now: str | None = None,
    ) -> FirstRunAttemptSnapshot:
        key = self._key(operation_key)
        timestamp = self._time(now)
        with self._lock:
            loaded = self._load()
            self._refuse_states(loaded)
            records = list(loaded.records)
            index = self._index(records, subject)
            if index is None:
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_FOUND, "no attempt exists")
            current = records[index].attempt
            self._match_identity(current, attempt_id, key)
            if current.state == "cancelled":
                return current
            self._match_revision(current, expected_revision)
            if current.state not in ("pending", "bound"):
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt cannot be cancelled")
            attempt = self._replace(
                current,
                state="cancelled",
                revision=current.revision + 1,
                updated_at=timestamp,
            )
            records[index] = _Record(records[index].subject, attempt)
            self._write(records)
            return attempt

    def _key(self, value: object) -> str:
        if (
            not isinstance(value, str)
            or not 16 <= len(value) <= 128
            or _OPERATION_KEY.fullmatch(value) is None
        ):
            raise FirstRunStoreError(FIRST_RUN_OPERATION_KEY_CONFLICT, "operation key is not valid")
        return value

    def _time(self, value: str | None) -> str:
        try:
            return _timestamp(value if value is not None else _now())
        except ValueError as error:
            raise FirstRunStoreError(FIRST_RUN_OPERATION_KEY_CONFLICT, "timestamp is not valid") from error

    def _same_selection(
        self,
        attempt: FirstRunAttemptSnapshot,
        selection: FirstRunAttemptSelection,
        receipt_revision: int,
    ) -> bool:
        return (
            attempt.receipt_revision == receipt_revision
            and attempt.login_epoch == selection.login_epoch
            and attempt.agent_uri == selection.agent_uri
            and attempt.agent_created_at_ms == selection.agent_created_at_ms
            and attempt.provider == selection.provider
            and attempt.model == selection.model
            and attempt.harness == selection.harness
            and attempt.config_revision == selection.config_revision
            and attempt.credential == selection.credential
        )

    def _match(
        self,
        attempt: FirstRunAttemptSnapshot,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
    ) -> None:
        self._match_identity(attempt, attempt_id, operation_key)
        self._match_revision(attempt, expected_revision)

    @staticmethod
    def _match_identity(
        attempt: FirstRunAttemptSnapshot,
        attempt_id: str,
        operation_key: str,
    ) -> None:
        if attempt.attempt_id != attempt_id or attempt.operation_key != operation_key:
            raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt does not match")

    @staticmethod
    def _match_revision(
        attempt: FirstRunAttemptSnapshot,
        expected_revision: int,
    ) -> None:
        if expected_revision != attempt.revision:
            raise FirstRunStoreError(FIRST_RUN_ATTEMPT_STALE, "the attempt revision changed")

    @staticmethod
    def _replace(attempt: FirstRunAttemptSnapshot, **changes: Any) -> FirstRunAttemptSnapshot:
        values = {
            "attempt_id": attempt.attempt_id,
            "revision": attempt.revision,
            "state": attempt.state,
            "operation_key": attempt.operation_key,
            "send_key": attempt.send_key,
            "receipt_revision": attempt.receipt_revision,
            "login_epoch": attempt.login_epoch,
            "agent_uri": attempt.agent_uri,
            "agent_created_at_ms": attempt.agent_created_at_ms,
            "provider": attempt.provider,
            "model": attempt.model,
            "harness": attempt.harness,
            "config_revision": attempt.config_revision,
            "credential": attempt.credential,
            "request_message_id": attempt.request_message_id,
            "conversation_id": attempt.conversation_id,
            "reply_message_id": attempt.reply_message_id,
            "updated_at": attempt.updated_at,
        }
        values.update(changes)
        return FirstRunAttemptSnapshot(**values)

    def _refuse_states(self, loaded: _Loaded) -> None:
        if loaded.state == "corrupt":
            raise FirstRunStoreError(FIRST_RUN_STORE_CORRUPT, "attempt record is corrupt and was preserved")
        if loaded.state == "unsupported-version":
            raise FirstRunStoreError(
                FIRST_RUN_STORE_UNSUPPORTED_VERSION,
                "attempt record version is unsupported and was preserved",
            )
        if loaded.state == "unavailable":
            raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "attempt record is unavailable")

    @staticmethod
    def _index(records: list[_Record], subject: OnboardingSubject | None) -> int | None:
        if subject is None:
            return None
        for position, record in enumerate(records):
            if record.subject == subject:
                return position
        return None

    def _find(self, loaded: _Loaded, subject: OnboardingSubject) -> FirstRunAttemptSnapshot | None:
        index = self._index(list(loaded.records), subject)
        return None if index is None else loaded.records[index].attempt

    def _load(self) -> _Loaded:
        try:
            secure_private_directory(self._root, mode=0o700)
        except FileNotFoundError:
            return _Loaded("none")
        except (PrivateAccessUnsupported, OSError):
            return _Loaded("corrupt")
        try:
            raw, _ = read_private_file(self._path, mode=0o600, max_bytes=MAX_BYTES)
        except FileNotFoundError:
            return _Loaded("none")
        except PrivateAccessUnsupported:
            return _Loaded("corrupt")
        except OSError:
            return _Loaded("unavailable")
        try:
            return _parse(raw)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return _Loaded("corrupt")

    def _require_root(self) -> None:
        try:
            secure_private_directory(self._root, mode=0o700)
            return
        except FileNotFoundError:
            pass
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "attempt root is not private") from error
        missing: list[Path] = []
        current = self._root
        while True:
            try:
                current.lstat()
                break
            except FileNotFoundError:
                missing.append(current)
                parent = current.parent
                if parent == current:
                    break
                current = parent
            except OSError as error:
                raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "attempt root is not private") from error
        if current != current.parent:
            self._private_component(current)
        for directory in reversed(missing):
            self._private_component(directory.parent)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "attempt root is not private") from error
            self._private_component(directory)

    def _private_component(self, path: Path) -> None:
        try:
            secure_private_directory(path, mode=0o700)
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "attempt root is not private") from error

    def _write(self, records: list[_Record]) -> None:
        self._require_root()
        try:
            inspect_private_file(self._path, mode=0o600)
        except FileNotFoundError:
            pass
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(FIRST_RUN_STORE_CORRUPT, "attempt file is not private") from error
        document = {
            "version": VERSION,
            "records": [
                record.as_json()
                for record in sorted(
                    records,
                    key=lambda record: (
                        record.subject.issuer,
                        record.subject.user_id,
                        record.subject.device_uid,
                    ),
                )
            ],
        }
        payload = (
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{FILENAME}.", suffix=".tmp", dir=self._root
            )
            harden_private_path(Path(temporary), mode=0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(FIRST_RUN_STORE_WRITE_FAILED, "attempt could not be committed") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        try:
            fsync_directory(self._root)
        except OSError as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_COMMIT_UNCONFIRMED,
                "attempt durability is unconfirmed",
            ) from error
