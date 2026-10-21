"""TLS 1.3 and exact-SPKI-pin primitives for the opt-in pairing server.

The server refuses anything below TLS 1.3, disables compression and ticket
based resumption, and requires a P-256 host certificate whose private key is
the pairing challenge signing key.  The client never loads a system CA store
and never checks a hostname: it requires a verified chain against one explicit
certificate file supplied by the caller, then checks the exact SHA-256 SPKI
pin.  The shipped client helper is for local tests, not a production mobile
client.

CPython's :mod:`ssl` module has no public per-handshake certificate callback.
The pin is therefore checked immediately after the TLS handshake completes
and before this module returns a socket or writes an application byte.  A
mismatch closes the connection with :class:`PairingTlsError`; no pairing
frame can be sent through that connection.
"""

from __future__ import annotations

import hmac
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ..crypto import CryptoError, host_key_fingerprint, public_key_raw

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")

MATERIAL_INVALID = "pairing-tls-material-invalid"
CERTIFICATE_INVALID = "pairing-tls-certificate-invalid"
PIN_INVALID = "pairing-tls-pin-invalid"
PIN_MISMATCH = "pairing-tls-pin-mismatch"
HANDSHAKE_FAILED = "pairing-tls-handshake-failed"


class PairingTlsError(Exception):
    """A closed-set TLS/pin refusal that never carries key material.

    The audit layer records only :attr:`code` and a generic public message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HostTlsMaterial:
    """Validated temporary or persistent P-256 host TLS material.

    Loading this object reads the certificate and private key from disk.  It
    contains no pairing code, client key or request key.  Persistence and
    lifecycle are deliberately out of scope for this slice.
    """

    certfile: Path
    keyfile: Path
    certificate: x509.Certificate
    private_key: ec.EllipticCurvePrivateKey
    fingerprint: str


def _path(value: object, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise PairingTlsError(MATERIAL_INVALID, f"{field} must be a path")
    return Path(value)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PairingTlsError(
            MATERIAL_INVALID, "host TLS material is unreadable"
        ) from error


def _require_p256_public(
    key: object, *, code: str = MATERIAL_INVALID
) -> bytes:
    if (
        not isinstance(key, ec.EllipticCurvePublicKey)
        or key.curve.name != "secp256r1"
    ):
        raise PairingTlsError(code, "host key must be P-256")
    try:
        return public_key_raw(key)
    except CryptoError as error:
        raise PairingTlsError(code, "host key must be P-256") from error


def _require_p256_private(
    key: object, *, code: str = MATERIAL_INVALID
) -> ec.EllipticCurvePrivateKey:
    if (
        not isinstance(key, ec.EllipticCurvePrivateKey)
        or key.curve.name != "secp256r1"
    ):
        raise PairingTlsError(code, "host key must be P-256")
    _require_p256_public(key.public_key(), code=code)
    return key


def host_key_fingerprint_from_certificate(
    certificate: x509.Certificate,
) -> str:
    """Return the exact ``sha256:<hex>`` pin for a certificate's SPKI."""

    if not isinstance(certificate, x509.Certificate):
        raise PairingTlsError(
            CERTIFICATE_INVALID, "peer certificate is invalid"
        )
    _require_p256_public(certificate.public_key(), code=CERTIFICATE_INVALID)
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return host_key_fingerprint(spki)


def load_host_tls_material(
    certfile: object, keyfile: object
) -> HostTlsMaterial:
    """Load and validate a P-256 certificate/key pair without listening."""

    cert_path = _path(certfile, "certfile")
    key_path = _path(keyfile, "keyfile")
    cert_bytes = _read(cert_path)
    key_bytes = _read(key_path)
    try:
        certificate = x509.load_pem_x509_certificate(cert_bytes)
    except ValueError as error:
        raise PairingTlsError(
            MATERIAL_INVALID, "host certificate is invalid"
        ) from error
    try:
        private_key = serialization.load_pem_private_key(
            key_bytes, password=None
        )
    except (ValueError, TypeError, UnsupportedAlgorithm) as error:
        raise PairingTlsError(
            MATERIAL_INVALID, "host private key is invalid"
        ) from error
    checked_key = _require_p256_private(private_key)
    cert_public = _require_p256_public(certificate.public_key())
    key_public = _require_p256_public(checked_key.public_key())
    if not hmac.compare_digest(cert_public, key_public):
        raise PairingTlsError(
            MATERIAL_INVALID, "host certificate and key do not match"
        )
    return HostTlsMaterial(
        certfile=cert_path,
        keyfile=key_path,
        certificate=certificate,
        private_key=checked_key,
        fingerprint=host_key_fingerprint_from_certificate(certificate),
    )


