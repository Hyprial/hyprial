"""Sealed service grants: one secret sealed to one remote daemon grant key.

WIP scope (owner-approved 2026-11-23): this module holds a sealed grant record
in daemon-private state.  It deliberately does not deliver the record to the
remote daemon yet; delivery/acknowledgement is a follow-up slice.  The plaintext
secret never enters argv, logs, the service catalog, OrgFS, or the record file.

The envelope reuses the same primitives as the identity key envelope (random
DEK, ephemeral X25519, HKDF-SHA256, AES-256-GCM) under its own domain string,
so a service-grant ciphertext cannot be replayed as an identity-key envelope.
The record is not signed: custody is the daemon-private directory (0700/0600,
no symlinks, atomic publish).  Signing and cross-device key discovery are
explicit follow-ups in the WIP design note.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hyprial.kernel import (
    PrivateAccessUnsupported,
    ipc_errors,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)

from .models import AccessSnapshot, RegistrySnapshot, ServiceConnectError


MAX_SECRET_BYTES = 16384
MAX_RECORD_BYTES = 65536
_GRANT_ID = re.compile(r"sg\.[a-z2-7]{26}\Z")
_RECIPIENT_PUBLIC_KEY = re.compile(r"x25519:[0-9a-f]{64}\Z")
_RECIPIENT_KEY_ID = re.compile(r"x25519:[0-9a-f]{32}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_DOMAIN = b"hyprial/service-grant/wrap/v1"
_HEADER_FIELDS = frozenset(
    {
        "v",
        "alg",
        "wrap",
        "grantId",
        "service",
        "deviceId",
        "recipientKeyId",
        "createdAt",
    }
)
_RECORD_FIELDS = frozenset({"header", "recipient", "payload"})
_RECIPIENT_FIELDS = frozenset({"recipientKeyId", "epk", "nonce", "wrappedDek"})
_PAYLOAD_FIELDS = frozenset({"nonce", "ciphertext"})


def _invalid(message: str = "service grant request is invalid") -> ServiceConnectError:
    return ServiceConnectError(ipc_errors.INVALID_ARGUMENT, message)


def _unavailable() -> ServiceConnectError:
    return ServiceConnectError(
        ipc_errors.SERVICE_UNAVAILABLE, "service grant is unavailable"
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, size: int | None = None, *, maximum: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_RECORD_BYTES:
        raise _unavailable()
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _unavailable() from error
    if (size is not None and len(result) != size) or (
        maximum is not None and len(result) > maximum
    ):
        raise _unavailable()
    return result


def recipient_key_id(public_key: bytes) -> str:
    """The stable id used in the record and returned to the operator."""

    return "x25519:" + hashlib.sha256(public_key).hexdigest()[:32]


def parse_recipient_key(value: object) -> tuple[str, bytes]:
    """Validate an explicit X25519 grant key; no trust discovery here."""

    if not isinstance(value, str) or _RECIPIENT_PUBLIC_KEY.fullmatch(value) is None:
        raise _invalid("recipientKey must be x25519:<64 lowercase hex>")
    try:
        public_key = bytes.fromhex(value[7:])
        X25519PublicKey.from_public_bytes(public_key)
    except ValueError as error:
        raise _invalid("recipientKey is not a usable X25519 public key") from error
    return recipient_key_id(public_key), public_key


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _unavailable()
    return value


def _kek(
    private: X25519PrivateKey,
    peer: bytes,
    ephemeral_public: bytes,
    recipient_public: bytes,
    aad: bytes,
    key_id_value: str,
) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
    except (ValueError, TypeError) as error:
        raise _unavailable() from error
    if shared == bytes(32):
        raise _unavailable()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral_public + recipient_public,
        info=_DOMAIN + b"\0" + aad + b"\0" + key_id_value.encode("ascii"),
    ).derive(shared)


def seal_grant(
    secret: bytes,
    *,
    grant_id: str,
    service: str,
    device_id: str,
    recipient_public: bytes,
    created_at: int,
) -> dict[str, object]:
    """Seal one payload to one X25519 public key under a fresh DEK."""

    if (
        type(secret) is not bytes
        or not 0 < len(secret) <= MAX_SECRET_BYTES
        or _GRANT_ID.fullmatch(grant_id) is None
        or _IDENTIFIER.fullmatch(service) is None
        or _IDENTIFIER.fullmatch(device_id) is None
        or type(created_at) is not int
        or created_at < 0
    ):
        raise _invalid()
    key_id_value = recipient_key_id(recipient_public)
    header = {
        "v": 1,
        "alg": "A256GCM",
        "wrap": "X25519-HKDF-SHA256-A256GCM",
        "grantId": grant_id,
        "service": service,
        "deviceId": device_id,
        "recipientKeyId": key_id_value,
        "createdAt": created_at,
    }
    aad = _canonical(header)
    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    payload = {
        "nonce": _b64(nonce),
        "ciphertext": _b64(AESGCM(dek).encrypt(nonce, secret, aad)),
    }
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes_raw()
    kek = _kek(
        ephemeral,
        recipient_public,
        ephemeral_public,
        recipient_public,
        aad,
        key_id_value,
    )
    wrap_nonce = secrets.token_bytes(12)
    wrapped = AESGCM(kek).encrypt(
        wrap_nonce, dek, aad + b"\0" + key_id_value.encode("ascii")
    )
    return {
        "header": header,
        "recipient": {
            "recipientKeyId": key_id_value,
            "epk": _b64(ephemeral_public),
            "nonce": _b64(wrap_nonce),
            "wrappedDek": _b64(wrapped),
        },
        "payload": payload,
    }


def _validate_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise _unavailable()
    header = record["header"]
    recipient = record["recipient"]
    payload = record["payload"]
    if (
        not isinstance(header, dict)
        or set(header) != _HEADER_FIELDS
        or not isinstance(recipient, dict)
        or set(recipient) != _RECIPIENT_FIELDS
        or not isinstance(payload, dict)
        or set(payload) != _PAYLOAD_FIELDS
    ):
        raise _unavailable()
    if (
        header["v"] != 1
        or header["alg"] != "A256GCM"
        or header["wrap"] != "X25519-HKDF-SHA256-A256GCM"
        or _GRANT_ID.fullmatch(str(header["grantId"])) is None
        or _IDENTIFIER.fullmatch(str(header["service"])) is None
        or _IDENTIFIER.fullmatch(str(header["deviceId"])) is None
        or _RECIPIENT_KEY_ID.fullmatch(str(header["recipientKeyId"])) is None
        or type(header["createdAt"]) is not int
        or header["createdAt"] < 0
        or recipient["recipientKeyId"] != header["recipientKeyId"]
    ):
        raise _unavailable()
    return record


def open_grant(record: object, *, private_key: bytes) -> bytes:
    """Verify and open one record for tests and a future recipient path."""

    value = _validate_record(record)
    header = value["header"]
    recipient = value["recipient"]
    assert isinstance(header, dict)
    assert isinstance(recipient, dict)
    if type(private_key) is not bytes or len(private_key) != 32:
        raise _unavailable()
    private = X25519PrivateKey.from_private_bytes(private_key)
    public = private.public_key().public_bytes_raw()
    key_id_value = recipient_key_id(public)
    if key_id_value != recipient["recipientKeyId"]:
        raise _unavailable()
    aad = _canonical(header)
    ephemeral_public = _unb64(recipient["epk"], 32)
    kek = _kek(
        private,
        ephemeral_public,
        ephemeral_public,
        public,
        aad,
        key_id_value,
    )
    try:
        dek = AESGCM(kek).decrypt(
            _unb64(recipient["nonce"], 12),
            _unb64(recipient["wrappedDek"], 48),
            aad + b"\0" + key_id_value.encode("ascii"),
        )
        payload = value["payload"]
        assert isinstance(payload, dict)
        return AESGCM(dek).decrypt(
            _unb64(payload["nonce"], 12),
            _unb64(
                payload["ciphertext"],
                maximum=MAX_SECRET_BYTES + 16,
            ),
            aad,
        )
    except (InvalidTag, ValueError):
        raise _unavailable() from None


def new_grant_id() -> str:
    value = base64.b32encode(secrets.token_bytes(16)).decode("ascii").lower()
    return "sg." + value.rstrip("=")


@dataclass(frozen=True, slots=True)
class GrantRecord:
    grant_id: str
    service: str
    device_id: str
    recipient_key_id: str
    created_at: int
    record: dict[str, object]

    def public_view(self) -> dict[str, object]:
        """Return only non-secret fields; the envelope stays daemon-private."""

        return {
            "grantId": self.grant_id,
            "service": self.service,
            "deviceId": self.device_id,
            "recipientKeyId": self.recipient_key_id,
            "createdAt": self.created_at,
        }


class GrantStore:
    """One daemon-private directory of immutable sealed grant records."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()

    def create(
        self,
        *,
        service: str,
        device_id: str,
        recipient_public: bytes,
        secret: bytes,
        created_at: int,
    ) -> GrantRecord:
        grant_id = new_grant_id()
        record = seal_grant(
            secret,
            grant_id=grant_id,
            service=service,
            device_id=device_id,
            recipient_public=recipient_public,
            created_at=created_at,
        )
        data = _canonical(record)
        if len(data) > MAX_RECORD_BYTES:
            raise _unavailable()
        try:
            secure_private_directory(self.root, create=True, mode=0o700)
            write_private_file_atomic(
                self.root / f"{grant_id}.json", data, mode=0o600, exclusive=True
            )
        except (OSError, PrivateAccessUnsupported) as error:
            raise _unavailable() from error
        return GrantRecord(
            grant_id=grant_id,
            service=service,
            device_id=device_id,
            recipient_key_id=recipient_key_id(recipient_public),
            created_at=created_at,
            record=record,
        )

    def read(self, grant_id: str) -> dict[str, object]:
        if _GRANT_ID.fullmatch(grant_id) is None:
            raise _invalid()
        try:
            payload, _ = read_private_file(
                self.root / f"{grant_id}.json", mode=0o600, max_bytes=MAX_RECORD_BYTES
            )
            value = json.loads(payload.decode("utf-8"))
        except (OSError, PrivateAccessUnsupported, UnicodeError, ValueError) as error:
            raise _unavailable() from error
        return _validate_record(value)


