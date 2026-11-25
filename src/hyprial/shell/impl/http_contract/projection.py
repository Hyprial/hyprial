"""Deterministic HTTP OpenAPI projection from an already-admitted catalog.

The input is the daemon-owned HTTP catalog (the methods the H-01 entry already
accepted), never a hand-written method table and never a business handler.  A
write must arrive with ``mutates=true`` and ``retry=never``; a read keeps the
existing ``retry=safe`` profile.  The projector refuses a mislabeled entry
instead of silently widening what HTTP may call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import is_declared_error_code

from .confirmation import confirmation_fields, missing_preview_pairs
from .envelope import (
    BEARER_SCHEME,
    DESCRIPTION,
    DISPATCH_STATES,
    ENVELOPE_SCHEMA_VERSION,
    ERROR_STATUSES,
    HTTP_CONTRACT_VERSION,
    JSON_SCHEMA_DIALECT,
    OPENAPI_VERSION,
    READ_RETRY,
    STATUS_DESCRIPTIONS,
    TITLE,
    WRITE_RETRY,
    codes_for_status,
    declared_error_envelope_schema,
    security_scheme,
    success_envelope_schema,
    thaw_json,
    transport_error_documents,
)
from .transport import paths as transport_paths, schemas as transport_schemas

_METHOD_NAME = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
_OWNER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_IDENTITY_MODES = frozenset(
    {"none", "local-owner-selector", "local-owner-user-selector"}
)
_STATES = frozenset({"available", "not-ready"})
_CALLER = {"source": "server-binding", "authority": "transport"}


class ProjectionError(ValueError):
    """The input cannot be projected without guessing or widening the profile."""


@dataclass(frozen=True, slots=True)
class HttpMethod:
    """One admitted HTTP method; business schemas stay exactly as declared."""

    name: str
    owner: str | None
    state: str
    callable: bool
    reason: str | None
    mode: str
    mutates: bool
    retry: str
    confirmation: str
    audience: tuple[str, ...]
    caller_mode: str
    caller_param: str | None
    readiness: str | None
    params_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    errors: tuple[Mapping[str, Any], ...]
    preview: Mapping[str, Any] | None = None
    preview_of: str | None = None
    on_behalf_of: str | None = None
    params_constraints: Mapping[str, Any] | None = None
    host_only_params: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "audience", tuple(self.audience or ()))
        normalized: list[Any] = []
        for error in self.errors or ():
            if isinstance(error, Mapping):
                item = dict(error)
                item.setdefault("description", "")
                normalized.append(item)
            else:
                normalized.append(error)
        object.__setattr__(self, "errors", tuple(normalized))
        self._validate()

    @property
    def path(self) -> str:
        return f"/daemon/request/{self.name}"

    @property
    def call_profile(self) -> str:
        return "write" if self.mutates else "read"

    def _validate(self) -> None:
        _require(isinstance(self.name, str) and bool(_METHOD_NAME.fullmatch(self.name)),
                 "invalid method name")
        _require(
            self.owner is None
            or (isinstance(self.owner, str) and bool(_OWNER.fullmatch(self.owner))),
            f"{self.name}: invalid owner {self.owner!r}",
        )
        _require(isinstance(self.state, str) and self.state in _STATES,
                 f"{self.name}: unsupported state {self.state!r}")
        _require(type(self.callable) is bool, f"{self.name}: callable must be a bool")
        _require(self.reason is None or isinstance(self.reason, str), f"{self.name}: invalid reason")
        _require(self.mode == "unary", f"{self.name}: HTTP profile requires unary mode")
        _require(type(self.mutates) is bool, f"{self.name}: mutates must be a bool")
        try:
            confirmation_fields(
                self.confirmation, self.mutates, self.params_schema,
                self.result_schema, self.preview, self.preview_of,
            )
        except ValueError as error:
            raise ProjectionError(f"{self.name}: {error}") from error
        if self.confirmation == "execute":
            _require(
                self.caller_mode == "none" and self.on_behalf_of is None
                and self.params_constraints is None,
                f"{self.name}: execute accepts no injected or replacement parameters",
            )
        _require(isinstance(self.params_schema, Mapping), f"{self.name}: paramsSchema is not an object")
        _require(isinstance(self.result_schema, Mapping), f"{self.name}: resultSchema is not an object")
        _require(
            bool(self.audience) and all(isinstance(item, str) and item for item in self.audience),
            f"{self.name}: audience must be a non-empty string tuple",
        )
        _require("local-operator" in self.audience, f"{self.name}: local-operator audience is required")
        if self.mutates:
            _require(
                self.retry == WRITE_RETRY,
                f"{self.name}: a write must declare retry=never, not {self.retry!r}",
            )
        else:
            _require(
                self.retry == READ_RETRY,
                f"{self.name}: a read must keep retry=safe, not {self.retry!r}",
            )
        _require(
            isinstance(self.caller_mode, str) and self.caller_mode in _IDENTITY_MODES,
            f"{self.name}: unsupported HTTP identity mode {self.caller_mode!r}",
        )
        if self.caller_mode == "none":
            _require(
                self.caller_param is None,
                f"{self.name}: identity mode none cannot name a caller selector",
            )
        else:
            _require(
                isinstance(self.caller_param, str) and bool(self.caller_param),
                f"{self.name}: caller selector needs a parameter name",
            )
            properties = self.params_schema.get("properties")
            declared = (
                properties.get(self.caller_param)
                if isinstance(properties, Mapping)
                else None
            )
            if declared is None and self.caller_mode == "local-owner-user-selector":
                _require(
                    self.caller_param in self.host_only_params,
                    f"{self.name}: caller selector {self.caller_param!r} is not a declared parameter",
                )
            if declared is not None:
                declared_type = (
                    declared.get("type") if isinstance(declared, Mapping) else None
                )
                _require(
                    declared_type == "string"
                    or (
                        isinstance(declared_type, (list, tuple))
                        and "string" in declared_type
                    ),
                    f"{self.name}: caller selector {self.caller_param!r} must accept a string",
                )
        _require(
            self.readiness is None
            or (isinstance(self.readiness, str) and bool(self.readiness.strip())),
            f"{self.name}: readiness probe must be a non-empty string or null",
        )
        _require(bool(self.errors), f"{self.name}: at least one declared error is required")
        codes: set[str] = set()
        for error in self.errors:
            _require(isinstance(error, Mapping), f"{self.name}: declared error is not an object")
            code = error.get("code")
            _require(
                is_declared_error_code(code),
                f"{self.name}: invalid declared error code {code!r}",
            )
            _require(code not in codes, f"{self.name}: duplicate declared error {code}")
            codes.add(code)
            _require(
                isinstance(error.get("description"), str),
                f"{self.name}: declared error {code} description is not a string",
            )
            data_schema = error.get("dataSchema")
            _require(
                data_schema is None or isinstance(data_schema, Mapping),
                f"{self.name}: declared error {code} has a non-object dataSchema",
            )

    @classmethod
    def from_catalog_entry(
        cls, entry: Mapping[str, Any], *, owner: str | None = None
    ) -> "HttpMethod":
        """Read one admitted catalog entry without inventing missing fields.

        The H-01 ``Route.public_json()`` seam has no ``owner``; the optional
        argument is the explicit adapter from a daemon declaration catalog.
        """

        _require(isinstance(entry, Mapping), "catalog method is not an object")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ProjectionError("catalog method has no name")
        binding = entry.get("httpBinding")
        _require(isinstance(binding, Mapping), f"{name}: httpBinding is missing")
        identity = binding.get("identity")
        _require(isinstance(identity, Mapping), f"{name}: httpBinding.identity is missing")
        declared_owner = entry.get("owner")
        resolved_owner = owner if declared_owner is None else declared_owner
        return cls(
            name=name,
            owner=resolved_owner,
            state=entry.get("state"),
            callable=entry.get("callable"),
            reason=entry.get("reason"),
            mode=entry.get("mode"),
            mutates=entry.get("mutates"),
            retry=entry.get("retry"),
            confirmation=entry.get("confirmation"),
            audience=entry.get("audience"),
            caller_mode=identity.get("mode"),
            caller_param=identity.get("param"),
            readiness=binding.get("readiness"),
            params_schema=entry.get("paramsSchema"),
            result_schema=entry.get("resultSchema"),
            errors=entry.get("errors"),
            preview=entry.get("preview"),
            preview_of=entry.get("previewOf"),
            on_behalf_of=binding.get("onBehalfOf"),
            params_constraints=binding.get("paramsConstraints"),
            host_only_params=frozenset(entry.get("x-hyprial-http-host-only", ())),
        )


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ProjectionError(message)


def _component(prefix: str, name: str) -> str:
    return f"{prefix}.{name}"


def _operation(method: HttpMethod) -> dict[str, Any]:
    if method.mutates:
        description = (
            f"Invoke {method.name} (mutates=true, retry=never). The HTTP entry "
            "treats this request as never retried. A timeout, cancellation or disconnect "
            "after dispatch keeps the dispatch fact (`dispatched` or `unknown`) "
            "and never claims a rollback. A request budget exhausted before "
            "dispatch returns 408 with dispatch=not-dispatched."
        )
    else:
        description = (
            f"Invoke {method.name} (mutates=false, retry=safe). The HTTP entry "
            "forwards one unary read; retry is safe only when the client did not "
            "observe a completed response. The request budget is checked before "
            "dispatch."
        )
    if method.caller_mode != "none":
        caller = dict(_CALLER)
        if method.caller_mode == "local-owner-selector":
            foreign = "refused-with-400-before-dispatch"
            selector_rule = (
                "a missing value is injected from the server configuration and any "
                "browser-supplied value is refused with 400 before dispatch"
            )
        elif method.caller_mode == "local-owner-user-selector":
            foreign = "refused-before-dispatch"
            selector_rule = (
                "a missing value is injected as the trusted owner's user identity and "
                "a foreign value is refused with 403 before dispatch"
            )
        else:
            foreign = "refused-before-dispatch"
            selector_rule = (
                "a missing value is injected from the server configuration and a "
                "foreign value is refused with 403 before dispatch"
            )
        caller["selector"] = {
            "param": method.caller_param,
            "mode": method.caller_mode,
            "missing": "injected-from-server",
            "foreign": foreign,
        }
        body_description = (
            "Method paramsSchema from daemon.catalog. "
            f"`{method.caller_param}` is the caller selector, not a business target: "
            f"{selector_rule}. Other declared fields are "
            "ordinary business params under daemon authorization."
        )
        if method.caller_mode == "local-owner-user-selector":
            description += (
                " The caller selector mirrors the trusted owner's user identity; "
                "it does not select a business target."
            )
        else:
            description += (
                " The caller selector parameter mirrors the server-bound identity; it "
                "does not select a business target. Ordinary business params remain "
                "under daemon authorization."
            )
    else:
        caller = dict(_CALLER)
        body_description = (
            "Method paramsSchema from daemon.catalog. The bearer token "
            "authenticates the server-configured caller; no caller selector is "
            "declared. Other declared fields are ordinary business params under "
            "daemon authorization."
        )
    if method.state != "available":
        description += (
            f" State {method.state!r}: the method refuses with 503 until the "
            "daemon reports it available."
        )

    responses: dict[str, Any] = {
        "200": {
            "description": STATUS_DESCRIPTIONS[200],
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": f"#/components/schemas/{_component('success', method.name)}"
                    }
                }
            },
        }
    }
    for status in ERROR_STATUSES:
        responses[str(status)] = {
            "description": STATUS_DESCRIPTIONS[status],
            "x-hyprial-codes": codes_for_status(status),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                }
            },
        }
    if method.errors:
        responses["default"] = {
            "description": (
                "Declared domain errors. The transport status follows the shared "
                "code map; dispatch says whether the daemon saw the request."
            ),
            "x-hyprial-codes": [str(error["code"]) for error in method.errors],
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            declared_error_envelope_schema(error)
                            for error in method.errors
                        ]
                    }
                }
            },
        }

    operation: dict[str, Any] = {
        "operationId": method.name,
        "summary": f"Invoke {method.name}",
        "description": description,
        "security": [{BEARER_SCHEME: []}],
        "x-hyprial-method": method.name,
        "x-hyprial-state": method.state,
        "x-hyprial-callable": method.callable,
        "x-hyprial-reason": method.reason,
        "x-hyprial-call-profile": method.call_profile,
        "x-hyprial-mode": method.mode,
        "x-hyprial-mutates": method.mutates,
        "x-hyprial-retry": method.retry,
        "x-hyprial-confirmation": method.confirmation,
        "x-hyprial-audience": list(method.audience),
        "x-hyprial-http-binding": {
            "identity": {
                "mode": method.caller_mode,
                "param": method.caller_param,
            },
            "readiness": method.readiness,
            **(
                {"onBehalfOf": method.on_behalf_of}
                if method.on_behalf_of is not None
                else {}
            ),
            **(
                {"paramsConstraints": thaw_json(method.params_constraints)}
                if method.params_constraints is not None
                else {}
            ),
        },
        "x-hyprial-caller": caller,
        "x-hyprial-identity": {
            "mode": method.caller_mode,
            "param": method.caller_param,
        },
        "x-hyprial-target": None,
        "x-hyprial-errors": [thaw_json(error) for error in method.errors],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": f"#/components/schemas/{_component('params', method.name)}"
                    },
                    "description": body_description,
                }
            },
        },
        "responses": responses,
    }
    if method.preview is not None:
        operation["x-hyprial-preview"] = thaw_json(method.preview)
    if method.preview_of is not None:
        operation["x-hyprial-preview-of"] = method.preview_of
    if method.owner is not None:
        operation["tags"] = [method.owner]
        operation["x-hyprial-owner"] = method.owner
    return operation


def build_openapi(methods: Iterable[HttpMethod]) -> dict[str, Any]:
    """Project admitted methods into one canonical OpenAPI 3.1 document."""

    ordered = sorted(methods, key=lambda item: item.name)
    names = [method.name for method in ordered]
    _require(len(names) == len(set(names)), "duplicate method names in the projection")

    _require(
        not missing_preview_pairs({method.name: method for method in ordered}),
        "execute requires an admitted preview method",
    )
    schemas: dict[str, Any] = transport_schemas()
    paths: dict[str, Any] = transport_paths()
    summaries: list[dict[str, Any]] = []
    owners: set[str] = set()
    for method in ordered:
        if method.owner is not None:
            owners.add(method.owner)
        schemas[_component("params", method.name)] = thaw_json(method.params_schema)
        schemas[_component("result", method.name)] = thaw_json(method.result_schema)
        schemas[_component("success", method.name)] = success_envelope_schema(
            method.name, _component("result", method.name)
        )
        paths[method.path] = {"post": _operation(method)}
        summaries.append(
            {
                "name": method.name,
                "owner": method.owner,
                "callProfile": method.call_profile,
                "mutates": method.mutates,
                "retry": method.retry,
                "confirmation": method.confirmation,
                **confirmation_fields(
                    method.confirmation, method.mutates, method.params_schema,
                    method.result_schema, method.preview, method.preview_of,
                ),
                "state": method.state,
                "callable": method.callable,
                "identity": {
                    "mode": method.caller_mode,
                    "param": method.caller_param,
                },
                "target": None,
            }
        )

    return {
        "openapi": OPENAPI_VERSION,
        "jsonSchemaDialect": JSON_SCHEMA_DIALECT,
        "info": {"title": TITLE, "version": str(HTTP_CONTRACT_VERSION), "description": DESCRIPTION},
        "servers": [{"url": "/", "description": "Same-origin local request entry"}],
        "tags": [
            {"name": owner, "description": f"Methods declared by {owner}"}
            for owner in sorted(owners)
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {BEARER_SCHEME: security_scheme()},
            "schemas": schemas,
        },
        "x-hyprial-http-contract": {
            "contractVersion": HTTP_CONTRACT_VERSION,
            "envelopeSchemaVersion": ENVELOPE_SCHEMA_VERSION,
            "dispatchStates": list(DISPATCH_STATES),
            "callProfiles": ["read", "write"],
            "methods": summaries,
            "transportErrors": transport_error_documents(),
        },
    }


def project_catalog(
    document: Mapping[str, Any], *, owners: Mapping[str, str] | None = None,
    examples: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Project an admitted HTTP catalog, with an optional owner adapter.

    ``GET /daemon/request`` returns ``Route.public_json()`` entries, which do
    not carry ``owner``.  Pass an explicit ``owners`` map (or the output of
    :func:`owners_from_document` over a ``daemon.catalog`` payload) to add
    tags; membership and business schemas never depend on it.
    """

    _require(isinstance(document, Mapping), "catalog document is not an object")
    _require(
        owners is None or isinstance(owners, Mapping),
        "owners must be a mapping of method name to owner",
    )
    _require(examples is None or isinstance(examples, Mapping), "examples must be a mapping")
    declared_version = document.get("httpContractVersion", HTTP_CONTRACT_VERSION)
    _require(
        declared_version == HTTP_CONTRACT_VERSION,
        f"unsupported httpContractVersion {declared_version!r}",
    )
    methods = document.get("methods")
    _require(isinstance(methods, list), "catalog document has no methods list")
    projected = []
    for entry in methods:
        owner = None
        if owners is not None and isinstance(entry, Mapping):
            name = entry.get("name")
            if isinstance(name, str):
                owner = owners.get(name)
        projected.append(HttpMethod.from_catalog_entry(entry, owner=owner))
    result = build_openapi(projected)
    for method in projected:
        cases = () if examples is None else examples.get(method.name, ())
        if not cases:
            continue
        operation = result["paths"][method.path]["post"]
        request = operation["requestBody"]["content"]["application/json"]
        success = operation["responses"]["200"]["content"]["application/json"]
        request_examples: dict[str, Any] = {}
        success_examples: dict[str, Any] = {}
        for case in cases:
            name = case["name"]
            _require(name not in request_examples, f"{method.name}: duplicate example {name}")
            request_examples[name] = {
                "summary": case["description"], "value": thaw_json(case["params"]),
            }
            if case["kind"] in ("success", "empty", "unconfigured"):
                success_examples[name] = {
                    "summary": case["description"],
                    "value": {
                        "schemaVersion": ENVELOPE_SCHEMA_VERSION,
                        "ok": True,
                        "method": method.name,
                        "requestId": "example-request-id",
                        "dispatch": "dispatched",
                        "result": thaw_json(case["result"]),
                    },
                }
        request["examples"] = request_examples
        if success_examples:
            success["examples"] = success_examples
        operation["x-hyprial-examples"] = [thaw_json(case) for case in cases]
    return result


