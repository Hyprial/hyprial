"""Internal common transport, resource ownership, and sealed-media contract."""

from .http import HttpEffectAttempt, classify_httpx_exception, execute_http_effect
from .lifecycle import TransportResourceOwner
from .media import NativeMediaDenied, SCHEMA_VERSION, SealedNativeMediaAuthority
from .types import (
    HttpAccepted,
    HttpEffectObservation,
    HttpEffectState,
    HttpRequestPhase,
    NativeMediaScope,
    ResourceCloseOutcome,
    ResourceCloseState,
    SealedNativeMedia,
    WebSocketCloseObservation,
    WebSocketCloseOrigin,
    WebSocketLocalClose,
)
from .websocket import WebSocketOpenError, observe_connection_closed, open_websocket_once

__all__ = [
    "HttpAccepted",
    "HttpEffectAttempt",
    "HttpEffectObservation",
    "HttpEffectState",
    "HttpRequestPhase",
    "NativeMediaDenied",
    "NativeMediaScope",
    "ResourceCloseOutcome",
    "ResourceCloseState",
    "SCHEMA_VERSION",
    "SealedNativeMedia",
    "SealedNativeMediaAuthority",
    "TransportResourceOwner",
    "WebSocketCloseObservation",
    "WebSocketCloseOrigin",
    "WebSocketLocalClose",
    "WebSocketOpenError",
    "classify_httpx_exception",
    "execute_http_effect",
    "observe_connection_closed",
    "open_websocket_once",
]
