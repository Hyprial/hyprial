"""Per-agent sqlite values with an append-only operation log.

The database is one file directly under one agent home. Values are deliberately
plaintext in ``secrets``; events carry fingerprints only. SQLite secure-delete
is enabled on every writer connection so rotate/remove cannot leave an old
value in free pages.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from math import log2
from pathlib import Path

from hyprial.identity.impl.agents.runtime.secrets import (
    SecretEnvironmentNameRejected,
    validate_secret_environment_name,
)
from hyprial.kernel import (
    PrivateAccessUnsupported,
    harden_private_path,
    private_metadata_violation,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
  service        TEXT    NOT NULL,
  field          TEXT    NOT NULL,
  value          TEXT    NOT NULL,
  fingerprint    TEXT    NOT NULL,
  updated_at_ms  INTEGER NOT NULL,
  PRIMARY KEY (service, field)
);

CREATE TABLE IF NOT EXISTS secret_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  at_ms       INTEGER NOT NULL,
  actor       TEXT    NOT NULL,
  via         TEXT    NOT NULL,
  action      TEXT    NOT NULL,
  service     TEXT    NOT NULL,
  field       TEXT,
  fingerprint TEXT,
  result      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS secret_events_no_update BEFORE UPDATE ON secret_events
BEGIN SELECT RAISE(ABORT, 'secret_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS secret_events_no_delete BEFORE DELETE ON secret_events
BEGIN SELECT RAISE(ABORT, 'secret_events is append-only'); END;
"""

_EVENT_ACTIONS = frozenset(
    {
        "set",
        "rotate",
        "remove",
        "materialize",
        "rejected",
        "revoke_remote",
    }
)
_LOW_ENTROPY_FIELD_PARTS = frozenset(
    {
        "PASSWORD",
        "PASS",
        "PASSPHRASE",
        "PASSCODE",
        "PASSWD",
        "PWD",
        "PIN",
        "PINCODE",
    }
)
_MIN_TOKEN_LENGTH = 16
_MIN_TOKEN_ENTROPY_BITS_PER_CHARACTER = 3.5
_HMAC_KEY_FILE = ".secret-hmac-key"
_HMAC_KEY_BYTES = 32
_HMAC_KEY_EVENT_SERVICE = "agent-secret-key"
_HEX_CHARACTERS = frozenset("0123456789abcdef")


