"""orgfs read-only HTTPS web service — integration tests over real TLS.

Every test talks to the REAL server: a throwaway self-signed certificate in
tmp, the service's own TLS termination, and a fake daemon speaking the real
version-1 newline-delimited JSON IPC protocol on a real unix socket.  Nothing
here touches the production daemon or ``~/.hyprial`` (the suite-wide conftest
isolates ``HYPRIAL_HOME``/``HARNESS_STATE_DIR`` per test).

Brief: docs/notes/orgfs-web/brief.md — HTTPS only, fail-closed identity through a
pluggable ``PeerIdentityResolver``, fail-closed space-membership check through
the daemon IPC, the #934 path form, and the update sections (PROXY v2 /
socket client-address modes, unix listener with a 0700 parent, tagged
identities never members).
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.webserver import (
    CLIENT_ADDRESS_PROXY_V2,
    CLIENT_ADDRESS_SOCKET,
    PROXY_V2_IDENTITY_TLV_TYPE,
    FakePeerIdentityResolver,
    Identity,
    WebServiceConfig,
    build_server,
)

OWNER = "alice@org-a.example"
SPACE = "space-1"
MEMBER_ADDR = "100.64.20.10"
NON_MEMBER_ADDR = "100.64.20.20"
TAGGED_ADDR = "100.64.20.30"
UNKNOWN_ADDR = "100.64.20.40"

MEMBER_IDENTITY = Identity(login_name=OWNER, tags=())
NON_MEMBER_IDENTITY = Identity(login_name="mallory@org-b.example", tags=())
TAGGED_IDENTITY = Identity(login_name="tagged-devices", tags=("tag:e2e",))

NODE_URI_DOC = f"orgfs:{OWNER}:{SPACE}:doc-1"
NODE_URI_BLOB = f"orgfs:{OWNER}:{SPACE}:blob-1"
NODE_URI_DIR = f"orgfs:{OWNER}:{SPACE}:dir-1"
NODE_URI_PENDING = f"orgfs:{OWNER}:{SPACE}:pend-1"

BLOB_BYTES = b"\x89PNG\r\n\x1a\n-fake-"


class _DaemonFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _node(node_id: str, kind: str, name: str, *, content_state: str = "arrived") -> dict:
    return {
        "space_id": SPACE,
        "node_id": node_id,
        "kind": kind,
        "name": name,
        "path": name,
        "size": None,
        "blob_hash": None,
        "doc_id": None,
        "version": "v1",
        "modified_by": f"user:{OWNER}",
        "modified_via": None,
        "name_conflict": False,
        "deleted": False,
        "contentState": content_state,
        "uri": f"orgfs:{OWNER}:{SPACE}:{node_id}",
    }


def _orgfs_handlers() -> dict:
    doc = _node("doc-1", "doc", "readme.md")
    blob = _node("blob-1", "blob", "logo.png")
    directory = _node("dir-1", "dir", "docs")
    pending = _node("pend-1", "doc", "draft.md", content_state="pending")
    nodes = {n["uri"]: n for n in (doc, blob, directory, pending)}

    def stat(params: dict) -> dict:
        node = nodes.get(str(params.get("node", "")))
        if node is None:
            raise _DaemonFailure("unknown-doc")
        return node

    def read(params: dict) -> dict:
        node = nodes.get(str(params.get("node", "")))
        if node is None:
            raise _DaemonFailure("unknown-doc")
        if node["kind"] == "doc":
            if node["contentState"] == "pending":
                raise _DaemonFailure("content-pending")
            return {"node": node, "text": "hello world", "version": "v1"}
        if node["kind"] == "blob":
            return {
                "node": node,
                "contentB64": base64.b64encode(BLOB_BYTES).decode("ascii"),
            }
        raise _DaemonFailure("invalid-argument")

    def ls(params: dict) -> dict:
        if str(params.get("path", "")) != NODE_URI_DIR:
            raise _DaemonFailure("unknown-doc")
        return {"nodes": [doc, blob]}

    return {
        "orgfs.members": lambda params: {
            "members": [
                {"space_id": SPACE, "user": f"user:{OWNER}", "mode": "rw"},
                {"space_id": SPACE, "user": "user:bob@org-a.example", "mode": "ro"},
            ]
        },
        "orgfs.stat": stat,
        "orgfs.read": read,
        "orgfs.ls": ls,
    }


class _FakeDaemon:
    """A real unix-socket server speaking the daemon's NDJSON v1 IPC."""

    def __init__(self, socket_path: Path, handlers: dict) -> None:
        self.socket_path = socket_path
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(str(socket_path))
        except OSError:
            self._listener.close()
            raise
        self._listener.listen(16)
        self._listener.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        data = bytearray()
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data.extend(chunk)
        frame = json.loads(data.split(b"\n", 1)[0])
        method = str(frame.get("method"))
        params = frame.get("params") or {}
        self.calls.append((method, params))
        handler = self.handlers.get(method)
        try:
            if handler is None:
                raise _DaemonFailure("unknown-method")
            result = handler(params)
            reply = {"version": 1, "id": frame["id"], "result": result}
        except _DaemonFailure as failure:
            reply = {
                "version": 1,
                "id": frame["id"],
                "error": {"code": failure.code, "message": failure.code},
            }
        conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)


