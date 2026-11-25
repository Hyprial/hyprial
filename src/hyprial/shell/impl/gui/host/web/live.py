# Decision record (GUI source launch, PR 3):
# - `hyprial gui dev` puts this host in front of the Vite dev server: the page
#   bytes and the HMR socket come from Vite, everything else (the session, the
#   Host and Origin rule, /daemon/*, /__gui/healthz) is still this host. Vite
#   holds no backend variable, no grant and no bearer, and the browser never
#   learns its port.
# - The upstream must be a loopback literal origin (127.0.0.1 or [::1]) with a
#   port, no userinfo, path, query or fragment; anything else refuses the start.
# - HTTP is GET and HEAD only. The request headers that cross are an exact
#   allowlist; cookies, Authorization, Origin and Host never do. The response is
#   streamed unchanged except hop-by-hop headers, Set-Cookie and
#   Access-Control-* (a conforming host emits no CORS header). The path is the
#   normalised one the host already judged, with dot segments resolved, and a
#   control character in it or in the query is a 400 before anything leaves.
# - The path must start with "/" (2026-11-16, found in review). An HTTP parser
#   accepts request targets such as "*@other-host/x"; appended to the origin
#   that reads as userinfo plus another host, which made the relay reachable
#   to any address for a local process, over HTTP and the WebSocket alike. The
#   joined URL is also parsed back and its scheme, host, port and userinfo must
#   equal the configured origin before anything leaves.
# - Headers the dev server names in its Connection header are hop-by-hop too
#   (RFC 9110 7.6.1) and are dropped with the fixed set.
# - The client is built like the session upstream: trust_env False (httpx
#   honours HTTP(S)_PROXY by default), no redirects, a reject-all cookie
#   policy. The WebSocket client passes proxy=None for the same reason:
#   websockets reads the proxy environment unless told not to.
# - An upstream that cannot be reached before a response starts is a host
#   envelope, 502 UPSTREAM_UNAVAILABLE with not-dispatched (an asset read has no
#   side effect). A failure after the response started cannot change the status
#   and propagates, which closes that one connection.
# - The WebSocket is relayed frame for frame with the subprotocol the upstream
#   chose (vite-hmr) and the query it was given (Vite's token). The caller has
#   already applied the Host and Origin rule with Origin required and kept
#   /daemon/* out; the upstream sees Origin set to its own origin.
"""Reverse proxy to the Vite dev server: HTTP assets and the HMR WebSocket."""

from __future__ import annotations

import asyncio
from http.cookiejar import DefaultCookiePolicy
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import WebSocketException

from hyprial.kernel import ipc_errors

from ..envelope import HostFailure, Receive, Request, Send, failure_reply, new_request_id, send_reply

FORWARDED_REQUEST_HEADERS = (
    "accept",
    "accept-encoding",
    "accept-language",
    "if-none-match",
    "if-modified-since",
    "range",
    "user-agent",
    "sec-fetch-dest",
    "sec-fetch-mode",
)
DROPPED_RESPONSE_HEADERS = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "set-cookie", "proxy-connection"}
)
UPSTREAM_CONNECT_SECONDS = 5.0
UPSTREAM_READ_SECONDS = 60.0
CLOSE_NORMAL = 1000
CLOSE_POLICY = 1008
CLOSE_INTERNAL = 1011
UNSENDABLE_CLOSE_CODES = frozenset({1005, 1006, 1015})


class LiveOriginError(ValueError):
    """The ``--live-upstream`` value is not a loopback origin the host may relay to."""


