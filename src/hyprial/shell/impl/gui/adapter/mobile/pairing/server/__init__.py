"""S3 opt-in loopback pairing server over the S1 state machine.

Importing this package does not listen, read host material, bind a port or
start a thread.  The caller constructs :class:`PairingServer` with explicit
node/TLS/authority/clock/port inputs and calls :meth:`PairingServer.start`.
There is no CLI, production exposure, approval wire route or host.py wiring
in this slice.
"""

from __future__ import annotations

from .controller import PairingServer
from .limits import (
    DEFAULT_FRAME_RATE_LIMIT,
    DEFAULT_FRAME_RATE_WINDOW_SECONDS,
    DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONNECTIONS,
    MAX_CLIENT_FRAMES_PER_CONNECTION,
    MAX_CLIENT_OPERATIONS_PER_CONNECTION,
    FrameRateLimiter,
    drain_bounded,
    read_frame,
    read_line,
)
from .tls import (
    CERTIFICATE_INVALID,
    HANDSHAKE_FAILED,
    MATERIAL_INVALID,
    PIN_INVALID,
    PIN_MISMATCH,
    HostTlsMaterial,
    PairingTlsError,
    connect_pinned,
    host_key_fingerprint_from_certificate,
    load_host_tls_material,
    server_ssl_context,
    verify_pinned_peer,
)

__all__ = [
    "CERTIFICATE_INVALID",
    "DEFAULT_FRAME_RATE_LIMIT",
    "DEFAULT_FRAME_RATE_WINDOW_SECONDS",
    "DEFAULT_HANDSHAKE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONNECTIONS",
    "HANDSHAKE_FAILED",
    "HostTlsMaterial",
    "MATERIAL_INVALID",
    "MAX_CLIENT_FRAMES_PER_CONNECTION",
    "MAX_CLIENT_OPERATIONS_PER_CONNECTION",
    "PIN_INVALID",
    "PIN_MISMATCH",
    "FrameRateLimiter",
    "PairingServer",
    "PairingTlsError",
    "connect_pinned",
    "drain_bounded",
    "host_key_fingerprint_from_certificate",
    "load_host_tls_material",
    "read_frame",
    "read_line",
    "server_ssl_context",
    "verify_pinned_peer",
]
