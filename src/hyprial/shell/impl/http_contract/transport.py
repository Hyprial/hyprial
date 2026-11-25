"""Fixed transport endpoints in the canonical HTTP OpenAPI document.

These are the routes the request entry always serves.  Their bodies are the
same transport/auth/error vocabulary as the method operations; only the method
paths come from the daemon catalog.  No business method name or schema lives
here.
"""

from __future__ import annotations

from typing import Any

from .envelope import (
    BEARER_SCHEME,
    ENVELOPE_SCHEMA_VERSION,
    ERROR_STATUSES,
    STATUS_DESCRIPTIONS,
    codes_for_status,
    error_envelope_schema,
)

TRANSPORT_TAG = "transport"


def _error_responses() -> dict[str, Any]:
    return {
        str(status): {
            "description": STATUS_DESCRIPTIONS[status],
            "x-hyprial-codes": codes_for_status(status),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                }
            },
        }
        for status in ERROR_STATUSES
    }


def _secured(responses: dict[str, Any]) -> dict[str, Any]:
    responses.update(_error_responses())
    return {
        "security": [{BEARER_SCHEME: []}],
        "responses": responses,
    }


def schemas() -> dict[str, Any]:
    """Fresh transport component schemas; business schemas are added later."""

    return {
        "ErrorEnvelope": error_envelope_schema(),
        "HealthResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"const": True}},
        },
        "ReadyResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "ready", "transport"],
            "properties": {
                "schemaVersion": {"const": ENVELOPE_SCHEMA_VERSION},
                "ok": {"type": "boolean"},
                "ready": {"type": "boolean"},
                "transport": {"const": "ready"},
                "backend": {"type": ["object", "null"]},
                "catalog": {"type": ["object", "null"]},
                "methods": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["name", "state"],
                        "properties": {
                            "name": {"type": "string"},
                            "state": {"type": "string"},
                        },
                    },
                },
                "notReady": {"type": ["array", "null"], "items": {"type": "string"}},
                "reason": {"type": ["string", "null"]},
                "code": {"type": ["string", "null"]},
                "requestId": {"type": ["string", "null"]},
            },
        },
        "CatalogMethod": {
            "type": "object",
            "additionalProperties": True,
            "required": [
                "name",
                "path",
                "state",
                "callable",
                "httpBinding",
                "mode",
                "mutates",
                "retry",
                "confirmation",
                "audience",
                "paramsSchema",
                "resultSchema",
                "errors",
            ],
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "state": {"type": "string"},
                "callable": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
                "httpBinding": {"type": "object"},
                "mode": {"type": "string"},
                "mutates": {"type": "boolean"},
                "retry": {"type": "string"},
                "confirmation": {"type": "string"},
                "audience": {"type": "array", "items": {"type": "string"}},
                "paramsSchema": {"type": "object"},
                "resultSchema": {"type": "object"},
                "errors": {"type": "array", "items": {"type": "object"}},
            },
        },
        "CatalogResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schemaVersion",
                "ok",
                "httpContractVersion",
                "catalogVersion",
                "catalogDigest",
                "declarationDigest",
                "generatedFrom",
                "backend",
                "principal",
                "methods",
            ],
            "properties": {
                "schemaVersion": {"const": ENVELOPE_SCHEMA_VERSION},
                "ok": {"const": True},
                "httpContractVersion": {"const": 1},
                "catalogVersion": {"type": "string"},
                "catalogDigest": {"type": "string"},
                "declarationDigest": {"type": "string"},
                "generatedFrom": {"const": "daemon.catalog"},
                "backend": {"type": "object"},
                "principal": {"type": "object"},
                "methods": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/CatalogMethod"},
                },
            },
        },
        "OpenAPIResponse": {
            "type": "object",
            "additionalProperties": True,
            "required": ["openapi", "info", "paths"],
            "properties": {
                "openapi": {"type": "string"},
                "info": {"type": "object"},
                "paths": {"type": "object"},
            },
        },
    }


def _readyz_operation() -> dict[str, Any]:
    """Readiness has two legitimate 503 bodies, not only an error envelope.

    A not-ready snapshot and a refresh failure return the ReadyResponse shape;
    a transport refusal before the handler returns ErrorEnvelope.
    """

    operation: dict[str, Any] = {
        "operationId": "transport.readyz",
        "summary": "Daemon catalog readiness",
        "description": (
            "Readiness is a snapshot of the daemon catalog and phase. A 503 "
            "body is either a readiness snapshot or a transport refusal; "
            "dispatch/retry decisions never follow from the status alone."
        ),
        "tags": [TRANSPORT_TAG],
        "x-hyprial-transport": "readyz",
        **_secured(
            {
                "200": {
                    "description": "Ready readiness snapshot.",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ReadyResponse"}
                        }
                    },
                }
            }
        ),
    }
    operation["responses"]["503"] = {
        "description": (
            "Not-ready snapshot, refresh failure, or a transport refusal. "
            "The body distinguishes the readiness shape from ErrorEnvelope."
        ),
        "x-hyprial-codes": codes_for_status(503),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/ReadyResponse"},
                        {"$ref": "#/components/schemas/ErrorEnvelope"},
                    ]
                }
            }
        },
    }
    return operation


def paths() -> dict[str, Any]:
    """Fresh fixed-endpoint operations; method paths are merged by the caller."""

    return {
        "/healthz": {
            "get": {
                "operationId": "transport.healthz",
                "summary": "Transport health",
                "description": "Unauthenticated liveness only; no daemon state.",
                "tags": [TRANSPORT_TAG],
                "x-hyprial-transport": "healthz",
                "responses": {
                    "200": {
                        "description": "The transport process is alive.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/HealthResponse"
                                }
                            }
                        },
                    }
                },
            }
        },
        "/readyz": {"get": _readyz_operation()},
        "/daemon/request": {
            "get": {
                "operationId": "transport.catalog",
                "summary": "Admitted HTTP method catalog",
                "description": (
                    "The daemon-owned method catalog admitted by this HTTP "
                    "profile. Per-method schemas and membership are authoritative."
                ),
                "tags": [TRANSPORT_TAG],
                "x-hyprial-transport": "catalog",
                **_secured(
                    {
                        "200": {
                            "description": "Catalog snapshot.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CatalogResponse"
                                    }
                                }
                            },
                        }
                    }
                ),
            }
        },
        "/openapi.json": {
            "get": {
                "operationId": "transport.openapi",
                "summary": "This canonical OpenAPI document",
                "description": (
                    "Authenticated schema document. The same canonical projection "
                    "is exported offline for client generation."
                ),
                "tags": [TRANSPORT_TAG],
                "x-hyprial-transport": "openapi",
                **_secured(
                    {
                        "200": {
                            "description": "Canonical OpenAPI document.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/OpenAPIResponse"
                                    }
                                }
                            },
                        }
                    }
                ),
            }
        },
    }


__all__ = ["TRANSPORT_TAG", "paths", "schemas"]
