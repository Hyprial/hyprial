"""Known algorithm failures at the transport cryptography boundary."""

from __future__ import annotations

from typing import Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from hyprial.identity.impl.device.transport_storage import TransportCredentialError


def certificate_public_key(certificate: x509.Certificate) -> Any:
    try:
        return certificate.public_key()
    except UnsupportedAlgorithm:
        pass
    raise _unsupported_certificate_algorithm()


def certificate_signature_hash(certificate: x509.Certificate) -> hashes.HashAlgorithm | None:
    try:
        return certificate.signature_hash_algorithm
    except UnsupportedAlgorithm:
        pass
    raise _unsupported_certificate_algorithm()


def verify_certificate_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    """Preserve the bounded verifier's algorithms and ordinary signature errors."""

    public_key = certificate_public_key(issuer)
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                child.signature,
                child.tbs_certificate_bytes,
                padding.PKCS1v15(),
                certificate_signature_hash(child),
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(certificate_signature_hash(child)),
            )
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(child.signature, child.tbs_certificate_bytes)
        else:
            raise TypeError("unsupported certificate public key")
    except UnsupportedAlgorithm:
        pass
    else:
        return
    raise _unsupported_certificate_algorithm()


def load_private_key_bytes(encoded: bytes) -> Any:
    try:
        return serialization.load_pem_private_key(encoded, password=None)
    except UnsupportedAlgorithm:
        pass
    raise TransportCredentialError(
        "TRANSPORT_STAGING_CONFLICT", "transport staging key is unreadable"
    )


def _unsupported_certificate_algorithm() -> TransportCredentialError:
    return TransportCredentialError(
        "TRANSPORT_RESPONSE_INVALID", "transport certificate algorithm is unsupported"
    )
