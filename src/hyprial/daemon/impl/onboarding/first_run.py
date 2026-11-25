"""P1 first-run receipt storage and read projection.

Decision: web-auth-r3-p1-20261118.  This module owns the one small daemon-side
record that P3 will use for facts that cannot be derived from the live account,
device, configuration or message authorities.  It is deliberately not a public
daemon method: P1 has no browser write path, and a record cannot mark the
journey complete by itself.

The document is strict JSON at ``<onboarding-root>/first-run.json``.  A missing
file means no history.  A malformed or unknown-version file is preserved
byte-for-byte and reported as such; it is never replaced by an empty history.
Writes are atomic replacements under a 0700 directory and a 0600 file.  A
record is selected only by the server-side ``(issuer, userId, deviceUid)``
subject; there is no shared fallback for an unstable identity.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hyprial.identity import (
    IdentityRecordError,
    OnboardingSubject,
    read_onboarding_subject,
)
from hyprial.kernel import (
    PrivateAccessUnsupported,
    fsync_directory,
    harden_private_path,
    inspect_private_file,
    read_private_file,
    secure_private_directory,
)

from hyprial.daemon.impl.request_catalog.onboarding import FirstRunSnapshot
from hyprial.daemon.impl.request_catalog.onboarding.schemas import (
    FIRST_RUN_CONFIRMED_STEPS,
)

FIRST_RUN_REPLY_UNVERIFIED = "FIRST_RUN_REPLY_UNVERIFIED"
FIRST_RUN_CONFIGURATION_CHANGED = "FIRST_RUN_CONFIGURATION_CHANGED"
FIRST_RUN_STORE_CORRUPT = "FIRST_RUN_STORE_CORRUPT"
FIRST_RUN_STORE_UNSUPPORTED_VERSION = "FIRST_RUN_STORE_UNSUPPORTED_VERSION"
FIRST_RUN_STORE_UNAVAILABLE = "FIRST_RUN_STORE_UNAVAILABLE"
FIRST_RUN_STORE_WRITE_FAILED = "FIRST_RUN_STORE_WRITE_FAILED"
FIRST_RUN_STORE_COMMIT_UNCONFIRMED = "FIRST_RUN_STORE_COMMIT_UNCONFIRMED"
FIRST_RUN_REVISION_CONFLICT = "FIRST_RUN_REVISION_CONFLICT"
FIRST_RUN_OPERATION_KEY_CONFLICT = "FIRST_RUN_OPERATION_KEY_CONFLICT"
FIRST_RUN_SUBJECT_REQUIRED = "FIRST_RUN_SUBJECT_REQUIRED"
FIRST_RUN_RECORD_LIMIT = "FIRST_RUN_RECORD_LIMIT"

FILENAME = "first-run.json"
VERSION = 1
MAX_BYTES = 1_048_576
MAX_RECORDS = 32
MAX_SUBJECT_TEXT = 2048
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?Z$"
)
_DOCUMENT_FIELDS = frozenset({"version", "records"})
_RECORD_FIELDS = frozenset(
    {
        "issuer",
        "userId",
        "deviceUid",
        "revision",
        "confirmed",
        "lastOperationKey",
        "updatedAt",
        "skipped",
    }
)
_LEGACY_RECORD_FIELDS = _RECORD_FIELDS - {"skipped"}


class FirstRunStoreError(RuntimeError):
    """A refused local receipt operation with a stable internal code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Record:
    subject: OnboardingSubject
    revision: int
    confirmed: tuple[str, ...]
    last_operation_key: str
    updated_at: str
    skipped: bool = False

    def as_json(self) -> dict[str, Any]:
        payload = {
            "issuer": self.subject.issuer,
            "userId": self.subject.user_id,
            "deviceUid": self.subject.device_uid,
            "revision": self.revision,
            "confirmed": list(self.confirmed),
            "lastOperationKey": self.last_operation_key,
            "updatedAt": self.updated_at,
        }
        if self.skipped:
            payload["skipped"] = True
        return payload


@dataclass(frozen=True, slots=True)
class _Loaded:
    state: str
    records: tuple[_Record, ...] = ()