def _harden(context: ssl.SSLContext) -> None:
    """Apply the minimum-version and downgrade/compression guardrails."""

    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_3
    except (ValueError, ssl.SSLError) as error:
        raise PairingTlsError(
            HANDSHAKE_FAILED, "TLS 1.3 is unavailable"
        ) from error
    context.options |= ssl.OP_NO_COMPRESSION
    # Ticket/PSK resumption must stay off: a resumed TLS 1.3 session may not
    # present a certificate, which would make an SPKI pin unverifiable.
    context.options |= ssl.OP_NO_TICKET
    try:
        context.num_tickets = 0
    except (AttributeError, ValueError):
        pass
    if hasattr(ssl, "OP_NO_RENEGOTIATION"):
        context.options |= ssl.OP_NO_RENEGOTIATION


def server_ssl_context(material: HostTlsMaterial) -> ssl.SSLContext:
    """Build the TLS 1.3 server context for validated host material."""

    if not isinstance(material, HostTlsMaterial):
        raise PairingTlsError(
            MATERIAL_INVALID, "host TLS material is invalid"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.verify_mode = ssl.CERT_NONE
    _harden(context)
    try:
        context.load_cert_chain(
            certfile=str(material.certfile), keyfile=str(material.keyfile)
        )
    except (OSError, ssl.SSLError) as error:
        raise PairingTlsError(
            MATERIAL_INVALID, "host TLS material is unreadable"
        ) from error
    return context


def _client_context(trust_certfile: object) -> ssl.SSLContext:
    """A pin-only client context with one explicit trust anchor.

    Only the exact certificate file supplied by the caller is loaded.  The
    client requires a verified chain (``CERT_REQUIRED``), disables hostname
    matching, and never loads the system CA store.  The exact SPKI pin is
    checked after the handshake and before any application byte.
    """

    path = _path(trust_certfile, "trust_certfile")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    _harden(context)
    try:
        context.load_verify_locations(cafile=str(path))
    except (OSError, ssl.SSLError, ValueError) as error:
        raise PairingTlsError(
            CERTIFICATE_INVALID, "trust anchor is invalid"
        ) from error
    return context


def _expected_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise PairingTlsError(PIN_INVALID, "expected SPKI pin is invalid")
    return value


def verify_pinned_peer(
    cert_der: object,
    expected_fingerprint: object,
    *,
    now: datetime | None = None,
) -> None:
    """Verify one peer certificate against the exact SPKI pin.

    A wrong pin, a non-P-256 key, an unparsable certificate or an invalid
    validity window is refused.  The expected certificate is never inferred
    from the peer, so first-accept/TOFU cannot happen.
    """

    expected = _expected_fingerprint(expected_fingerprint)
    if not isinstance(cert_der, (bytes, bytearray, memoryview)):
        raise PairingTlsError(
            CERTIFICATE_INVALID, "peer certificate is invalid"
        )
    raw = bytes(cert_der)
    if not raw:
        raise PairingTlsError(
            CERTIFICATE_INVALID, "peer certificate is invalid"
        )
    try:
        certificate = x509.load_der_x509_certificate(raw)
    except ValueError as error:
        raise PairingTlsError(
            CERTIFICATE_INVALID, "peer certificate is invalid"
        ) from error
    actual = host_key_fingerprint_from_certificate(certificate)
    current = now or datetime.now(timezone.utc)
    if (
        current < certificate.not_valid_before_utc
        or current > certificate.not_valid_after_utc
    ):
        raise PairingTlsError(
            CERTIFICATE_INVALID, "peer certificate is not currently valid"
        )
    if not hmac.compare_digest(actual, expected):
        raise PairingTlsError(PIN_MISMATCH, "peer SPKI pin does not match")


def connect_pinned(
    host: str,
    port: int,
    *,
    fingerprint: str,
    trust_certfile: str | Path,
    timeout: float = 10.0,
) -> ssl.SSLSocket:
    """Connect with TLS 1.3 and return a socket only after the pin matches.

    ``trust_certfile`` is the only trust anchor; no system CA store, hostname
    or TOFU fallback is used.  The TLS handshake must verify against that
    exact certificate, and the completed handshake is then checked against
    ``fingerprint`` before this function returns.  Callers cannot send a
    pairing hello through an unverified, unpinned or mismatched channel.
    This helper is for local tests, not a production mobile client.
    """

    expected = _expected_fingerprint(fingerprint)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not timeout > 0
    ):
        raise ValueError("timeout must be a positive number")
    context = _client_context(trust_certfile)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        conn = context.wrap_socket(raw, server_hostname=None)
    except (ssl.SSLError, OSError) as error:
        try:
            raw.close()
        except OSError:
            pass
        raise PairingTlsError(
            HANDSHAKE_FAILED, "TLS handshake failed"
        ) from error
    try:
        cert_der = conn.getpeercert(binary_form=True)
        verify_pinned_peer(cert_der, expected)
    except PairingTlsError:
        try:
            conn.close()
        except OSError:
            pass
        raise
    return conn