class FingerprintUnavailable(ValueError):
    """The per-agent HMAC key is absent or fails its integrity checks."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        suffix = "" if reason is None else f"{{{reason}}}"
        super().__init__(f"fingerprint_unavailable{suffix}")


def _fingerprint_kind(fingerprint: str) -> str:
    if fingerprint.startswith("hmac16:"):
        digest = fingerprint.removeprefix("hmac16:")
        if len(digest) == 16 and set(digest) <= _HEX_CHARACTERS:
            return "hmac16"
    if len(fingerprint) == 16 and set(fingerprint) <= _HEX_CHARACTERS:
        return "legacy_sha"
    length, separator, _tail = fingerprint.partition(",tail=")
    if separator and length.startswith("len=") and length.removeprefix("len=").isdigit():
        return "legacy_plaintext_tail"
    return "unknown"


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """One current value; diagnostics do not reveal plaintext."""

    service: str
    field: str
    value: str = dataclass_field(repr=False)
    fingerprint: str
    updated_at_ms: int

    @property
    def fingerprint_kind(self) -> str:
        """Classify the persisted marker without exposing the stored value."""

        return _fingerprint_kind(self.fingerprint)


@dataclass(frozen=True, slots=True)
class SecretEvent:
    """One append-only operation record without plaintext."""

    seq: int
    at_ms: int
    actor: str
    via: str
    action: str
    service: str
    field: str | None
    fingerprint: str | None
    result: str

    @property
    def fingerprint_kind(self) -> str | None:
        """Classify a persisted marker for compatible history rendering."""

        return None if self.fingerprint is None else _fingerprint_kind(self.fingerprint)


def secret_fingerprint(
    field: str, value: str, *, hmac_key: bytes | None = None
) -> str:
    """Derive the specified non-secret comparison marker for one value.

    The conservative form is the default. A bare hash is used only when the
    value itself is at least 16 ASCII characters, contains no whitespace, and
    has at least 3.5 bits of observed Shannon entropy per character. A
    password/PIN-shaped field always forces the conservative form; its name
    can never promote a value into the hash form.
    """

    if not isinstance(field, str) or not isinstance(value, str):
        raise TypeError("secret fingerprint field and value must be strings")
    normalized = value.removesuffix("\n")
    password_like = bool(_LOW_ENTROPY_FIELD_PARTS.intersection(field.split("_")))
    if password_like or not _value_is_token_like(normalized):
        if not isinstance(hmac_key, bytes) or len(hmac_key) != _HMAC_KEY_BYTES:
            raise FingerprintUnavailable()
        digest = hmac.new(
            hmac_key, normalized.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]
        return f"hmac16:{digest}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _value_is_token_like(value: str) -> bool:
    if (
        len(value) < _MIN_TOKEN_LENGTH
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        return False
    counts = Counter(value)
    length = len(value)
    entropy = -sum(
        (count / length) * log2(count / length) for count in counts.values()
    )
    return entropy >= _MIN_TOKEN_ENTROPY_BITS_PER_CHARACTER


def agent_secret_store_exists(hyprial_home: Path, agent: str) -> bool:
    """Return whether a real store file exists without initializing anything."""

    from hyprial.identity.impl.agents.registry._core import AgentRegistry

    root = Path(hyprial_home)
    if not root.is_absolute():
        raise ValueError("hyprial home must be absolute")
    name = AgentRegistry.normalize_actor(agent)
    agent_home = root / "agents" / name
    try:
        home_metadata = agent_home.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("agent home existence could not be verified") from None
    if stat.S_ISLNK(home_metadata.st_mode) or not stat.S_ISDIR(home_metadata.st_mode):
        return False
    try:
        database_metadata = (agent_home / "secrets.sqlite").lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("secret store existence could not be verified") from None
    return stat.S_ISREG(database_metadata.st_mode) and not stat.S_ISLNK(
        database_metadata.st_mode
    )


class AgentSecretStore:
    """Own one ``secrets.sqlite`` under an already provisioned agent home."""

    def __init__(self, agent_home: Path) -> None:
        home = Path(agent_home)
        if not home.is_absolute():
            raise ValueError("agent home must be absolute")
        self.agent_home = home
        self.path = home / "secrets.sqlite"
        self.key_path = home / _HMAC_KEY_FILE
        self._initialize()

    @property
    def fingerprint_available(self) -> bool:
        """Return whether the existing private HMAC key can be read safely."""

        try:
            self._hmac_key(create=False)
        except FingerprintUnavailable:
            return False
        return True

    def fingerprint(self, field: str, value: str, *, create_key: bool = False) -> str:
        """Fingerprint a value with this store's key and shared normalization."""

        key = self._hmac_key(create=create_key)
        return secret_fingerprint(field, value, hmac_key=key)

    def set(
        self,
        *,
        service: str,
        field: str,
        value: str,
        actor: str,
        via: str,
        at_ms: int,
    ) -> StoredSecret:
        """Insert or replace one current value and append one ``set`` event."""

        self._validate_write(service, field, value, actor, via, at_ms)
        fingerprint = self._fingerprint_for_write(
            field,
            value,
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(service, field) DO UPDATE SET
                  value = excluded.value,
                  fingerprint = excluded.fingerprint,
                  updated_at_ms = excluded.updated_at_ms
                """,
                (service, field, value, fingerprint, at_ms),
            )
            self._insert_event(
                connection,
                at_ms=at_ms,
                actor=actor,
                via=via,
                action="set",
                service=service,
                field=field,
                fingerprint=fingerprint,
                result="ok",
            )
        return StoredSecret(service, field, value, fingerprint, at_ms)

    def rotate(
        self,
        *,
        service: str,
        field: str,
        value: str,
        actor: str,
        via: str,
        at_ms: int,
    ) -> StoredSecret:
        """Replace an existing value and retain only its old fingerprint."""

        self._validate_write(service, field, value, actor, via, at_ms)
        fingerprint = self._fingerprint_for_write(
            field,
            value,
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE secrets
                SET value = ?, fingerprint = ?, updated_at_ms = ?
                WHERE service = ? AND field = ?
                """,
                (value, fingerprint, at_ms, service, field),
            )
            if cursor.rowcount != 1:
                raise KeyError((service, field))
            self._insert_event(
                connection,
                at_ms=at_ms,
                actor=actor,
                via=via,
                action="rotate",
                service=service,
                field=field,
                fingerprint=fingerprint,
                result="ok",
            )
        return StoredSecret(service, field, value, fingerprint, at_ms)

    def remove(
        self,
        *,
        service: str,
        field: str | None,
        actor: str,
        via: str,
        at_ms: int,
    ) -> tuple[StoredSecret, ...]:
        """Delete matching values and append one ``remove`` event per row."""

        self._validate_event_inputs(
            action="remove",
            service=service,
            field=field,
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        with self._connect() as connection:
            if field is None:
                rows = connection.execute(
                    """
                    SELECT service, field, value, fingerprint, updated_at_ms
                    FROM secrets WHERE service = ? ORDER BY field
                    """,
                    (service,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT service, field, value, fingerprint, updated_at_ms
                    FROM secrets WHERE service = ? AND field = ?
                    """,
                    (service, field),
                ).fetchall()
            removed = tuple(self._stored_secret(row) for row in rows)
            for secret in removed:
                connection.execute(
                    "DELETE FROM secrets WHERE service = ? AND field = ?",
                    (secret.service, secret.field),
                )
                self._insert_event(
                    connection,
                    at_ms=at_ms,
                    actor=actor,
                    via=via,
                    action="remove",
                    service=secret.service,
                    field=secret.field,
                    fingerprint=(
                        None
                        if secret.fingerprint_kind == "legacy_plaintext_tail"
                        else secret.fingerprint
                    ),
                    result="ok",
                )
        return removed

    def record_event(
        self,
        *,
        action: str,
        service: str,
        field: str | None,
        actor: str,
        via: str,
        at_ms: int,
        result: str = "ok",
    ) -> SecretEvent:
        """Append a non-value event; remote revoke does not mutate the row."""

        self._validate_event_inputs(
            action=action,
            service=service,
            field=field,
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        if not isinstance(result, str) or not result:
            raise ValueError("secret event result must not be blank")
        with self._connect() as connection:
            fingerprint = None
            if action == "revoke_remote" and field is not None:
                row = connection.execute(
                    """
                    SELECT fingerprint FROM secrets
                    WHERE service = ? AND field = ?
                    """,
                    (service, field),
                ).fetchone()
                fingerprint = None if row is None else str(row[0])
                if (
                    fingerprint is not None
                    and _fingerprint_kind(fingerprint) == "legacy_plaintext_tail"
                ):
                    fingerprint = None
            cursor = self._insert_event(
                connection,
                at_ms=at_ms,
                actor=actor,
                via=via,
                action=action,
                service=service,
                field=field,
                fingerprint=fingerprint,
                result=result,
            )
            seq = int(cursor.lastrowid)
        return SecretEvent(
            seq, at_ms, actor, via, action, service, field, fingerprint, result
        )

    def get(self, service: str, field: str) -> StoredSecret | None:
        """Read one current table row; event history never hides it."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT service, field, value, fingerprint, updated_at_ms
                FROM secrets WHERE service = ? AND field = ?
                """,
                (service, field),
            ).fetchone()
        return None if row is None else self._stored_secret(row)

    def list(self, *, service: str | None = None) -> tuple[StoredSecret, ...]:
        """Read current rows in stable key order."""

        with self._connect() as connection:
            if service is None:
                rows = connection.execute(
                    """
                    SELECT service, field, value, fingerprint, updated_at_ms
                    FROM secrets ORDER BY service, field
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT service, field, value, fingerprint, updated_at_ms
                    FROM secrets WHERE service = ? ORDER BY field
                    """,
                    (service,),
                ).fetchall()
        return tuple(self._stored_secret(row) for row in rows)

    def history(self, *, service: str | None = None) -> tuple[SecretEvent, ...]:
        """Read append-only event rows in sequence order."""

        with self._connect() as connection:
            if service is None:
                rows = connection.execute(
                    "SELECT * FROM secret_events ORDER BY seq"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM secret_events WHERE service = ? ORDER BY seq",
                    (service,),
                ).fetchall()
        return tuple(SecretEvent(*row) for row in rows)

    def _initialize(self) -> None:
        self._require_private_home()
        created = False
        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
            created = True
        try:
            if created:
                harden_private_path(self.path, mode=0o600)
            self._require_private_database()
            with self._connect(initialize=False) as connection:
                connection.executescript(_SCHEMA)
        except PrivateAccessUnsupported:
            raise ValueError("private access is unsupported for secrets.sqlite") from None

    def _hmac_key(self, *, create: bool) -> bytes:
        if create:
            return self._establish_hmac_key(
                actor="system",
                via="daemon",
                at_ms=0,
            )
        with self._connect() as connection:
            established = self._hmac_key_event(connection)
        key = self._read_hmac_key()
        if established is not None:
            self._require_matching_hmac_key(key, established)
        return key

    def _fingerprint_for_write(
        self,
        field: str,
        value: str,
        *,
        actor: str,
        via: str,
        at_ms: int,
    ) -> str:
        key = self._establish_hmac_key(
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        return secret_fingerprint(field, value, hmac_key=key)

    def fingerprint_matches(self, field: str, value: str, expected: str) -> bool:
        """Verify one marker only with the scheme encoded by that marker."""

        kind = _fingerprint_kind(expected)
        if kind == "hmac16":
            key = self._hmac_key(create=False)
            normalized = value.removesuffix("\n")
            digest = hmac.new(
                key, normalized.encode("utf-8"), hashlib.sha256
            ).hexdigest()[:16]
            calculated = f"hmac16:{digest}"
        elif kind == "legacy_sha":
            normalized = value.removesuffix("\n")
            calculated = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        elif kind == "legacy_plaintext_tail":
            calculated = f"len={len(value)},tail={value[-4:]}"
        else:
            return False
        return hmac.compare_digest(calculated, expected)

    def _read_hmac_key(self) -> bytes:
        try:
            metadata = self.key_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise FingerprintUnavailable()
            violation = private_metadata_violation(
                self.key_path, metadata, mode=0o600
            )
            if violation is not None:
                raise FingerprintUnavailable()
            key = self.key_path.read_bytes()
        except FingerprintUnavailable:
            raise
        except (OSError, PrivateAccessUnsupported):
            raise FingerprintUnavailable() from None
        if len(key) != _HMAC_KEY_BYTES:
            raise FingerprintUnavailable()
        return key

    def _establish_hmac_key(
        self,
        *,
        actor: str,
        via: str,
        at_ms: int,
    ) -> bytes:
        """Create or adopt K and append its one durable lifecycle event."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            established = self._hmac_key_event(connection)
            if established is not None:
                key = self._read_hmac_key()
                self._require_matching_hmac_key(key, established)
                return key

            has_hmac_fingerprint = connection.execute(
                """
                SELECT 1 FROM secrets WHERE fingerprint LIKE 'hmac16:%'
                UNION ALL
                SELECT 1 FROM secret_events
                WHERE action != 'hmac_key_created'
                  AND fingerprint LIKE 'hmac16:%'
                LIMIT 1
                """
            ).fetchone()
            try:
                key = self._read_hmac_key()
            except FingerprintUnavailable:
                if has_hmac_fingerprint is not None:
                    raise
                key = self._create_hmac_key()
            key_fp16 = hashlib.sha256(key).hexdigest()[:16]
            self._insert_event(
                connection,
                at_ms=at_ms,
                actor=actor,
                via=via,
                action="hmac_key_created",
                service=_HMAC_KEY_EVENT_SERVICE,
                field=None,
                fingerprint=key_fp16,
                result="ok",
            )
            return key

    @staticmethod
    def _hmac_key_event(connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            """
            SELECT fingerprint FROM secret_events
            WHERE action = 'hmac_key_created'
            ORDER BY seq DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return "" if row[0] is None else str(row[0])

    @staticmethod
    def _require_matching_hmac_key(key: bytes, expected: str) -> None:
        actual = hashlib.sha256(key).hexdigest()[:16]
        if not hmac.compare_digest(actual, expected):
            raise FingerprintUnavailable("key_mismatch")

    def _create_hmac_key(self) -> bytes:
        key = secrets.token_bytes(_HMAC_KEY_BYTES)
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created = True
            written = 0
            while written < len(key):
                written += os.write(descriptor, key[written:])
            os.fsync(descriptor)
        except FileExistsError:
            return self._read_hmac_key()
        except OSError:
            if created:
                try:
                    self.key_path.unlink()
                except OSError:
                    pass
            raise FingerprintUnavailable() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            harden_private_path(self.key_path, mode=0o600)
            return self._read_hmac_key()
        except (OSError, PrivateAccessUnsupported, FingerprintUnavailable):
            try:
                self.key_path.unlink()
            except OSError:
                pass
            raise FingerprintUnavailable() from None

    def _connect(self, *, initialize: bool = True) -> sqlite3.Connection:
        if initialize:
            self._require_private_home()
            self._require_private_database()
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _require_private_home(self) -> None:
        try:
            metadata = self.agent_home.lstat()
        except OSError:
            raise ValueError("agent home must be an existing private directory") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("agent home must be an existing private directory")
        try:
            violation = private_metadata_violation(
                self.agent_home, metadata, mode=0o700
            )
        except OSError:
            raise ValueError("agent home privacy could not be verified") from None
        if violation is not None:
            raise ValueError(f"agent home is not private: {violation}")

    def _require_private_database(self) -> None:
        try:
            metadata = self.path.lstat()
        except OSError:
            raise ValueError("secrets.sqlite must be an existing private file") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("secrets.sqlite must be a regular file")
        try:
            violation = private_metadata_violation(self.path, metadata, mode=0o600)
        except OSError:
            raise ValueError("secrets.sqlite privacy could not be verified") from None
        if violation is not None:
            raise ValueError(f"secrets.sqlite is not private: {violation}")

    def _validate_write(
        self,
        service: str,
        field: str,
        value: str,
        actor: str,
        via: str,
        at_ms: int,
    ) -> None:
        self._validate_event_inputs(
            action="set",
            service=service,
            field=field,
            actor=actor,
            via=via,
            at_ms=at_ms,
        )
        if not isinstance(value, str):
            raise TypeError("secret value must be a string")
        try:
            validate_secret_environment_name(field)
        except SecretEnvironmentNameRejected as error:
            self.record_event(
                action=error.event.action,
                service=service,
                field=error.event.field,
                actor=actor,
                via=via,
                at_ms=at_ms,
                result=error.event.result,
            )
            raise

    @staticmethod
    def _validate_event_inputs(
        *,
        action: str,
        service: str,
        field: str | None,
        actor: str,
        via: str,
        at_ms: int,
    ) -> None:
        if action not in _EVENT_ACTIONS:
            raise ValueError("unknown secret event action")
        if not isinstance(service, str) or not service:
            raise ValueError("secret service must not be blank")
        if field is not None and (not isinstance(field, str) or not field):
            raise ValueError("secret field must not be blank")
        if not isinstance(actor, str) or not actor:
            raise ValueError("secret event actor must not be blank")
        if via not in {"cli", "daemon"}:
            raise ValueError("secret event via must be cli or daemon")
        if not isinstance(at_ms, int) or isinstance(at_ms, bool) or at_ms < 0:
            raise ValueError("secret event timestamp must be a non-negative integer")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        at_ms: int,
        actor: str,
        via: str,
        action: str,
        service: str,
        field: str | None,
        fingerprint: str | None,
        result: str,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (at_ms, actor, via, action, service, field, fingerprint, result),
        )

    @staticmethod
    def _stored_secret(row: tuple[object, ...]) -> StoredSecret:
        return StoredSecret(
            service=str(row[0]),
            field=str(row[1]),
            value=str(row[2]),
            fingerprint=str(row[3]),
            updated_at_ms=int(row[4]),
        )
