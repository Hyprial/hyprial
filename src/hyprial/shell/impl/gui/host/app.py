# Decision record (GUI session host, PR 2b):
# - Plain ASGI, no framework: the host owns every status, header and 404, and a
#   framework's redirect and not-found behaviour would have to be undone.
# - Only /daemon/* is the session surface, judged on the normalised path:
#   /daemon/../index.html is a static resource and passes the Host check first.
#   Every other path is a static resource and still passes the Host and Origin
#   rule, which is stricter than the Node host and invisible to the GUI.
# - Order of checks per request: target form, same-origin, host authority
#   (read before every route), then the route. An authority change observed
#   after the upstream call becomes GUI_SESSION_CHANGED, with dispatch unknown
#   once a write was sent.
# - The principal a browser sees is projected from the session phase and the
#   host config; no request field, header, cookie or query is consulted.
# - Failures that are not HostFailure become HOST_INTERNAL_ERROR with no
#   detail: nothing about the fault is echoed to the browser. Once a write has
#   been relayed any such failure carries dispatch unknown, never not-dispatched.
# - PR 3: with a live upstream (hyprial gui dev) every non-/daemon path that
#   passes the Host and Origin rule and is not /__gui/healthz is relayed to the
#   Vite dev server instead of the asset tree. A WebSocket upgrade is judged
#   like a mutation: Host, Origin required, same-origin Fetch Metadata, and a
#   /daemon/* target is never relayed; a refused upgrade is closed before the
#   upstream is contacted. The health mode is development only then.
# - PR 4b-H: developer tools exist only when the host was built with a DevTools
#   record (--dev-tools). Without it the two developer routes are unknown
#   /daemon paths (404 before the method check), the session body has no
#   devTools key, and /docs-assets/* and the tool prefix are answered 404 by the
#   host itself, also in front of a live upstream, which is never contacted for
#   them. With it the session body carries devTools, and /docs is the docs page
#   when the docs capability holds; a live upstream still serves the tool
#   sources, because live source exposes every source file by design.
#   /docs without the docs capability is the single-page fallback over a tree,
#   but in front of a live upstream the host answers 404 itself (owner decision
#   Q-2): the dev server is never asked for /docs, so a live-source launch
#   without the tools cannot be mistaken for one that serves the docs page.
# - POST /daemon/session/attach hands the live generation, CSRF value and expiry
#   to a second document that presents the proof a refresh needs (Origin,
#   control cookie, view) plus the session cookie. It contacts no upstream,
#   rotates nothing and sets no cookie, so the opener keeps working.
# - GET /daemon/openapi.json is a session-authenticated read of the daemon
#   driver's schema. The bearer is added by the upstream client and the document
#   is projected before it is answered; over the Contract Mock the route does
#   not exist.
"""ASGI entry for the same-origin session host."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import unquote

import httpx

from hyprial.kernel import ipc_errors

from .config import AuthorityUnavailable, AuthorityView, HostConfig
from .envelope import (
    MAX_REQUEST_BYTES,
    MAX_SESSION_BYTES,
    HostFailure,
    Receive,
    Reply,
    Request,
    Send,
    failure_reply,
    json_reply,
    new_request_id,
    relay_reply,
    send_reply,
    success_reply,
)
from .session import (
    CONTROL_COOKIE,
    SESSION_COOKIE,
    Clock,
    GrantStatus,
    GuiSession,
    RotatedSession,
    SessionAuthority,
    SessionStore,
    SystemClock,
    expire_cookies,
    parse_cookies,
    random_token,
    require_same_origin,
    session_cookies,
    session_required,
)
from .upstream import UpstreamClient
from .web.devtools import ATTACH_PATH, DOCS_ASSETS_PREFIX, DOCS_PATH, SCHEMA_PATH, DevTools, is_tool_path
from .web.launch import LaunchFiles
from .web.live import CLOSE_POLICY, LiveUpstream

REQUEST_PREFIX = "/daemon/request"
METHOD_NAME = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
SESSION_PATH = "/daemon/session"
HEALTH_PATH = "/__gui/healthz"
SESSION_CONTRACT = {"version": 1, "min": 1, "max": 1}
SESSION_CONTRACT_HEADER = "x-hyprial-session-contract"
_WELL_FORMED_VERSION = re.compile(r"[1-9][0-9]*")


class Resources(Protocol):
    """Serves everything that is not ``/daemon/*``: assets and the launcher probe."""

    def health(self) -> Mapping[str, Any]: ...

    def serve(self, path: str, *, head: bool) -> Reply: ...


class _Route:
    def __init__(self, kind: str, method: str | None = None, query: str = "") -> None:
        self.kind, self.method, self.query = kind, method, query


def normalise_path(raw: str) -> str:
    """Resolve a request path the way a WHATWG URL parser does.

    Backslashes are slashes, tabs and newlines vanish, ``.``/``..`` segments
    (also percent-encoded) are resolved and cannot climb above the root, and a
    leading ``//`` names an authority that is dropped. Nothing is rejected.
    """
    path = raw.split("#", 1)[0].replace("\\", "/")
    path = path.replace("\t", "").replace("\n", "").replace("\r", "")
    if not path.startswith("/"):
        return path
    if path.startswith("//"):
        rest = path[2:]
        slash = rest.find("/")
        path = rest[slash:] if slash >= 0 else "/"
    segments = path.split("/")[1:]
    resolved: list[str] = []
    for index, segment in enumerate(segments):
        lowered = segment.lower()
        last = index == len(segments) - 1
        if lowered in (".", "%2e"):
            if last:
                resolved.append("")
        elif lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
            if resolved:
                resolved.pop()
            if last:
                resolved.append("")
        else:
            resolved.append(segment)
    return "/" + "/".join(resolved)


def _route_of(path: str, query: str) -> _Route | None:
    """The route of a normalised ``/daemon/...`` path, ``None`` outside that prefix."""
    if not path.startswith("/daemon/"):
        return None
    suffix = f"?{query}" if query else ""
    simple = {
        SESSION_PATH: "session",
        f"{SESSION_PATH}/status": "status",
        f"{SESSION_PATH}/logout": "logout",
        "/daemon/readyz": "ready",
        ATTACH_PATH: "attach",
        SCHEMA_PATH: "openapi",
    }
    if path in simple:
        return _Route(simple[path])
    if path == REQUEST_PREFIX:
        return _Route("catalog", query=suffix)
    if path.startswith(f"{REQUEST_PREFIX}/"):
        name = path[len(REQUEST_PREFIX) + 1 :]
        if "/" not in name and METHOD_NAME.match(name):
            return _Route("method", name, suffix)
        return _Route("missing", name or None)
    return _Route("missing")


def principal_view(config: HostConfig, session: GuiSession) -> dict[str, Any]:
    """Explicit nulls for bootstrap, the verified identity for a serving view."""
    principal = config.principal
    if session.phase == "bootstrap" or principal is None:
        return {"kind": "anonymous-bootstrap", "owner": None, "node": None, "actor": None}
    return {"kind": "serving", "owner": principal.owner, "node": principal.node, "actor": principal.actor}


def _grant_failure(status: GrantStatus) -> HostFailure:
    if status == "used":
        return HostFailure(409, "LAUNCH_GRANT_USED", "the one-time launch grant was already consumed")
    if status == "expired":
        return HostFailure(401, "LAUNCH_GRANT_EXPIRED", "the one-time launch grant has expired")
    return HostFailure(403, "LAUNCH_GRANT_INVALID", "the launch grant is not valid for this host")


def _applied_contract_version(request: Request) -> int:
    """The session contract version this response applies: the client's, else 1.

    An absent header means version 1; any present value (empty, repeated or
    malformed included) must be an integer inside the served range, otherwise
    the request is refused before the grant, the probe or any consumption.
    """
    raw = request.raw_header(SESSION_CONTRACT_HEADER)
    if raw is None:
        return SESSION_CONTRACT["min"]
    if _WELL_FORMED_VERSION.fullmatch(raw) and SESSION_CONTRACT["min"] <= int(raw) <= SESSION_CONTRACT["max"]:
        return int(raw)
    raise HostFailure(
        409,
        "SESSION_CONTRACT_UNSUPPORTED",
        "the host does not serve the session contract version the client asked for",
        data={"min": SESSION_CONTRACT["min"], "max": SESSION_CONTRACT["max"]},
    )


def _require_method(request: Request, expected: str, message: str, method: str | None = None) -> None:
    if request.method != expected:
        raise HostFailure(405, "HTTP_METHOD_NOT_ALLOWED", message, method=method)


class HostApp:
    """The ASGI application: lifespan, static resources and the session routes."""

    def __init__(
        self,
        config: HostConfig,
        assets: Resources | None,
        *,
        upstream: UpstreamClient,
        clock: Clock,
        tokens: Callable[[], str],
        on_grant_consumed: Callable[[], None] | None,
        live: LiveUpstream | None = None,
        dev_tools: DevTools | None = None,
        authority: Callable[[], HostConfig] | None = None,
    ) -> None:
        self.config = config
        self.assets = assets
        self.live = live
        self.dev_tools = dev_tools
        self.upstream = upstream
        self.clock = clock
        self.store = SessionStore(config, clock=clock, tokens=tokens)
        self.authority = AuthorityView(config, self.store.invalidate_business_sessions, authority)
        self.launch = LaunchFiles(config, self.store, clock)
        self._on_grant_consumed = on_grant_consumed

    async def startup(self) -> None:
        self.launch.start()

    async def shutdown(self) -> None:
        self.launch.close()
        await self.upstream.aclose()
        if self.live is not None:
            await self.live.aclose()

    async def __call__(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(Request(scope, receive), send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)

    async def _websocket(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None:
        path = normalise_path((scope.get("raw_path") or scope["path"].encode("utf-8")).decode("latin-1"))
        admitted = self.live is not None and _route_of(path, "") is None
        if admitted:
            try:
                self._check_origin(Request({**scope, "method": "GET"}, receive), require_origin=True)
            except HostFailure:
                admitted = False
        if not admitted or self.live is None:
            await send({"type": "websocket.close", "code": CLOSE_POLICY})
            return
        await self.live.relay_websocket(scope, path, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await self.startup()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, request: Request, send: Send) -> None:
        path = normalise_path(request.path)
        route = _route_of(path, request.query)
        if route is not None:
            await send_reply(send, await self._daemon(request, route))
            return
        reply: Reply | None
        try:
            reply = self._static_gate(request, path)
            if reply is None:
                reply = self._developer_resource(path)
            if reply is None and self.live is None:
                reply = self._static_resource(path, request)
        except Exception:
            reply = failure_reply(
                new_request_id(),
                HostFailure(500, "HOST_INTERNAL_ERROR", "the same-origin host could not complete the request"),
            )
        if reply is None and self.live is not None:
            await self.live.relay_http(request, path, send)
            return
        assert reply is not None
        await send_reply(send, reply, include_body=request.method != "HEAD")

    def _check_origin(self, request: Request, *, require_origin: bool) -> None:
        require_same_origin(
            host=request.header("host"),
            origin=request.raw_header("origin"),
            fetch_site=request.raw_header("sec-fetch-site"),
            listener_port=request.listener_port,
            require_origin=require_origin,
        )

    def _static_gate(self, request: Request, path: str) -> Reply | None:
        """The reply for a refused or health request, ``None`` when a resource is to be served."""
        request_id = new_request_id()
        try:
            self._check_origin(request, require_origin=False)
        except HostFailure as failure:
            return failure_reply(request_id, failure)
        if request.method not in ("GET", "HEAD"):
            return failure_reply(
                request_id, HostFailure(405, "HTTP_METHOD_NOT_ALLOWED", "only GET and HEAD are served here")
            )
        if path == HEALTH_PATH:
            return json_reply(200, self._health())
        return None

    def _developer_resource(self, path: str) -> Reply | None:
        """The reply for a docs path or a gated tool path, ``None`` for any other resource."""
        missing = failure_reply(new_request_id(), HostFailure(404, "NOT_FOUND", "not found"))
        tools = self.dev_tools
        docs = tools is not None and tools.api_docs(self.config.driver)
        if path == DOCS_PATH:
            if tools is not None and docs:
                return tools.page()
            return missing if self.live is not None else None
        if path.startswith(DOCS_ASSETS_PREFIX):
            found = tools.asset(path[len(DOCS_ASSETS_PREFIX) :]) if tools is not None and docs else None
            return found if found is not None else missing
        if tools is None and self.live is not None and is_tool_path(unquote(path, errors="replace")):
            return missing
        return None

    def _static_resource(self, path: str, request: Request) -> Reply:
        if self.assets is None:
            return failure_reply(new_request_id(), HostFailure(404, "NOT_FOUND", "not found"))
        return self.assets.serve(path, head=request.method == "HEAD")

    def _health(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "service": "hyprial-gui",
            "mode": "production" if self.live is None else "development",
            "dataMode": "hosted",
            "driver": self.config.driver,
            "backendConfigured": True,
            "session": "launch-required",
            "devTools": self.dev_tools is not None,
        }
        if self.assets is not None:
            payload.update(self.assets.health())
        return payload

    async def _daemon(self, request: Request, route: _Route) -> Reply:
        request_id = new_request_id()
        try:
            return await self._dispatch(request, route, request_id)
        except HostFailure as failure:
            if route.method is not None and failure.method is None:
                failure = failure.but(method=route.method)
            return failure_reply(request_id, failure)
        except Exception:
            return failure_reply(
                request_id,
                HostFailure(
                    500,
                    "HOST_INTERNAL_ERROR",
                    "the same-origin host could not complete the request",
                    method=route.method,
                ),
            )

    def _read_authority(self) -> HostConfig:
        try:
            return self.authority.read()
        except AuthorityUnavailable:
            raise HostFailure(
                503, "GUI_AUTHORITY_UNAVAILABLE", "the launcher authority is unavailable"
            ) from None

    async def _dispatch(self, request: Request, route: _Route, request_id: str) -> Reply:
        self._check_origin(request, require_origin=request.method != "GET")
        config = self._read_authority()

        def assert_current(dispatched_write: bool = False) -> None:
            try:
                same = self._read_authority() is config
            except HostFailure:
                same = False
            if not same:
                raise HostFailure(
                    409,
                    "GUI_SESSION_CHANGED",
                    "the backend authority changed; refresh the view without replaying the operation",
                    dispatch="unknown" if dispatched_write else "not-dispatched",
                )

        if route.kind in ("attach", "openapi") and not self._serves(route.kind, config):
            route = _Route("missing")
        if route.kind == "missing":
            raise HostFailure(404, ipc_errors.METHOD_NOT_FOUND, "unknown daemon request path", method=route.method)
        if route.kind == "attach":
            return await self._attach(request, request_id)
        if route.kind == "openapi":
            return await self._openapi(request, config, request_id, assert_current)
        if route.kind == "session":
            return await self._open_session(request, config, request_id, assert_current)
        if route.kind == "status":
            return await self._status(request, request_id)
        if route.kind == "logout":
            return await self._logout(request, request_id)
        if route.kind in ("ready", "catalog"):
            return await self._read_route(request, route, config, request_id, assert_current)
        return await self._method(request, route, config, request_id, assert_current)

    def _serves(self, kind: str, config: HostConfig) -> bool:
        """Whether a developer route exists on this host for the current authority."""
        if self.dev_tools is None:
            return False
        return kind == "attach" or config.driver == "daemon"

    def _cookies(self, request: Request) -> dict[str, str]:
        return parse_cookies(request.raw_header("cookie"))

    def _authenticate(self, request: Request) -> GuiSession:
        return self.store.validate(
            self._cookies(request).get(SESSION_COOKIE),
            request.header("x-hyprial-view"),
            request.header("x-hyprial-generation"),
        )

    def _require_csrf(self, request: Request, session: GuiSession, method: str | None = None) -> None:
        if not self.store.csrf_matches(session, request.header("x-hyprial-csrf")):
            raise HostFailure(
                403,
                "CSRF_REJECTED",
                "the mutation CSRF value is missing or no longer current",
                method=method,
            )

    def _require_pin(self, request: Request, session: GuiSession, method: str | None = None) -> None:
        candidate = request.header("x-hyprial-catalog-digest")
        if candidate is not None and candidate != session.catalog_digest:
            raise HostFailure(
                409,
                "CATALOG_MISMATCH",
                "the browser catalog digest differs from the session pin",
                method=method,
            )

    def _session_reply(
        self,
        config: HostConfig,
        authority: SessionAuthority,
        rotated: RotatedSession,
        request_id: str,
        version: int,
    ) -> Reply:
        session = rotated.session
        fields = {
            "driver": authority.driver,
            "phase": authority.phase,
            "view": session.view,
            "generation": session.generation,
            "csrf": session.csrf,
            "expiresAt": session.expires_at,
            "sessionContract": {**SESSION_CONTRACT, "version": version},
            "principal": principal_view(config, session),
            "catalog": {
                "digest": authority.catalog_digest,
                "declarationDigest": authority.declaration_digest,
                "backendEpoch": authority.backend_epoch,
                "httpContractVersion": authority.http_contract_version,
                "catalogVersion": authority.catalog_version,
                "methodCount": len(authority.methods),
                "methods": [
                    {"name": m.name, "callable": m.callable, "state": m.state, "mutates": m.mutates}
                    for m in authority.methods
                ],
            },
        }
        if self.dev_tools is not None:
            fields["devTools"] = self.dev_tools.capability(authority.driver)
        return success_reply(request_id, fields, cookies=session_cookies(rotated, self.store.now))

    async def _open_session(
        self, request: Request, config: HostConfig, request_id: str, assert_current: Callable[..., None]
    ) -> Reply:
        _require_method(request, "POST", "session establishment requires POST")
        body, _ = await request.json_object(MAX_SESSION_BYTES)
        grant = body.get("grant")
        if "grant" in body:
            if len(body) != 1 or not isinstance(grant, str) or not grant:
                raise HostFailure(
                    400, ipc_errors.INVALID_REQUEST, 'an initial session body is exactly {"grant":"<one-time grant>"}'
                )
        elif body:
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "a refresh body must be an empty JSON object")
        version = _applied_contract_version(request)
        if isinstance(grant, str):
            initial = self.store.grant_status(grant)
            if initial != "ok":
                raise _grant_failure(initial)
            authority = await self.upstream.describe(config)
            assert_current()
            consumed = self.store.consume_grant(grant)
            if consumed != "ok":
                raise _grant_failure(consumed)
            self.launch.grant_consumed()
            if self._on_grant_consumed is not None:
                self._on_grant_consumed()
            rotated = self.store.mint(authority)
            return self._session_reply(config, authority, rotated, request_id, version)
        control_id = self._cookies(request).get(CONTROL_COOKIE)
        view = request.header("x-hyprial-view")
        if not control_id or not view:
            raise session_required("refresh requires the control cookie and view proof")
        self.store.require_control(control_id, view)
        authority = await self.upstream.describe(config)
        assert_current()
        rotated = self.store.refresh(control_id, view, authority)
        return self._session_reply(config, authority, rotated, request_id, version)

    async def _status(self, request: Request, request_id: str) -> Reply:
        _require_method(request, "POST", "session status requires POST")
        body, _ = await request.json_object(MAX_SESSION_BYTES)
        if body:
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "session status takes no authority fields")
        self._read_authority()
        cookies = self._cookies(request)
        current = self.store.is_current(
            cookies.get(CONTROL_COOKIE),
            request.header("x-hyprial-view"),
            cookies.get(SESSION_COOKIE),
            request.header("x-hyprial-generation"),
        )
        return success_reply(request_id, {"current": current})

    async def _attach(self, request: Request, request_id: str) -> Reply:
        _require_method(request, "POST", "session attach requires POST")
        body, _ = await request.json_object(MAX_SESSION_BYTES)
        if body:
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "session attach takes no authority fields")
        cookies = self._cookies(request)
        session = self.store.attach(
            cookies.get(CONTROL_COOKIE), cookies.get(SESSION_COOKIE), request.header("x-hyprial-view")
        )
        return success_reply(
            request_id, {"generation": session.generation, "csrf": session.csrf, "expiresAt": session.expires_at}
        )

    async def _openapi(
        self, request: Request, config: HostConfig, request_id: str, assert_current: Callable[..., None]
    ) -> Reply:
        _require_method(request, "GET", "the schema requires GET")
        self._authenticate(request)
        document = await self.upstream.schema(config)
        assert_current()
        return json_reply(200, document, {"x-hyprial-request-id": request_id})

    async def _logout(self, request: Request, request_id: str) -> Reply:
        _require_method(request, "POST", "logout requires POST")
        await request.json_object(MAX_SESSION_BYTES)
        session = self._authenticate(request)
        self._require_csrf(request, session)
        self.store.revoke(session.id, include_control=True)
        return success_reply(request_id, {"revoked": True}, cookies=expire_cookies())

    async def _read_route(
        self,
        request: Request,
        route: _Route,
        config: HostConfig,
        request_id: str,
        assert_current: Callable[..., None],
    ) -> Reply:
        ready = route.kind == "ready"
        _require_method(request, "GET", "readyz requires GET" if ready else "catalog requires GET")
        session = self._authenticate(request)
        self._require_pin(request, session)
        if config.driver == "contract-mock":
            return self._mock_read(config, session, ready, request_id)
        result = await self.upstream.relay(
            config,
            path="/readyz" if ready else f"{REQUEST_PREFIX}{route.query}",
            method="GET",
            incoming_request_id=request.header("x-hyprial-request-id"),
            body=None,
            write=False,
            pinned_digest=session.catalog_digest,
            expected_method=None,
            fallback_request_id=request_id,
        )
        assert_current()
        return relay_reply(result.status, result.headers, result.body)

    def _mock_read(self, config: HostConfig, session: GuiSession, ready: bool, request_id: str) -> Reply:
        digest = {"x-hyprial-catalog-digest": session.catalog_digest}
        if ready:
            return success_reply(
                request_id,
                {
                    "ready": True,
                    "transport": "ready",
                    "driver": "contract-mock",
                    "backend": {"available": True, "epoch": session.backend_epoch},
                    "catalog": {
                        "fresh": True,
                        "catalogDigest": session.catalog_digest,
                        "declarationDigest": session.declaration_digest,
                    },
                },
                digest,
            )
        payload = {
            "schemaVersion": 1,
            "ok": True,
            "requestId": request_id,
            "httpContractVersion": session.http_contract_version,
            "catalogVersion": session.catalog_version,
            "catalogDigest": session.catalog_digest,
            "declarationDigest": session.declaration_digest,
            "generatedFrom": "contract-mock",
            "backend": {"kind": "contract-mock", "epoch": session.backend_epoch},
            "principal": principal_view(config, session),
            "methods": [
                {
                    "name": m.name,
                    "path": f"{REQUEST_PREFIX}/{m.name}",
                    "state": m.state,
                    "callable": m.callable,
                    "mutates": m.mutates,
                }
                for m in session.methods.values()
            ],
        }
        return json_reply(200, payload, {"x-hyprial-request-id": request_id, **digest})

    async def _method(
        self,
        request: Request,
        route: _Route,
        config: HostConfig,
        request_id: str,
        assert_current: Callable[..., None],
    ) -> Reply:
        name = route.method or ""
        _require_method(request, "POST", "daemon methods require POST", name)
        session = self._authenticate(request)
        metadata = session.methods.get(name)
        if metadata is None:
            raise HostFailure(404, ipc_errors.METHOD_NOT_FOUND, "the method is not present in the pinned catalog")
        if not metadata.callable:
            raise HostFailure(
                409, "METHOD_NOT_CALLABLE", "the method is present in the pinned catalog but is not callable"
            )
        self._require_pin(request, session, name)
        write = metadata.mutates
        if write:
            self._require_csrf(request, session, name)
        _, raw = await request.json_object(MAX_REQUEST_BYTES, name)
        assert_current()
        self._authenticate(request)
        try:
            result = await self.upstream.relay(
                config,
                path=f"{REQUEST_PREFIX}/{name}{route.query}",
                method="POST",
                incoming_request_id=request.header("x-hyprial-request-id"),
                body=raw,
                write=write,
                pinned_digest=session.catalog_digest,
                expected_method=name,
                fallback_request_id=request_id,
            )
            assert_current(write)
            return relay_reply(result.status, result.headers, result.body)
        except HostFailure:
            raise
        except Exception:
            raise HostFailure(
                500,
                "HOST_INTERNAL_ERROR",
                "the same-origin host could not complete the request",
                dispatch="unknown" if write else "not-dispatched",
            ) from None


def build_app(
    config: HostConfig,
    assets: Resources | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
    tokens: Callable[[], str] = random_token,
    on_grant_consumed: Callable[[], None] | None = None,
    live: LiveUpstream | None = None,
    dev_tools: DevTools | None = None,
    authority: Callable[[], HostConfig] | None = None,
) -> HostApp:
    """Build the host application; ``transport`` and ``clock`` are test seams.

    ``authority`` is an in-process source of immutable ``HostConfig`` values
    (the daemon driver served in this process).
    """
    return HostApp(
        config,
        assets,
        upstream=UpstreamClient(transport=transport),
        clock=clock or SystemClock(),
        tokens=tokens,
        on_grant_consumed=on_grant_consumed,
        live=live,
        dev_tools=dev_tools,
        authority=authority,
    )
