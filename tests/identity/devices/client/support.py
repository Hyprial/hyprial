"""Loopback TLS authority doubles for the one-request client witnesses."""

from __future__ import annotations

import json
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.identity.devices.binding import support as binding_support
from hyprial.identity.impl.device.client import ClientTlsMaterial

ENVELOPE_VERSION = "hyprial.transport.v1"


@dataclass
class Observation:
    connections: int = 0
    requests: list[bytes] = field(default_factory=list)
    extra_after_response: int = 0
    closed_by_client: bool = False
    client_certificate_der: bytes | None = None
    errors: list[str] = field(default_factory=list)


def certificate_data(**overrides) -> dict[str, object]:
    claims = binding_support.frozen_vectors()["vectors"][0]["input"]
    data: dict[str, object] = {
        "ownerUserId": claims["ownerUserId"],
        "owner": claims["owner"],
        "bindingId": claims["bindingId"],
        "deviceUid": claims["deviceUid"],
        "credentialGeneration": claims["credentialGeneration"],
        "certificatePem": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        "chainPem": "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n",
        "cn": "hdev-" + str(claims["bindingId"]) + "-g1",
        "spkiSha256": claims["spkiSha256"],
        "serial": "01",
        "notBefore": 1792560000,
        "notAfter": 1792563600,
        "renewableUntil": claims["renewableUntil"],
        "trustBundleId": claims["trustBundleId"],
        "policyIssuer": claims["policyIssuer"],
    }
    data.update(overrides)
    return data


def success_envelope(operation_id: str, data: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "ok": True,
            "version": ENVELOPE_VERSION,
            "operationId": operation_id,
            "data": data,
        },
        separators=(",", ":"),
    ).encode()


def error_envelope(
    operation_id: str, code: str, retryable: bool, *, data: object = None
) -> bytes:
    return json.dumps(
        {
            "ok": False,
            "version": ENVELOPE_VERSION,
            "operationId": operation_id,
            "code": code,
            "retryable": retryable,
            "data": data,
        },
        separators=(",", ":"),
    ).encode()


def raw_response(
    body: bytes,
    *,
    status: str = "200 OK",
    content_length: int | None = None,
    connection: str = "close",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    headers = [
        f"HTTP/1.1 {status}",
        "Content-Type: application/json",
        f"Content-Length: {len(body) if content_length is None else content_length}",
        f"Connection: {connection}",
    ]
    headers += [f"{name}: {value}" for name, value in extra_headers]
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + body


class LoopbackAuthority:
    """One-shot TLS server that records exactly what the client sent."""

    def __init__(
        self,
        responders,
        *,
        ca=None,
        server_ca=None,
        require_client_cert: bool = False,
        accept_timeout: float = 4.0,
        max_sessions: int = 1,
    ) -> None:
        self.ca = ca or binding_support.make_ca("client root")
        self.server_ca = server_ca or self.ca
        self.responders = list(responders)
        self.observation = Observation()
        self.require_client_cert = require_client_cert
        self.accept_timeout = accept_timeout
        self.max_sessions = max_sessions
        self._listener: socket.socket | None = None
        self._port = 0
        self._thread: threading.Thread | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    def __enter__(self) -> "LoopbackAuthority":
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._port = self._listener.getsockname()[1]
        self._listener.listen(2)
        self._temporary = tempfile.TemporaryDirectory(prefix="hyprial-authority-tls-")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._thread is not None:
            self._thread.join(timeout=self.accept_timeout + 2.0)
        if self._listener is not None:
            self._listener.close()
        if self._temporary is not None:
            self._temporary.cleanup()

    def _server_certificate_files(self) -> tuple[str, str]:
        directory = Path(self._temporary.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = binding_support.issue_leaf(
            self.server_ca,
            key.public_key(),
            common_name="authority.test",
            dns_sans=("authority.test",),
            ip_addresses=("127.0.0.1",),
        )
        key_path = directory / "server-key.pem"
        cert_path = directory / "server-cert.pem"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        return str(cert_path), str(key_path)

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert_path, key_path = self._server_certificate_files()
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        if self.require_client_cert:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cadata=self.ca.pem.decode("ascii"))
        return context

    def _serve(self) -> None:
        try:
            context = self._context()
            for index in range(max(1, self.max_sessions)):
                self._listener.settimeout(
                    self.accept_timeout if index == 0 else 0.5
                )
                try:
                    connection, _address = self._listener.accept()
                except socket.timeout:
                    if index == 0:
                        self.observation.errors.append("no-connection")
                    return
                self.observation.connections += 1
                self._handle(context, connection)
        except BaseException as error:  # noqa: BLE001 - recorded, not raised
            self.observation.errors.append(type(error).__name__)

    def _handle(self, context: ssl.SSLContext, connection: socket.socket) -> None:
        try:
            connection.settimeout(self.accept_timeout)
            tls = context.wrap_socket(connection, server_side=True)
        except (ssl.SSLError, OSError):
            connection.close()
            self.observation.errors.append("handshake")
            return
        try:
            self.observation.client_certificate_der = tls.getpeercert(binary_form=True)
            tls.settimeout(self.accept_timeout)
            request, leftover = self._read_request(tls)
            self.observation.requests.append(request)
            if leftover:
                self.observation.extra_after_response += len(leftover)
                self.observation.requests.append(leftover)
            responder = (
                self.responders.pop(0) if self.responders else (lambda _request: None)
            )
            response = responder(request)
            if response:
                tls.sendall(response)
            tls.settimeout(0.5)
            extra = bytearray()
            while True:
                try:
                    chunk = tls.recv(4096)
                except (socket.timeout, OSError, ssl.SSLError):
                    break
                if not chunk:
                    self.observation.closed_by_client = True
                    break
                extra += chunk
            if extra:
                self.observation.extra_after_response += len(extra)
                self.observation.requests.append(bytes(extra))
        finally:
            tls.close()

    @staticmethod
    def _read_request(tls: ssl.SSLSocket) -> tuple[bytes, bytes]:
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            try:
                chunk = tls.recv(4096)
            except (socket.timeout, OSError, ssl.SSLError):
                return bytes(buffer), b""
            if not chunk:
                return bytes(buffer), b""
            buffer += chunk
        head, _, body = bytes(buffer).partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(body) < length:
            try:
                chunk = tls.recv(4096)
            except (socket.timeout, OSError, ssl.SSLError):
                break
            if not chunk:
                break
            body += chunk
        accepted = body[:length]
        return head + b"\r\n\r\n" + accepted, body[length:]


def client_material(ca=None) -> ClientTlsMaterial:
    authority_ca = ca or binding_support.make_ca("client root")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = binding_support.issue_leaf(
        authority_ca, key.public_key(), common_name="device-client", dns_sans=("device.test",)
    )
    return ClientTlsMaterial(
        certificate_chain_pem=certificate.public_bytes(serialization.Encoding.PEM),
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        trust_bundle_pem=authority_ca.pem,
    )


def wait_for_thread(server: LoopbackAuthority, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while server._thread is not None and server._thread.is_alive():
        if time.monotonic() > deadline:
            break
        time.sleep(0.01)
