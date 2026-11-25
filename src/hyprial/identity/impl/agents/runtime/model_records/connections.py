"""Versioned local model connections share the Secret record and its write lock.

Connection identity is unrelated to model-vendor names and model identifiers. Only
an explicit configuration write migrates a legacy route. Launch admission is
separate; this record does not grant a credential or enable a harness mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
import math
from urllib.parse import urlsplit

PROFILE = "openai-chat-completions/v1"


def validate_endpoint(endpoint: object, *, connection: bool = False) -> None:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise ValueError("invalid model service endpoint")
    if any(ord(char) <= 32 or ord(char) == 127 for char in endpoint):
        raise ValueError("invalid model service endpoint")
    if connection and any(char.isspace() or char in '\\"<>?#' for char in endpoint):
        raise ValueError("invalid model service endpoint")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in endpoint):
        raise ValueError("invalid model service endpoint")
    if parsed.port is not None and parsed.port < 1:
        raise ValueError("invalid model service endpoint")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("insecure model service endpoint")


def integer(value: object, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid integer")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid integer")
    result = int(value)
    if result != value or result < minimum:
        raise ValueError("invalid integer")
    return result


def validate_key(value: object) -> None:
    if (not isinstance(value, str) or not 1 <= len(value) <= 16384
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise InvalidConnection("model connection requires a usable saved field")


def configuration(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not {"version", "endpoint", "protocol", "profile"} <= set(value) or set(value) - {"version", "endpoint", "protocol", "profile", "preset"}:
        raise ValueError("invalid model connection")
    if integer(value["version"]) != 1:
        raise ValueError("unsupported model connection version")
    if value["protocol"] != "openai-completions" or value["profile"] != PROFILE:
        raise ValueError("unsupported model connection profile")
    validate_endpoint(value["endpoint"], connection=True)
    preset = value.get("preset", {"id": "custom", "version": 1})
    if (not isinstance(preset, Mapping) or set(preset) != {"id", "version"}
            or not isinstance(preset["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", preset["id"])
            or integer(preset["version"], 1) < 1):
        raise ValueError("invalid model connection preset provenance")
    return {**value, "version": 1, "preset": {**preset, "version": integer(preset["version"], 1)}}


@dataclass(frozen=True, slots=True)
class ModelConnection:
    endpoint: str
    protocol: str
    profile: str
    connection_ref: str
    config_revision: int
    preset_id: str = "custom"
    preset_version: int = 1

    @property
    def vendor_id(self) -> None:
        return None

    def __post_init__(self) -> None:
        configuration(self.configuration())
        if not isinstance(self.connection_ref, str) or not re.fullmatch(r"[a-f0-9]{32}", self.connection_ref):
            raise ValueError("invalid model connection reference")
        if type(self.config_revision) is not int or self.config_revision < 1:
            raise ValueError("invalid model connection revision")

    def configuration(self) -> dict[str, object]:
        return {"version": 1, "endpoint": self.endpoint, "protocol": self.protocol, "profile": self.profile,
                "preset": {"id": self.preset_id, "version": self.preset_version}}

    def document(self) -> dict[str, object]:
        return {**self.configuration(), "connectionRef": self.connection_ref,
                "configRevision": self.config_revision}

    @classmethod
    def decode(cls, value: object) -> ModelConnection:
        if not isinstance(value, dict) or set(value) != {
            "version", "endpoint", "protocol", "profile", "preset", "connectionRef", "configRevision"
        }:
            raise ValueError("invalid model connection record")
        configuration({key: value[key] for key in ("version", "endpoint", "protocol", "profile", "preset")})
        return cls(value["endpoint"], value["protocol"], value["profile"],
                   value["connectionRef"], value["configRevision"], value["preset"]["id"], value["preset"]["version"])


class ConnectionConflict(ValueError):
    """The stored connection changed since the operator read its revision."""


class InvalidConnection(ValueError):
    """A new connection lacks a usable credential; no replacement is written."""
