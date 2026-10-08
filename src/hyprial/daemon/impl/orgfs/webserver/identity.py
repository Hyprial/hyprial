from __future__ import annotations
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
import ipaddress
import socket


#: ``client_address`` mode: the TCP socket peer IS the client (front b',
#: the tailscale container whose netns this service shares).
CLIENT_ADDRESS_SOCKET = "socket"

#: ``client_address`` mode: the front forwards raw TCP and conveys the
#: original tailnet source in a PROXY protocol v2 header (front a).
CLIENT_ADDRESS_PROXY_V2 = "proxy-v2"

CLIENT_ADDRESS_MODES = frozenset({CLIENT_ADDRESS_SOCKET, CLIENT_ADDRESS_PROXY_V2})

#: Custom PROXY v2 TLV type carrying the peer's nodekey as UTF-8 text
#: (``nodekey:…``), written by the Tailcat sidecar's ``expose`` from
#: ``Server.PeerKey`` at accept time.  In proxy-v2 mode this TLV is the
#: ONLY identity source; the nodekey is then mapped to an owner through
#: the injected resolver (the org directory).  A missing or malformed TLV,
#: or an unknown nodekey, is a 403.
PROXY_V2_IDENTITY_TLV_TYPE = 0xE0

#: Wire form of a Tailcat node public key (``nodekey:<base32>``).
NODEKEY_PREFIX = "nodekey:"

_PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
_PROXY_V2_HEADER_LEN = 16

DEFAULT_RETRY_AFTER_SECONDS = 5

#: Accept-side budget for the PROXY header read plus the TLS handshake, so a
#: stalled connection cannot pin a handler thread.
HANDSHAKE_TIMEOUT_SECONDS = 10.0


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
    """Resolves a connection's client address to a peer identity.

    Returns ``None`` when the resolver knows no peer at that address.
    Raises on lookup failure — the caller logs the reason and answers 403.
    """

    def resolve(self, conn: ConnInfo) -> Identity | None: ...


#: Maps a peer's Tailcat nodekey (``nodekey:…``, the PROXY v2 TLV ``0xE0``
#: payload) to the device owner recorded in the org directory, or None when
#: no known device carries that key.  Fail closed: an unknown key is a 403.
NodekeyOwnerResolver = Callable[[str], str | None]


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


def _nodekey_from_tlv(value: bytes) -> str:
    """Parse the front-supplied nodekey TLV; anything malformed raises."""

    try:
        nodekey = value.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ProxyProtocolError(
            f"nodekey TLV is not UTF-8 text: {error}"
        ) from error
    if not nodekey.startswith(NODEKEY_PREFIX) or not nodekey[len(NODEKEY_PREFIX):].strip():
        raise ProxyProtocolError(
            "nodekey TLV must be 'nodekey:<key>' text from the front"
        )
    return nodekey


def identity_from_nodekey(
    nodekey: str, resolver: NodekeyOwnerResolver
) -> Identity | None:
    """The org-directory identity behind one peer nodekey, or None.

    The directory records each member device's public keys and owner; the
    resolver answers with the owner for a known key.  An unknown key is no
    identity (403), never an open-door fallback.
    """

    owner = resolver(nodekey)
    if not isinstance(owner, str) or not owner.strip():
        return None
    return Identity(login_name=owner.strip(), tags=())