def owners_from_document(document: Mapping[str, Any]) -> dict[str, str]:
    """Read an explicit owner adapter from a declaration catalog or plain map.

    A ``daemon.catalog`` payload has ``methods[].name`` and ``methods[].owner``;
    a plain ``{"method.name": "owner"}`` map is also accepted.  Entries without
    a usable owner are omitted rather than guessed from the method name.
    """

    _require(isinstance(document, Mapping), "owner document is not an object")
    methods = document.get("methods")
    if isinstance(methods, list):
        resolved: dict[str, str] = {}
        for entry in methods:
            _require(isinstance(entry, Mapping), "owner document method is not an object")
            name = entry.get("name")
            owner = entry.get("owner")
            if isinstance(name, str) and isinstance(owner, str) and owner:
                resolved[name] = owner
        return resolved
    resolved = {}
    for name, owner in document.items():
        _require(
            isinstance(name, str) and isinstance(owner, str) and bool(owner),
            "owner map needs non-empty string method names and owners",
        )
        resolved[name] = owner
    return resolved


def canonical_json(document: Mapping[str, Any]) -> str:
    """Stable bytes-as-text: sorted keys, no whitespace, no NaN, UTF-8."""

    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def document_digest(document: Mapping[str, Any]) -> str:
    payload = canonical_json(document).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "HttpMethod",
    "ProjectionError",
    "build_openapi",
    "canonical_json",
    "document_digest",
    "owners_from_document",
    "project_catalog",
]