class FirstRunVerificationError(RuntimeError):
    """A refused reply confirmation with a stable internal code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _required_fact(value: object, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} is not valid")
    return value


@dataclass(frozen=True, slots=True)
class FirstRunConfiguration:
    """The non-secret configuration and grant facts the user selected."""

    provider: str | None
    model: str | None
    harness: str
    grant_id: str | None
    grant_revision: int

    def __post_init__(self) -> None:
        for label, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("grantId", self.grant_id),
        ):
            if value is not None:
                _required_fact(value, label)
        _required_fact(self.harness, "harness")
        if type(self.grant_revision) is not int or self.grant_revision < 0:
            raise ValueError("grantRevision must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class FirstRunAttempt:
    """One reply attempt, associated by the request message and replyTo."""

    agent_uri: str
    conversation_id: str
    request_message_id: str
    configuration: FirstRunConfiguration

    def __post_init__(self) -> None:
        _required_fact(self.agent_uri, "agentUri")
        _required_fact(self.conversation_id, "conversationId")
        _required_fact(self.request_message_id, "requestMessageId")
        if not isinstance(self.configuration, FirstRunConfiguration):
            raise ValueError("configuration is not a FirstRunConfiguration")


@dataclass(frozen=True, slots=True)
class FirstRunReply:
    """One durable inbox row selected by its ``replyTo`` payload field."""

    message_id: str
    reply_to: str
    sender: str
    recipient: str
    conversation_id: str
    intent: str
    received_at_ms: int

    def __post_init__(self) -> None:
        for label, value in (
            ("messageId", self.message_id),
            ("replyTo", self.reply_to),
            ("sender", self.sender),
            ("recipient", self.recipient),
            ("conversationId", self.conversation_id),
            ("intent", self.intent),
        ):
            _required_fact(value, label)
        if type(self.received_at_ms) is not int or self.received_at_ms < 0:
            raise ValueError("receivedAtMs must be a non-negative integer")


def verify_first_run_attempt(
    *,
    attempt: FirstRunAttempt,
    reply: FirstRunReply | None,
    current: FirstRunConfiguration,
    trusted_sender: str,
) -> FirstRunReply:
    """Prove one durable reply answers this attempt under current facts.

    The only association is ``reply_to`` naming the attempt's request message.
    Conversation plus time never selects a reply, and an old reply that answers
    another request cannot be reused.  The current configuration and grant must
    still equal the attempt's selection; grant revision alone never proves the
    same key value, so callers bind that separately with the agent inventory.
    """

    _required_fact(trusted_sender, "trustedSender")
    if not isinstance(attempt, FirstRunAttempt):
        raise ValueError("attempt is not a FirstRunAttempt")
    if not isinstance(current, FirstRunConfiguration):
        raise ValueError("current is not a FirstRunConfiguration")
    verified = (
        reply is not None
        and isinstance(reply, FirstRunReply)
        and reply.reply_to == attempt.request_message_id
        and reply.sender == attempt.agent_uri
        and reply.recipient == trusted_sender
        and reply.conversation_id == attempt.conversation_id
        and reply.intent == "reply"
    )
    if not verified:
        raise FirstRunVerificationError(
            FIRST_RUN_REPLY_UNVERIFIED,
            "no durable reply answers this attempt",
        )
    if current != attempt.configuration:
        raise FirstRunVerificationError(
            FIRST_RUN_CONFIGURATION_CHANGED,
            "the selected configuration or grant changed",
        )
    return reply


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_value(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("record timestamp is not valid")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("record timestamp is not a calendar time") from error
    return value


def _operation_key_value(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 128
        or _OPERATION_KEY.fullmatch(value) is None
    ):
        raise ValueError("operation key is outside the local receipt format")
    return value


def _validate_operation_key(value: object) -> str:
    try:
        return _operation_key_value(value)
    except ValueError as error:
        raise FirstRunStoreError(
            FIRST_RUN_OPERATION_KEY_CONFLICT,
            str(error),
        ) from error


def _confirmation_steps(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise FirstRunStoreError(
            FIRST_RUN_OPERATION_KEY_CONFLICT,
            "at least one first-run confirmation is required",
        )
    if (
        any(not isinstance(step, str) for step in value)
        or any(step not in FIRST_RUN_CONFIRMED_STEPS for step in value)
        or len(set(value)) != len(value)
    ):
        raise FirstRunStoreError(
            FIRST_RUN_OPERATION_KEY_CONFLICT,
            "unknown or duplicate first-run confirmation",
        )
    return tuple(
        candidate for candidate in FIRST_RUN_CONFIRMED_STEPS if candidate in value
    )


def _snapshot(record: _Record) -> FirstRunSnapshot:
    return FirstRunSnapshot(
        state="recorded",
        revision=record.revision,
        confirmed=record.confirmed,
        updated_at=record.updated_at,
        skipped=record.skipped,
    )


def _validate_record(value: object) -> _Record:
    if not isinstance(value, Mapping) or set(value) not in (_RECORD_FIELDS, _LEGACY_RECORD_FIELDS):
        raise ValueError("record fields do not match the local format")
    issuer = value["issuer"]
    user_id = value["userId"]
    device_uid = value["deviceUid"]
    for text in (issuer, user_id, device_uid):
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_SUBJECT_TEXT
            or any(ord(character) < 0x20 for character in text)
        ):
            raise ValueError("record subject is not valid")
    subject = OnboardingSubject(
        issuer=issuer,
        user_id=user_id,
        device_uid=device_uid,
    )
    revision = value["revision"]
    if type(revision) is not int or revision < 1 or revision > 2**31 - 1:
        raise ValueError("record revision is not valid")
    confirmed = value["confirmed"]
    if (
        not isinstance(confirmed, list)
        or any(step not in FIRST_RUN_CONFIRMED_STEPS for step in confirmed)
        or len(set(confirmed)) != len(confirmed)
        or len(confirmed) > len(FIRST_RUN_CONFIRMED_STEPS)
    ):
        raise ValueError("record confirmations are not valid")
    skipped = value.get("skipped", False)
    if type(skipped) is not bool:
        raise ValueError("record skipped flag is not valid")
    timestamp = _timestamp_value(value["updatedAt"])
    return _Record(
        subject=subject,
        revision=revision,
        confirmed=tuple(step for step in FIRST_RUN_CONFIRMED_STEPS if step in confirmed),
        last_operation_key=_operation_key_value(value["lastOperationKey"]),
        updated_at=timestamp,
        skipped=value.get("skipped", False),
    )


def _parse_document(raw: bytes) -> _Loaded:
    if len(raw) > MAX_BYTES:
        raise ValueError("first-run record exceeds the size bound")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("first-run record top level is not an object")
    version = value.get("version")
    if type(version) is not int:
        raise ValueError("first-run record version is not an integer")
    if version != VERSION:
        return _Loaded("unsupported-version")
    if set(value) != _DOCUMENT_FIELDS:
        raise ValueError("first-run record fields do not match the local format")
    records = value["records"]
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("first-run records are not a bounded array")
    parsed = tuple(_validate_record(record) for record in records)
    if len({(record.subject.issuer, record.subject.user_id, record.subject.device_uid) for record in parsed}) != len(parsed):
        raise ValueError("first-run records contain duplicate subjects")
    return _Loaded("recorded", parsed)


_ABSENT = object()


def first_run_status(
    home: Path,
    onboarding_root: Path,
    *,
    authenticated: bool = True,
    subject_reader: Callable[[Path], OnboardingSubject | None] = read_onboarding_subject,
    subject: OnboardingSubject | None | object = _ABSENT,
) -> FirstRunSnapshot:
    """Read the current subject's receipt without creating or repairing files.

    An unauthenticated caller must not learn whether some local identity has a
    receipt: return no history before reading settings, the device record or the
    receipt file.  This is the bootstrap-before-login case; it is not a shared
    partition fallback.  A caller that already captured a trusted identity view
    passes its ``subject`` so the receipt is not selected from a later
    generation.
    """

    if not authenticated:
        return FirstRunSnapshot(state="none")
    if subject is _ABSENT:
        try:
            subject = subject_reader(Path(home))
        except (IdentityRecordError, OSError, ValueError):
            return FirstRunSnapshot(state="unavailable")
    if subject is None or not isinstance(subject, OnboardingSubject):
        return FirstRunSnapshot(state="none")
    return FirstRunStore(Path(onboarding_root)).read(subject)


class FirstRunStore:
    """Strict local receipt storage for one daemon-owned onboarding root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._path = self._root / FILENAME
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self, subject: OnboardingSubject | None) -> FirstRunSnapshot:
        if subject is None:
            return FirstRunSnapshot(state="none")
        with self._lock:
            loaded = self._load()
            if loaded.state != "recorded":
                return FirstRunSnapshot(state=loaded.state)
            record = self._find(loaded.records, subject)
            if record is None:
                return FirstRunSnapshot(state="none")
            return _snapshot(record)

    def read_internal(
        self,
        subject: OnboardingSubject | None,
    ) -> tuple[FirstRunSnapshot, str | None]:
        """Read the receipt plus its last operation key for P3 compensation.

        The key stays server-side; the public status projection never carries it.
        """

        if subject is None:
            return FirstRunSnapshot(state="none"), None
        with self._lock:
            loaded = self._load()
            if loaded.state != "recorded":
                return FirstRunSnapshot(state=loaded.state), None
            record = self._find(loaded.records, subject)
            if record is None:
                return FirstRunSnapshot(state="none"), None
            return _snapshot(record), record.last_operation_key

    def confirm(
        self,
        subject: OnboardingSubject | None,
        *,
        step: str,
        expected_revision: int,
        operation_key: str,
        now: str | None = None,
    ) -> FirstRunSnapshot:
        """Record one non-derivable confirmation; P3 owns the public wrapper."""

        return self.confirm_many(
            subject,
            steps=(step,),
            expected_revision=expected_revision,
            operation_key=operation_key,
            now=now,
        )

    def confirm_many(
        self,
        subject: OnboardingSubject | None,
        *,
        steps: object,
        expected_revision: int,
        operation_key: str,
        now: str | None = None,
    ) -> FirstRunSnapshot:
        """Record one or more confirmations in one atomic write."""

        if subject is None:
            raise FirstRunStoreError(
                FIRST_RUN_SUBJECT_REQUIRED,
                "a stable onboarding subject is required",
            )
        requested = _confirmation_steps(steps)
        if type(expected_revision) is not int or expected_revision < 0:
            raise FirstRunStoreError(
                FIRST_RUN_REVISION_CONFLICT,
                "expected revision must be a non-negative integer",
            )
        operation_key = _validate_operation_key(operation_key)
        try:
            timestamp = _timestamp_value(now if now is not None else _now())
        except ValueError as error:
            raise FirstRunStoreError(
                FIRST_RUN_OPERATION_KEY_CONFLICT,
                "timestamp must be a UTC timestamp",
            ) from error
        with self._lock:
            loaded = self._load()
            if loaded.state == "corrupt":
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_CORRUPT,
                    "first-run record is corrupt and was preserved",
                )
            if loaded.state == "unsupported-version":
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_UNSUPPORTED_VERSION,
                    "first-run record version is unsupported and was preserved",
                )
            if loaded.state == "unavailable":
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_UNAVAILABLE,
                    "first-run record is unavailable",
                )
            records = list(loaded.records)
            index = next(
                (
                    position
                    for position, record in enumerate(records)
                    if record.subject == subject
                ),
                None,
            )
            if index is None:
                if records and len(records) >= MAX_RECORDS:
                    raise FirstRunStoreError(
                        FIRST_RUN_RECORD_LIMIT,
                        "first-run record limit reached",
                    )
                if expected_revision != 0:
                    raise FirstRunStoreError(
                        FIRST_RUN_REVISION_CONFLICT,
                        "no record exists at the expected revision",
                    )
                record = _Record(subject, 1, requested, operation_key, timestamp)
                records.append(record)
            else:
                current = records[index]
                if all(step in current.confirmed for step in requested):
                    self._flush_root()
                    return _snapshot(current)
                if current.last_operation_key == operation_key:
                    raise FirstRunStoreError(
                        FIRST_RUN_OPERATION_KEY_CONFLICT,
                        "operation key already belongs to another confirmation",
                    )
                if expected_revision != current.revision:
                    raise FirstRunStoreError(
                        FIRST_RUN_REVISION_CONFLICT,
                        "the first-run revision changed",
                    )
                confirmed = tuple(
                    candidate
                    for candidate in FIRST_RUN_CONFIRMED_STEPS
                    if candidate in (*current.confirmed, *requested)
                )
                record = _Record(
                    subject,
                    current.revision + 1,
                    confirmed,
                    operation_key,
                    timestamp,
                )
                records[index] = record
            self._write(records)
            return _snapshot(record)

    def skip(
        self,
        subject: OnboardingSubject | None,
        *,
        expected_revision: int,
        operation_key: str,
        now: str | None = None,
    ) -> FirstRunSnapshot:
        """Persist the user's explicit shortcut past the first-run flow."""

        if subject is None:
            raise FirstRunStoreError(FIRST_RUN_SUBJECT_REQUIRED, "a stable subject is required")
        if type(expected_revision) is not int or expected_revision < 0:
            raise FirstRunStoreError(FIRST_RUN_REVISION_CONFLICT, "expected revision must be a non-negative integer")
        operation_key = _validate_operation_key(operation_key)
        try:
            timestamp = _timestamp_value(now if now is not None else _now())
        except ValueError as error:
            raise FirstRunStoreError(FIRST_RUN_OPERATION_KEY_CONFLICT, "timestamp must be a UTC timestamp") from error
        with self._lock:
            loaded = self._load()
            if loaded.state == "corrupt":
                raise FirstRunStoreError(FIRST_RUN_STORE_CORRUPT, "first-run record is corrupt and was preserved")
            if loaded.state == "unsupported-version":
                raise FirstRunStoreError(FIRST_RUN_STORE_UNSUPPORTED_VERSION, "first-run record version is unsupported and was preserved")
            if loaded.state == "unavailable":
                raise FirstRunStoreError(FIRST_RUN_STORE_UNAVAILABLE, "first-run record is unavailable")
            records = list(loaded.records)
            index = next((position for position, record in enumerate(records) if record.subject == subject), None)
            if index is None:
                if records and len(records) >= MAX_RECORDS:
                    raise FirstRunStoreError(FIRST_RUN_RECORD_LIMIT, "first-run record limit reached")
                if expected_revision != 0:
                    raise FirstRunStoreError(FIRST_RUN_REVISION_CONFLICT, "no record exists at the expected revision")
                record = _Record(subject, 1, (), operation_key, timestamp, True)
                records.append(record)
            else:
                current = records[index]
                if current.skipped:
                    self._flush_root()
                    return _snapshot(current)
                if current.last_operation_key == operation_key:
                    raise FirstRunStoreError(FIRST_RUN_OPERATION_KEY_CONFLICT, "operation key already belongs to another confirmation")
                if expected_revision != current.revision:
                    raise FirstRunStoreError(FIRST_RUN_REVISION_CONFLICT, "the first-run revision changed")
                record = _Record(subject, current.revision + 1, current.confirmed, operation_key, timestamp, True)
                records[index] = record
            self._write(records)
            return _snapshot(record)

    def _find(self, records: tuple[_Record, ...], subject: OnboardingSubject) -> _Record | None:
        for record in records:
            if record.subject == subject:
                return record
        return None

    def _load(self) -> _Loaded:
        try:
            secure_private_directory(self._root, mode=0o700)
        except FileNotFoundError:
            return _Loaded("none")
        except (PrivateAccessUnsupported, OSError):
            return _Loaded("corrupt")
        try:
            raw, _ = read_private_file(
                self._path,
                mode=0o600,
                max_bytes=MAX_BYTES,
            )
        except FileNotFoundError:
            return _Loaded("none")
        except PrivateAccessUnsupported:
            return _Loaded("corrupt")
        except OSError:
            return _Loaded("unavailable")
        try:
            return _parse_document(raw)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return _Loaded("corrupt")

    def _require_root(self, *, create: bool) -> None:
        try:
            secure_private_directory(self._root, mode=0o700)
            return
        except FileNotFoundError:
            if not create:
                raise
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_UNAVAILABLE,
                "first-run root is not a private directory",
            ) from error
        self._create_private_root()
        try:
            secure_private_directory(self._root, mode=0o700)
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_UNAVAILABLE,
                "first-run root is not a private directory",
            ) from error

    def _validate_root_component(self, path: Path) -> None:
        try:
            secure_private_directory(path, mode=0o700)
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_UNAVAILABLE,
                "first-run root is not a private directory",
            ) from error

    def _create_private_root(self) -> None:
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
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_UNAVAILABLE,
                    "first-run root is not a private directory",
                ) from error
        if current != current.parent:
            self._validate_root_component(current)
        for directory in reversed(missing):
            self._validate_root_component(directory.parent)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                self._validate_root_component(directory)
                continue
            except OSError as error:
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_UNAVAILABLE,
                    "first-run root is not a private directory",
                ) from error
            try:
                harden_private_path(directory, mode=0o700)
            except (PrivateAccessUnsupported, OSError) as error:
                raise FirstRunStoreError(
                    FIRST_RUN_STORE_UNAVAILABLE,
                    "first-run root is not a private directory",
                ) from error
            self._validate_root_component(directory)

    def _flush_root(self) -> None:
        try:
            fsync_directory(self._root)
        except OSError as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_COMMIT_UNCONFIRMED,
                "first-run receipt durability is unconfirmed",
            ) from error

    def _require_file_private(self) -> None:
        try:
            inspect_private_file(self._path, mode=0o600)
        except FileNotFoundError:
            return
        except (PrivateAccessUnsupported, OSError) as error:
            raise FirstRunStoreError(
                FIRST_RUN_STORE_CORRUPT,
                "first-run receipt is not a private file; refusing replacement",
            ) from error

    def _write(self, records: list[_Record]) -> None:
        self._require_root(create=True)
        self._require_file_private()
        document = {
            "version": VERSION,
            "records": [record.as_json() for record in sorted(
                records,
                key=lambda record: (
                    record.subject.issuer,
                    record.subject.user_id,
                    record.subject.device_uid,
                ),
            )],
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
            raise FirstRunStoreError(
                FIRST_RUN_STORE_WRITE_FAILED,
                "first-run receipt could not be committed",
            ) from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._flush_root()
