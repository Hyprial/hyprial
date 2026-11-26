"""Service-grant profile: a credential sealed to one recipient device key, unsigned.

Decision (service grant S2, 2026-11-26, plan v2 D4 and section 3.4): no owner
signature; the grant document's author rule in OrgFS is the only statement of
who wrote it. The header binds org, grantId, the service endpoint
(grantor owner, deviceId, port), the recipient device (owner, deviceId) and the
recipient's encryptionKeyId; it is the AEAD associated data and is mixed into
the HKDF info, so a ciphertext copied into another grant document, or re-labelled
for another key, does not open. Opening requires the caller to rebuild the
expected header from the document it read; the envelope's own header is never
trusted on its own. The canonical encoding is the bounded canonical JSON of the
whole header (keys sorted, no whitespace, UTF-8, integers only). Seal-time input
faults are INVALID_ARGUMENT; every opening failure is one value-free
PAYLOAD_INVALID. Grant ids are sg. plus 26 lowercase base32 characters.
Identifiers refuse empty values, control characters and "/" so the
<owner>/<deviceId> rendering stays unambiguous, and a leading "@", which is
selector syntax ("@members" grants never carry a credential).
"""
from __future__ import annotations

import json
import re

from hyprial.kernel import DaemonRequestError, ipc_errors
from ..user_key.crypto import canonical_bytes
from ..user_key.encryption.crypto import key_id as encryption_key_id
from ..user_key.models import Error as UserKeyError
from . import core
from .profile import Profile, Recipient

WRAP_DOMAIN = b"hyprial/service-grant/wrap/v1"
HEADER_FIELDS = frozenset({"v", "alg", "wrap", "org", "grantId", "service", "recipient", "encryptionKeyId"})
ARGUMENT = ipc_errors.INVALID_ARGUMENT
REFUSED = ipc_errors.PAYLOAD_INVALID
_GRANT_ID = re.compile(r"sg\.[a-z2-7]{26}\Z")
_KEY_ID = re.compile(r"x25519:[0-9a-f]{32}\Z")


class ServiceGrantError(DaemonRequestError):
    """Value-free refusal: never carries key material, plaintext or parser text."""

    def __init__(self, code: str):
        messages = {ARGUMENT: "service grant sealing input invalid",
                    REFUSED: "service grant credential cannot be opened"}
        super().__init__(code, messages.get(code, "service grant credential refused"))


def _identifier(value: object) -> bool:
    return (type(value) is str and 0 < len(value) <= 256 and not value.startswith("@")
            and all(0x20 <= ord(c) != 0x7F and not 0xD800 <= ord(c) <= 0xDFFF and c != "/" for c in value))


def _port(value: object) -> bool:
    return type(value) is int and 0 < value < 65536


def _valid(header: object) -> bool:
    if type(header) is not dict or header.keys() != HEADER_FIELDS:
        return False
    service, recipient = header["service"], header["recipient"]
    return (type(header["v"]) is int and header["v"] == 1 and header["alg"] == "A256GCM"
            and header["wrap"] == "X25519-HKDF-SHA256-A256GCM" and _identifier(header["org"])
            and type(header["grantId"]) is str and _GRANT_ID.match(header["grantId"]) is not None
            and type(service) is dict and service.keys() == {"owner", "deviceId", "port"}
            and _identifier(service["owner"]) and _identifier(service["deviceId"]) and _port(service["port"])
            and type(recipient) is dict and recipient.keys() == {"owner", "deviceId"}
            and _identifier(recipient["owner"]) and _identifier(recipient["deviceId"])
            and type(header["encryptionKeyId"]) is str and _KEY_ID.match(header["encryptionKeyId"]) is not None)


def _check_header(header: dict) -> None:
    if not _valid(header):
        raise ServiceGrantError(REFUSED)


SERVICE_GRANT = Profile(
    name="service-grant", wrap_domain=WRAP_DOMAIN, header_fields=HEADER_FIELDS,
    check_header=_check_header, signature=None, error=ServiceGrantError,
    invalid=REFUSED, unseal=REFUSED, missing=REFUSED, argument=ARGUMENT,
    recipient_type=Recipient, max_recipients=1, reject_weak_recipients=True,
)


def _argument_header(header: object) -> dict:
    try:
        value = json.loads(canonical_bytes(header))
    except UserKeyError:
        raise ServiceGrantError(ARGUMENT) from None
    if not _valid(value):
        raise ServiceGrantError(ARGUMENT)
    return value


def service_grant_header(*, org: str, grant_id: str, grantor: str, service_device: str, port: int,
                         recipient_owner: str, recipient_device: str, encryption_key_id: str) -> dict:
    """Build the one canonical header for a grant; INVALID_ARGUMENT when a field is unusable."""
    return _argument_header({
        "v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "org": org, "grantId": grant_id,
        "service": {"owner": grantor, "deviceId": service_device, "port": port},
        "recipient": {"owner": recipient_owner, "deviceId": recipient_device},
        "encryptionKeyId": encryption_key_id,
    })


def seal_service_grant(secret: bytes, *, header: dict, recipient_public_key: bytes) -> dict:
    """Seal a credential to one device key. The header must name that key's id."""
    header = _argument_header(header)
    if type(recipient_public_key) is not bytes or len(recipient_public_key) != 32:
        raise ServiceGrantError(ARGUMENT)
    key_id = encryption_key_id(recipient_public_key)
    if header["encryptionKeyId"] != key_id:
        raise ServiceGrantError(ARGUMENT)
    return core.seal(SERVICE_GRANT, secret, header=header,
                     recipients=[Recipient(key_id, recipient_public_key)])


def open_service_grant(envelope: dict, *, header: dict, private_key: bytes) -> bytes:
    """Open only if the envelope carries exactly the expected header for this grant and key."""
    expected = _argument_header(header)
    value = core.verify(SERVICE_GRANT, envelope)
    if value["header"] != expected:
        raise ServiceGrantError(REFUSED)
    return core.open(SERVICE_GRANT, value, recipient_key_id=expected["encryptionKeyId"],
                     private_key=private_key)
