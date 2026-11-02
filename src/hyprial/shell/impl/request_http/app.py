"""FastAPI application, route guards and the Uvicorn-facing HTTP surface.

The daemon catalog remains the only source of method membership and business
JSON Schemas.  This module owns the transport envelope, authentication,
framing, CORS/preflight, route registration, exception conversion and the
OpenAPI projection for the real FastAPI application.

GRS review #2146: closing with unread incoming bytes can reset the 413 body on
macOS. Authenticated oversized requests up to twice the request-byte budget
are discarded without parsing, within the existing body/request deadlines,
before sending 413 + Connection: close. Larger declarations fail immediately.

wf-61d46aa513404f45a938d9e764f8dd55#implGRS3 (revGRS2 finding b): the Host
name is derived with ``rpartition(":")`` instead of ``rsplit(":", 1)``; the
single-colon branch guarantees identical semantics. This is HTTP Host-header
grammar, not agent:/channel: identity parsing, and keeps the address-parsing
guard (tests/kernel/addressing/test_address_parsing_guard.py) to its URI-grammar
scope without a new whitelist entry (whitelist only shrinks).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hyprial import __version__
from hyprial.kernel import ipc_errors

from .errors import HttpFailure
from .server import _Envelope

if TYPE_CHECKING:
    from .server import RequestHttpServer

_METHOD_PATH = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
_MAX_REQUEST_LINE = 512
_MAX_HEADER_BYTES = 8 * 1024
_MAX_HEADERS = 50
_DUPLICATE_REFUSED = (
    b"authorization",
    b"origin",
    b"host",
    b"content-type",
    b"x-hyprial-catalog-digest",
)
_FIXED_PATHS = frozenset({"/healthz", "/readyz", "/daemon/request", "/openapi.json"})
_CORS_METHODS = "GET, POST, OPTIONS"
_CORS_HEADERS = "Authorization, Content-Type, X-Hyprial-Catalog-Digest"
_CORS_EXPOSE = "X-Hyprial-Request-Id, X-Hyprial-Catalog-Digest"
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer token from server.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(_StrictModel):
    ok: bool


class ErrorEnvelope(_StrictModel):
    schemaVersion: int
    ok: bool
    requestId: str
    code: str
    message: str
    dispatch: str
    method: str | None = None
    data: dict[str, Any] | None = None


class SuccessEnvelope(_StrictModel):
    schemaVersion: int
    ok: bool
    method: str
    requestId: str
    dispatch: str
    result: dict[str, Any]


class ReadyResponse(_StrictModel):
    ok: bool
    ready: bool
    transport: str
    backend: dict[str, Any] | None = None
    catalog: dict[str, Any] | None = None
    methods: list[dict[str, Any]] | None = None
    notReady: list[str] | None = None
    reason: str | None = None
    code: str | None = None
    requestId: str | None = None


class CatalogResponse(_StrictModel):
    schemaVersion: int
    ok: bool
    httpContractVersion: int
    catalogVersion: str
    catalogDigest: str
    declarationDigest: str
    generatedFrom: str
    backend: dict[str, Any]
    principal: dict[str, Any]
    methods: list[dict[str, Any]]


class OpenAPIResponse(BaseModel):
    openapi: str
    info: dict[str, Any]
    paths: dict[str, Any]


def _header_values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


def _first_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    values = _header_values(headers, name)
    return values[0] if values else None


def _request_id(request: Request) -> str:
    return str(request.scope["state"]["request_id"])


def _started(request: Request) -> float:
    return float(request.scope["state"]["started"])


def _method_name(path: str) -> str | None:
    prefix = "/daemon/request/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return None


def _safe_body(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        return b""


def _encode(
    server: RequestHttpServer, envelope: _Envelope, request_id: str, method: str | None
) -> tuple[bytes, _Envelope]:
    limit = server.config.budgets.max_response_bytes
    body = _safe_body(envelope.payload)
    if body and len(body) <= limit:
        return body, envelope
    failure = HttpFailure(
        "INVALID_RESPONSE",
        "response exceeds the configured response budget",
        status=502,
        dispatch="unknown",
    )
    fallback = _Envelope(
        502,
        server._error_payload(failure, request_id, method),
        failure.code,
        failure.dispatch,
    )
    body = _safe_body(fallback.payload)
    if len(body) > limit:
        # The minimum configured budget cannot fit even the standard error
        # envelope.  Send the required status without exceeding the budget.
        return b"", fallback
    return body, fallback


def _render(
    server: RequestHttpServer,
    envelope: _Envelope,
    request_id: str,
    method: str | None,
    *,
    origin: str | None = None,
    cors: bool = False,
) -> Response:
    body, envelope = _encode(server, envelope, request_id, method)
    headers = {
        "X-Hyprial-Request-Id": request_id,
        "Connection": "close",
    }
    view = server._view
    if view is not None:
        headers["X-Hyprial-Catalog-Digest"] = view.http_contract_digest
    if cors and origin is not None and origin in server.config.allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Expose-Headers"] = _CORS_EXPOSE
    return Response(
        content=body,
        status_code=envelope.status,
        headers=headers,
        media_type="application/json",
    )


def _failure_envelope(
    server: RequestHttpServer, failure: HttpFailure, request_id: str, method: str | None
) -> _Envelope:
    return _Envelope(
        failure.status,
        server._error_payload(failure, request_id, method),
        failure.code,
        failure.dispatch,
    )


def _method_for_scope(scope: Scope) -> str | None:
    return _method_name(str(scope.get("path", "")))


class RequestContextMiddleware:
    """Request-target, header, CORS, concurrency and response-header guards."""

    def __init__(self, app: ASGIApp, server: RequestHttpServer) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = f"http-{uuid4().hex}"
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["started"] = time.monotonic()
        headers = list(scope.get("headers", []))
        origin = _first_header(headers, b"origin")
        method_name = _method_for_scope(scope)

        if scope.get("method") == "OPTIONS":
            await self._preflight(scope, receive, send, request_id, headers, origin)
            return

        if not self.server.semaphore.acquire(blocking=False):
            failure = HttpFailure("HTTP_BUSY", "too many concurrent requests", status=429)
            await self._send_failure(
                scope, receive, send, request_id, method_name, failure, origin, cors=True
            )
            return
        try:
            try:
                self._validate_target(scope, headers)
                self._validate_headers(scope, headers, origin)
                self._validate_route(scope)
            except HttpFailure as failure:
                await self._send_failure(
                    scope, receive, send, request_id, method_name, failure, origin, cors=True
                )
                return

            async def send_with_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    mutable = MutableHeaders(scope=message)
                    if "x-hyprial-request-id" not in mutable:
                        mutable["X-Hyprial-Request-Id"] = request_id
                    view = self.server._view
                    if "x-hyprial-catalog-digest" not in mutable and view is not None:
                        mutable["X-Hyprial-Catalog-Digest"] = view.http_contract_digest
                    if "connection" not in mutable:
                        mutable["Connection"] = "close"
                await send(message)

            await self.app(scope, receive, send_with_headers)
        finally:
            self.server.semaphore.release()

    async def _send_failure(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        method: str | None,
        failure: HttpFailure,
        origin: str | None,
        *,
        cors: bool,
    ) -> None:
        response = _render(
            self.server,
            _failure_envelope(self.server, failure, request_id, method),
            request_id,
            method,
            origin=origin,
            cors=cors,
        )
        await response(scope, receive, send)

    def _validate_target(self, scope: Scope, headers: list[tuple[bytes, bytes]]) -> None:
        raw = scope.get("hyprial.raw_target")
        if not raw:
            raw = scope.get("raw_path")
        if not raw:
            raw = str(scope.get("path", "")).encode("latin-1", "replace")
        if len(raw) > _MAX_REQUEST_LINE:
            raise HttpFailure("HTTP_URI_TOO_LONG", "request line is too long", status=414)
        target = raw.decode("latin-1", "replace")
        if target.startswith("//") or not target.startswith("/"):
            raise HttpFailure(
                ipc_errors.INVALID_REQUEST,
                "request target must be origin-form",
                status=400,
            )
        if "?" in target or "#" in target:
            raise HttpFailure(
                ipc_errors.INVALID_REQUEST,
                "query and fragment are not accepted",
                status=400,
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
            raise HttpFailure(
                ipc_errors.INVALID_REQUEST,
                "control characters are not accepted",
                status=400,
            )
        if ".." in target.split("/"):
            raise HttpFailure(
                ipc_errors.INVALID_REQUEST,
                "path traversal is not accepted",
                status=400,
            )
        if scope.get("query_string"):
            raise HttpFailure(
                ipc_errors.INVALID_REQUEST,
                "query and fragment are not accepted",
                status=400,
            )
        if target in _FIXED_PATHS:
            return
        prefix = "/daemon/request/"
        if target.startswith(prefix) and _METHOD_PATH.fullmatch(target[len(prefix) :]):
            return
        raise HttpFailure(ipc_errors.METHOD_NOT_FOUND, "unknown path", status=404)

    def _validate_headers(
        self, scope: Scope, headers: list[tuple[bytes, bytes]], origin: str | None
    ) -> None:
        if len(headers) > _MAX_HEADERS:
            raise HttpFailure("HTTP_HEADERS_TOO_LARGE", "too many headers", status=431)
        total = sum(len(name) + len(value) + 4 for name, value in headers)
        if total > _MAX_HEADER_BYTES:
            raise HttpFailure("HTTP_HEADERS_TOO_LARGE", "headers exceed the budget", status=431)
        for name in _DUPLICATE_REFUSED:
            if len(_header_values(headers, name)) > 1:
                raise HttpFailure(
                    ipc_errors.INVALID_REQUEST,
                    f"duplicate {name.decode()} header",
                    status=400,
                )
        host = _first_header(headers, b"host")
        if host is None:
            raise HttpFailure("HTTP_HOST_DENIED", "Host header is required", status=403)
        name = host.rpartition(":")[0] if host.count(":") == 1 else host
        if host.startswith("["):
            name = host.split("]", 1)[0] + "]"
        if name not in self.server.config.allowed_hosts:
            raise HttpFailure("HTTP_HOST_DENIED", "Host is not allowed", status=403)
        if origin is not None and origin not in self.server.config.allowed_origins:
            raise HttpFailure("HTTP_ORIGIN_DENIED", "Origin is not allowed", status=403)

    def _validate_route(self, scope: Scope) -> None:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        if path == "/healthz":
            allowed = {"GET"}
        elif path in {"/readyz", "/daemon/request", "/openapi.json"}:
            allowed = {"GET"}
        else:
            allowed = {"POST"}
        if method not in allowed:
            raise HttpFailure(
                "HTTP_METHOD_NOT_ALLOWED", "method is not allowed", status=405
            )

    async def _preflight(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        headers: list[tuple[bytes, bytes]],
        origin: str | None,
    ) -> None:
        try:
            self._validate_target(scope, headers)
            self._validate_headers(scope, headers, origin)
            if origin is None:
                raise HttpFailure(
                    "HTTP_ORIGIN_DENIED", "preflight requires an Origin", status=403
                )
        except HttpFailure as failure:
            await self._send_failure(
                scope, receive, send, request_id, None, failure, origin, cors=True
            )
            return
        response_headers = {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": _CORS_METHODS,
            "Access-Control-Allow-Headers": _CORS_HEADERS,
            "Access-Control-Max-Age": "600",
            "Access-Control-Expose-Headers": _CORS_EXPOSE,
            "X-Hyprial-Request-Id": request_id,
            "Connection": "close",
        }
        view = self.server._view
        if view is not None:
            response_headers["X-Hyprial-Catalog-Digest"] = view.http_contract_digest
        await Response(status_code=204, headers=response_headers)(scope, receive, send)


def _require_bearer(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    server: RequestHttpServer = request.app.state.server
    value = _first_header(list(request.scope["headers"]), b"authorization")
    expected = f"Bearer {server.token}"
    try:
        authorized = value is not None and secrets.compare_digest(value, expected)
    except TypeError:
        authorized = False
    if not authorized:
        raise HttpFailure("HTTP_UNAUTHORIZED", "bearer token is required", status=401)


async def _discard_oversized_body(
    server: RequestHttpServer, request: Request, length: int
) -> None:
    if length > 2 * server.config.budgets.max_request_bytes:
        return
    remaining = server.config.budgets.request_timeout - (time.monotonic() - _started(request))
    if remaining <= 0:
        return
    with anyio.move_on_after(min(server.config.budgets.header_timeout, remaining)):
        received = 0
        while received < length:
            message = await request.receive()
            if message["type"] != "http.request":
                return
            received += len(message.get("body", b""))
            if not message.get("more_body", False):
                return


async def _require_json_framing(request: Request) -> None:
    server: RequestHttpServer = request.app.state.server
    headers = list(request.scope["headers"])
    if _header_values(headers, b"transfer-encoding"):
        raise HttpFailure(
            ipc_errors.INVALID_REQUEST, "transfer-encoding is not accepted", status=400
        )
    content_encoding = _first_header(headers, b"content-encoding")
    if content_encoding not in (None, "identity"):
        raise HttpFailure(
            "HTTP_UNSUPPORTED_MEDIA_TYPE", "content-encoding is not accepted", status=415
        )
    raw_lengths = _header_values(headers, b"content-length")
    if not raw_lengths:
        raise HttpFailure("HTTP_LENGTH_REQUIRED", "Content-Length is required", status=411)
    if len(raw_lengths) != 1:
        raise HttpFailure(
            ipc_errors.INVALID_REQUEST, "duplicate Content-Length", status=400
        )
    text = raw_lengths[0].strip()
    if not text.isdigit():
        raise HttpFailure(
            ipc_errors.INVALID_REQUEST,
            "Content-Length must be a non-negative integer",
            status=400,
        )
    length = int(text)
    if length > server.config.budgets.max_request_bytes:
        await _discard_oversized_body(server, request, length)
        raise HttpFailure(
            "HTTP_BODY_TOO_LARGE", "request body exceeds the budget", status=413
        )
    content_type = _first_header(headers, b"content-type") or ""
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HttpFailure(
            "HTTP_UNSUPPORTED_MEDIA_TYPE",
            "Content-Type must be application/json",
            status=415,
        )
    if _first_header(headers, b"idempotency-key") is not None:
        raise HttpFailure(
            "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED",
            "Idempotency-Key is not supported by this safe-read surface",
            status=400,
        )
    request.scope["state"]["body_length"] = length


async def _read_params(server: RequestHttpServer, request: Request) -> dict[str, Any]:
    started = _started(request)
    remaining = server.config.budgets.request_timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise HttpFailure("HTTP_TIMEOUT", "request budget expired", status=408)
    read_timeout = min(server.config.budgets.header_timeout, remaining)
    try:
        with anyio.fail_after(read_timeout):
            raw = await request.body()
    except TimeoutError as error:
        raise HttpFailure("HTTP_TIMEOUT", "request body timed out", status=408) from error
    length = int(request.scope["state"].get("body_length", 0))
    if len(raw) != length:
        raise HttpFailure(
            ipc_errors.INVALID_REQUEST,
            "request body ended before Content-Length",
            status=400,
        )
    if len(raw) > server.config.budgets.max_request_bytes:
        raise HttpFailure(
            "HTTP_BODY_TOO_LARGE", "request body exceeds the budget", status=413
        )
    if not raw:
        return {}
    try:
        params = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HttpFailure(
            ipc_errors.INVALID_ARGUMENT, "body must be a JSON object", status=400
        ) from error
    if not isinstance(params, dict):
        raise HttpFailure(
            ipc_errors.INVALID_ARGUMENT, "body must be a JSON object", status=400
        )
    return params


def _success_schema(route: Any) -> dict[str, Any]:
    schema = deepcopy(SuccessEnvelope.model_json_schema())
    properties = schema.setdefault("properties", {})
    properties["result"] = deepcopy(dict(route.result_schema))
    return schema


def _method_openapi(route: Any) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": deepcopy(dict(route.params_schema)),
                    "description": (
                        "Method-specific body from daemon.catalog. A missing "
                        "local-operator selector is injected with principal.actor "
                        "before validation."
                    ),
                }
            },
        },
        "x-hyprial-method": route.name,
        "x-hyprial-state": route.state,
        "x-hyprial-http-binding": deepcopy(dict(route.http_binding)),
        "x-hyprial-identity": deepcopy(dict(route.http_binding["identity"])),
        "x-hyprial-errors": deepcopy(list(route.errors)),
        "responses": {
            "200": {
                "description": "Successful safe unary read",
                "content": {
                    "application/json": {"schema": _success_schema(route)}
                },
            },
            "400": {"description": "Local schema refusal", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "401": {"description": "Missing or wrong bearer", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "403": {"description": "Host, Origin or selector refusal", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "404": {"description": "Unknown or unexposed method", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "409": {"description": "Catalog/version mismatch", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "413": {"description": "Request body too large", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "415": {"description": "Unsupported media type", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "429": {"description": "Concurrency budget exhausted", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "500": {"description": "Daemon/internal failure", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "502": {"description": "Invalid daemon response", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "503": {"description": "Not ready/unavailable", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
            "504": {"description": "Daemon timeout", "content": {"application/json": {"schema": ErrorEnvelope.model_json_schema()}}},
        },
    }


def _decorate_openapi(schema: dict[str, Any], server: RequestHttpServer) -> None:
    schema["info"]["description"] = (
        "Hyprial local-operator HTTP request entry. Transport envelopes are typed "
        "here; per-method request/result JSON Schemas and membership come from the "
        "authoritative daemon catalog at GET /daemon/request."
    )
    view = server.current_view()
    for route in view.routes.values():
        item = schema.get("paths", {}).get(route.path, {}).get("post")
        if item is None:
            continue
        item["summary"] = f"Invoke {route.name}"
        item["x-hyprial-method"] = route.name
        item["x-hyprial-state"] = route.state
        item["x-hyprial-http-binding"] = deepcopy(dict(route.http_binding))
        item["x-hyprial-identity"] = deepcopy(dict(route.http_binding["identity"]))
        item["x-hyprial-errors"] = deepcopy(list(route.errors))
        item["description"] = (
            f"State `{route.state}` from daemon.catalog. The body schema is the "
            "daemon-declared paramsSchema; the result schema is the declared "
            "resultSchema wrapped in the typed success envelope."
        )
        item["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": deepcopy(dict(route.params_schema)),
                    "description": (
                        "A missing local-operator selector is injected with "
                        "principal.actor before validation."
                    ),
                }
            },
        }
        item.setdefault("responses", {}).setdefault("200", {})["content"] = {
            "application/json": {"schema": _success_schema(route)}
        }


def build_app(server: RequestHttpServer) -> FastAPI:
    """Build the real FastAPI app served by Uvicorn for this server."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.lifespan_started = True
        try:
            yield
        finally:
            app.state.lifespan_started = False

    app = FastAPI(
        title="Hyprial HTTP request entry",
        version=__version__,
        description=(
            "Local-operator safe unary reads. Transport envelopes are fixed; "
            "method schemas and membership come from daemon.catalog."
        ),
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.state.server = server

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(server.config.allowed_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Hyprial-Catalog-Digest"],
        expose_headers=["X-Hyprial-Request-Id", "X-Hyprial-Catalog-Digest"],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, server=server)

    @app.exception_handler(HttpFailure)
    async def _handle_http_failure(request: Request, failure: HttpFailure) -> Response:
        method = _method_name(str(request.scope.get("path", "")))
        return _render(
            server,
            _failure_envelope(server, failure, _request_id(request), method),
            _request_id(request),
            method,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, error: RequestValidationError
    ) -> Response:
        failure = HttpFailure(
            ipc_errors.INVALID_ARGUMENT,
            "request does not match the declared transport schema",
            status=400,
        )
        method = _method_name(str(request.scope.get("path", "")))
        return _render(
            server,
            _failure_envelope(server, failure, _request_id(request), method),
            _request_id(request),
            method,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        code = {
            404: ipc_errors.METHOD_NOT_FOUND,
            405: "HTTP_METHOD_NOT_ALLOWED",
            408: "HTTP_TIMEOUT",
            413: "HTTP_BODY_TOO_LARGE",
            415: "HTTP_UNSUPPORTED_MEDIA_TYPE",
            429: "HTTP_BUSY",
        }.get(error.status_code, "HTTP_INTERNAL")
        failure = HttpFailure(code, "request was refused by the transport", status=error.status_code)
        method = _method_name(str(request.scope.get("path", "")))
        return _render(
            server,
            _failure_envelope(server, failure, _request_id(request), method),
            _request_id(request),
            method,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, error: Exception) -> Response:
        method = _method_name(str(request.scope.get("path", "")))
        server._log("internal_error", requestId=_request_id(request), method=method)
        failure = HttpFailure(
            "HTTP_INTERNAL",
            "internal server error",
            dispatch="unknown" if method is not None else "not-dispatched",
        )
        return _render(
            server,
            _failure_envelope(server, failure, _request_id(request), method),
            _request_id(request),
            method,
        )

    async def healthz(request: Request) -> Response:
        return _render(
            server,
            _Envelope(200, {"ok": True}),
            _request_id(request),
            None,
        )

    async def readyz(request: Request) -> Response:
        envelope = await run_in_threadpool(
            server._ready, _request_id(request), _started(request)
        )
        return _render(server, envelope, _request_id(request), None)

    async def catalog(request: Request) -> Response:
        envelope = await run_in_threadpool(
            server._catalog, _request_id(request), _started(request)
        )
        return _render(server, envelope, _request_id(request), None)

    async def openapi_json(request: Request) -> Response:
        document = app.openapi()
        return _render(
            server,
            _Envelope(200, document),
            _request_id(request),
            None,
        )

    def method_endpoint(name: str) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            params = await _read_params(server, request)
            envelope = await run_in_threadpool(
                server._invoke,
                name,
                _request_id(request),
                _started(request),
                params,
                _first_header(list(request.scope["headers"]), b"x-hyprial-catalog-digest"),
            )
            return _render(server, envelope, _request_id(request), name)

        return endpoint

    app.add_api_route(
        "/healthz",
        healthz,
        methods=["GET"],
        response_model=HealthResponse,
        include_in_schema=True,
        tags=["transport"],
    )
    auth = [Depends(_require_bearer)]
    app.add_api_route(
        "/readyz",
        readyz,
        methods=["GET"],
        response_model=ReadyResponse,
        dependencies=auth,
        include_in_schema=True,
        tags=["transport"],
    )
    app.add_api_route(
        "/daemon/request",
        catalog,
        methods=["GET"],
        response_model=CatalogResponse,
        dependencies=auth,
        include_in_schema=True,
        tags=["transport"],
    )
    app.add_api_route(
        "/openapi.json",
        openapi_json,
        methods=["GET"],
        response_model=OpenAPIResponse,
        dependencies=auth,
        include_in_schema=True,
        tags=["transport"],
    )

    view = server.current_view()
    for route in sorted(view.routes.values(), key=lambda item: item.name):
        app.add_api_route(
            route.path,
            method_endpoint(route.name),
            methods=["POST"],
            response_model=SuccessEnvelope,
            dependencies=[Depends(_require_bearer), Depends(_require_json_framing)],
            openapi_extra=_method_openapi(route),
            include_in_schema=True,
            tags=["methods"],
        )

    async def unknown_method(request: Request) -> Response:
        name = str(request.path_params["method"])
        if not _METHOD_PATH.fullmatch(name):
            failure = HttpFailure(
                ipc_errors.METHOD_NOT_FOUND, "unknown method", status=404
            )
            return _render(
                server,
                _failure_envelope(
                    server, failure, _request_id(request), _method_name(request.url.path)
                ),
                _request_id(request),
                _method_name(request.url.path),
            )
        params = await _read_params(server, request)
        envelope = await run_in_threadpool(
            server._invoke,
            name,
            _request_id(request),
            _started(request),
            params,
            _first_header(list(request.scope["headers"]), b"x-hyprial-catalog-digest"),
        )
        return _render(server, envelope, _request_id(request), name)

    app.add_api_route(
        "/daemon/request/{method}",
        unknown_method,
        methods=["POST"],
        response_model=SuccessEnvelope,
        dependencies=[Depends(_require_bearer), Depends(_require_json_framing)],
        include_in_schema=False,
    )

    original_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        _decorate_openapi(schema, server)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    return app


def http_protocol_class(header_timeout: float) -> type:
    """Uvicorn protocol preserving the target and enforcing the header deadline."""

    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

    class _RawTargetProtocol(HttpToolsProtocol):
        _hyprial_header_timeout = float(header_timeout)
        _hyprial_header_timer: Any = None
        _hyprial_headers_complete = False

        def _cancel_header_timer(self) -> None:
            timer = self._hyprial_header_timer
            if timer is not None:
                timer.cancel()
                self._hyprial_header_timer = None

        def _header_deadline(self) -> None:
            self._hyprial_header_timer = None
            if (
                not self._hyprial_headers_complete
                and self.transport is not None
                and not self.transport.is_closing()
            ):
                self.transport.close()

        def on_message_begin(self) -> None:
            self._cancel_header_timer()
            self._hyprial_headers_complete = False
            super().on_message_begin()
            self._hyprial_header_timer = self.loop.call_later(
                self._hyprial_header_timeout, self._header_deadline
            )

        def on_headers_complete(self) -> None:
            self._hyprial_headers_complete = True
            self._cancel_header_timer()
            self.scope["hyprial.raw_target"] = bytes(self.url)
            super().on_headers_complete()

        def connection_lost(self, exc: Exception | None) -> None:
            self._cancel_header_timer()
            super().connection_lost(exc)

    return _RawTargetProtocol


__all__ = [
    "CatalogResponse",
    "ErrorEnvelope",
    "HealthResponse",
    "OpenAPIResponse",
    "ReadyResponse",
    "RequestContextMiddleware",
    "SuccessEnvelope",
    "build_app",
    "http_protocol_class",
]
