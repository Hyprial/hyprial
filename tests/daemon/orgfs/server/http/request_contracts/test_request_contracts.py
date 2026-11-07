from __future__ import annotations

import http.client
import socket
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs import webserver
from hyprial.daemon.impl.forwarding.membership.projection import owner_for_key
from hyprial.daemon.impl.orgfs.webserver import (
    CLIENT_ADDRESS_PROXY_V2,
    CLIENT_ADDRESS_SOCKET,
    PROXY_V2_IDENTITY_TLV_TYPE,
    DirectoryNodeKeyResolver,
    FakePeerIdentityResolver,
    Identity,
    ProxyProtocolError,
    WebServiceConfig,
    build_server,
    check_config,
    parse_listen,
)
from hyprial.daemon.impl.orgfs.webserver import identity as identity_owner

from ..support import (
    BLOB_BYTES,
    DIRECTORY,
    MEMBER_ADDR,
    MEMBER_IDENTITY,
    MEMBER_NODEKEY,
    NODE_URI_DOC,
    NON_MEMBER_ADDR,
    NON_MEMBER_IDENTITY,
    NON_MEMBER_NODEKEY,
    OWNER,
    SPACE,
    TAGGED_IDENTITY,
    UNKNOWN_NODEKEY,
    _client_context,
    _config,
    _DaemonFailure,
    _garbage_tlv,
    _go_sidecar_header,
    _https_request,
    _nodekey_tlv,
    _orgfs_handlers,
    _proxy_mode_server,
    _raw_connect,
    _socket_mode_server,
    _write_self_signed_cert,
    build_proxy_v2_header,
)
from ..support import (
    cert_paths as cert_paths,
)
from ..support import (
    fake_daemon as fake_daemon,
)
from ..support import (
    serve_web as serve_web,
)
from ..support import (
    sock_dir as sock_dir,
)


def test_member_reads_a_doc_over_real_tls(serve_web, fake_daemon, cert_paths):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, headers, body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 200
    assert body == b"hello world"
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    methods = [method for method, _params in daemon.calls]
    assert methods == ["orgfs.members", "orgfs.stat", "orgfs.read"]


def test_non_member_is_403_and_never_reads(serve_web, fake_daemon, cert_paths):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": NON_MEMBER_IDENTITY}
    )
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    # The denial happens before any content IPC: no stat, no read.
    assert [method for method, _params in daemon.calls] == ["orgfs.members"]


def test_no_identity_is_403(serve_web, fake_daemon, cert_paths):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {}
    )
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    assert daemon.calls == []


def test_tagged_identity_is_403(serve_web, fake_daemon, cert_paths):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": TAGGED_IDENTITY}
    )
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    # A tag-owned identity never even reaches the membership lookup.
    assert daemon.calls == []


def test_resolver_error_fails_closed(serve_web, fake_daemon, cert_paths):
    daemon = fake_daemon()
    resolver = FakePeerIdentityResolver(error=RuntimeError("whois exploded"))
    server = serve_web(_config(cert_paths, daemon), resolver)
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    assert daemon.calls == []


def test_no_resolver_configured_fails_closed(serve_web, fake_daemon, cert_paths):
    daemon = fake_daemon()
    server = serve_web(_config(cert_paths, daemon), None)
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    assert daemon.calls == []


def test_membership_lookup_failure_fails_closed(serve_web, fake_daemon, cert_paths):
    handlers = _orgfs_handlers()
    handlers["orgfs.members"] = lambda params: (_ for _ in ()).throw(
        _DaemonFailure("unknown-space")
    )
    daemon = fake_daemon(handlers)
    resolver = FakePeerIdentityResolver({"127.0.0.1": MEMBER_IDENTITY})
    server = serve_web(_config(cert_paths, daemon), resolver)
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 403
    assert [method for method, _params in daemon.calls] == ["orgfs.members"]


def test_listen_host_must_be_an_ip_literal():
    # A host name could resolve to a wildcard, so only literals are accepted.
    with pytest.raises(ValueError):
        webserver.parse_listen("localhost:8443")
    assert webserver.parse_listen("127.0.0.1:8443").host == "127.0.0.1"


