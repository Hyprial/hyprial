"""Typed boundary for the library's lazy certificate-extension decoding."""

from __future__ import annotations

from cryptography import x509

from hyprial.identity.impl.device.transport_storage import TransportCredentialError


def read_certificate_extensions(certificate: x509.Certificate) -> x509.Extensions:
    """Return decoded extensions without exposing known decoder exceptions."""

    try:
        return certificate.extensions
    except (x509.DuplicateExtension, x509.UnsupportedGeneralNameType, ValueError):
        pass
    # Raising after the handler detaches the original decoder's public context.
    raise TransportCredentialError(
        "TRANSPORT_RESPONSE_INVALID", "transport certificate extensions cannot be decoded"
    )
