"""Receiver-owned Matrix credentials and injectable provider ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{name} exceeds 4096 bytes")
    return value


def normalize_homeserver(value: str) -> str:
    """Return a credential-free HTTPS Matrix homeserver origin."""

    parsed = urlsplit(_required(value, "homeserver"))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Matrix homeserver must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Matrix homeserver must not contain user info")
    if parsed.query or parsed.fragment:
        raise ValueError("Matrix homeserver must not contain query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("Matrix homeserver must not contain a path")
    host = parsed.hostname.lower()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(("https", host, "", "", ""))


REVIEWED_MATRIX_CRYPTO_DESCRIPTOR = MappingProxyType({
    "matrix_nio": MappingProxyType({
        "package": "matrix-nio[e2e]",
        "version": "0.26.0",
        "artifact": "wheel",
        "artifact_sha256": "1803d7ba6dbc54a101a02695a5a767a1fb53632e46aced9d116990f4a98eadf3",
        "source_commit": "331c93ed3e9ca86e7444b4dfdf05086654842ace",
        "license": "ISC",
    }),
    "vodozemac": MappingProxyType({
        "package": "vodozemac",
        "version": "0.10.0",
        "artifact": "sdist",
        "artifact_sha256": "9602edb87ab7d16e7b4aad5a087473d8fb6b3ee99043f9aaf0f54bb78970dde9",
        "source_commit": "810836143ea0ca36f1f849f43e63331990c6575a",
        "license": "MIT",
        "embedded_rust": MappingProxyType({
            "package": "vodozemac",
            "version": "0.10.0",
            "crate_sha256": "b98bf83c0992966775b8012f194b07b44928996163e5a05b741b43891571ae5b",
            "license": "Apache-2.0",
        }),
    }),
})


def crypto_descriptor_is_reviewed(crypto: object) -> bool:
    """Require the exact dependency identity reviewed by the lock owner."""

    try:
        descriptor = getattr(crypto, "descriptor", None)
    except Exception:
        return False
    if type(descriptor) is not dict:
        return False
    if not all(type(component) is dict for component in descriptor.values()):
        return False
    embedded = descriptor.get("vodozemac", {}).get("embedded_rust")
    if type(embedded) is not dict:
        return False
    return descriptor == REVIEWED_MATRIX_CRYPTO_DESCRIPTOR


def freeze_room_encryption_evidence(
    instance_id: str, generation: int, room_id: str, encrypted: bool
) -> str:
    """Bind authoritative room encryption state to one instance generation and room."""

    _required(instance_id, "instance_id")
    _required(room_id, "room_id")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be positive")
    if not isinstance(encrypted, bool):
        raise TypeError("encrypted must be a boolean")
    state = "encrypted" if encrypted else "plaintext"
    digest = hashlib.sha256(
        (
            "hyprial-matrix-room-encryption-v1\0"
            f"{instance_id}\0{generation}\0{room_id}\0{state}"
        ).encode("utf-8")
    ).hexdigest()
    return f"matrix-room-encryption-v1:{state}:{digest}"


def read_room_encryption_evidence(
    evidence: object, instance_id: str, generation: int, room_id: str
) -> bool:
    """Validate frozen evidence and return its authoritative encryption state."""

    if not isinstance(evidence, str):
        raise ValueError("Matrix room encryption evidence is required")
    for encrypted in (False, True):
        if evidence == freeze_room_encryption_evidence(
            instance_id, generation, room_id, encrypted
        ):
            return encrypted
    raise ValueError("Matrix room encryption evidence mismatch")


@dataclass(frozen=True, slots=True)
class MatrixCredentials:
    instance_id: str
    organization_id: str
    generation: int
    homeserver: str
    user_id: str
    device_id: str | None
    access_token: str | None
    password: str | None

    def __post_init__(self) -> None:
        for name in ("instance_id", "organization_id", "user_id"):
            _required(getattr(self, name), name)
        if self.generation < 1:
            raise ValueError("generation must be positive")
        object.__setattr__(self, "homeserver", normalize_homeserver(self.homeserver))
        if self.device_id is not None:
            _required(self.device_id, "device_id")
        if (self.access_token is None) == (self.password is None):
            raise ValueError("exactly one Matrix access token or password is required")
        _required(self.access_token or self.password or "", "Matrix credential")

    def __repr__(self) -> str:
        auth_kind = "access-token" if self.access_token is not None else "password"
        return (
            "MatrixCredentials("
            f"instance_id={self.instance_id!r}, organization_id={self.organization_id!r}, "
            f"generation={self.generation!r}, homeserver={self.homeserver!r}, "
            f"user_id={self.user_id!r}, device_id={self.device_id!r}, "
            f"auth_kind={auth_kind!r}, secret=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class MatrixOutboundRecord:
    room_id: str
    event_id: str | None
    payload_digest: str
    outcome: str

    def __post_init__(self) -> None:
        _required(self.room_id, "room_id")
        _required(self.payload_digest, "payload_digest")
        if self.outcome not in ("accepted", "unknown"):
            raise ValueError("Matrix outbound outcome must be accepted or unknown")
        if self.outcome == "accepted":
            _required(self.event_id or "", "event_id")
        elif self.event_id is not None:
            raise ValueError("unknown Matrix outbound outcome cannot have an event id")


class MatrixCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> MatrixCredentials: ...


class MatrixStateStore(Protocol):
    async def load_sync_token(self, instance_id: str, generation: int) -> str | None: ...

    async def commit_sync_token(
        self, instance_id: str, generation: int, token: str
    ) -> None: ...

    async def record_outbound(
        self,
        instance_id: str,
        generation: int,
        room_id: str,
        transaction_id: str,
        event_id: str | None,
        payload_digest: str,
        outcome: str,
    ) -> None: ...

    async def lookup_outbound_transaction(
        self, instance_id: str, generation: int, transaction_id: str
    ) -> MatrixOutboundRecord | None: ...


class MatrixTransport(Protocol):
    async def login(self, credentials: MatrixCredentials) -> object: ...

    async def whoami(self, credentials: MatrixCredentials) -> object: ...

    async def connect(self, credentials: MatrixCredentials) -> None: ...

    async def sync(self, since: str | None, *, timeout_ms: int) -> object: ...

    async def room_encryption(self, room_id: str) -> bool: ...

    async def send_event(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> object: ...

    async def reconcile_event(self, room_id: str, transaction_id: str) -> object: ...

    async def close(self) -> None: ...


class MatrixCryptoBackend(Protocol):
    @property
    def dependency(self) -> str: ...

    @property
    def descriptor(self) -> object: ...

    async def prepare(self, instance_id: str, generation: int) -> bool: ...

    async def room_ready(self, room_id: str) -> bool: ...

    async def decrypt(self, room_id: str, event: Mapping[str, object]) -> object: ...

    async def encrypt(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> tuple[str, object]: ...


__all__ = [
    "MatrixCredentialResolver",
    "MatrixCredentials",
    "MatrixCryptoBackend",
    "MatrixOutboundRecord",
    "MatrixStateStore",
    "MatrixTransport",
    "REVIEWED_MATRIX_CRYPTO_DESCRIPTOR",
    "crypto_descriptor_is_reviewed",
    "freeze_room_encryption_evidence",
    "normalize_homeserver",
    "read_room_encryption_evidence",
]
