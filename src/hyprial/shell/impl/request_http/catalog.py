"""daemon.catalog consumption, method filtering, routes and contract digests.

The daemon owns the runtime catalog.  This module never mutates it and never
infers an identity binding from a parameter name: only ``httpBinding`` from
the daemon response enters a route.  The local ``daemon_catalog()`` projection
is used for one declaration-digest comparison, never to build routes.  A route
that is ``not-ready`` only because of the daemon's phase carries one of
``PHASE_REASONS``; the route set and both digests do not depend on the phase.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from hyprial.kernel import project_http_params_schema
from ..http_contract import confirmation_fields, missing_preview_pairs

DECLARATION_FIELDS = (
    "name",
    "owner",
    "httpExposed",
    "httpBinding",
    "mode",
    "mutates",
    "retry",
    "confirmation",
    "audience",
    "paramsSchema",
    "resultSchema",
    "errors",
    "ipcParamValidation",
)
SUPPORTED_IDENTITY_MODES = frozenset(
    {"none", "local-owner-selector", "local-owner-user-selector"}
)
PHASE_REASONS = frozenset({"ACCOUNT_REQUIRED", "BOOTSTRAP_ONLY"})
HTTP_CONTRACT_VERSION = 1


class CatalogProblem(Exception):
    """A daemon catalog cannot be used as the HTTP route authority."""

    def __init__(self, code: str, reason: str, message: str) -> None:
        self.code = code
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Backend:
    kind: str
    socket: str
    version: str
    node_id: str
    owner: str | None
    epoch: str

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "socket": self.socket,
            "version": self.version,
            "nodeId": self.node_id,
            "owner": self.owner,
            "epoch": self.epoch,
        }


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    path: str
    state: str
    callable: bool
    reason: str | None
    http_binding: Mapping[str, Any]
    mode: str
    mutates: bool
    retry: str
    confirmation: str
    audience: tuple[str, ...]
    params_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    errors: tuple[Mapping[str, Any], ...]
    #: Fields the daemon schema accepts only from a trusted host/MCP caller;
    #: the HTTP body must not contain them and the HTTP paramsSchema omits them.
    http_forbidden: frozenset[str] = frozenset()
    preview: Mapping[str, Any] | None = None
    preview_of: str | None = None

    def public_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "state": self.state,
            "callable": self.callable,
            "reason": self.reason,
            "httpBinding": dict(self.http_binding),
            "mode": self.mode,
            "mutates": self.mutates,
            "retry": self.retry,
            "confirmation": self.confirmation,
            **confirmation_fields(
                self.confirmation, self.mutates, self.params_schema,
                self.result_schema, self.preview, self.preview_of,
            ),
            "audience": list(self.audience),
            "paramsSchema": dict(self.params_schema),
            "resultSchema": dict(self.result_schema),
            "errors": [dict(error) for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class CatalogView:
    backend: Backend
    catalog_version: str
    package_version: str
    declaration_digest: str
    http_contract_digest: str
    routes: Mapping[str, Route]
    skipped: Mapping[str, str]


def canonical_digest(document: Any) -> str:
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def declaration_document(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(entries, key=lambda entry: str(entry["name"]))
    return {
        "schemaVersion": 1,
        "methods": [
            {
                **{field: entry.get(field) for field in DECLARATION_FIELDS},
                **{field: entry[field] for field in ("preview", "previewOf") if field in entry},
            } for entry in ordered
        ],
    }


def declaration_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    return canonical_digest(declaration_document(entries))


def http_contract_document(routes: Iterable[Route]) -> dict[str, Any]:
    ordered = sorted(routes, key=lambda route: route.name)
    return {
        "httpContractVersion": HTTP_CONTRACT_VERSION,
        "catalogVersion": "1",
        "methods": [
            {
                "name": route.name,
                "path": route.path,
                "httpBinding": {"identity": dict(route.http_binding["identity"])},
                "mode": route.mode,
                "mutates": route.mutates,
                "retry": route.retry,
                "confirmation": route.confirmation,
                **confirmation_fields(
                    route.confirmation, route.mutates, route.params_schema,
                    route.result_schema, route.preview, route.preview_of,
                ),
                "requiredAudience": ["local-operator"],
                "paramsSchema": dict(route.params_schema),
                "resultSchema": dict(route.result_schema),
                "errors": [dict(error) for error in route.errors],
            }
            for route in ordered
        ],
    }


def http_contract_digest(routes: Iterable[Route]) -> str:
    return canonical_digest(http_contract_document(routes))


def _require(condition: bool, reason: str, message: str) -> None:
    if not condition:
        raise CatalogProblem("CATALOG_MISMATCH", reason, message)


def _ping_backend(ping: Mapping[str, Any], socket_path: str) -> Backend:
    _require(isinstance(ping, Mapping), "PING_SHAPE", "ping response is not an object")
    for field in ("version", "nodeId", "epoch", "phase"):
        _require(
            isinstance(ping.get(field), str) and bool(ping.get(field)),
            "PING_SHAPE",
            f"ping response has no {field}",
        )
    _require(
        (ping.get("phase") == "bootstrap" and ping.get("owner") is None)
        or (isinstance(ping.get("owner"), str) and bool(ping["owner"])),
        "PING_SHAPE", "ping response has no owner outside bootstrap",
    )
    return Backend(
        kind="local-daemon",
        socket=socket_path,
        version=ping["version"],
        node_id=ping["nodeId"],
        owner=ping["owner"],
        epoch=ping["epoch"],
    )


def _validate_entry(entry: Any) -> Mapping[str, Any]:
    _require(isinstance(entry, Mapping), "CATALOG_SHAPE", "catalog method is not an object")
    name = entry.get("name")
    _require(isinstance(name, str) and bool(name), "CATALOG_SHAPE", "catalog method has no name")
    _require(entry.get("declared") is True, "CATALOG_SHAPE", f"{name}: declared is not true")
    for field in ("httpExposed", "mutates"):
        _require(type(entry.get(field)) is bool, "CATALOG_SHAPE", f"{name}: {field} is not a bool")
    for field in ("mode", "retry", "confirmation", "state"):
        _require(isinstance(entry.get(field), str), "CATALOG_SHAPE", f"{name}: {field} is missing")
    _require(isinstance(entry.get("audience"), list), "CATALOG_SHAPE", f"{name}: audience is missing")
    _require(isinstance(entry.get("paramsSchema"), Mapping), "CATALOG_SHAPE", f"{name}: paramsSchema is missing")
    _require(isinstance(entry.get("resultSchema"), Mapping), "CATALOG_SHAPE", f"{name}: resultSchema is missing")
    _require(isinstance(entry.get("errors"), list), "CATALOG_SHAPE", f"{name}: errors is missing")
    return entry


def _http_forbidden_fields(schema: Mapping[str, Any]) -> frozenset[str]:
    """Fields a trusted host/MCP caller supplies, never the HTTP body."""

    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    return frozenset(
        str(name)
        for name, declaration in properties.items()
        if isinstance(declaration, Mapping)
        and declaration.get("x-hyprial-http-forbidden") is True
    )


def _unsupported_reason(entry: Mapping[str, Any]) -> str | None:
    binding = entry.get("httpBinding")
    if not isinstance(binding, Mapping):
        return "HTTP_BINDING_MISSING"
    identity = binding.get("identity")
    if not isinstance(identity, Mapping):
        return "HTTP_BINDING_MISSING"
    mode = identity.get("mode")
    if mode not in SUPPORTED_IDENTITY_MODES:
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    if mode in {"local-owner-selector", "local-owner-user-selector"} and not (
        isinstance(identity.get("param"), str) and identity.get("param")
    ):
        return "HTTP_BINDING_MISSING"
    if "local-operator" not in entry["audience"]:
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    try:
        confirmation_fields(
            entry["confirmation"], entry["mutates"], entry["paramsSchema"],
            entry["resultSchema"], entry.get("preview"), entry.get("previewOf"),
        )
    except ValueError:
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    if entry["confirmation"] == "execute" and (
        mode != "none" or binding.get("onBehalfOf") is not None
        or binding.get("paramsConstraints") is not None
    ):
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    if entry["mode"] != "unary":
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    if entry["mutates"] is True:
        # A write is admitted only under the never-retry profile; the entry
        # never retries it on timeout, disconnect, cancellation or refresh.
        if entry["retry"] != "never":
            return "HTTP_CALL_PROFILE_UNSUPPORTED"
    elif entry["mutates"] is False:
        # Reads keep the existing safe-retry profile.
        if entry["retry"] != "safe":
            return "HTTP_CALL_PROFILE_UNSUPPORTED"
    else:
        return "HTTP_CALL_PROFILE_UNSUPPORTED"
    if entry["state"] not in {"available", "not-ready"}:
        return str(entry.get("reason") or "HTTP_CALL_PROFILE_UNSUPPORTED")
    return None


def build_view(
    ping: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    socket_path: str,
    local_entries: Iterable[Mapping[str, Any]],
) -> CatalogView:
    """Validate one daemon snapshot and build the supported HTTP surface."""

    _require(isinstance(catalog, Mapping), "CATALOG_SHAPE", "catalog response is not an object")
    _require(catalog.get("schemaVersion") == 1, "CATALOG_SHAPE", "catalog schemaVersion is not 1")
    _require(catalog.get("catalogVersion") == "1", "CATALOG_SHAPE", "catalog version is not 1")
    _require(isinstance(catalog.get("epoch"), str) and bool(catalog.get("epoch")), "CATALOG_SHAPE", "catalog has no epoch")
    _require(isinstance(catalog.get("packageVersion"), str), "CATALOG_SHAPE", "catalog has no packageVersion")
    raw_methods = catalog.get("methods")
    _require(isinstance(raw_methods, list), "CATALOG_SHAPE", "catalog methods is not a list")

    entries = [_validate_entry(entry) for entry in raw_methods]
    names = [entry["name"] for entry in entries]
    _require(len(set(names)) == len(names), "CATALOG_SHAPE", "catalog has duplicate method names")

    daemon_digest = declaration_digest(entries)
    local_digest = declaration_digest(local_entries)
    _require(
        daemon_digest == local_digest,
        "DECLARATION_MISMATCH",
        "daemon declarations differ from this package's declaration projection",
    )

    routes: dict[str, Route] = {}
    skipped: dict[str, str] = {}
    for entry in entries:
        if not entry["httpExposed"]:
            continue
        reason = _unsupported_reason(entry)
        if reason is not None:
            skipped[entry["name"]] = reason
            continue
        binding = entry["httpBinding"]
        try:
            params_schema = project_http_params_schema(
                entry["paramsSchema"], binding.get("paramsConstraints"),
            )
        except ValueError as error:
            raise CatalogProblem("CATALOG_SHAPE", "HTTP_PARAMS_INVALID", str(error)) from error
        routes[entry["name"]] = Route(
            name=entry["name"],
            path=f"/daemon/request/{entry['name']}",
            state=entry["state"],
            callable=entry["state"] == "available" and entry.get("callable") is True,
            reason=entry.get("reason"),
            http_binding=binding,
            mode=entry["mode"],
            mutates=entry["mutates"],
            retry=entry["retry"],
            confirmation=entry["confirmation"],
            preview=entry.get("preview"),
            preview_of=entry.get("previewOf"),
            audience=tuple(entry["audience"]),
            params_schema=params_schema,
            result_schema=entry["resultSchema"],
            errors=tuple(entry["errors"]),
            http_forbidden=_http_forbidden_fields(entry["paramsSchema"]),
        )

    for name in missing_preview_pairs(routes):
        routes.pop(name)
        skipped[name] = "HTTP_CALL_PROFILE_UNSUPPORTED"

    return CatalogView(
        backend=_ping_backend(ping, socket_path),
        catalog_version="1",
        package_version=catalog["packageVersion"],
        declaration_digest=daemon_digest,
        http_contract_digest=http_contract_digest(routes.values()),
        routes=routes,
        skipped=skipped,
    )


__all__ = [
    "Backend",
    "CatalogProblem",
    "CatalogView",
    "HTTP_CONTRACT_VERSION",
    "PHASE_REASONS",
    "Route",
    "build_view",
    "canonical_digest",
    "declaration_digest",
    "declaration_document",
    "http_contract_digest",
    "http_contract_document",
]