def test_wildcard_bind_is_refused():
    for listen in ("0.0.0.0:443", "[::]:443", ":::443", ":443", "*:443"):
        with pytest.raises(ValueError):
            parse_listen(listen)
    # Loopback and explicit non-wildcard addresses stay allowed.
    assert parse_listen("127.0.0.1:8443").host == "127.0.0.1"
    assert parse_listen("[::1]:8443").host == "::1"
    assert parse_listen("100.64.20.2:443").host == "100.64.20.2"


def test_there_is_no_plaintext_listener(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    host, port = server.server_address
    received = b""
    with socket.create_connection((host, port), timeout=10) as raw:
        raw.settimeout(5)
        try:
            raw.sendall(b"GET / HTTP/1.0\r\n\r\n")
            while True:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                received += chunk
        except OSError:
            pass
    # A TLS listener answers plaintext with an alert and EOF, never with HTTP.
    assert not received.startswith(b"HTTP/")


def test_content_pending_is_503_with_retry_after(serve_web, fake_daemon, cert_paths):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/pend-1")
    assert status == 503
    assert headers["Retry-After"] == "3"
    assert [method for method, _params in daemon.calls] == ["orgfs.members", "orgfs.stat"]


def test_method_not_allowed_is_405(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        status, _headers, _body = _https_request(
            server, f"/{OWNER}/{SPACE}/doc-1", method=method
        )
        assert status == 405, method


def test_path_uri_round_trip(serve_web, fake_daemon, cert_paths):
    owner, space_id, node_id = "carol@org-c.example", "space-9", "node-7"
    from hyprial.kernel.impl.primitives.uri import canonical_orgfs_uri, parse_orgfs_uri

    uri = canonical_orgfs_uri(owner, space_id, node_id)
    assert parse_orgfs_uri(uri) == (owner, space_id, node_id)
    # The web path is the three payload segments verbatim, and the service
    # maps it back to the identical URI on the daemon wire.
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 200
    stat_params = [params for method, params in daemon.calls if method == "orgfs.stat"]
    assert stat_params == [{"spaceId": SPACE, "node": NODE_URI_DOC}]


def test_unknown_doc_is_404(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, _headers, _body = _https_request(server, f"/{OWNER}/{SPACE}/nope-1")
    assert status == 404


def test_invalid_and_cross_space_uris_are_400(serve_web, fake_daemon, cert_paths):
    handlers = _orgfs_handlers()
    real_stat = handlers["orgfs.stat"]

    def stat(params: dict) -> dict:
        if "other-space" in str(params.get("node", "")):
            raise _DaemonFailure("cross-space-uri")
        return real_stat(params)

    handlers["orgfs.stat"] = stat
    daemon = fake_daemon(handlers)
    resolver = FakePeerIdentityResolver({"127.0.0.1": MEMBER_IDENTITY})
    server = serve_web(_config(cert_paths, daemon), resolver)

    # Malformed shapes are rejected before any daemon call.
    for path in (f"/{OWNER}/{SPACE}", f"/{OWNER}/{SPACE}/doc-1/extra", "/one"):
        status, _headers, _body = _https_request(server, path)
        assert status == 400, path
    assert daemon.calls == []

    status, _headers, _body = _https_request(server, f"/{OWNER}/other-space/doc-1")
    assert status == 400


def test_other_orgfs_errors_are_502_with_the_typed_code(serve_web, fake_daemon, cert_paths):
    handlers = _orgfs_handlers()
    handlers["orgfs.stat"] = lambda params: (_ for _ in ()).throw(
        _DaemonFailure("too-large")
    )
    daemon = fake_daemon(handlers)
    resolver = FakePeerIdentityResolver({"127.0.0.1": MEMBER_IDENTITY})
    server = serve_web(_config(cert_paths, daemon), resolver)
    status, _headers, body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1")
    assert status == 502
    assert b"too-large" in body
    assert b"Traceback" not in body


def test_blob_is_served_with_a_guessed_content_type(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, headers, body = _https_request(server, f"/{OWNER}/{SPACE}/blob-1")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body == BLOB_BYTES


def test_directory_listing_links_to_the_entries_own_paths(
    serve_web, fake_daemon, cert_paths
):
    server, daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, headers, body = _https_request(server, f"/{OWNER}/{SPACE}/dir-1")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert f'href="/{OWNER}/{SPACE}/doc-1"'.encode() in body
    assert f'href="/{OWNER}/{SPACE}/blob-1"'.encode() in body
    assert b"readme.md" in body
    assert b"logo.png" in body
    assert [method for method, _params in daemon.calls] == [
        "orgfs.members",
        "orgfs.stat",
        "orgfs.ls",
    ]


def test_no_directory_index_at_root(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, _headers, _body = _https_request(server, "/")
    assert status == 404


def test_head_is_allowed_and_bodiless(serve_web, fake_daemon, cert_paths):
    server, _daemon, _resolver = _socket_mode_server(
        serve_web, fake_daemon, cert_paths, {"127.0.0.1": MEMBER_IDENTITY}
    )
    status, headers, body = _https_request(server, f"/{OWNER}/{SPACE}/doc-1", method="HEAD")
    assert status == 200
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body == b""


def test_proxy_v2_member_reads_a_doc(serve_web, fake_daemon, cert_paths, sock_dir):
    server, _daemon = _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir)
    status, _headers, body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(MEMBER_NODEKEY),
    )
    assert status == 200
    assert body == b"hello world"


def test_proxy_v2_non_member_is_403(serve_web, fake_daemon, cert_paths, sock_dir):
    server, daemon = _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir)
    status, _headers, _body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=NON_MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(NON_MEMBER_NODEKEY),
    )
    assert status == 403
    assert [method for method, _params in daemon.calls] == ["orgfs.members"]


@pytest.mark.parametrize(
    "tlvs",
    [
        pytest.param(b"", id="no-identity-tlv"),
        pytest.param(
            _nodekey_tlv(UNKNOWN_NODEKEY), id="nodekey-not-in-the-directory"
        ),
        pytest.param(
            _garbage_tlv(b"not a nodekey at all"), id="not-a-nodekey"
        ),
        pytest.param(_nodekey_tlv("nodekey:"), id="empty-key"),
        pytest.param(
            bytes([PROXY_V2_IDENTITY_TLV_TYPE])
            + (3).to_bytes(2, "big")
            + b"\xff\xfe\xfd",
            id="not-utf8",
        ),
    ],
)
def test_proxy_v2_identity_tlv_reject_paths_are_403_and_never_read(
    serve_web, fake_daemon, cert_paths, sock_dir, tlvs
):
    server, daemon = _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir)
    status, _headers, _body = _https_request(
        server, f"/{OWNER}/{SPACE}/doc-1", proxy_source=MEMBER_ADDR, proxy_tlvs=tlvs
    )
    assert status == 403
    assert daemon.calls == []


