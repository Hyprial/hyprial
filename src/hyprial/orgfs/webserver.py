"""The orgfs read-only HTTPS web service (notes/orgfs-web/brief.md).

A long-running, front-agnostic read-only web front for orgfs:

- **HTTPS only.**  TLS terminates here, from the configured cert and key
  paths; there is no plaintext listener.  The service never manages DNS or
  certificates (infra-ops owns both) and never hard-codes a domain — the
  configured host is accepted as-is.
- **Front-agnostic listen.**  ``listen`` is an explicit ``host:port``
  (loopback or an explicit non-wildcard address) or ``unix:/path``.  Wildcard
  binds are refused.  A unix listener's parent directory is created with
  mode 0700; a symlinked, world-writable or group-writable parent is refused
  (update 3: a same-OS-user process can forge a PROXY header on loopback
  TCP, so the unix listener is the recommended proxy-v2 deployment).
- **Pluggable, fail-closed identity.**  A :class:`PeerIdentityResolver`
  turns the connection's client address into an :class:`Identity`.  The
  client address is the TCP socket peer (``client_address = socket``, the
  tailscale-container front b') or the source of a required PROXY protocol
  v2 header (``client_address = proxy-v2``, the forwarding front a).  Ships
  a tailscaled LocalAPI whois resolver and a fake for tests.  No resolver,
  no header, no identity, a resolver error, or a tagged identity: **403**,
  always — there is no "open if unknown" mode.
- **Fail-closed membership.**  Every request checks the space's
  authoritative member list through the local daemon's IPC
  (``orgfs.members``); a non-member or a failed lookup is a 403.  orgfs
  content itself is only ever read through the same daemon IPC
  (``orgfs.stat`` / ``orgfs.read`` / ``orgfs.ls``) — the store is never
  opened directly.
- **Routes** are the #934 path form ``/<owner>/<spaceId>/<nodeId>``,
  bijective with the canonical URI through :func:`hyprial.uri.parse_orgfs_uri`.
  GET and HEAD only; anything else is 405.
"""

from __future__ import annotations

import base64
import binascii
import html
import http.client
import http.server
import ipaddress
import json
import logging
import mimetypes
import os
import signal
import socket
import socketserver
import ssl
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.uri import canonical_orgfs_uri, canonical_user_uri, parse_orgfs_uri

#: ``client_address`` mode: the TCP socket peer IS the client (front b',
#: the tailscale container whose netns this service shares).
CLIENT_ADDRESS_SOCKET = "socket"

#: ``client_address`` mode: the front forwards raw TCP and conveys the
#: original tailnet source in a PROXY protocol v2 header (front a).
CLIENT_ADDRESS_PROXY_V2 = "proxy-v2"

CLIENT_ADDRESS_MODES = frozenset({CLIENT_ADDRESS_SOCKET, CLIENT_ADDRESS_PROXY_V2})

#: Custom PROXY v2 TLV type carrying the identity the front (the tsnet
#: sidecar) resolved with WhoIs at accept time, as UTF-8 JSON:
#: ``{"found": true, "loginName": str, "nodeName": str, "tags": [str], ...}``
#: or ``{"found": false, "error": str}``.  In proxy-v2 mode this TLV is the
#: ONLY identity source; a missing or malformed TLV is a 403.
PROXY_V2_IDENTITY_TLV_TYPE = 0xE0

_PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
_PROXY_V2_HEADER_LEN = 16

DEFAULT_RETRY_AFTER_SECONDS = 5
DEFAULT_WHOIS_CACHE_SECONDS = 5.0

#: Accept-side budget for the PROXY header read plus the TLS handshake, so a
#: stalled connection cannot pin a handler thread.
HANDSHAKE_TIMEOUT_SECONDS = 10.0

#: Daemon IPC round-trip budget per orgfs call.
DAEMON_IPC_TIMEOUT_SECONDS = 10.0

_DAEMON_IPC_MAX_RESPONSE = 8 * 1024 * 1024

_LOG = logging.getLogger("hyprial.orgfs.webserver")

