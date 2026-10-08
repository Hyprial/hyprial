"""Private service-connect values and closed validation helpers."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

from hyprial.kernel import ipc_errors


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ENV_DENY_EXACT = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "ENV",
        "GLOBIGNORE",
        "GOPATH",
        "HOME",
        "IFS",
        "NODE_OPTIONS",
        "PATH",
        "PERL5OPT",
        "RUBYOPT",
        "SHELL",
        "SHELLOPTS",
        "SSLKEYLOGFILE",
        "ZDOTDIR",
    }
)
_ENV_DENY_PREFIXES = ("DYLD_", "JAVA_TOOL_OPTIONS", "LD_", "PYTHON")
_SERVER_PUBLIC = re.compile(r"nodekey:[0-9a-f]{64}\Z")
_TAILCAT_MATERIAL = re.compile(r"(?:^|[^A-Za-z0-9])tc[A-Za-z0-9_-]{8,}")
_ENTRY_FIELDS = frozenset(
    {
        "name",
        "deviceId",
        "remotePort",
        "protocol",
        "localPort",
        "usage",
        "env",
        "auth",
        "guide",
    }
)
SERVICE_CODES = frozenset(
    {
        ipc_errors.SERVICE_INVALID_TARGET,
        ipc_errors.SERVICE_PORT_IN_USE,
        ipc_errors.SERVICE_NAME_CONFLICT,
        ipc_errors.SERVICE_UNAVAILABLE,
        ipc_errors.SERVICE_NOT_REGISTERED,
        ipc_errors.SERVICE_SIDECAR_UNSUPPORTED,
        ipc_errors.SERVICE_REGISTRY_INVALID,
        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
        ipc_errors.SERVICE_NOT_AUTHORIZED,
    }
)


class ServiceConnectError(RuntimeError):
    """A fixed-code, sanitized service-connect domain failure."""

    def __init__(self, code: str, message: str) -> None:
        if code not in SERVICE_CODES and code != ipc_errors.INVALID_ARGUMENT:
            code = ipc_errors.SERVICE_UNAVAILABLE
            message = "service operation failed"
        super().__init__(message)
        self.code = code


def invalid_registry(message: str = "service catalog is invalid") -> ServiceConnectError:
    return ServiceConnectError(ipc_errors.SERVICE_REGISTRY_INVALID, message)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise invalid_registry(f"{label} is invalid")
    return value


def _strict_int(
    value: object, label: str, *, minimum: int, maximum: int = 65535
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise invalid_registry(f"{label} is invalid")
    return value


def _public_text(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise invalid_registry(f"{label} is invalid")
    if "\x00" in value or _TAILCAT_MATERIAL.search(value) or "nodekey:" in value:
        raise invalid_registry(f"{label} contains private material")
    for token in re.findall(r"(?:https?|tcp)://[^\s'\"<>]+", value):
        parsed = urlsplit(token)
        if parsed.username is not None or parsed.password is not None:
            raise invalid_registry(f"{label} contains credentials")
        host = parsed.hostname
        if host is not None:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
    return value


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    device_id: str
    remote_port: int
    protocol: str
    local_port: int | None
    usage: str | None
    env: tuple[tuple[str, str], ...]
    auth: str
    guide: str

    @classmethod
    def from_json(cls, value: object) -> "CatalogEntry":
        if not isinstance(value, Mapping) or set(value) != _ENTRY_FIELDS:
            raise invalid_registry()
        name = _identifier(value["name"], "service name")
        device_id = _identifier(value["deviceId"], "device ID")
        remote_port = _strict_int(
            value["remotePort"], "remote port", minimum=1
        )
        protocol = value["protocol"]
        if protocol not in {"tcp", "http"}:
            raise invalid_registry("service protocol is invalid")
        local_value = value["localPort"]
        local_port = (
            None
            if local_value is None
            else _strict_int(local_value, "local port", minimum=1024)
        )
        usage = _public_text(value["usage"], "usage", nullable=True)
        raw_env = value["env"]
        if not isinstance(raw_env, Mapping):
            raise invalid_registry("service environment is invalid")
        env: list[tuple[str, str]] = []
        for key, raw in raw_env.items():
            if (
                not isinstance(key, str)
                or _ENV_NAME.fullmatch(key) is None
                or key in _ENV_DENY_EXACT
                or key.startswith(_ENV_DENY_PREFIXES)
            ):
                raise invalid_registry("service environment name is invalid")
            text = _public_text(raw, "service environment value")
            assert text is not None
            if "{" in text.replace("{port}", "") or "}" in text.replace(
                "{port}", ""
            ):
                raise invalid_registry("service environment template is invalid")
            env.append((key, text))
        auth = _public_text(value["auth"], "service auth")
        guide = _public_text(value["guide"], "service guide")
        assert auth is not None and guide is not None
        return cls(
            name,
            device_id,
            remote_port,
            str(protocol),
            local_port,
            usage,
            tuple(sorted(env)),
            auth,
            guide,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "deviceId": self.device_id,
            "remotePort": self.remote_port,
            "protocol": self.protocol,
            "localPort": self.local_port,
            "usage": self.usage,
            "env": dict(self.env),
            "auth": self.auth,
            "guide": self.guide,
        }


@dataclass(frozen=True, slots=True)
class CatalogTrust:
    space_id: str
    owner: str


@dataclass(frozen=True, slots=True)
class ProtectedDevice:
    device_id: str
    server_public: str = field(repr=False)
    address: str = field(repr=False)
    generation: int
    ports: tuple[int, ...] = field(repr=False)
    enabled: bool = field(repr=False)

    @classmethod
    def from_json(cls, value: object) -> "ProtectedDevice":
        fields = {
            "deviceId",
            "serverPublic",
            "address",
            "generation",
            "ports",
            "enabled",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise invalid_registry("protected service record is invalid")
        device_id = _identifier(value["deviceId"], "protected device ID")
        server_public = value["serverPublic"]
        address = value["address"]
        if (
            not isinstance(server_public, str)
            or _SERVER_PUBLIC.fullmatch(server_public) is None
            or not isinstance(address, str)
            or not address.startswith("tc")
            or len(address) > 4096
        ):
            raise invalid_registry("protected service record is invalid")
        generation = _strict_int(
            value["generation"], "protected generation", minimum=1, maximum=2**63 - 1
        )
        ports_value = value["ports"]
        if not isinstance(ports_value, list) or not ports_value:
            raise invalid_registry("protected service ports are invalid")
        ports = tuple(
            _strict_int(port, "protected service port", minimum=1)
            for port in ports_value
        )
        if len(set(ports)) != len(ports):
            raise invalid_registry("protected service ports are invalid")
        enabled = value["enabled"]
        if not isinstance(enabled, bool):
            raise invalid_registry("protected service enabled flag is invalid")
        return cls(
            device_id,
            server_public,
            address,
            generation,
            tuple(sorted(ports)),
            enabled,
        )

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.server_public,
            self.address,
            self.ports,
            self.enabled,
        )


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    catalog_trust: CatalogTrust | None
    devices: tuple[ProtectedDevice, ...] = field(repr=False)

    def resolve_device(self, device_id: str, remote_port: int) -> ProtectedDevice:
        device = next(
            (value for value in self.devices if value.device_id == device_id), None
        )
        if device is None or not device.enabled:
            raise ServiceConnectError(
                ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                "service device is unavailable",
            )
        if remote_port not in device.ports:
            raise ServiceConnectError(
                ipc_errors.SERVICE_NOT_AUTHORIZED,
                "service port is not authorized",
            )
        return device


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    entries: tuple[CatalogEntry, ...]
    source: str
    space_id: str | None
    owner: str | None
    error: str | None
    cache: object | None = field(default=None, repr=False)

    def find(self, name: str) -> CatalogEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def entry(self, name: str) -> CatalogEntry:
        entry = self.find(name)
        if entry is None:
            raise ServiceConnectError(
                ipc_errors.SERVICE_NOT_REGISTERED,
                "service is not registered",
            )
        return entry
