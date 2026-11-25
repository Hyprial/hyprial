"""Canonical HTTP request contract for the local request entry.

Pure, offline-deterministic transport projection shared by the development
schema exporter, the served OpenAPI document and the front-end client
generator.  It reads no home, socket, identity or clock.
"""

from __future__ import annotations

from .confirmation import (
    confirmation_fields as confirmation_fields,
    missing_preview_pairs as missing_preview_pairs,
)
from .envelope import (
    BEARER_SCHEME as BEARER_SCHEME,
    DESCRIPTION as DESCRIPTION,
    DISPATCH_STATES as DISPATCH_STATES,
    ENVELOPE_SCHEMA_VERSION as ENVELOPE_SCHEMA_VERSION,
    ERROR_STATUSES as ERROR_STATUSES,
    HTTP_CONTRACT_VERSION as HTTP_CONTRACT_VERSION,
    JSON_SCHEMA_DIALECT as JSON_SCHEMA_DIALECT,
    OPENAPI_VERSION as OPENAPI_VERSION,
    READ_RETRY as READ_RETRY,
    STATUS_DESCRIPTIONS as STATUS_DESCRIPTIONS,
    TITLE as TITLE,
    TRANSPORT_ERRORS as TRANSPORT_ERRORS,
    TransportError as TransportError,
    WRITE_RETRY as WRITE_RETRY,
    codes_for_status as codes_for_status,
    declared_error_envelope_schema as declared_error_envelope_schema,
    error_envelope_schema as error_envelope_schema,
    security_scheme as security_scheme,
    success_envelope_schema as success_envelope_schema,
    thaw_json as thaw_json,
    transport_error_documents as transport_error_documents,
)
from .projection import (
    HttpMethod as HttpMethod,
    ProjectionError as ProjectionError,
    build_openapi as build_openapi,
    canonical_json as canonical_json,
    document_digest as document_digest,
    owners_from_document as owners_from_document,
    project_catalog as project_catalog,
)

__all__ = [
    "BEARER_SCHEME",
    "DESCRIPTION",
    "DISPATCH_STATES",
    "ENVELOPE_SCHEMA_VERSION",
    "ERROR_STATUSES",
    "HTTP_CONTRACT_VERSION",
    "HttpMethod",
    "JSON_SCHEMA_DIALECT",
    "OPENAPI_VERSION",
    "ProjectionError",
    "READ_RETRY",
    "STATUS_DESCRIPTIONS",
    "TITLE",
    "TRANSPORT_ERRORS",
    "TransportError",
    "WRITE_RETRY",
    "confirmation_fields",
    "missing_preview_pairs",
    "build_openapi",
    "canonical_json",
    "codes_for_status",
    "declared_error_envelope_schema",
    "document_digest",
    "error_envelope_schema",
    "owners_from_document",
    "project_catalog",
    "security_scheme",
    "success_envelope_schema",
    "thaw_json",
    "transport_error_documents",
]