def test_proxy_v2_without_a_nodekey_resolver_fails_closed(
    serve_web, fake_daemon, cert_paths, sock_dir
):
    daemon = fake_daemon()
    listen = f"unix:{sock_dir / 'nores' / 'web.sock'}"
    server = serve_web(
        _config(
            cert_paths, daemon, listen=listen, client_address=CLIENT_ADDRESS_PROXY_V2
        ),
        None,
        nodekey_resolver=None,
    )
    status, _headers, _body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(MEMBER_NODEKEY),
    )
    assert status == 403
    assert daemon.calls == []


def test_proxy_v2_resolver_error_fails_closed(
    serve_web, fake_daemon, cert_paths, sock_dir
):
    daemon = fake_daemon()

    def explode(_nodekey: str) -> str | None:
        raise RuntimeError("directory lookup exploded")

    listen = f"unix:{sock_dir / 'boom' / 'web.sock'}"
    server = serve_web(
        _config(
            cert_paths, daemon, listen=listen, client_address=CLIENT_ADDRESS_PROXY_V2
        ),
        None,
        nodekey_resolver=explode,
    )
    status, _headers, _body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(MEMBER_NODEKEY),
    )
    assert status == 403
    assert daemon.calls == []


def test_proxy_v2_connection_without_a_header_is_rejected(
    serve_web, fake_daemon, cert_paths, sock_dir
):
    server, _daemon = _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir)
    raw = _raw_connect(server)
    try:
        # No PROXY header: the TLS handshake itself must fail.
        with pytest.raises(OSError):
            _client_context().wrap_socket(raw, server_hostname="fs.test.example")
    finally:
        raw.close()


