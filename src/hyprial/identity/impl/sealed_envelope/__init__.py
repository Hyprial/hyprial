"""Profile-parameterized sealed envelopes: provider keys (signed) and service grants (unsigned)."""
from .profile import Profile, Recipient, Signature
from .service_grant import (
    GRANT_ID_PATTERN, ServiceGrantError, open_service_grant, seal_service_grant, service_grant_header,
    service_grant_path_segment,
)


class SealedEnvelope:
    """The identity-facade namespace for the service-grant profile: sealing and its identifier rules."""
    GRANT_ID_PATTERN = GRANT_ID_PATTERN
    ServiceGrantError = ServiceGrantError
    seal_service_grant = staticmethod(seal_service_grant)
    service_grant_path_segment = staticmethod(service_grant_path_segment)


__all__ = [
    "GRANT_ID_PATTERN",
    "Profile",
    "Recipient",
    "SealedEnvelope",
    "ServiceGrantError",
    "Signature",
    "open_service_grant",
    "seal_service_grant",
    "service_grant_header",
    "service_grant_path_segment",
]
