"""Profile-parameterized sealed envelopes: provider keys (signed) and service grants (unsigned)."""
from .profile import Profile, Recipient, Signature
from .service_grant import (
    ServiceGrantError, open_service_grant, seal_service_grant, service_grant_header,
)


class SealedEnvelope:
    """Namespace for a future identity-facade export; no caller is wired in this slice."""
    ServiceGrantError = ServiceGrantError
    seal_service_grant = staticmethod(seal_service_grant)


__all__ = [
    "Profile",
    "Recipient",
    "SealedEnvelope",
    "ServiceGrantError",
    "Signature",
    "open_service_grant",
    "seal_service_grant",
    "service_grant_header",
]