def test_proxy_v2_header_parser_rejects_bad_blocks():
    good = build_proxy_v2_header(MEMBER_ADDR)
    source, port, tlvs = webserver.parse_proxy_v2_block(good)
    assert (source, port) == (MEMBER_ADDR, 4321)
    assert tlvs == {}
    with pytest.raises(ProxyProtocolError):
        webserver.parse_proxy_v2_block(b"GET / HTTP/1.1\r\n")
    with pytest.raises(ProxyProtocolError):
        # LOCAL command (0x20) carries no source address, even with a full
        # address block attached.
        webserver.parse_proxy_v2_block(
            b"\r\n\r\n\x00\r\nQUIT\n\x20\x11\x00\x0c" + (b"\x00" * 12)
        )


def test_unix_listener_serves_and_creates_its_parent_0700(
    serve_web, fake_daemon, cert_paths, sock_dir
):
    socket_path = sock_dir / "svc" / "web.sock"
    daemon = fake_daemon()
    config = _config(
        cert_paths,
        daemon,
        listen=f"unix:{socket_path}",
        client_address=CLIENT_ADDRESS_PROXY_V2,
    )
    server = serve_web(config, None, nodekey_resolver=DIRECTORY.get)
    assert stat.S_IMODE((sock_dir / "svc").stat().st_mode) == 0o700
    status, _headers, body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(MEMBER_NODEKEY),
    )
    assert status == 200
    assert body == b"hello world"


def test_unix_listener_refuses_unsafe_parents(serve_web, fake_daemon, cert_paths, sock_dir):
    daemon = fake_daemon()
    resolver = FakePeerIdentityResolver({MEMBER_ADDR: MEMBER_IDENTITY})

    symlinked = sock_dir / "link"
    symlinked.symlink_to(sock_dir)
    with pytest.raises(ValueError):
        build_server(
            _config(
                cert_paths,
                daemon,
                listen=f"unix:{symlinked / 'web.sock'}",
                client_address=CLIENT_ADDRESS_PROXY_V2,
            ),
            resolver=resolver,
        )

    world_writable = sock_dir / "world"
    world_writable.mkdir(mode=0o777)
    world_writable.chmod(0o777)
    with pytest.raises(ValueError):
        build_server(
            _config(
                cert_paths,
                daemon,
                listen=f"unix:{world_writable / 'web.sock'}",
                client_address=CLIENT_ADDRESS_PROXY_V2,
            ),
            resolver=resolver,
        )

    group_writable = sock_dir / "group"
    group_writable.mkdir(mode=0o770)
    group_writable.chmod(0o770)
    with pytest.raises(ValueError):
        build_server(
            _config(
                cert_paths,
                daemon,
                listen=f"unix:{group_writable / 'web.sock'}",
                client_address=CLIENT_ADDRESS_PROXY_V2,
            ),
            resolver=resolver,
        )


def test_proxy_v2_on_a_tcp_listener_is_a_config_error(cert_paths):
    # Any local process could reach a TCP listener and forge the identity TLV.
    with pytest.raises(ValueError):
        build_server(
            _config(
                cert_paths,
                None,
                listen="127.0.0.1:0",
                client_address=CLIENT_ADDRESS_PROXY_V2,
            ),
            resolver=None,
        )


def test_socket_mode_on_a_unix_listener_is_a_config_error(cert_paths, sock_dir):
    server = None
    with pytest.raises(ValueError):
        try:
            server = build_server(
                _config(
                    cert_paths,
                    None,
                    listen=f"unix:{sock_dir / 'web.sock'}",
                    client_address=CLIENT_ADDRESS_SOCKET,
                ),
                resolver=None,
            )
        finally:
            if server is not None:
                server.server_close()


