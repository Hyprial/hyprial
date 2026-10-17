"""AT06 second slice: the target's offline trust anchor for bundle signatures.

``signature.py`` answers *"does this bundle digest verify under key X"*; it
deliberately takes the public key through an injected resolver so it never has
to decide who is allowed to sign.  This module is that decision: a small,
operator-managed, **offline** set of source public keys.

Why a local file and not a directory service: the target has to verify a bundle
that arrives from a machine that may be gone, offline, or already destroyed by
the migration.  A trust anchor that needs the source to be reachable cannot
verify a transfer that happened yesterday, and a failed DNS/JWKS fetch must
never turn into "assume trusted".  ``trusted-source-keys.json`` is loaded from
the target's own home, so the only failure mode of a missing file is refusing
every signature -- fail-closed, which is what this lane wants.

Shape (card 399, design draft ``.artifacts/at06/keyed-signature-design-001.md``
section 11):

* one record per ``(keyId, actor)`` pair: the public half, the actor and source
  machine it is meant to identify, when it was added, and an optional
  ``retiredAt``;
* ``keyId`` is never free-form: it must be the hash of the public half
  (``signature.public_key_id``), so a record cannot claim someone else's id;
* retirement is *recorded, not enforced here*: ``now`` decides whether a key is
  past its ``retiredAt``, and the caller picks the policy (warn by default,
  refuse when configured).  A bundle already in flight when the clock rolls
  past ``retiredAt`` must not silently become unlandable.

Library-only first slice, like ``signature.py``: nothing on the production path
imports this yet.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from hyprial.kernel import atomic_json_write

from hyprial.daemon.impl.transfer.archive.signature import (
    KEY_ID_PREFIX,
    ensure_private_directory,
    public_key_id,
)

__all__ = [
    "RETIREMENT_REFUSE",
    "RETIREMENT_WARN",
    "TRUST_STORE_RELPATH",
    "TRUST_STORE_SCHEMA_VERSION",
    "TrustStore",
    "TrustStoreError",
    "TrustStoreFormatError",
    "TrustStoreKeyConflict",
    "TrustStoreUnknownKey",
    "TrustedSourceKey",
    "add_trusted_source_key",
    "load_trust_store",
    "retire_trusted_source_key",
    "trust_store_path",
    "write_trust_store",
]

TRUST_STORE_SCHEMA_VERSION = 1
TRUST_STORE_RELPATH = "secrets/transfer/trusted-source-keys.json"
#: A key past its ``retiredAt`` still resolves; the gate records a notice.
RETIREMENT_WARN = "warn"
#: A key past its ``retiredAt`` is refused outright.
RETIREMENT_REFUSE = "refuse"
RETIREMENT_POLICIES = (RETIREMENT_WARN, RETIREMENT_REFUSE)

_KEY_FIELDS = frozenset(
    {"keyId", "publicKey", "actor", "sourceMachine", "addedAt", "retiredAt"}
)


class TrustStoreError(Exception):
    """Base class for trust-anchor failures."""

    code = "trust_store_error"


class TrustStoreFormatError(TrustStoreError):
    """The trust store (or one record in it) is not well formed."""

    code = "trust_store_malformed"


class TrustStoreKeyConflict(TrustStoreError):
    """The same key id is registered against different material or identity."""

    code = "trust_store_key_conflict"


class TrustStoreUnknownKey(TrustStoreError):
    """The key id the operation named is not registered."""

    code = "trust_store_unknown_key"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, label: str, *, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise TrustStoreFormatError(f"{label} must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise TrustStoreFormatError(f"{label} is not valid base64") from error
    if length is not None and len(raw) != length:
        raise TrustStoreFormatError(f"{label} must be {length} bytes")
    return raw


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrustStoreFormatError(f"{label} must be a non-empty string")
    return value


def _timestamp(value: object, label: str) -> datetime:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise TrustStoreFormatError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise TrustStoreFormatError(f"{label} must carry a UTC offset")
    return parsed


@dataclass(frozen=True)
class TrustedSourceKey:
    """One source machine's signature public key, as the target records it."""

    key_id: str
    public_key: bytes
    actor: str
    source_machine: str
    added_at: str
    retired_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        actor: str,
        source_machine: str,
        public_key: bytes,
        added_at: str,
        retired_at: str | None = None,
    ) -> "TrustedSourceKey":
        """Build a record, deriving ``keyId`` from the key itself."""

        _timestamp(added_at, "addedAt")
        if retired_at is not None:
            _timestamp(retired_at, "retiredAt")
        return cls(
            key_id=public_key_id(public_key),
            public_key=public_key,
            actor=_required_text(actor, "actor"),
            source_machine=_required_text(source_machine, "sourceMachine"),
            added_at=added_at,
            retired_at=retired_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "keyId": self.key_id,
            "publicKey": _b64(self.public_key),
            "actor": self.actor,
            "sourceMachine": self.source_machine,
            "addedAt": self.added_at,
            "retiredAt": self.retired_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "TrustedSourceKey":
        if not isinstance(raw, Mapping):
            raise TrustStoreFormatError("a trusted key record must be an object")
        if set(raw) != _KEY_FIELDS:
            raise TrustStoreFormatError(
                "a trusted key record does not have exactly the frozen fields"
            )
        key_id = _required_text(raw.get("keyId"), "keyId")
        if not key_id.startswith(KEY_ID_PREFIX):
            raise TrustStoreFormatError(f"keyId must start with {KEY_ID_PREFIX!r}")
        public_key = _unb64(raw.get("publicKey"), "publicKey", length=32)
        if public_key_id(public_key) != key_id:
            raise TrustStoreFormatError(
                "the recorded public key does not hash to the recorded keyId"
            )
        added_at = _required_text(raw.get("addedAt"), "addedAt")
        _timestamp(added_at, "addedAt")
        retired_raw = raw.get("retiredAt")
        if retired_raw is not None:
            _timestamp(retired_raw, "retiredAt")
        return cls(
            key_id=key_id,
            public_key=public_key,
            actor=_required_text(raw.get("actor"), "actor"),
            source_machine=_required_text(raw.get("sourceMachine"), "sourceMachine"),
            added_at=added_at,
            retired_at=None if retired_raw is None else str(retired_raw),
        )

    def identity(self) -> tuple[str, str]:
        """What a re-registration must agree on to be idempotent."""

        return (self.actor, self.source_machine)


@dataclass(frozen=True)
class TrustStore:
    """The target's whole offline trust anchor."""

    keys: tuple[TrustedSourceKey, ...] = ()
    retirement_policy: str = RETIREMENT_WARN
    schema_version: int = TRUST_STORE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "retirementPolicy": self.retirement_policy,
            "keys": [key.as_dict() for key in self.keys],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "TrustStore":
        if not isinstance(raw, Mapping):
            raise TrustStoreFormatError("the trust store must be an object")
        if set(raw) != {"schemaVersion", "retirementPolicy", "keys"}:
            raise TrustStoreFormatError(
                "the trust store does not have exactly the frozen fields"
            )
        if raw.get("schemaVersion") != TRUST_STORE_SCHEMA_VERSION:
            raise TrustStoreFormatError("unsupported trust store schemaVersion")
        policy = raw.get("retirementPolicy")
        if policy not in RETIREMENT_POLICIES:
            raise TrustStoreFormatError(
                f"retirementPolicy must be one of {list(RETIREMENT_POLICIES)}"
            )
        keys_raw = raw.get("keys")
        if not isinstance(keys_raw, list):
            raise TrustStoreFormatError("keys must be a list")
        keys: list[TrustedSourceKey] = []
        seen: dict[str, TrustedSourceKey] = {}
        for item in keys_raw:
            key = TrustedSourceKey.from_dict(item)
            existing = seen.get(key.key_id)
            if existing is not None:
                raise TrustStoreFormatError(
                    f"keyId {key.key_id!r} is registered more than once"
                )
            seen[key.key_id] = key
            keys.append(key)
        return cls(keys=tuple(keys), retirement_policy=str(policy))

    def key(self, key_id: str) -> TrustedSourceKey | None:
        for candidate in self.keys:
            if candidate.key_id == key_id:
                return candidate
        return None

    def resolver(self) -> Callable[[str], bytes | None]:
        """The callable ``verify_bundle_signature`` wants.

        A retired key still resolves: retirement is a policy question the gate
        answers with ``retired_at``/``retirement_policy``, not a second way for
        a key to disappear.  An unknown key resolves to ``None`` -- the caller
        turns that into a refusal.
        """

        def resolve(key_id: str) -> bytes | None:
            found = self.key(key_id)
            return None if found is None else found.public_key

        return resolve

    def is_retired(self, key_id: str, *, now: datetime) -> bool:
        if now.tzinfo is None:
            raise TrustStoreFormatError("now must carry a UTC offset")
        found = self.key(key_id)
        if found is None or found.retired_at is None:
            return False
        return now >= _timestamp(found.retired_at, "retiredAt")

    def retirement_notice(self, key_id: str, *, now: datetime) -> str | None:
        """A human line when the key is past its ``retiredAt``, else ``None``."""

        found = self.key(key_id)
        if found is None or not self.is_retired(key_id, now=now):
            return None
        return (
            f"{key_id} was retired at {found.retired_at} and signed this "
            f"bundle anyway; retirementPolicy={self.retirement_policy}"
        )

    def with_key(self, key: TrustedSourceKey) -> "TrustStore":
        """Register ``key``; re-registering the same material is a no-op."""

        existing = self.key(key.key_id)
        if existing is not None:
            if existing.identity() != key.identity():
                raise TrustStoreKeyConflict(
                    f"{key.key_id} is already registered to "
                    f"{existing.actor!r}/{existing.source_machine!r}; refusing to "
                    f"re-point it at {key.actor!r}/{key.source_machine!r}"
                )
            if existing.retired_at == key.retired_at:
                return self
            if key.retired_at is not None:
                raise TrustStoreKeyConflict(
                    f"{key.key_id} is already registered; use retire/reinstate "
                    "instead of re-registering it as retired"
                )
            return replace(
                self,
                keys=tuple(
                    key if candidate.key_id == key.key_id else candidate
                    for candidate in self.keys
                ),
            )
        return replace(self, keys=(*self.keys, key))

    def retiring(self, key_id: str, *, retired_at: str) -> "TrustStore":
        _timestamp(retired_at, "retiredAt")
        existing = self.key(key_id)
        if existing is None:
            raise TrustStoreUnknownKey(f"no trusted source key {key_id!r}")
        return replace(
            self,
            keys=tuple(
                replace(candidate, retired_at=retired_at)
                if candidate.key_id == key_id
                else candidate
                for candidate in self.keys
            ),
        )


def trust_store_path(home: Path) -> Path:
    """Where this target keeps the keys it will accept (0600)."""

    return Path(home) / TRUST_STORE_RELPATH


def load_trust_store(path: Path) -> TrustStore:
    """Read the store; a missing file is an empty store, never a wildcard.

    An empty store refuses every signature, so a mistyped or absent path
    degrades to "trust nobody" instead of "trust anybody".
    """

    path = Path(path)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return TrustStore()
    except ValueError as error:
        raise TrustStoreFormatError(
            f"trust store at {path} is not valid JSON"
        ) from error
    if not isinstance(raw, Mapping):
        raise TrustStoreFormatError("the trust store must be an object")
    return TrustStore.from_dict(raw)


def write_trust_store(path: Path, store: TrustStore) -> None:
    """Persist the store at mode 0600 under 0700 secret directories."""

    path = Path(path)
    ensure_private_directory(path.parent)
    atomic_json_write(path, store.as_dict())


def add_trusted_source_key(
    path: Path,
    *,
    actor: str,
    source_machine: str,
    public_key: bytes,
    added_at: str,
) -> TrustStore:
    """Enroll one source key; returns the store as written."""

    store = load_trust_store(path).with_key(
        TrustedSourceKey.create(
            actor=actor,
            source_machine=source_machine,
            public_key=public_key,
            added_at=added_at,
        )
    )
    write_trust_store(path, store)
    return store


def retire_trusted_source_key(
    path: Path, key_id: str, *, retired_at: str
) -> TrustStore:
    """Mark one key as retired (a record, not a delete: audits need the line)."""

    store = load_trust_store(path).retiring(key_id, retired_at=retired_at)
    write_trust_store(path, store)
    return store
