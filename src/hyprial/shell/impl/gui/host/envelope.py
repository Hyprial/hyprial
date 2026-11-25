# Decision record (GUI session host, PR 2b):
# - Host-generated failures use the same top-level envelope as the daemon HTTP
#   entry. Upstream envelopes are relayed byte for byte and never rebuilt; only
#   host refusals, transport loss and invalid upstream framing are rebuilt.
# - A host request id (gui-<uuid4>) identifies only a host refusal or transport
#   loss; a relayed upstream request id always wins for dispatched responses.
# - Header lookups fail closed: a header that is repeated or empty reads as
#   absent, so a duplicated credential header can never select a value.
# - Bodies are bounded while they are read, not after: a declared length over
#   the limit is refused before any byte is consumed.
# - JSON is parsed strictly on both sides of the relay: NaN, Infinity and
#   -Infinity are not JSON (the Node host refused them too) and a document
#   nested deeper than the interpreter allows is a parse failure, never an
#   internal error. loads_strict raises ValueError for every such case.
# - A value that becomes an HTTP header (request id, digest, dispatch) must be
#   printable ASCII; anything else cannot be sent, so it is refused instead of
#   reaching the ASGI server as an encoding fault.
"""Host envelopes, bounded body readers and the minimal ASGI request view."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping

from hyprial.kernel import ipc_errors

Dispatch = Literal["not-dispatched", "dispatched", "unknown"]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

MAX_REQUEST_BYTES = 131_072
MAX_SESSION_BYTES = 8_192
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

BASE_HEADERS = (
    ("content-type", "application/json; charset=utf-8"),
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
)


class HostFailure(Exception):
    """A refusal the host itself produces, with its dispatch truth."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        dispatch: Dispatch = "not-dispatched",
        method: str | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.dispatch = dispatch
        self.method = method
        self.data = data

    def but(self, **changes: Any) -> "HostFailure":
        """A copy with some fields replaced (dispatch, method)."""
        fields: dict[str, Any] = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "dispatch": self.dispatch,
            "method": self.method,
            "data": self.data,
        }
        fields.update(changes)
        status, code, message = fields.pop("status"), fields.pop("code"), fields.pop("message")
        return HostFailure(status, code, message, **fields)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def loads_strict(raw: bytes) -> Any:
    """Parse UTF-8 JSON strictly; every failure, nesting depth included, is a ``ValueError``."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON is nested too deeply") from None
    except UnicodeDecodeError as error:
        raise ValueError("JSON is not UTF-8") from error


def header_safe(value: str) -> bool:
    """True when ``value`` can be sent as an HTTP header value: printable ASCII only."""
    return value.isascii() and value.isprintable()


def new_request_id() -> str:
    """Host-generated request ids carry the ``gui-`` prefix."""
    return f"gui-{uuid.uuid4()}"


def encode_json(payload: Any) -> bytes:
    """Compact UTF-8 JSON, the framing every host envelope uses."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Reply:
    """A complete response: status, headers, body and any number of cookies."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    cookies: tuple[str, ...] = field(default=())

    def with_headers(self, extra: Iterable[tuple[str, str]]) -> "Reply":
        return replace(self, headers=(*self.headers, *extra))


def json_reply(
    status: int,
    payload: Any,
    headers: Mapping[str, str] | None = None,
    cookies: Iterable[str] = (),
) -> Reply:
    merged = dict(BASE_HEADERS)
    merged.update({name.lower(): value for name, value in (headers or {}).items()})
    return Reply(status, tuple(merged.items()), encode_json(payload), tuple(cookies))


def success_reply(
    request_id: str,
    fields: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    cookies: Iterable[str] = (),
) -> Reply:
    payload = {"schemaVersion": 1, "ok": True, "requestId": request_id, **fields}
    return json_reply(200, payload, {"x-hyprial-request-id": request_id, **(headers or {})}, cookies)


def failure_reply(request_id: str, failure: HostFailure) -> Reply:
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "ok": False,
        "requestId": request_id,
        "code": failure.code,
        "message": failure.message,
        "dispatch": failure.dispatch,
    }
    if failure.method is not None:
        payload["method"] = failure.method
    if failure.data is not None:
        payload["data"] = failure.data
    return json_reply(failure.status, payload, {"x-hyprial-request-id": request_id})


def relay_reply(status: int, headers: Mapping[str, str], body: bytes) -> Reply:
    return Reply(status, tuple(headers.items()), body)


async def send_reply(send: Send, reply: Reply, *, include_body: bool = True) -> None:
    """Write one complete response through the ASGI ``send`` callable."""
    pairs = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in reply.headers]
    pairs.extend((b"set-cookie", value.encode("latin-1")) for value in reply.cookies)
    if not any(name == b"content-length" for name, _ in pairs):
        pairs.append((b"content-length", str(len(reply.body)).encode()))
    await send({"type": "http.response.start", "status": reply.status, "headers": pairs})
    await send({"type": "http.response.body", "body": reply.body if include_body else b""})


class Request:
    """The parts of an ASGI HTTP scope the host reads, and nothing else."""

    def __init__(self, scope: Mapping[str, Any], receive: Receive) -> None:
        self._receive = receive
        self.method: str = scope["method"]
        raw = scope.get("raw_path") or scope["path"].encode("utf-8")
        self.path: str = raw.decode("latin-1")
        self.query: str = scope.get("query_string", b"").decode("latin-1")
        self._headers = [
            (name.decode("latin-1").lower(), value.decode("latin-1")) for name, value in scope["headers"]
        ]
        server = scope.get("server")
        self.listener_port: int | None = server[1] if server else None

    def header(self, name: str) -> str | None:
        """The single non-empty value of a header, else ``None``."""
        values = [value for key, value in self._headers if key == name]
        if len(values) != 1 or not values[0]:
            return None
        return values[0]

    def raw_header(self, name: str) -> str | None:
        """Every value of a header joined, preserving an explicit empty value."""
        values = [value for key, value in self._headers if key == name]
        return ", ".join(values) if values else None

    async def body(self, limit: int, method: str | None = None) -> bytes:
        """Read the body, refusing as soon as the declared or real size exceeds ``limit``."""
        declared = self.header("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise _too_large(method)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await self._receive()
            if message["type"] != "http.request":
                raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request body was interrupted", method=method)
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                raise _too_large(method)
            chunks.append(chunk)
            if not message.get("more_body"):
                return b"".join(chunks)

    async def json_object(
        self, limit: int, method: str | None = None
    ) -> tuple[dict[str, Any], bytes]:
        """Require ``application/json`` and a JSON object; return it with its raw bytes."""
        media = self.header("content-type")
        if media is None or media.split(";", 1)[0].strip().lower() != "application/json":
            raise HostFailure(
                415, "HTTP_UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json", method=method
            )
        raw = await self.body(limit, method)
        try:
            value = loads_strict(raw)
        except ValueError:
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request body is not valid JSON", method=method) from None
        if not isinstance(value, dict):
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request body must be a JSON object", method=method)
        return value, raw


def _too_large(method: str | None) -> HostFailure:
    return HostFailure(413, "HTTP_REQUEST_TOO_LARGE", "request body is too large", method=method)