def test_nodekey_tlv_parsing_rejects_malformed_values():
    assert identity_owner._nodekey_from_tlv(b"nodekey:abc123") == "nodekey:abc123"
    # Whitespace is trimmed; the prefix and a non-empty key are required.
    assert identity_owner._nodekey_from_tlv(b"  nodekey:abc123 \n") == "nodekey:abc123"
    for bad in (b"nodekey:", b"   ", b"garbage", b"\xff\xfe"):
        with pytest.raises(ProxyProtocolError):
            identity_owner._nodekey_from_tlv(bad)


def test_identity_from_nodekey_maps_the_directory_owner():
    directory = {MEMBER_NODEKEY: OWNER}
    identity = identity_owner.identity_from_nodekey(
        MEMBER_NODEKEY, directory.get
    )
    assert identity == Identity(login_name=OWNER, tags=())
    # An unknown key is no identity — fail closed, never a fallback owner.
    assert (
        identity_owner.identity_from_nodekey(UNKNOWN_NODEKEY, directory.get) is None
    )
    # A resolver that answers with a non-string is treated as unknown.
    assert (
        identity_owner.identity_from_nodekey(
            MEMBER_NODEKEY, lambda _key: 42  # type: ignore[arg-type, return-value]
        )
        is None
    )


def _roster_lookup(tailnets):
    # Exercise the daemon's real attribution projection, not an HTTP-side directory.
    def lookup(params):
        assert set(params) == {"peerKey"}
        status = {"tailnets": [
            {"memberReady": True, "members": rows} for rows in tailnets.values()
        ]}
        return {"owner": owner_for_key(status, params["peerKey"], "nodekey:local-server")}
    return lookup


def test_directory_nodekey_resolver_requests_daemon_roster_attribution_over_ipc(
    fake_daemon,
):
    handlers = _orgfs_handlers()
    handlers["org.network"] = _roster_lookup({
        "acme": [{
            "deviceId": "node-a", "owner": OWNER, "state": "active",
            "serverPublic": "nodekey:peer-server", "masterPublic": "nodekey:client-a",
            "clientPublicByServer": {
                "nodekey:local-server": MEMBER_NODEKEY,
                "nodekey:foreign-server": "nodekey:foreign-client",
            },
        }],
    })
    daemon = fake_daemon(handlers)
    resolver = DirectoryNodeKeyResolver(daemon.socket_path)
    assert resolver(MEMBER_NODEKEY) == OWNER
    # Master and foreign-server keys are not identities on this server.
    assert resolver("nodekey:client-a") is None
    assert resolver(UNKNOWN_NODEKEY) is None
    calls = [method for method, _params in daemon.calls]
    assert calls.count("org.network") == 3
    assert [params for _method, params in daemon.calls] == [
        {"peerKey": key} for key in (MEMBER_NODEKEY, "nodekey:client-a", UNKNOWN_NODEKEY)
    ]


def test_directory_nodekey_resolver_propagates_daemon_key_refusal(fake_daemon):
    # §7 moves attribution to the daemon/sidecar. An invalid derived announcement
    # is refused there; the HTTP adapter must preserve that refusal, not parse rows.
    handlers = _orgfs_handlers()
    handlers["org.network"] = lambda params: {"owner": None}
    daemon = fake_daemon(handlers)
    resolver = DirectoryNodeKeyResolver(daemon.socket_path)

    assert resolver(MEMBER_NODEKEY) is None
    assert daemon.calls == [("org.network", {"peerKey": MEMBER_NODEKEY})]


