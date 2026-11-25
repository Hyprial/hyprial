"""Typed one-request device authority client (identity domain)."""

from hyprial.identity.impl.device.client.errors import (
    OUTCOME_NOT_SENT,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    OUTCOMES,
    TransportClientError,
)
from hyprial.identity.impl.device.client.http import (
    DEVICE_CERTIFICATE_FIELDS,
    DEVICE_CERTIFICATE_ROUTE,
    ENVELOPE_VERSION,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    RETIRE_BINDING_FIELDS,
    RETIRE_BINDING_ROUTE,
    DeviceAuthorityConfig,
    DeviceTransportClient,
)
from hyprial.identity.impl.device.client.mtls import (
    ClientTlsMaterial,
    load_client_tls_material,
)

__all__ = [
    "DEVICE_CERTIFICATE_FIELDS",
    "DEVICE_CERTIFICATE_ROUTE",
    "ENVELOPE_VERSION",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OUTCOMES",
    "OUTCOME_NOT_SENT",
    "OUTCOME_REJECTED",
    "OUTCOME_UNKNOWN",
    "RETIRE_BINDING_FIELDS",
    "RETIRE_BINDING_ROUTE",
    "ClientTlsMaterial",
    "DeviceAuthorityConfig",
    "DeviceTransportClient",
    "TransportClientError",
    "load_client_tls_material",
]
