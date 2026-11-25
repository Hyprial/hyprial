"""Client TLS material read through the M1 safe readers, never by path."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hyprial.identity.impl.device.client.errors import (
    CLIENT_CONFIG_INVALID,
    CREDENTIAL_ABSENT,
    OUTCOME_NOT_SENT,
    TransportClientError,
    _public_error_boundary,
)
from hyprial.identity.impl.device.transport_credential import (
    TRANSPORT_KEY_RELPATH,
    TRANSPORT_TRUST_BUNDLE_RELPATH,
    read_transport_credential,
)
from hyprial.identity.impl.device.transport_storage import (
    TransportCredentialError,
    _read_safe_file,
)


@dataclass(frozen=True, slots=True)
class ClientTlsMaterial:
    """In-memory mTLS material for one device request; never logged."""

    certificate_chain_pem: bytes = field(repr=False)
    private_key_pem: bytes = field(repr=False)
    trust_bundle_pem: bytes = field(repr=False)

    @_public_error_boundary
    def __post_init__(self) -> None:
        # The material type is part of the public surface, so an invalid field
        # must fail typed at construction instead of reaching os.write and
        # raising a raw TypeError out of the client.
        for value in (
            self.certificate_chain_pem,
            self.private_key_pem,
            self.trust_bundle_pem,
        ):
            if not isinstance(value, bytes) or not value:
                raise TransportClientError(
                    CLIENT_CONFIG_INVALID,
                    retryable=False,
                    outcome=OUTCOME_NOT_SENT,
                )


@_public_error_boundary
def load_client_tls_material(home: Path) -> ClientTlsMaterial:
    """Return validated key/chain/bundle bytes or a typed client failure."""

    home = Path(home)
    try:
        credential = read_transport_credential(home)
    except TransportCredentialError as error:
        raise _from_credential_error(error) from None
    if credential is None:
        raise TransportClientError(
            CREDENTIAL_ABSENT,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        )
    try:
        key = _read_safe_file(home / TRANSPORT_KEY_RELPATH, 0o600)
        bundle = _read_safe_file(home / TRANSPORT_TRUST_BUNDLE_RELPATH, 0o644)
    except TransportCredentialError as error:
        raise _from_credential_error(error) from None
    except OSError:
        raise TransportClientError(
            CREDENTIAL_ABSENT,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        ) from None
    try:
        chain = (credential.certificate_pem + credential.chain_pem).encode("ascii")
    except UnicodeError:
        raise TransportClientError(
            CREDENTIAL_ABSENT,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        ) from None
    return ClientTlsMaterial(
        certificate_chain_pem=chain,
        private_key_pem=key,
        trust_bundle_pem=bundle,
    )


def _from_credential_error(error: TransportCredentialError) -> TransportClientError:
    return TransportClientError(
        error.code,
        retryable=False,
        outcome=OUTCOME_NOT_SENT,
    )
