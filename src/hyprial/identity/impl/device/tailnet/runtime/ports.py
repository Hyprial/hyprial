"""Immutable public value shapes for the tailnet credential runtime."""

from dataclasses import dataclass, fields
import re
from typing import Protocol
from urllib.parse import urlsplit

from ..ports import TailnetAuthorityPin, TailnetMembership


def text(value):
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError("invalid credential text")
    return value


def integer(value, minimum=1, maximum=2**31 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid credential integer")
    return value


def digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid credential digest")
    return value


def pin(value):
    if type(value) is not TailnetAuthorityPin:
        raise ValueError("invalid authority pin")
    for field in fields(value):
        item = getattr(value, field.name)
        integer(item, maximum=65535) if field.name == "control_port" else text(item)
    return value


@dataclass(frozen=True, slots=True)
class DialTarget:
    authority: TailnetAuthorityPin
    remote_port: int

    def __post_init__(self):
        pin(self.authority)
        integer(self.remote_port, maximum=65535)


@dataclass(frozen=True, slots=True)
class Descriptor:
    version: str
    tailnet_id: str
    target: DialTarget
    origin: str
    server_name: str
    ca_certificate_pem: bytes
    ca_sha256: str

    def __post_init__(self):
        if type(self.version) is not str or self.version != "hyprial.tailnet.credential-authority.v1":
            raise ValueError("invalid descriptor version")
        text(self.tailnet_id)
        if type(self.target) is not DialTarget or self.target.authority.tailnet_id != self.tailnet_id:
            raise ValueError("invalid descriptor target")
        text(self.server_name)
        parsed = urlsplit(text(self.origin))
        if (parsed.scheme != "https" or parsed.hostname != self.server_name
                or parsed.port != self.target.remote_port or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("invalid descriptor origin")
        if type(self.ca_certificate_pem) is not bytes or not self.ca_certificate_pem:
            raise ValueError("invalid descriptor certificate")
        digest(self.ca_sha256)


@dataclass(frozen=True, slots=True)
class MtlsReference:
    credential_generation: int
    credential_sha256: str
    key_sha256: str
    association_sha256: str
    ca_sha256: str

    def __post_init__(self):
        integer(self.credential_generation)
        for field in fields(self):
            if field.name != "credential_generation":
                digest(getattr(self, field.name))


@dataclass(frozen=True, slots=True)
class FrozenTransport:
    descriptor: Descriptor
    mtls: MtlsReference | None

    def __post_init__(self):
        if type(self.descriptor) is not Descriptor or (
            self.mtls is not None and type(self.mtls) is not MtlsReference
        ):
            raise ValueError("invalid frozen transport")


@dataclass(frozen=True, slots=True)
class MemberProjection:
    owner: str
    device_id: str
    master_public: str
    server_public: str
    client_public_by_server: tuple[tuple[str, str], ...]
    address: str
    state: str
    revision: int

    def __post_init__(self):
        for name in ("owner", "device_id", "master_public", "server_public", "state"):
            text(getattr(self, name))
        integer(self.revision)
        if self.state not in ("active", "tombstone"):
            raise ValueError("invalid member state")
        if self.state == "active":
            text(self.address)
        elif type(self.address) is not str or self.address != "" or self.client_public_by_server != ():
            raise ValueError("invalid tombstone projection")
        pairs = self.client_public_by_server
        if type(pairs) is not tuple or any(
            type(pair) is not tuple or len(pair) != 2
            or any(type(item) is not str or not item for item in pair) for pair in pairs
        ) or len({pair[0] for pair in pairs}) != len(pairs):
            raise ValueError("invalid immutable derived keys")


class MembershipSource(Protocol):
    def membership(self, tailnet_id: str) -> TailnetMembership: ...
    def members(self, tailnet_id: str) -> tuple[MemberProjection, ...]: ...
