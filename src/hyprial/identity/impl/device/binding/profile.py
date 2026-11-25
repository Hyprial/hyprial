"""Signed binding profile v3: ten claims, JCS digest and namespace-scoped URI."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from uuid import UUID

from cryptography import x509

from hyprial.identity.impl.device.binding.extensions import read_certificate_extensions
from hyprial.identity.impl.device.binding.jcs import (
    MAX_SAFE_INTEGER,
    JcsError,
    canonical_bytes,
)
from hyprial.identity.impl.device.transport_storage import TransportCredentialError

PROFILE = "hyprial.transport.binding.v1"
# A fixed SAN protocol identifier, independent of policyIssuer and profile.issuer.
# Never derive this namespace from a configured issuer or a runtime endpoint.
BINDING_URI_NAMESPACE = "https://auth.hyprial.com/transport/binding/"
BINDING_URI_PREFIX = BINDING_URI_NAMESPACE + "v1/sha256/"
MAX_GENERATION = 2**31 - 1

CLAIM_FIELDS = (
    "profile",
    "policyIssuer",
    "bindingId",
    "ownerUserId",
    "owner",
    "deviceUid",
    "credentialGeneration",
    "spkiSha256",
    "renewableUntil",
    "trustBundleId",
)

_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_DEVICE_UID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


def binding_claims(
    *,
    policy_issuer: str,
    binding_id: str,
    owner_user_id: str,
    owner: str,
    device_uid: str,
    credential_generation: int,
    spki_sha256: str,
    renewable_until: int,
    trust_bundle_id: str,
) -> dict[str, object]:
    """Build the exact ten-claim object from response and device facts."""

    claims: dict[str, object] = {
        "profile": PROFILE,
        "policyIssuer": policy_issuer,
        "bindingId": binding_id,
        "ownerUserId": owner_user_id,
        "owner": owner,
        "deviceUid": device_uid,
        "credentialGeneration": credential_generation,
        "spkiSha256": spki_sha256,
        "renewableUntil": renewable_until,
        "trustBundleId": trust_bundle_id,
    }
    _check_claims(claims)
    return claims


def canonical_binding_bytes(claims: Mapping[str, object]) -> bytes:
    """Validate the exact profile schema, then return JCS UTF-8 bytes."""

    _check_claims(claims)
    try:
        return canonical_bytes(claims)
    except JcsError:
        raise _invalid() from None


def binding_digest(claims: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_binding_bytes(claims)).hexdigest()


def binding_uri(claims: Mapping[str, object]) -> str:
    return BINDING_URI_PREFIX + binding_digest(claims)


def verify_certificate_binding(
    certificate: x509.Certificate, claims: Mapping[str, object]
) -> str:
    """Require exactly one namespaced binding URI equal to the claim digest."""

    expected = binding_uri(claims)
    try:
        extension = read_certificate_extensions(certificate).get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        raise _invalid() from None
    values = extension.get_values_for_type(x509.UniformResourceIdentifier)
    namespaced = [value for value in values if value.startswith(BINDING_URI_NAMESPACE)]
    if len(namespaced) != 1:
        raise _invalid()
    if namespaced[0] != expected:
        raise _invalid()
    return expected[len(BINDING_URI_PREFIX) :]


def verify_credential_binding(certificate: x509.Certificate, credential) -> str:
    """Recompute the ten claims from one validated projection, then verify."""

    return verify_certificate_binding(
        certificate,
        binding_claims(
            policy_issuer=credential.policy_issuer,
            binding_id=credential.binding_id,
            owner_user_id=credential.owner_user_id,
            owner=credential.owner,
            device_uid=credential.device_uid,
            credential_generation=credential.credential_generation,
            spki_sha256=credential.spki_sha256,
            renewable_until=credential.renewable_until,
            trust_bundle_id=credential.trust_bundle_id,
        ),
    )


def _check_claims(claims: Mapping[str, object]) -> None:
    if not isinstance(claims, Mapping) or set(claims) != set(CLAIM_FIELDS):
        raise _invalid()
    if claims["profile"] != PROFILE:
        raise _invalid()
    for name in (
        "policyIssuer",
        "ownerUserId",
        "owner",
        "trustBundleId",
    ):
        _text(claims[name])
    if not isinstance(claims["bindingId"], str) or _UUID4.fullmatch(
        claims["bindingId"]
    ) is None:
        raise _invalid()
    if str(UUID(claims["bindingId"])) != claims["bindingId"]:
        raise _invalid()
    if not isinstance(claims["deviceUid"], str) or _DEVICE_UID.fullmatch(
        claims["deviceUid"]
    ) is None:
        raise _invalid()
    if not isinstance(claims["spkiSha256"], str) or _HEX_64.fullmatch(
        claims["spkiSha256"]
    ) is None:
        raise _invalid()
    _generation(claims["credentialGeneration"])
    horizon = claims["renewableUntil"]
    if (
        isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or not 0 <= horizon <= MAX_SAFE_INTEGER
    ):
        raise _invalid()


def _text(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None


def _generation(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_GENERATION
    ):
        raise _invalid()


def _invalid() -> TransportCredentialError:
    return TransportCredentialError(
        "TRANSPORT_RESPONSE_INVALID",
        "signed binding profile does not match the frozen v3 profile",
    )