_LOG = logging.getLogger(__name__)


def _secret(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 32768:
        raise _invalid()
    try:
        secret = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _invalid() from error
    if not 0 < len(secret) <= MAX_SECRET_BYTES:
        raise _invalid()
    return secret


def grant_service(
    name: str,
    params: Mapping[str, object],
    access: AccessSnapshot,
    catalog: RegistrySnapshot,
    access_path: str | Path,
) -> dict[str, object]:
    """Resolve fail-closed, seal, store privately, and audit without the secret."""

    entry = catalog.entry(name)
    access.resolve_device(entry.device_id, entry.remote_port)
    key_id, public_key = parse_recipient_key(params.get("recipientKey"))
    secret = _secret(params.get("secretB64"))
    root = Path(access_path).with_name("service-grants")
    record = GrantStore(root).create(
        service=entry.name,
        device_id=entry.device_id,
        recipient_public=public_key,
        secret=secret,
        created_at=time.time_ns() // 1_000_000,
    )
    _LOG.info(
        "service.grant.created",
        extra={
            "event": "service.grant.created",
            "grantId": record.grant_id,
            "serviceName": entry.name,
            "deviceId": entry.device_id,
            "recipientKeyId": key_id,
            "createdAt": record.created_at,
        },
    )
    return {"grant": record.public_view()}


__all__ = [
    "GrantRecord",
    "GrantStore",
    "MAX_SECRET_BYTES",
    "grant_service",
    "new_grant_id",
    "open_grant",
    "parse_recipient_key",
    "recipient_key_id",
    "seal_grant",
]