@pytest.fixture
def sock_dir():
    """A short /tmp dir: pytest's tmp_path exceeds the AF_UNIX path limit."""

    with tempfile.TemporaryDirectory(prefix="ow-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def fake_daemon(sock_dir: Path):
    daemons: list[_FakeDaemon] = []

    def start(handlers: dict | None = None) -> _FakeDaemon:
        daemon = _FakeDaemon(sock_dir / f"daemon-{len(daemons)}.sock", handlers or _orgfs_handlers())
        daemons.append(daemon)
        return daemon

    yield start
    for daemon in daemons:
        daemon.close()


def _write_self_signed_cert(
    directory: Path,
    *,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fs.test.example")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (now - timedelta(days=1)))
        .not_valid_after(not_after or (now + timedelta(days=30)))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("fs.test.example")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def cert_paths(tmp_path: Path) -> tuple[Path, Path]:
    return _write_self_signed_cert(tmp_path)


def _config(
    cert_paths: tuple[Path, Path],
    daemon: _FakeDaemon | None,
    *,
    listen: str = "127.0.0.1:0",
    client_address: str = CLIENT_ADDRESS_SOCKET,
    retry_after_seconds: int = 3,
) -> WebServiceConfig:
    return WebServiceConfig(
        host="fs.test.example",
        listen=listen,
        cert=cert_paths[0],
        key=cert_paths[1],
        client_address=client_address,
        retry_after_seconds=retry_after_seconds,
        daemon_socket=daemon.socket_path if daemon is not None else None,
    )


@pytest.fixture
def serve_web():
    servers: list[object] = []

    def start(
        config: WebServiceConfig,
        resolver: object | None,
        nodekey_resolver: object | None = None,
    ):
        server = build_server(
            config, resolver=resolver, nodekey_resolver=nodekey_resolver
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    time.sleep(0.05)


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def build_proxy_v2_header(
    source_address: str,
    source_port: int = 4321,
    *,
    tlvs: bytes = b"",
) -> bytes:
    signature = b"\r\n\r\n\x00\r\nQUIT\n"
    src = socket.inet_pton(socket.AF_INET, source_address)
    dst = socket.inet_pton(socket.AF_INET, "100.64.20.2")
    addresses = src + dst + source_port.to_bytes(2, "big") + (443).to_bytes(2, "big")
    block = addresses + tlvs
    return signature + b"\x21\x11" + len(block).to_bytes(2, "big") + block


def _https_request(
    server,
    path: str,
    *,
    method: str = "GET",
    proxy_source: str | None = None,
    proxy_tlvs: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.server_address
    if server.address_family == socket.AF_UNIX:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.connect(endpoint)
    else:
        raw = socket.create_connection((endpoint[0], endpoint[1]), timeout=10)
    try:
        if proxy_source is not None:
            raw.sendall(build_proxy_v2_header(proxy_source, tlvs=proxy_tlvs))
        tls = _client_context().wrap_socket(raw, server_hostname="fs.test.example")
    except BaseException:
        raw.close()
        raise
    connection = http.client.HTTPConnection("fs.test.example", timeout=10)
    try:
        connection.sock = tls
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def _socket_mode_server(serve_web, fake_daemon, cert_paths, identity_by_addr, daemon=None):
    daemon = daemon or fake_daemon()
    resolver = FakePeerIdentityResolver(identity_by_addr)
    server = serve_web(_config(cert_paths, daemon), resolver)
    return server, daemon, resolver


def _proxy_mode_server(serve_web, fake_daemon, cert_paths, sock_dir, daemon=None, directory=None):
    """proxy-v2 mode: a unix listener; identity only from the front's TLV 0xE0."""

    daemon = daemon or fake_daemon()
    listen = f"unix:{sock_dir / 'fs' / 'web.sock'}"
    server = serve_web(
        _config(
            cert_paths, daemon, listen=listen, client_address=CLIENT_ADDRESS_PROXY_V2
        ),
        None,
        nodekey_resolver=(directory if directory is not None else DIRECTORY).get,
    )
    return server, daemon


MEMBER_NODEKEY = "nodekey:member-public-key-1"
NON_MEMBER_NODEKEY = "nodekey:nonmember-public-key-2"
UNKNOWN_NODEKEY = "nodekey:unknown-public-key-3"

MEMBER_IDENTITY = Identity(login_name=OWNER, tags=())
NON_MEMBER_IDENTITY = Identity(login_name="mallory@org-b.example", tags=())
TAGGED_IDENTITY = Identity(login_name="tagged-devices", tags=("tag:e2e",))

#: nodekey → owner, the org-directory resolver the daemon injects.
DIRECTORY = {
    MEMBER_NODEKEY: OWNER,
    NON_MEMBER_NODEKEY: "mallory@org-b.example",
}


def _garbage_tlv(payload: bytes) -> bytes:
    return bytes([PROXY_V2_IDENTITY_TLV_TYPE]) + len(payload).to_bytes(2, "big") + payload


def _nodekey_tlv(nodekey: str) -> bytes:
    value = nodekey.encode()
    return bytes([PROXY_V2_IDENTITY_TLV_TYPE]) + len(value).to_bytes(2, "big") + value


def _raw_connect(server) -> socket.socket:
    if server.address_family == socket.AF_UNIX:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(10)
        raw.connect(server.server_address)
        return raw
    host, port = server.server_address[:2]
    return socket.create_connection((host, port), timeout=10)




# --- the brief's route/status surface -------------------------------------










































# --- update 2: the PROXY v2 client-address mode ----------------------------












# --- update 3: the unix-socket listener ------------------------------------










# --- the tailscaled LocalAPI whois resolver --------------------------------






# --- operability: --check ---------------------------------------------------




# --- the CLI surface --------------------------------------------------------




# --- wire compatibility with the tsnet sidecar (PR #943) --------------------


def _go_sidecar_header(source: str, destination: str, nodekey: str) -> bytes:
    """Byte-for-byte port of the Tailcat sidecar's PROXY v2 header
    (sidecar/hyprial-tailcat, protocol v3): TLV 0xE0 carries the peer's
    nodekey as UTF-8 text — no JSON identity, no whois."""

    import ipaddress as _ip

    src_host, src_port = source.rsplit(":", 1)
    dst_host, dst_port = destination.rsplit(":", 1)
    src = _ip.ip_address(src_host.strip("[]"))
    dst = _ip.ip_address(dst_host.strip("[]"))
    encoded = nodekey.encode()
    family, address_length = (0x11, 12) if src.version == 4 else (0x21, 36)
    payload_length = address_length + 3 + len(encoded)
    header = bytearray(b"\r\n\r\n\x00\r\nQUIT\n")
    header += bytes([0x21, family]) + payload_length.to_bytes(2, "big")
    header += src.packed + dst.packed
    header += int(src_port).to_bytes(2, "big") + int(dst_port).to_bytes(2, "big")
    header += bytes([0xE0]) + len(encoded).to_bytes(2, "big") + encoded
    return bytes(header)








# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'DIRECTORY',
    'MEMBER_NODEKEY',
    'NON_MEMBER_NODEKEY',
    'UNKNOWN_NODEKEY',
    '_garbage_tlv',
    '_nodekey_tlv',
    'BLOB_BYTES',
    'MEMBER_ADDR',
    'MEMBER_IDENTITY',
    'NODE_URI_BLOB',
    'NODE_URI_DIR',
    'NODE_URI_DOC',
    'NODE_URI_PENDING',
    'NON_MEMBER_ADDR',
    'NON_MEMBER_IDENTITY',
    'OWNER',
    'SPACE',
    'TAGGED_ADDR',
    'TAGGED_IDENTITY',
    'UNKNOWN_ADDR',
    '_DaemonFailure',
    '_FakeDaemon',
    '_client_context',
    '_config',
    '_go_sidecar_header',
    '_https_request',
    '_node',
    '_orgfs_handlers',
    '_proxy_mode_server',
    '_raw_connect',
    '_socket_mode_server',
    '_write_self_signed_cert',
    'build_proxy_v2_header',
    'cert_paths',
    'fake_daemon',
    'serve_web',
    'sock_dir',
]
