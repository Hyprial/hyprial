"""Local-only TLS harness for the S3 pairing-server tests.

Every certificate and key is generated below pytest's temporary directory and
never leaves it.  The client loads only the expected host certificate as its
trust anchor (``CERT_REQUIRED`` with hostname matching disabled) and then
checks the exact SPKI pin after the handshake.  No system CA store or TOFU
fallback is used.
"""

from __future__ import annotations

import ipaddress
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.x509.oid import NameOID

from hyprial.shell.impl.gui.adapter.mobile.pairing import (
    ClientPairingState,
    encode_frame,
    encode_public_key,
    host_key_fingerprint,
)
from hyprial.shell.impl.gui.adapter.mobile.pairing.server import (
    PairingServer,
    connect_pinned,
    read_frame,
)
from hyprial.shell.impl.gui.adapter.mobile.store import MobileAccessStore
from tests.shell.gui.adapter.mobile.helpers import (
    NODE,
    OWNER,
    PERMISSIONS,
)


@dataclass(frozen=True)
class TlsMaterial:
    """Temporary P-256 TLS material plus an unrelated derive key."""

    certfile: Path
    keyfile: Path
    certificate: x509.Certificate
    private_key: ec.EllipticCurvePrivateKey
    derive_private_key: ec.EllipticCurvePrivateKey
    fingerprint: str


def _p256_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _self_signed(
    private_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "m11-pairing-test")]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )


def make_tls_material(directory: Path) -> TlsMaterial:
    """Write one temporary P-256 certificate/key pair below ``directory``."""

    directory.mkdir(parents=True, exist_ok=True)
    private_key = _p256_key()
    certificate = _self_signed(private_key)
    certfile = directory / "host.crt"
    keyfile = directory / "host.key"
    certfile.write_bytes(certificate.public_bytes(Encoding.PEM))
    keyfile.write_bytes(
        private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    keyfile.chmod(0o600)
    spki = certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return TlsMaterial(
        certfile=certfile,
        keyfile=keyfile,
        certificate=certificate,
        private_key=private_key,
        derive_private_key=_p256_key(),
        fingerprint=host_key_fingerprint(spki),
    )


def write_rsa_material(directory: Path) -> tuple[Path, Path]:
    """Create an RSA certificate/key pair for the fail-closed key test."""

    directory.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    certificate = _self_signed(private_key)
    certfile = directory / "rsa.crt"
    keyfile = directory / "rsa.key"
    certfile.write_bytes(certificate.public_bytes(Encoding.PEM))
    keyfile.write_bytes(
        private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    return certfile, keyfile


class AuditRecorder:
    """Thread-safe in-memory audit sink used instead of production storage."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[dict] = []

    def __call__(self, event: dict) -> None:
        with self._condition:
            self._events.append(dict(event))
            self._condition.notify_all()

    def snapshot(self) -> list[dict]:
        with self._condition:
            return [dict(event) for event in self._events]

    def wait_for(self, name: str, *, timeout: float = 3.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                matches = [
                    dict(event)
                    for event in self._events
                    if event.get("event") == name
                ]
                if matches:
                    return matches
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    seen = [event.get("event") for event in self._events]
                    raise AssertionError(
                        f"audit event {name!r} not observed; saw {seen!r}"
                    )
                self._condition.wait(remaining)


class RecordingSocket:
    """Record application bytes received from the server for leak checks."""

    def __init__(self, sock: ssl.SSLSocket) -> None:
        self._sock = sock
        self.received = bytearray()

    def recv(self, size: int) -> bytes:
        data = self._sock.recv(size)
        self.received.extend(data)
        return data

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "RecordingSocket":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def new_client(
    material: TlsMaterial,
    *,
    fingerprint: str | None = None,
) -> tuple[ClientPairingState, str]:
    """Build a client state whose pinned key is the test certificate's key."""

    private_key = _p256_key()
    client = ClientPairingState(
        node_id=NODE,
        host_key_fingerprint=fingerprint or material.fingerprint,
        host_public_key=material.private_key.public_key(),
        client_private_key=private_key,
    )
    return client, encode_public_key(private_key.public_key())


def send_frame(sock: RecordingSocket, frame: dict) -> None:
    sock.sendall(encode_frame(frame))


def open_channel(
    server: PairingServer,
    material: TlsMaterial,
) -> tuple[RecordingSocket, ClientPairingState, str, dict]:
    """Open TLS, send hello, verify the challenge and return the channel."""

    client, client_public = new_client(material)
    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=material.fingerprint,
        trust_certfile=material.certfile,
        timeout=3.0,
    )
    recording = RecordingSocket(sock)
    send_frame(recording, client.hello())
    challenge = client.accept_challenge(read_frame(recording, timeout=3.0))
    return recording, client, client_public, challenge


def redeem_once(
    server: PairingServer,
    material: TlsMaterial,
    code: str,
) -> dict:
    """Complete one pending pairing request; used to prove a code still works."""

    recording, client, _client_public, _challenge = open_channel(
        server, material
    )
    try:
        send_frame(
            recording,
            client.redeem_start(
                code=code,
                device_id="dev-a",
                device_uid="uid-a",
                name="mate phone",
                owner=OWNER,
                permissions=PERMISSIONS,
            ),
        )
        return client.accept_status(read_frame(recording, timeout=3.0))
    finally:
        client.clear()
        recording.close()


def build_server(
    authority: object,
    material: TlsMaterial,
    **overrides: object,
) -> PairingServer:
    """Construct a server without starting it (the default-off boundary)."""

    options: dict[str, object] = {
        "node_id": NODE,
        "authority": authority,
        "certfile": material.certfile,
        "keyfile": material.keyfile,
        "host_derive_key": material.derive_private_key,
        "port": 0,
        "handshake_timeout": 2.0,
        "idle_timeout": 3.0,
    }
    clock = getattr(authority, "clock", None)
    if callable(clock):
        options["clock"] = clock
    options.setdefault("audit", lambda _event: None)
    options.update(overrides)
    return PairingServer(**options)


def pairing_request_count(store: MobileAccessStore) -> int:
    """Count authoritative pairing requests, not just wire attempts."""

    return sum(
        1
        for event in store.audit_events(limit=500)
        if event["event"] == "pairing-requested"
    )


def tls12_client_context(trust_certfile: Path) -> ssl.SSLContext:
    """A verified client capped below TLS 1.3, for the downgrade probe."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(trust_certfile))
    return context