#: orgfs typed codes with a dedicated HTTP mapping; ``content-pending`` is
#: 503 + Retry-After and every other code is a 502 carrying the typed code
#: (brief §2).
_ORGFS_ERROR_STATUS = {
    "unknown-doc": 404,
    "cross-space-uri": 400,
    "invalid-uri": 400,
}

_CONTENT_PENDING = ipc_errors.ORGFS_CONTENT_PENDING


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """A tailnet peer's identity as the resolver asserted it.

    ``login_name`` is the human login (``user``-prefix-free).  A tagged
    device carries ``tags`` and is NEVER a space member — the tagged-devices
    profile has no human LoginName (update 3).
    """

    login_name: str | None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnInfo:
    """What the front conveyed about one connection's client.

    ``source_address`` is the TCP socket peer in ``socket`` mode and the
    PROXY v2 source in ``proxy-v2`` mode — never the socket peer in
    ``proxy-v2`` mode (update 3: the sidecar dials from loopback).
    """

    source_address: str | None
    source_port: int | None
    transport: str


class PeerIdentityResolver(Protocol):
    """Resolves a connection's client address to a tailnet identity.

    Returns ``None`` when the tailnet knows no peer at that address.
    Raises on lookup failure — the caller logs the reason and answers 403.
    """

    def resolve(self, conn: ConnInfo) -> Identity | None: ...


