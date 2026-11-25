from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


Response = tuple[int, dict[str, str], bytes]
Handler = Callable[[Request], Awaitable[Response]]


def json_response(status: int, body: object) -> Response:
    payload = json.dumps(body, separators=(",", ":")).encode()
    return status, {"content-type": "application/json"}, payload


class LocalHttpServer:
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._server: asyncio.Server | None = None
        self.requests: list[Request] = []

    async def __aenter__(self) -> LocalHttpServer:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        address = self._server.sockets[0].getsockname()
        return f"http://127.0.0.1:{address[1]}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head[:-4].decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {
                key.strip().lower(): value.strip()
                for line in lines[1:]
                for key, value in (line.split(":", 1),)
            }
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            request = Request(method, target, headers, body)
            self.requests.append(request)
            status, response_headers, response_body = await self._handler(request)
            reason = {
                200: "OK",
                400: "Bad Request",
                404: "Not Found",
                429: "Too Many Requests",
                500: "Internal Server Error",
                503: "Service Unavailable",
            }.get(status, "Response")
            headers_out = {
                "content-length": str(len(response_body)),
                "connection": "close",
                **response_headers,
            }
            encoded = "".join(
                f"{name}: {value}\r\n" for name, value in headers_out.items()
            ).encode("latin-1")
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
                + encoded
                + b"\r\n"
                + response_body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