def test_directory_nodekey_resolver_rejects_cross_org_identity_conflicts(fake_daemon):
    identity = {
        "deviceId": "node-a",
        "owner": OWNER,
        "masterPublic": "nodekey:master-a",
        "state": "active",
        "clientPublicByServer": {"nodekey:local-server": MEMBER_NODEKEY},
    }
    roster = {"acme": [identity], "other": [dict(identity)]}
    handlers = _orgfs_handlers()
    handlers["org.network"] = _roster_lookup(roster)
    daemon = fake_daemon(handlers)
    resolver = DirectoryNodeKeyResolver(daemon.socket_path)

    # The same protected identity can be projected through more than one org.
    assert resolver(MEMBER_NODEKEY) == OWNER
    roster["other"].append(
        {
            "deviceId": "node-b",
            "owner": "mallory@example.com",
            "masterPublic": "nodekey:master-b",
            "state": "active",
            "clientPublicByServer": {
                "nodekey:local-server": MEMBER_NODEKEY,
            },
        }
    )
    # A second identity claiming the derived key makes attribution ambiguous.
    assert resolver(MEMBER_NODEKEY) is None


def test_directory_nodekey_resolver_fails_closed_when_the_daemon_cannot_answer(
    fake_daemon,
):
    handlers = _orgfs_handlers()
    handlers["org.network"] = lambda params: (_ for _ in ()).throw(
        _DaemonFailure("unknown-method")
    )
    daemon = fake_daemon(handlers)
    resolver = DirectoryNodeKeyResolver(daemon.socket_path)
    assert resolver(MEMBER_NODEKEY) is None

    # A socket that never existed is the same fail-closed answer.
    missing = DirectoryNodeKeyResolver(daemon.socket_path.parent / "never-there.sock")
    assert missing("nodekey:anything") is None


def test_proxy_v2_end_to_end_with_the_ipc_backed_directory_resolver(
    serve_web, fake_daemon, cert_paths, sock_dir
):
    handlers = _orgfs_handlers()
    handlers["org.network"] = _roster_lookup({
        "acme": [{
            "deviceId": "node-a", "owner": OWNER, "state": "active",
            "serverPublic": "nodekey:peer-server", "masterPublic": "nodekey:client-a",
            "clientPublicByServer": {"nodekey:local-server": MEMBER_NODEKEY},
        }],
    })
    daemon = fake_daemon(handlers)
    listen = f"unix:{sock_dir / 'ipc' / 'web.sock'}"
    server = serve_web(
        _config(
            cert_paths, daemon, listen=listen, client_address=CLIENT_ADDRESS_PROXY_V2
        ),
        None,
        nodekey_resolver=DirectoryNodeKeyResolver(daemon.socket_path),
    )
    status, _headers, body = _https_request(
        server,
        f"/{OWNER}/{SPACE}/doc-1",
        proxy_source=MEMBER_ADDR,
        proxy_tlvs=_nodekey_tlv(MEMBER_NODEKEY),
    )
    assert status == 200
    assert body == b"hello world"
    methods = [method for method, _params in daemon.calls]
    assert methods[0] == "org.network"
    assert daemon.calls[0][1] == {"peerKey": MEMBER_NODEKEY}
    assert methods[1:] == ["orgfs.members", "orgfs.stat", "orgfs.read"]


def test_check_config_reports_cert_key_and_listen_problems(tmp_path: Path):
    cert_path, key_path = _write_self_signed_cert(tmp_path / "ok")

    expired_dir = tmp_path / "expired"
    expired_dir.mkdir()
    now = datetime.now(timezone.utc)
    expired_cert, expired_key = _write_self_signed_cert(
        expired_dir,
        not_before=now - timedelta(days=10),
        not_after=now - timedelta(days=1),
    )
    problems = check_config(
        WebServiceConfig(
            host="fs.test.example",
            listen="127.0.0.1:8443",
            cert=expired_cert,
            key=expired_key,
        )
    )
    assert any("expired" in problem for problem in problems)

    problems = check_config(
        WebServiceConfig(
            host="fs.test.example",
            listen="0.0.0.0:8443",
            cert=cert_path,
            key=key_path,
        )
    )
    assert any("wildcard" in problem for problem in problems)

    problems = check_config(
        WebServiceConfig(
            host="fs.test.example",
            listen="127.0.0.1:8443",
            cert=cert_path,
            key=tmp_path / "ok" / "missing-key.pem",
        )
    )
    assert any("key" in problem for problem in problems)

    # proxy-v2 (the default) on TCP is itself a problem: a forgeable header.
    assert any(
        "unix" in problem
        for problem in check_config(
            WebServiceConfig(
                host="fs.test.example",
                listen="127.0.0.1:8443",
                cert=cert_path,
                key=key_path,
            )
        )
    )
    assert (
        check_config(
            WebServiceConfig(
                host="fs.test.example",
                listen="127.0.0.1:8443",
                cert=cert_path,
                key=key_path,
                client_address=CLIENT_ADDRESS_SOCKET,
            )
        )
        == []
    )


