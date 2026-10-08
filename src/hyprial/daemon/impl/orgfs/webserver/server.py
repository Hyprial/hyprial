from __future__ import annotations
from collections.abc import Mapping
from typing import Any
import base64
import binascii
import html
import http.client
import http.server
import json
import logging
import mimetypes
import signal
import socket
import socketserver
import ssl
import threading
import urllib.parse
from hyprial.kernel import (
    canonical_orgfs_uri,
    canonical_user_uri,
    parse_orgfs_uri)
from hyprial.daemon.impl.orgfs.webserver.config import WebServiceConfig, _prepare_unix_socket, _validate_config
from hyprial.daemon.impl.orgfs.webserver.identity import (
    CLIENT_ADDRESS_PROXY_V2,
    CLIENT_ADDRESS_SOCKET,
    ConnInfo,
    HANDSHAKE_TIMEOUT_SECONDS,
    Identity,
    NodekeyOwnerResolver,
    PROXY_V2_IDENTITY_TLV_TYPE,
    PeerIdentityResolver,
    ProxyProtocolError,
    _nodekey_from_tlv,
    identity_from_nodekey,
    read_proxy_v2_header,
)
from hyprial.daemon.impl.orgfs.webserver.ipc import (
    DirectoryNodeKeyResolver,
    OrgfsIpcError,
    _CONTENT_PENDING,
    _LOG,
    _ORGFS_ERROR_STATUS,
    daemon_request,
    default_daemon_socket,
)



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
            # The front (Tailcat expose) wrote the peer's nodekey into TLV
            # 0xE0 at accept time; the nodekey plus the org directory is
            # the only identity source in this mode (no second lookup, no
            # fallback).
            raw_nodekey = tlvs.get(PROXY_V2_IDENTITY_TLV_TYPE)
            if raw_nodekey is None:
                raise _Denied("no-identity-tlv")
            try:
                nodekey = _nodekey_from_tlv(raw_nodekey)
            except ProxyProtocolError:
                raise _Denied("invalid-identity-tlv") from None
            resolver: NodekeyOwnerResolver | None = getattr(
                self.server, "web_nodekey_resolver", None
            )
            if resolver is None:
                raise _Denied("no-nodekey-resolver-configured")
            try:
                identity = identity_from_nodekey(nodekey, resolver)
            except Exception as error:
                _json_log(self.server.web_logger, "identity-error", error=repr(error))
                raise _Denied("resolver-error") from None
        else:
            socket_resolver: PeerIdentityResolver | None = self.server.web_resolver
            if socket_resolver is None:
                raise _Denied("no-resolver-configured")
            if conn_info is None or not conn_info.source_address:
                raise _Denied("no-client-address")
            try:
                identity = socket_resolver.resolve(conn_info)
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
    nodekey_resolver: NodekeyOwnerResolver | None = None,
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
        "web_nodekey_resolver": nodekey_resolver,
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


def serve(
    config: WebServiceConfig,
    *,
    resolver: PeerIdentityResolver | None = None,
    nodekey_resolver: NodekeyOwnerResolver | None = None,
) -> int:
    """Run the service until SIGINT/SIGTERM; returns the process exit code.

    In proxy-v2 mode the nodekey resolver defaults to the daemon's org
    directory over IPC: the standalone web process owns no directory of its
    own, and the daemon (``org.network``) is the authority.
    """

    if (
        nodekey_resolver is None
        and config.client_address == CLIENT_ADDRESS_PROXY_V2
    ):
        nodekey_resolver = DirectoryNodeKeyResolver(config.daemon_socket)
    server = build_server(
        config, resolver=resolver, nodekey_resolver=nodekey_resolver
    )

    def _stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
