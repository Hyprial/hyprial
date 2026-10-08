"""The orgfs read-only HTTPS web service (docs/notes/orgfs-web/brief.md).

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
- **Pluggable, fail-closed identity.**  Two modes.  ``client_address =
  socket``: a :class:`PeerIdentityResolver` turns the TCP peer address
  into an :class:`Identity` (a fake ships for tests).  ``client_address =
  proxy-v2`` (the Tailcat front): the required PROXY protocol v2 header's
  TLV ``0xE0`` carries the peer's nodekey, mapped to an owner through a
  :class:`NodekeyOwnerResolver` — the org directory, via the daemon's
  ``org.network`` IPC (:class:`DirectoryNodeKeyResolver`) when the
  process runs standalone.  No resolver, no header, no nodekey, an
  unknown key, or a resolver error: **403**, always — there is no "open
  if unknown" mode.
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
from hyprial.daemon.impl.orgfs.webserver.config import (
    ListenEndpoint,
    WebServiceConfig,
    check_config,
    parse_listen)
from hyprial.daemon.impl.orgfs.webserver.identity import (
    CLIENT_ADDRESS_MODES,
    CLIENT_ADDRESS_PROXY_V2,
    CLIENT_ADDRESS_SOCKET,
    ConnInfo,
    FakePeerIdentityResolver,
    Identity,
    NodekeyOwnerResolver,
    NODEKEY_PREFIX,
    PeerIdentityResolver,
    PROXY_V2_IDENTITY_TLV_TYPE as PROXY_V2_IDENTITY_TLV_TYPE,
    ProxyProtocolError,
    parse_proxy_v2_block,
    read_proxy_v2_header)
from hyprial.daemon.impl.orgfs.webserver.ipc import (
    DAEMON_IPC_TIMEOUT_SECONDS,
    DirectoryNodeKeyResolver,
    OrgfsIpcError,
    daemon_request,
    default_daemon_socket)
from hyprial.daemon.impl.orgfs.webserver.server import build_server, serve

__all__ = [
    "CLIENT_ADDRESS_MODES",
    "CLIENT_ADDRESS_PROXY_V2",
    "CLIENT_ADDRESS_SOCKET",
    "ConnInfo",
    "DAEMON_IPC_TIMEOUT_SECONDS",
    "DirectoryNodeKeyResolver",
    "FakePeerIdentityResolver",
    "Identity",
    "ListenEndpoint",
    "NodekeyOwnerResolver",
    "NODEKEY_PREFIX",
    "OrgfsIpcError",
    "PeerIdentityResolver",
    "ProxyProtocolError",
    "WebServiceConfig",
    "build_server",
    "check_config",
    "daemon_request",
    "default_daemon_socket",
    "parse_listen",
    "parse_proxy_v2_block",
    "read_proxy_v2_header",
    "serve",
]