def test_fs_web_check_cli(tmp_path: Path):
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.commands.common.root import app

    cert_path, key_path = _write_self_signed_cert(tmp_path / "ok")
    runner = CliRunner()
    good = runner.invoke(
        app,
        [
            "org", "fs", "space", "web",
            "--listen", "127.0.0.1:8443",
            "--client-address", "socket",
            "--host", "fs.test.example",
            "--cert", str(cert_path),
            "--key", str(key_path),
            "--check",
        ],
    )
    assert good.exit_code == 0, good.output
    assert "ok" in good.output

    bad = runner.invoke(
        app,
        [
            "org", "fs", "space", "web",
            "--listen", "0.0.0.0:8443",
            "--host", "fs.test.example",
            "--cert", str(cert_path),
            "--key", str(key_path),
            "--check",
        ],
    )
    assert bad.exit_code == 1
    assert "wildcard" in bad.output


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        pytest.param("100.64.20.11:51234", "100.64.20.10:443", id="tcp4"),
        pytest.param("[fd7a:115c:a1e0::b]:51234", "[fd7a:115c:a1e0::a]:443", id="tcp6"),
    ],
)
def test_sidecar_wire_header_parses_to_the_member_identity(source, destination):
    header = _go_sidecar_header(source, destination, MEMBER_NODEKEY)
    parsed_source, parsed_port, tlvs = webserver.parse_proxy_v2_block(header)
    assert parsed_source == source.rsplit(":", 1)[0].strip("[]")
    assert parsed_port == 51234
    identity = identity_owner.identity_from_nodekey(
        identity_owner._nodekey_from_tlv(tlvs[PROXY_V2_IDENTITY_TLV_TYPE]),
        DIRECTORY.get,
    )
    assert identity == Identity(login_name=OWNER, tags=())


def test_sidecar_wire_header_unknown_and_malformed_keys_are_not_identities():
    unknown = _go_sidecar_header(
        "100.64.20.11:1", "100.64.20.10:443", UNKNOWN_NODEKEY
    )
    _source, _port, tlvs = webserver.parse_proxy_v2_block(unknown)
    assert (
        identity_owner.identity_from_nodekey(
            identity_owner._nodekey_from_tlv(tlvs[PROXY_V2_IDENTITY_TLV_TYPE]),
            DIRECTORY.get,
        )
        is None
    )

    malformed = _go_sidecar_header(
        "100.64.20.11:1", "100.64.20.10:443", "whois-shape-not-a-nodekey"
    )
    _source, _port, tlvs = webserver.parse_proxy_v2_block(malformed)
    with pytest.raises(ProxyProtocolError):
        identity_owner._nodekey_from_tlv(tlvs[PROXY_V2_IDENTITY_TLV_TYPE])


def test_sidecar_tcp6_header_end_to_end_member_reads(serve_web, fake_daemon, cert_paths, sock_dir):
    server, _daemon = _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir)
    header = _go_sidecar_header(
        "[fd7a:115c:a1e0::b]:51234",
        "[fd7a:115c:a1e0::a]:443",
        MEMBER_NODEKEY,
    )
    raw = _raw_connect(server)
    try:
        raw.sendall(header)
        tls = _client_context().wrap_socket(raw, server_hostname="fs.test.example")
    except BaseException:
        raw.close()
        raise
    connection = http.client.HTTPConnection("fs.test.example", timeout=10)
    try:
        connection.sock = tls
        connection.request("GET", f"/{OWNER}/{SPACE}/doc-1")
        response = connection.getresponse()
        status, body = response.status, response.read()
    finally:
        connection.close()
    assert status == 200
    assert body == b"hello world"
