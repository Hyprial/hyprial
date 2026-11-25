"""Configuration and injected ports for the IMAP/SMTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Literal, Protocol
import unicodedata


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    return value


def canonical_address_key(address: str) -> str:
    """Return the typed mail identity key without folding the local part."""

    local, separator, domain = address.rpartition("@")
    if not separator or not local or not domain:
        raise ValueError("mail address must contain local and domain parts")
    normalized_local = unicodedata.normalize("NFC", local)
    canonical_domain = domain.encode("idna").decode("ascii").lower()
    return f"{normalized_local}@{canonical_domain}"


@dataclass(frozen=True, slots=True)
class MailCredentials:
    username: str
    password: str

    def __post_init__(self) -> None:
        _required(self.username, "username")
        _required(self.password, "password")


class MailCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> MailCredentials: ...


@dataclass(frozen=True, slots=True)
class MailConnectionConfig:
    mailbox_scope: str
    authenticated_mailbox_id: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    credential_ref: str
    smtp_mode: Literal["implicit-tls", "starttls"]
    folder: str = "INBOX"
    page_size: int = 500

    def __post_init__(self) -> None:
        for name in (
            "mailbox_scope",
            "authenticated_mailbox_id",
            "imap_host",
            "smtp_host",
            "credential_ref",
            "folder",
        ):
            _required(getattr(self, name), name)
        if self.imap_port not in range(1, 65536) or self.smtp_port not in range(1, 65536):
            raise ValueError("mail ports must be in 1..65535")
        if self.smtp_mode not in ("implicit-tls", "starttls"):
            raise ValueError("SMTP requires implicit TLS or STARTTLS")
        if not 1 <= self.page_size <= 500:
            raise ValueError("IMAP page size must be in 1..500")
        object.__setattr__(
            self,
            "authenticated_mailbox_id",
            canonical_address_key(self.authenticated_mailbox_id),
        )


@dataclass(frozen=True, slots=True)
class MailFetchEvidence:
    """Evidence that bytes came from one authenticated mailbox UID fetch."""

    mailbox_scope: str
    authenticated_mailbox_id: str
    folder: str
    uidvalidity: int
    uid: int
    raw_sha256: str

    def __post_init__(self) -> None:
        for name in ("mailbox_scope", "authenticated_mailbox_id", "folder", "raw_sha256"):
            _required(getattr(self, name), name)
        if self.uidvalidity < 1 or self.uid < 1:
            raise ValueError("mail fetch evidence UID values are invalid")
        if len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be a SHA-256 hex digest")
        try:
            bytes.fromhex(self.raw_sha256)
        except ValueError as exc:
            raise ValueError("raw_sha256 must be a SHA-256 hex digest") from exc
        object.__setattr__(
            self,
            "authenticated_mailbox_id",
            canonical_address_key(self.authenticated_mailbox_id),
        )

    def matches(self, raw: bytes) -> bool:
        return hashlib.sha256(raw).hexdigest() == self.raw_sha256


@dataclass(frozen=True, slots=True)
class MailCheckpoint:
    mailbox_scope: str
    folder: str
    uidvalidity: int
    uid: int

    def __post_init__(self) -> None:
        _required(self.mailbox_scope, "mailbox_scope")
        _required(self.folder, "folder")
        if self.uidvalidity < 1 or self.uid < 0:
            raise ValueError("mail checkpoint values are invalid")

    @property
    def token(self) -> str:
        return f"{self.mailbox_scope}:{self.folder}:{self.uidvalidity}:{self.uid}"

    @classmethod
    def parse(cls, token: str) -> MailCheckpoint:
        scope, folder, uidvalidity, uid = token.rsplit(":", 3)
        return cls(scope, folder, int(uidvalidity), int(uid))


class MailCheckpointLedger(Protocol):
    async def load(
        self, instance_id: str, generation: int, folder: str
    ) -> MailCheckpoint | None: ...

    async def commit(
        self,
        instance_id: str,
        generation: int,
        folder: str,
        checkpoint: MailCheckpoint,
    ) -> None: ...


PathLike = str | Path


__all__ = [
    "MailCheckpoint",
    "MailCheckpointLedger",
    "MailConnectionConfig",
    "MailCredentialResolver",
    "MailCredentials",
    "MailFetchEvidence",
]