class TailscaledWhoisResolver:
    """WhoIs against tailscaled's local API over its unix socket (front b').

    ``GET /localapi/v0/whois?addr=<ip>:<port>`` — the same LocalAPI shape a
    Hyprial sidecar would expose for front (a).  Results are cached only
    briefly (seconds), keyed by source address, per the brief; failures and
    unknown peers are not cached, so a transient error never pins a denial.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        cache_seconds: float = DEFAULT_WHOIS_CACHE_SECONDS,
        transport: Callable[[str], "tuple[int, Mapping[str, Any]]"] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._socket_path = str(socket_path)
        self._cache_seconds = cache_seconds
        self._transport = transport or self._localapi_transport
        self._now = now
        self._cache: dict[str, tuple[float, Identity]] = {}
        self._lock = threading.Lock()

    def resolve(self, conn: ConnInfo) -> Identity | None:
        if not conn.source_address:
            return None
        key = conn.source_address
        now = self._now()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        port = conn.source_port or 0
        status, payload = self._transport(f"{conn.source_address}:{port}")
        if status != 200:
            return None
        identity = _identity_from_whois(payload)
        if identity is not None:
            with self._lock:
                self._cache[key] = (now + self._cache_seconds, identity)
        return identity

    def _localapi_transport(self, addr: str) -> "tuple[int, Mapping[str, Any]]":
        connection = _UnixHTTPConnection(self._socket_path, timeout=5.0)
        try:
            query = urllib.parse.quote(addr, safe="")
            connection.request("GET", f"/localapi/v0/whois?addr={query}")
            response = connection.getresponse()
            body = response.read()
            status = response.status
        finally:
            connection.close()
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        return status, payload if isinstance(payload, Mapping) else {}


def _identity_from_whois(payload: Mapping[str, Any]) -> Identity | None:
    profile = payload.get("UserProfile")
    node = payload.get("Node")
    login = profile.get("LoginName") if isinstance(profile, Mapping) else None
    tags = node.get("Tags") if isinstance(node, Mapping) else None
    login_name = login.strip() if isinstance(login, str) and login.strip() else None
    if login_name is None and not tags:
        return None
    return Identity(
        login_name=login_name,
        tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
    )


class FakePeerIdentityResolver:
    """The test fake: a static address → identity map, or a fixed error.

    ``seen`` records every ConnInfo so tests can assert the service fed the
    resolver the PROXY-supplied source address rather than the socket peer.
    """

    def __init__(
        self,
        identities: Mapping[str, Identity | None] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._identities = dict(identities or {})
        self._error = error
        self.seen: list[ConnInfo] = []

    def resolve(self, conn: ConnInfo) -> Identity | None:
        self.seen.append(conn)
        if self._error is not None:
            raise self._error
        return self._identities.get(conn.source_address or "")


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a unix socket — tailscaled's localapi transport."""

    def __init__(self, socket_path: str, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


# ---------------------------------------------------------------------------
# PROXY protocol v2
# ---------------------------------------------------------------------------


class ProxyProtocolError(ValueError):
    """The connection did not present a valid PROXY v2 header."""


def parse_proxy_v2_block(block: bytes) -> tuple[str, int, dict[int, bytes]]:
    """Parse one complete PROXY v2 block (16-byte header + address + TLVs).

    Returns ``(source_address, source_port, tlvs)``.  Only version-2 PROXY
    commands over STREAM (TCP) inet/inet6 are accepted; a LOCAL command or
    any other shape is a :class:`ProxyProtocolError` — in proxy-v2 mode a
    connection without a valid header is rejected, never defaulted.
    """

    if len(block) < _PROXY_V2_HEADER_LEN or not block.startswith(_PROXY_V2_SIGNATURE):
        raise ProxyProtocolError("missing the PROXY v2 signature")
    version_command = block[12]
    if version_command >> 4 != 0x2:
        raise ProxyProtocolError("not a PROXY v2 header")
    if version_command & 0x0F != 0x1:
        raise ProxyProtocolError("not a PROXY command (LOCAL carries no source)")
    family_protocol = block[13]
    if family_protocol & 0x0F != 0x1:
        raise ProxyProtocolError("not a STREAM (TCP) proxied connection")
    declared = int.from_bytes(block[14:16], "big")
    if len(block) != _PROXY_V2_HEADER_LEN + declared:
        raise ProxyProtocolError("PROXY v2 length mismatch")
    payload = block[_PROXY_V2_HEADER_LEN:]
    family = family_protocol >> 4
    if family == 0x1:
        if len(payload) < 12:
            raise ProxyProtocolError("truncated IPv4 address block")
        source = str(ipaddress.IPv4Address(payload[0:4]))
        port = int.from_bytes(payload[8:10], "big")
        tlv_bytes = payload[12:]
    elif family == 0x2:
        if len(payload) < 36:
            raise ProxyProtocolError("truncated IPv6 address block")
        source = str(ipaddress.IPv6Address(payload[0:16]))
        port = int.from_bytes(payload[32:34], "big")
        tlv_bytes = payload[36:]
    else:
        raise ProxyProtocolError("unsupported address family")
    return source, port, _parse_tlvs(tlv_bytes)


def _parse_tlvs(data: bytes) -> dict[int, bytes]:
    tlvs: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        if offset + 3 > len(data):
            raise ProxyProtocolError("truncated TLV")
        tlv_type = data[offset]
        length = int.from_bytes(data[offset + 1 : offset + 3], "big")
        value = data[offset + 3 : offset + 3 + length]
        if len(value) != length:
            raise ProxyProtocolError("truncated TLV value")
        tlvs[tlv_type] = value
        offset += 3 + length
    return tlvs


def _read_exact(conn: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ProxyProtocolError("connection closed before the PROXY v2 header")
        data.extend(chunk)
    return bytes(data)


def read_proxy_v2_header(conn: socket.socket) -> tuple[str, int, dict[int, bytes]]:
    """Read and parse one PROXY v2 header from a freshly accepted socket."""

    fixed = _read_exact(conn, _PROXY_V2_HEADER_LEN)
    if fixed.startswith(_PROXY_V2_SIGNATURE) and fixed[12] >> 4 == 0x2:
        declared = int.from_bytes(fixed[14:16], "big")
        return parse_proxy_v2_block(fixed + _read_exact(conn, declared))
    return parse_proxy_v2_block(fixed)


def _identity_from_tlv(value: bytes) -> Identity | None:
    """Parse a front-resolved identity TLV; anything malformed raises."""

    try:
        record = json.loads(value)
    except ValueError as error:
        raise ProxyProtocolError(f"identity TLV is not JSON: {error}") from error
    if not isinstance(record, Mapping):
        raise ProxyProtocolError("identity TLV is not an object")
    found = record.get("found")
    if found is False:
        return None
    if found is not True:
        raise ProxyProtocolError("identity TLV lacks found:true/false")
    login = record.get("loginName", "")
    tags = record.get("tags", [])
    if not isinstance(login, str):
        raise ProxyProtocolError("identity TLV loginName must be a string")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ProxyProtocolError("identity TLV tags must be a list of strings")
    if not login.strip() and not tags:
        raise ProxyProtocolError("identity TLV has neither loginName nor tags")
    # A tagged node carries an empty loginName and non-empty tags; the caller
    # rejects it as tagged-identity rather than as malformed.
    return Identity(login_name=login.strip(), tags=tuple(tags))


# ---------------------------------------------------------------------------
# Daemon IPC (the only orgfs read path)
# ---------------------------------------------------------------------------


class OrgfsIpcError(Exception):
    """One daemon IPC failure: a typed orgfs/IPC code, never a stack trace."""

    def __init__(self, code: str, message: str = "", data: Any = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.data = data


def default_daemon_socket() -> Path:
    """The daemon socket for this environment's isolation boundary.

    Same precedence as the CLI's: an explicit state/home root wins over
    ``HARNESS_SOCKET_PATH`` so a test-owned layout can never escape onto the
    production daemon's socket.
    """

    from hyprial.home import configured_hyprial_home

    # Same precedence as the CLI's _socket_path: an explicit state/home root is
    # an isolation boundary, so HARNESS_SOCKET_PATH never overrides it.  The
    # home itself comes from the one authority (hyprial.home).
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir or "HYPRIAL_HOME" in os.environ:
        root = (
            Path(state_dir).expanduser().resolve()
            if state_dir
            else configured_hyprial_home()[0] / "state"
        )
        return root / "daemon.sock"
    configured = os.environ.get("HARNESS_SOCKET_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return configured_hyprial_home()[0] / "state" / "daemon.sock"


def daemon_request(
    socket_path: Path,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = DAEMON_IPC_TIMEOUT_SECONDS,
) -> Any:
    """One version-1 newline-delimited JSON call to the local daemon.

    The same protocol the CLI and MCP use; failures surface as
    :class:`OrgfsIpcError` with the typed code (orgfs codes pass through
    verbatim, transport failures get their own codes).
    """

    request_id = str(uuid4())
    frame: dict[str, Any] = {"version": 1, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(socket_path))
        except OSError as error:
            raise OrgfsIpcError(
                "daemon-unavailable", f"cannot connect to {socket_path}: {error}"
            ) from error
        try:
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as error:
            raise OrgfsIpcError(
                "daemon-unavailable", f"IPC write failed: {error}"
            ) from error
        buffer = bytearray()
        while len(buffer) <= _DAEMON_IPC_MAX_RESPONSE:
            try:
                chunk = client.recv(64 * 1024)
            except socket.timeout as error:
                raise OrgfsIpcError(
                    "ipc-timeout", f"no {method} answer within {timeout:g}s"
                ) from error
            if not chunk:
                raise OrgfsIpcError("daemon-unavailable", "daemon closed the connection")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except ValueError as error:
                    raise OrgfsIpcError(
                        "invalid-response", f"invalid daemon IPC response: {error}"
                    ) from error
                if not isinstance(response, dict) or response.get("version") != 1:
                    raise OrgfsIpcError("invalid-response", "unsupported IPC version")
                if response.get("id") != request_id:
                    continue
                failure = response.get("error")
                if isinstance(failure, dict):
                    raise OrgfsIpcError(
                        str(failure.get("code", "daemon-error")),
                        str(failure.get("message", "daemon request failed")),
                        failure.get("data"),
                    )
                if "result" not in response:
                    raise OrgfsIpcError("invalid-response", "missing result")
                return response["result"]
        raise OrgfsIpcError("invalid-response", "daemon IPC response exceeded 8 MiB")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListenEndpoint:
    kind: str  # "tcp" | "unix"
    host: str | None
    port: int | None
    path: Path | None


@dataclass(frozen=True, slots=True)
class WebServiceConfig:
    host: str
    listen: str
    cert: Path
    key: Path
    client_address: str = CLIENT_ADDRESS_PROXY_V2
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    whois_socket: Path | None = None
    whois_cache_seconds: float = DEFAULT_WHOIS_CACHE_SECONDS
    daemon_socket: Path | None = None


def parse_listen(value: str) -> ListenEndpoint:
    """Parse and validate the ``listen`` setting.

    ``host:port`` (loopback or an explicit non-wildcard address — wildcard
    binds are refused) or ``unix:/absolute/path``.
    """

    if value.startswith("unix:"):
        raw_path = value[len("unix:"):]
        path = Path(raw_path)
        if not raw_path or not path.is_absolute():
            raise ValueError(f"unix listen path must be absolute: {value!r}")
        return ListenEndpoint(kind="unix", host=None, port=None, path=path)
    host, separator, port_text = value.rpartition(":")
    if not separator:
        raise ValueError(f"listen must be host:port or unix:/path: {value!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"listen port is not a number: {value!r}") from None
    if not 0 <= port <= 65535:
        raise ValueError(f"listen port out of range: {value!r}")
    if not host or host == "*":
        raise ValueError(f"wildcard listen addresses are refused: {value!r}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        # A host name could resolve to a wildcard (or change later); only a
        # literal address is checkable here.
        raise ValueError(f"listen host must be an IP literal: {value!r}")
    if address.is_unspecified:
        raise ValueError(f"wildcard listen addresses are refused: {value!r}")
    return ListenEndpoint(kind="tcp", host=host, port=port, path=None)


def _validate_config(config: WebServiceConfig) -> ListenEndpoint:
    endpoint = parse_listen(config.listen)
    if config.client_address not in CLIENT_ADDRESS_MODES:
        raise ValueError(
            f"client_address must be one of {sorted(CLIENT_ADDRESS_MODES)}: "
            f"{config.client_address!r}"
        )
    if endpoint.kind == "unix" and config.client_address == CLIENT_ADDRESS_SOCKET:
        raise ValueError(
            "client_address=socket has no peer address on a unix listener; "
            "use client_address=proxy-v2"
        )
    if endpoint.kind != "unix" and config.client_address == CLIENT_ADDRESS_PROXY_V2:
        # Any local process can reach a TCP listener and forge the PROXY v2
        # identity header; the sidecar likewise refuses tcp targets for v2.
        raise ValueError(
            "client_address=proxy-v2 requires a unix: listener in a 0700 "
            "directory; a TCP listener would accept forged identity headers"
        )
    if not config.host.strip():
        raise ValueError("host must be a non-empty host name")
    return endpoint


def _prepare_unix_socket(path: Path) -> None:
    """Create/verify the unix listener's parent (update 3).

    A missing parent is created with mode 0700.  An existing parent that is
    a symlink, not a directory, or world-/group-writable is refused — a
    writable or redirected parent would let another principal steal the
    socket the PROXY-v2 trust depends on.
    """

    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o700)
    else:
        if stat.S_ISLNK(parent_stat.st_mode):
            raise ValueError(f"unix socket parent is a symlink: {parent}")
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError(f"unix socket parent is not a directory: {parent}")
        if parent_stat.st_mode & 0o022:
            raise ValueError(
                f"unix socket parent is group- or world-writable: {parent}"
            )
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise ValueError(f"unix socket path exists and is not a socket: {path}")
    path.unlink()


def check_config(config: WebServiceConfig) -> list[str]:
    """The ``--check`` surface: every problem found, or an empty list.

    Verifies the listen-address rules and cert/key readability plus
    certificate expiry.  Read-only: it binds nothing and creates nothing.
    """

    problems: list[str] = []
    try:
        _validate_config(config)
    except ValueError as error:
        problems.append(f"listen: {error}")
    cert_bytes: bytes | None = None
    try:
        cert_bytes = Path(config.cert).read_bytes()
    except OSError as error:
        problems.append(f"cert: cannot read {config.cert}: {error}")
    try:
        Path(config.key).read_bytes()
    except OSError as error:
        problems.append(f"key: cannot read {config.key}: {error}")
    if cert_bytes is not None:
        try:
            from cryptography import x509

            certificate = x509.load_pem_x509_certificate(cert_bytes)
            not_after = getattr(certificate, "not_valid_after_utc", None)
            if not_after is None:
                not_after = certificate.not_valid_after
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            if not_after.tzinfo is None:
                not_after = not_after.replace(tzinfo=datetime.timezone.utc)
            if not_after <= now:
                problems.append(f"cert: expired at {not_after.isoformat()}")
        except ValueError as error:
            problems.append(f"cert: cannot parse {config.cert}: {error}")
    return problems


# ---------------------------------------------------------------------------
# The HTTP service
# ---------------------------------------------------------------------------


class _Denied(Exception):
    """A fail-closed access decision; ``reason`` is logged, never served."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PendingContent(Exception):
    """``contentState: pending`` on stat — answered as 503 + Retry-After."""


def _json_log(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))


class _AcceptMixin:
    """Accept side: client-address extraction, then TLS termination."""

    handshake_timeout_seconds = HANDSHAKE_TIMEOUT_SECONDS

    def get_request(self) -> "tuple[socket.socket, Any]":
        raw, addr = super().get_request()  # type: ignore[misc]
        try:
            raw.settimeout(self.handshake_timeout_seconds)
            conn_info, tlvs = self._conn_info(raw, addr)
            tls = self.web_tls_context.wrap_socket(raw, server_side=True)
        except OSError:
            raw.close()
            raise
        except Exception as error:
            # socketserver only tolerates OSError from get_request; every
            # rejection (bad PROXY header, closed connection) becomes one.
            raw.close()
            raise OSError(f"connection rejected: {error}") from error
        tls.settimeout(None)
        tls.conn_info = conn_info  # type: ignore[attr-defined]
        tls.proxy_tlvs = tlvs  # type: ignore[attr-defined]
        return tls, addr

    def _conn_info(self, raw: socket.socket, addr: Any) -> tuple[ConnInfo, dict[int, bytes]]:
        config: WebServiceConfig = self.web_config
        if config.client_address == CLIENT_ADDRESS_PROXY_V2:
            source, port, tlvs = read_proxy_v2_header(raw)
            return (
                ConnInfo(
                    source_address=source,
                    source_port=port,
                    transport=CLIENT_ADDRESS_PROXY_V2,
                ),
                tlvs,
            )
        # socket mode: the TCP peer IS the client (front b').
        host, port = (addr[0], addr[1]) if isinstance(addr, tuple) else (None, None)
        return (
            ConnInfo(source_address=host, source_port=port, transport=CLIENT_ADDRESS_SOCKET),
            {},
        )


class _OrgfsWebHandler(http.server.BaseHTTPRequestHandler):
    """Read-only handler: GET/HEAD of ``/<owner>/<spaceId>/<nodeId>``."""

    server_version = "orgfs-web"
    protocol_version = "HTTP/1.1"

    # -- verb surface -----------------------------------------------------

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def _method_not_allowed(self) -> None:
        self._respond(405, b"method not allowed\n", head_only=self.command == "HEAD")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed

    def log_message(self, *_args: Any) -> None:
        # Structured JSON logs only; never the default stderr line.
        return

    # -- the request flow ---------------------------------------------------

    def _serve(self, *, head_only: bool) -> None:
        config: WebServiceConfig = self.server.web_config
        logger: logging.Logger = self.server.web_logger
        path = urllib.parse.urlsplit(self.path).path
        conn_info: ConnInfo | None = getattr(self.connection, "conn_info", None)
        _json_log(
            logger,
            "request",
            method=self.command,
            path=path,
            client=(conn_info.source_address if conn_info else None),
        )
        if path == "/":
            self._respond(404, b"not found\n", head_only=head_only)
            return
        segments = path[1:].split("/") if path.startswith("/") else path.split("/")
        if len(segments) != 3 or not all(segments):
            self._respond(400, b"invalid-uri\n", head_only=head_only)
            return
        try:
            uri = canonical_orgfs_uri(segments[0], segments[1], segments[2])
        except ValueError:
            self._respond(400, b"invalid-uri\n", head_only=head_only)
            return
        parsed = parse_orgfs_uri(uri)
        assert parsed is not None
        _owner, space_id, _node_id = parsed
        try:
            identity = self._identity()
            _json_log(
                logger,
                "identity",
                client=(conn_info.source_address if conn_info else None),
                login=identity.login_name,
            )
            self._check_membership(space_id, identity)
        except _Denied as denied:
            _json_log(
                logger,
                "decision",
                decision="deny",
                reason=denied.reason,
                space=space_id,
                path=path,
            )
            self._respond(403, b"forbidden\n", head_only=head_only)
            return
        _json_log(
            logger,
            "decision",
            decision="allow",
            space=space_id,
            login=identity.login_name,
            path=path,
        )
        try:
            self._serve_node(uri, space_id, head_only=head_only)
        except _PendingContent:
            self._respond(
                503,
                b"content pending\n",
                extra={"Retry-After": str(config.retry_after_seconds)},
                head_only=head_only,
            )
        except OrgfsIpcError as error:
            self._respond_orgfs_error(error, head_only=head_only)

    # -- access control (fail closed) ---------------------------------------

    def _identity(self) -> Identity:
        config: WebServiceConfig = self.server.web_config
        conn_info: ConnInfo | None = getattr(self.connection, "conn_info", None)
        tlvs: dict[int, bytes] = getattr(self.connection, "proxy_tlvs", {}) or {}
        if config.client_address == CLIENT_ADDRESS_PROXY_V2:
            # The front resolved WhoIs at accept time; its TLV is the only
            # identity source in this mode (no second lookup, no fallback).
            raw_identity = tlvs.get(PROXY_V2_IDENTITY_TLV_TYPE)
            if raw_identity is None:
                raise _Denied("no-identity-tlv")
            try:
                identity = _identity_from_tlv(raw_identity)
            except ProxyProtocolError:
                raise _Denied("invalid-identity-tlv") from None
        else:
            resolver: PeerIdentityResolver | None = self.server.web_resolver
            if resolver is None:
                raise _Denied("no-resolver-configured")
            if conn_info is None or not conn_info.source_address:
                raise _Denied("no-client-address")
            try:
                identity = resolver.resolve(conn_info)
            except Exception as error:
                _json_log(self.server.web_logger, "identity-error", error=repr(error))
                raise _Denied("resolver-error") from None
        if identity is None:
            raise _Denied("no-identity")
        if identity.tags:
            raise _Denied("tagged-identity")
        if not identity.login_name:
            raise _Denied("no-login-name")
        return identity

    def _check_membership(self, space_id: str, identity: Identity) -> None:
        try:
            result = self._daemon("orgfs.members", {"spaceId": space_id})
        except OrgfsIpcError as error:
            raise _Denied(f"membership-check-failed:{error.code}") from None
        members = result.get("members") if isinstance(result, Mapping) else None
        if not isinstance(members, list):
            raise _Denied("membership-check-failed:bad-response")
        users = {member.get("user") for member in members if isinstance(member, Mapping)}
        if canonical_user_uri(identity.login_name or "") not in users:
            raise _Denied("not-a-member")

    # -- content ------------------------------------------------------------

    def _serve_node(self, uri: str, space_id: str, *, head_only: bool) -> None:
        node = self._daemon("orgfs.stat", {"spaceId": space_id, "node": uri})
        if not isinstance(node, Mapping):
            raise OrgfsIpcError("invalid-response", "stat returned a non-object")
        if node.get("contentState") == "pending":
            raise _PendingContent
        kind = node.get("kind")
        if kind == "dir":
            body = self._directory_listing(uri, space_id, node).encode("utf-8")
            self._respond(200, body, "text/html; charset=utf-8", head_only=head_only)
            return
        content = self._daemon(
            "orgfs.read",
            {"spaceId": space_id, "node": uri, "waitSeconds": 0},
        )
        if not isinstance(content, Mapping):
            raise OrgfsIpcError("invalid-response", "read returned a non-object")
        if kind == "doc":
            text = content.get("text")
            if not isinstance(text, str):
                raise OrgfsIpcError("invalid-response", "doc read carried no text")
            self._respond(
                200,
                text.encode("utf-8"),
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return
        if kind == "blob":
            encoded = content.get("contentB64")
            if not isinstance(encoded, str):
                raise OrgfsIpcError("invalid-response", "blob read carried no bytes")
            try:
                blob = base64.b64decode(encoded)
            except (ValueError, binascii.Error):
                raise OrgfsIpcError(
                    "invalid-response", "blob read carried invalid base64"
                ) from None
            name = str(node.get("name") or "")
            guessed, _encoding = mimetypes.guess_type(name)
            self._respond(
                200,
                blob,
                guessed or "application/octet-stream",
                head_only=head_only,
            )
            return
        raise OrgfsIpcError("invalid-response", f"unknown node kind {kind!r}")

    def _directory_listing(self, uri: str, space_id: str, node: Mapping[str, Any]) -> str:
        listing = self._daemon("orgfs.ls", {"spaceId": space_id, "path": uri})
        entries = listing.get("nodes") if isinstance(listing, Mapping) else None
        if not isinstance(entries, list):
            raise OrgfsIpcError("invalid-response", "ls returned no nodes")
        title = html.escape(str(node.get("name") or "/"))
        rows = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            entry_uri = entry.get("uri")
            parsed = parse_orgfs_uri(entry_uri) if isinstance(entry_uri, str) else None
            if parsed is None:
                continue
            # Each entry links to its own path — the three URI segments
            # verbatim, the design §7 bijection.
            href = "/".join(parsed)
            name = html.escape(str(entry.get("name") or parsed[2]))
            suffix = "/" if entry.get("kind") == "dir" else ""
            rows.append(f'<li><a href="/{html.escape(href)}">{name}{suffix}</a></li>')
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f"<title>{title}</title></head><body>"
            f"<h1>{title}</h1><ul>{''.join(rows)}</ul></body></html>"
        )

    # -- plumbing -------------------------------------------------------------

    def _daemon(self, method: str, params: Mapping[str, Any]) -> Any:
        config: WebServiceConfig = self.server.web_config
        socket_path = config.daemon_socket or default_daemon_socket()
        return daemon_request(socket_path, method, params)

    def _respond_orgfs_error(self, error: OrgfsIpcError, *, head_only: bool) -> None:
        if error.code == _CONTENT_PENDING:
            config: WebServiceConfig = self.server.web_config
            self._respond(
                503,
                b"content pending\n",
                extra={"Retry-After": str(config.retry_after_seconds)},
                head_only=head_only,
            )
            return
        status = _ORGFS_ERROR_STATUS.get(error.code)
        if status is not None:
            self._respond(status, f"{error.code}\n".encode(), head_only=head_only)
            return
        # Every other typed orgfs/IPC error is a 502 with the typed
        # code — never a stack trace.
        self._respond(502, f"orgfs error: {error.code}\n".encode(), head_only=head_only)

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        *,
        extra: Mapping[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if not head_only:
            self.wfile.write(body)


def build_server(
    config: WebServiceConfig,
    *,
    resolver: PeerIdentityResolver | None,
    logger: logging.Logger | None = None,
) -> socketserver.BaseServer:
    """Bind the configured listener and return the ready-to-serve server."""

    endpoint = _validate_config(config)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(config.cert, config.key)
    attributes: dict[str, Any] = {
        "web_config": config,
        "web_resolver": resolver,
        "web_tls_context": tls_context,
        "web_logger": logger or _LOG,
        "daemon_threads": True,
    }
    if endpoint.kind == "unix":
        assert endpoint.path is not None
        _prepare_unix_socket(endpoint.path)
        server_cls = type(
            "_OrgfsWebUnixServer",
            (_AcceptMixin, socketserver.ThreadingMixIn, socketserver.UnixStreamServer),
            attributes,
        )
        return server_cls(str(endpoint.path), _OrgfsWebHandler)
    server_cls = type(
        "_OrgfsWebTcpServer",
        (_AcceptMixin, socketserver.ThreadingMixIn, socketserver.TCPServer),
        {**attributes, "allow_reuse_address": True},
    )
    return server_cls((endpoint.host, endpoint.port), _OrgfsWebHandler)


def serve(config: WebServiceConfig, *, resolver: PeerIdentityResolver | None = None) -> int:
    """Run the service until SIGINT/SIGTERM; returns the process exit code."""

    server = build_server(config, resolver=resolver)

    def _stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
