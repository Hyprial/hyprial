"""Service grants: owner-authored per-org service authorization and sealed credentials."""

from .ipc import handle_service_grants
from .models import (
    ALL_MEMBERS,
    GRANT_ID_PATTERN,
    GRANT_PROTOCOLS,
    GrantRecipient,
    SCHEMA_VERSION,
    ServiceEndpoint,
    ServiceGrant,
    StoredServiceGrant,
    generate_grant_id,
    grant_view,
    normalize_bare_owner,
    parse_device_target,
    require_bare_owner,
    validate_grant_id,
)
from .queries import (
    can_open_grant_credential,
    effective_grants_for_recipient,
    effective_grants_for_service,
    is_grant_effective,
    load_org_service_grants,
    recipient_matches,
)
from .service import ServiceGrantService

__all__ = [
    "ALL_MEMBERS",
    "GRANT_ID_PATTERN",
    "GRANT_PROTOCOLS",
    "GrantRecipient",
    "SCHEMA_VERSION",
    "ServiceEndpoint",
    "ServiceGrant",
    "ServiceGrantService",
    "StoredServiceGrant",
    "can_open_grant_credential",
    "effective_grants_for_recipient",
    "effective_grants_for_service",
    "generate_grant_id",
    "grant_view",
    "handle_service_grants",
    "is_grant_effective",
    "load_org_service_grants",
    "normalize_bare_owner",
    "parse_device_target",
    "recipient_matches",
    "require_bare_owner",
    "validate_grant_id",
]