def parse_live_origin(value: str) -> str:
    """Validate ``--live-upstream`` and return it as ``http://<loopback>:<port>``."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise LiveOriginError(f"--live-upstream is not a valid origin: {error}") from None
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "http"
        or hostname not in ("127.0.0.1", "::1")
        or port is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise LiveOriginError("--live-upstream must be http://127.0.0.1:<port> or http://[::1]:<port>")
    shown = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{shown}:{port}"


def _unsafe(text: str) -> bool:
    return any(ord(char) < 0x21 or ord(char) == 0x7F for char in text)


def _unavailable(message: str) -> HostFailure:
    return HostFailure(502, "UPSTREAM_UNAVAILABLE", message)


class LiveUpstream:
    """Relays everything outside ``/daemon/*`` to one loopback Vite dev server."""

    def __init__(
        self,
        origin: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect: Callable[..., Any] = connect_websocket,
    ) -> None:
        self._origin = parse_live_origin(origin)
        pinned = httpx.URL(self._origin)
        self._authority = (pinned.scheme, pinned.host, pinned.port, pinned.userinfo)
        self._connect = connect
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(UPSTREAM_READ_SECONDS, connect=UPSTREAM_CONNECT_SECONDS),
        )
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    @property
    def origin(self) -> str:
        return self._origin

    async def aclose(self) -> None:
        await self._client.aclose()

    def _target(self, path: str, query: str) -> str:
        """Join the origin and an absolute path; refuse anything that would name another authority."""
        refusal = HostFailure(400, ipc_errors.INVALID_REQUEST, "request target is not a valid origin-form URL")
        if not path.startswith("/") or _unsafe(path) or _unsafe(query):
            raise refusal
        target = f"{self._origin}{path}" + (f"?{query}" if query else "")
        try:
            built = httpx.URL(target)
        except httpx.InvalidURL:
            raise refusal from None
        if (built.scheme, built.host, built.port, built.userinfo) != self._authority:
            raise refusal
        return target

    async def relay_http(self, request: Request, path: str, send: Send) -> None:
        """Stream one GET or HEAD answer from the dev server, or a host envelope."""
        head = request.method == "HEAD"
        started = False
        try:
            url = self._target(path, request.query)
            headers = {name: value for name in FORWARDED_REQUEST_HEADERS if (value := request.header(name)) is not None}
            async with self._client.stream(request.method, url, headers=headers) as response:
                if not 100 <= response.status_code <= 599:
                    raise HostFailure(502, "UPSTREAM_INVALID_RESPONSE", "the development GUI server answered with an invalid status")
                named = {
                    token.strip().lower()
                    for value in response.headers.get_list("connection")
                    for token in value.split(",")
                    if token.strip()
                }
                pairs = [
                    (name.lower().encode("latin-1"), value.encode("latin-1"))
                    for name, value in response.headers.multi_items()
                    if name.lower() not in DROPPED_RESPONSE_HEADERS
                    and name.lower() not in named
                    and not name.lower().startswith("access-control-")
                ]
                started = True
                await send({"type": "http.response.start", "status": response.status_code, "headers": pairs})
                if not head:
                    async for chunk in response.aiter_raw():
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
        except HostFailure as failure:
            await send_reply(send, failure_reply(new_request_id(), failure), include_body=not head)
        except (httpx.HTTPError, OSError):
            if started:
                raise
            failure = _unavailable("the development GUI server is not reachable")
            await send_reply(send, failure_reply(new_request_id(), failure), include_body=not head)

    async def relay_websocket(self, scope: Mapping[str, Any], path: str, receive: Receive, send: Send) -> None:
        """Relay one WebSocket (Vite HMR) frame for frame; close before accept on any refusal."""
        first = await receive()
        if first["type"] != "websocket.connect":
            return
        query = scope.get("query_string", b"").decode("latin-1")
        try:
            url = self._target(path, query).replace("http://", "ws://", 1)
        except HostFailure:
            await send({"type": "websocket.close", "code": CLOSE_POLICY})
            return
        try:
            async with self._connect(
                url,
                subprotocols=list(scope.get("subprotocols") or ()) or None,
                origin=self._origin,
                proxy=None,
                open_timeout=UPSTREAM_CONNECT_SECONDS,
            ) as remote:
                await send({"type": "websocket.accept", "subprotocol": remote.subprotocol})
                await self._pump(remote, receive, send)
        except (WebSocketException, OSError, TimeoutError):
            await _close(send, CLOSE_INTERNAL)

    async def _pump(self, remote: Any, receive: Receive, send: Send) -> None:
        down = asyncio.create_task(_downstream(remote, send))
        up = asyncio.create_task(_upstream(remote, receive))
        done, pending = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
        if down in done:
            code = remote.close_code
            await _close(send, CLOSE_NORMAL if code is None or code == 1005 else CLOSE_INTERNAL if code in UNSENDABLE_CLOSE_CODES else code)


async def _downstream(remote: Any, send: Send) -> None:
    try:
        async for message in remote:
            key = "text" if isinstance(message, str) else "bytes"
            await send({"type": "websocket.send", key: message})
    except WebSocketException:
        return


async def _upstream(remote: Any, receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] != "websocket.receive":
            return
        payload = message.get("text") if message.get("text") is not None else message.get("bytes")
        if payload is None:
            continue
        try:
            await remote.send(payload)
        except WebSocketException:
            return


async def _close(send: Send, code: int) -> None:
    try:
        await send({"type": "websocket.close", "code": code})
    except (RuntimeError, OSError):
        return
