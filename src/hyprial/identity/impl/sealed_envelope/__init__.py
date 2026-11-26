"""Profile-parameterized sealed envelopes: provider keys (signed) and service grants (unsigned)."""
from .core import (
    is_weak_public_key as is_weak_public_key, open as open, seal as seal,
    unwrap_check as unwrap_check, unwrap_row as unwrap_row, verify as verify,
)
from .profile import Profile, Recipient, Signature
from .service_grant import (
    SERVICE_GRANT, ServiceGrantError, open_service_grant, seal_service_grant, service_grant_header,
)


class SealedEnvelope:
    """Namespace for a future identity-facade export; no caller is wired in this slice."""
    Profile = Profile
    Recipient = Recipient
    Signature = Signature
    SERVICE_GRANT = SERVICE_GRANT
    ServiceGrantError = ServiceGrantError
    service_grant_header = staticmethod(service_grant_header)
    seal_service_grant = staticmethod(seal_service_grant)
    open_service_grant = staticmethod(open_service_grant)
