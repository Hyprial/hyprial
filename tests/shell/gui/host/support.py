"""Shared builders for the host tests: configs, fake clocks and a fake upstream."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Callable

import httpx

from hyprial.shell.impl.gui.host.config import HostConfig, Principal

GRANT = "launch-grant-".ljust(64, "x")
UPSTREAM_TOKEN = "upstream-secret-never-for-browser"
PRINCIPAL = Principal(actor="user:owner", owner="owner", node="node")
DIGEST = "sha256:catalog"
DECLARATION = "sha256:declarations"
EPOCH = "daemon-epoch"
ENDPOINT = "http://127.0.0.1:8791"


def host_config(**overrides: Any) -> HostConfig:
    base = HostConfig(
        driver="daemon",
        principal=PRINCIPAL,
        endpoint=ENDPOINT,
        upstream_token=UPSTREAM_TOKEN,
        launch_grant=GRANT,
        handoff_path=None,
        publication_path=None,
        launch_ttl_ms=60_000,
        session_ttl_ms=60_000,
        max_sessions=8,
    )
    return replace(base, **overrides)


class FakeClock:
    """A manual clock: ``advance`` runs every callback that has become due."""

    def __init__(self, now_ms: int = 1_000_000) -> None:
        self._now = now_ms
        self._pending: list[tuple[float, int, Callable[[], None]]] = []
        self._serial = 0

    def now_ms(self) -> int:
        return int(self._now)

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> "FakeHandle":
        self._serial += 1
        entry = (self._now + delay_seconds * 1000, self._serial, callback)
        self._pending.append(entry)
        return FakeHandle(self, entry)

    def advance(self, milliseconds: float) -> None:
        target = self._now + milliseconds
        while True:
            due = sorted(
                (entry for entry in self._pending if entry[0] <= target), key=lambda e: (e[0], e[1])
            )
            if not due:
                break
            entry = due[0]
            self._pending.remove(entry)
            self._now = max(self._now, entry[0])
            entry[2]()
        self._now = target


class FakeHandle:
    def __init__(self, clock: FakeClock, entry: tuple[float, int, Callable[[], None]]) -> None:
        self._clock, self._entry = clock, entry

    def cancel(self) -> None:
        if self._entry in self._clock._pending:
            self._clock._pending.remove(self._entry)


def ready_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "ready": True,
        "transport": "ready",
        "backend": {"available": True, "owner": PRINCIPAL.owner, "nodeId": PRINCIPAL.node, "epoch": EPOCH},
        "catalog": {"fresh": True, "catalogDigest": DIGEST, "declarationDigest": DECLARATION},
        "notReady": [],
    }
    payload.update(overrides)
    return payload


def catalog_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "ok": True,
        "httpContractVersion": 1,
        "catalogVersion": "1",
        "catalogDigest": DIGEST,
        "declarationDigest": DECLARATION,
        "generatedFrom": "daemon.catalog",
        "backend": {"kind": "daemon", "owner": PRINCIPAL.owner, "nodeId": PRINCIPAL.node, "epoch": EPOCH},
        "principal": {"kind": "local-operator", "owner": PRINCIPAL.owner, "actor": PRINCIPAL.actor},
        "methods": [
            {"name": "agent.list", "state": "available", "callable": True, "mutates": False},
            {"name": "agent.create", "state": "available", "callable": True, "mutates": True},
            {"name": "message.query", "state": "available", "callable": True, "mutates": False},
        ],
    }
    payload.update(overrides)
    return payload


def method_success(method: str, request_id: str = "upstream-request") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "ok": True,
        "method": method,
        "requestId": request_id,
        "dispatch": "dispatched",
        "result": {"source": "upstream"},
    }


class FakeUpstream:
    """An ``httpx.MockTransport`` handler that records every request it sees."""

    def __init__(
        self,
        *,
        ready: dict[str, Any] | None = None,
        ready_status: int = 200,
        catalog: dict[str, Any] | None = None,
        catalog_status: int = 200,
        method: Callable[[httpx.Request, bytes], Any] | None = None,
        openapi: Any = None,
        openapi_status: int = 200,
    ) -> None:
        self.ready, self.ready_status = ready, ready_status
        self.catalog, self.catalog_status = catalog, catalog_status
        self.method = method
        self.openapi, self.openapi_status = openapi, openapi_status
        self.captured: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.captured.append(request)
        target = request.url.path
        if target == "/readyz":
            return json_response(self.ready_status, self.ready if self.ready is not None else ready_payload())
        if target == "/daemon/request":
            body = self.catalog if self.catalog is not None else catalog_payload()
            return json_response(
                self.catalog_status, body, {"x-hyprial-catalog-digest": str(body.get("catalogDigest", DIGEST))}
            )
        if target == "/openapi.json" and self.openapi is not None:
            return json_response(self.openapi_status, self.openapi)
        if target.startswith("/daemon/request/"):
            name = target[len("/daemon/request/") :].split("?", 1)[0]
            if self.method is None:
                return json_response(200, method_success(name))
            return self.method(request, request.content)
        return json_response(404, {"code": "NOT_FOUND"})

    def posts(self) -> list[httpx.Request]:
        return [request for request in self.captured if request.method == "POST"]


def json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json; charset=utf-8", **(headers or {})},
    )


ORIGIN = "http://127.0.0.1:4173"


class Reply:
    """A response as the browser sees it."""

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self.raw = response.text
        try:
            parsed = json.loads(self.raw)
        except ValueError:
            parsed = None
        self.json: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

    def cookies(self) -> str:
        return "; ".join(value.split(";", 1)[0] for value in self.headers.get_list("set-cookie"))


class Gateway:
    """Drives the ASGI host in process through ``httpx.ASGITransport``."""

    def __init__(self, client: httpx.AsyncClient, app: Any) -> None:
        self.client, self.app = client, app

    async def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        omit_origin: bool = False,
        origin: str | None = None,
        host: str | None = None,
        raw_body: bytes | None = None,
    ) -> Reply:
        sent: dict[str, str] = {}
        if not omit_origin:
            sent["origin"] = origin or ORIGIN
        if host is not None:
            sent["host"] = host
        content = raw_body
        if body is not None:
            content = json.dumps(body).encode()
        if content is not None:
            sent["content-type"] = "application/json"
        sent.update(headers or {})
        return Reply(await self.client.request(method, path, content=content, headers=sent))

    async def raw(
        self, raw_path: bytes, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes = b""
    ) -> tuple[int, dict[str, str], bytes]:
        """Call the ASGI app with an exact raw path that no HTTP client would send."""
        sent = {"host": "127.0.0.1:4173", "origin": ORIGIN, **(headers or {})}
        scope = {
            "type": "http",
            "method": method,
            "path": raw_path.decode("latin-1"),
            "raw_path": raw_path,
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in sent.items()],
            "server": ("127.0.0.1", 4173),
        }
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await self.app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body

    async def open(self, **options: Any) -> "Opened":
        reply = await self.call("/daemon/session", method="POST", body={"grant": GRANT}, **options)
        return Opened(reply)


class Opened:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.session = reply.json
        self.cookies = reply.cookies()

    def headers(self, *, csrf: str | None = None, digest: str | None = None, cookies: str | None = None) -> dict[str, str]:
        return browser_headers(self.session, self.cookies if cookies is None else cookies, csrf, digest)


def browser_headers(
    session: dict[str, Any], cookies: str, csrf: str | None = None, digest: str | None = None
) -> dict[str, str]:
    return {
        "cookie": cookies,
        "x-hyprial-view": str(session.get("view")),
        "x-hyprial-generation": str(session.get("generation")),
        "x-hyprial-csrf": str(session.get("csrf") if csrf is None else csrf),
        "x-hyprial-catalog-digest": str(digest or (session.get("catalog") or {}).get("digest")),
    }


def run_gateway(
    scenario: Callable[["Gateway"], Any],
    *,
    upstream: FakeUpstream | httpx.MockTransport | None = None,
    config: HostConfig | None = None,
    clock: Any = None,
    assets: Any = None,
    on_grant_consumed: Callable[[], None] | None = None,
    live: Any = None,
    dev_tools: Any = None,
    authority: Callable[[], HostConfig] | None = None,
) -> Any:
    """Run ``scenario`` against a freshly built host and always shut it down."""
    import asyncio
    from http.cookiejar import DefaultCookiePolicy

    from hyprial.shell.impl.gui.host.app import build_app

    if isinstance(upstream, FakeUpstream):
        transport: httpx.MockTransport | None = upstream.transport()
    else:
        transport = upstream
    if transport is None:
        transport = FakeUpstream().transport()

    async def main() -> Any:
        app = build_app(
            config or host_config(),
            assets,
            transport=transport,
            clock=clock,
            on_grant_consumed=on_grant_consumed,
            live=live,
            dev_tools=dev_tools,
            authority=authority,
        )
        await app.startup()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
                return await scenario(Gateway(client, app))
        finally:
            await app.shutdown()

    return asyncio.run(main())
