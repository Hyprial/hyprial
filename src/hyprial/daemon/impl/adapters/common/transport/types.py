"""Closed value types for the internal transport and native-media seam."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hyprial.daemon.impl.adapters.contracts import FrozenJsonV1


class HttpRequestPhase(StrEnum):
    PRE_SEND = "pre-send"
    MAY_HAVE_SENT = "may-have-sent"
    RESPONSE_RECEIVED = "response-received"


class HttpEffectState(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HttpAccepted:
    native_id: str | None

    def __post_init__(self) -> None:
        if self.native_id is not None and not self.native_id:
            raise ValueError("native_id must not be empty")


@dataclass(frozen=True, slots=True)
class HttpEffectObservation:
    phase: HttpRequestPhase
    state: HttpEffectState
    definitely_not_sent: bool
    status_code: int | None
    native_id: str | None
    exception_type: str | None
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HttpRequestPhase):
            raise TypeError("phase must be HttpRequestPhase")
        if not isinstance(self.state, HttpEffectState):
            raise TypeError("state must be HttpEffectState")
        if not isinstance(self.definitely_not_sent, bool):
            raise TypeError("definitely_not_sent must be bool")
        if self.state in (HttpEffectState.ACCEPTED, HttpEffectState.UNKNOWN):
            if self.definitely_not_sent:
                raise ValueError("accepted and unknown effects may not be definitely_not_sent")
        if self.state is HttpEffectState.ACCEPTED and self.status_code is None:
            raise ValueError("accepted HTTP effect requires a response status")
        if not self.reason_code:
            raise ValueError("reason_code must not be empty")


class WebSocketCloseOrigin(StrEnum):
    OPENING = "opening"
    LOCAL = "local"
    REMOTE = "remote"
    TRANSPORT = "transport"


@dataclass(frozen=True, slots=True)
class WebSocketLocalClose:
    code: int
    reason: str

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise TypeError("WebSocket close code must be an integer")
        if not 1000 <= self.code <= 4999:
            raise ValueError("WebSocket close code is outside the wire range")
        if len(self.reason.encode("utf-8")) > 123:
            raise ValueError("WebSocket close reason exceeds 123 UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class WebSocketCloseObservation:
    generation: int
    origin: WebSocketCloseOrigin
    clean: bool
    rcvd_code: int | None
    rcvd_reason: str | None
    sent_code: int | None
    sent_reason: str | None
    rcvd_then_sent: bool | None
    local_code: int | None
    local_reason: str | None
    opening_error: str | None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if not isinstance(self.origin, WebSocketCloseOrigin):
            raise TypeError("origin must be WebSocketCloseOrigin")
        if self.origin is WebSocketCloseOrigin.OPENING:
            if not self.opening_error:
                raise ValueError("opening observation requires opening_error")
        elif self.opening_error is not None:
            raise ValueError("established close cannot carry opening_error")


class ResourceCloseState(StrEnum):
    CLOSED = "closed"
    STALE_GENERATION = "stale-generation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResourceCloseOutcome:
    state: ResourceCloseState
    socket_generation: int | None
    socket_closed: bool
    client_closed: bool
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class NativeMediaScope:
    owner_principal: str
    organization_id: str
    provider: str
    instance_id: str
    generation: int
    target_ref_id: str
    content_digest: str

    def __post_init__(self) -> None:
        for name in (
            "owner_principal",
            "organization_id",
            "provider",
            "instance_id",
            "target_ref_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
            if len(value.encode("utf-8")) > 4096:
                raise ValueError(f"{name} exceeds 4096 UTF-8 bytes")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be an integer")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        _require_sha256(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class SealedNativeMedia:
    owner_principal: str
    organization_id: str
    provider: str
    instance_id: str
    generation: int
    target_ref_id: str
    content_digest: str
    name: str | None
    media_type: str | None
    size: int
    sha256: str
    native_metadata: FrozenJsonV1


def _require_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


__all__ = [
    "HttpAccepted",
    "HttpEffectObservation",
    "HttpEffectState",
    "HttpRequestPhase",
    "NativeMediaScope",
    "ResourceCloseOutcome",
    "ResourceCloseState",
    "SealedNativeMedia",
    "WebSocketCloseObservation",
    "WebSocketCloseOrigin",
    "WebSocketLocalClose",
]
