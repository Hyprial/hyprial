from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AuthorizedChatTarget,
    AudienceSnapshot,
    ChatRef,
    NativeTargetResolution,
    freeze_json_v1,
)


BOT_TOKEN = "xoxb-" + "b" * 20
APP_TOKEN = "xapp-" + "a" * 20


class Credentials:
    async def resolve(self, credential_ref: str):
        from hyprial.daemon.impl.adapters.slack import SlackCredentials

        assert credential_ref == "receiver:slack"
        return SlackCredentials(BOT_TOKEN, APP_TOKEN)


class Ledger:
    def __init__(self) -> None:
        self.value: str | None = None

    async def load(self, instance_id: str, generation: int) -> str | None:
        return self.value

    async def commit(self, instance_id: str, generation: int, token: str) -> None:
        self.value = token


def instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"slack\0slack-cloud\0T1\0BOT1").hexdigest()
    return AdapterInstanceRef(
        "slack-1", "slack", "socket-mode", "main", "org-1", fingerprint, 2
    )


def operation(*, kind: str = "text-send", **overrides: object) -> SimpleNamespace:
    request = {
        "provider": "slack",
        "organization_id": "org-1",
        "instance_id": "slack-1",
        "generation": 2,
        "receiver_ref": "receiver:slack",
        "method": "chat.postMessage" if kind == "text-send" else "files.uploadV2",
        "channel": "D1",
        "text": "hello",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(
        node_id="node-1",
        sealed_request=freeze_json_v1(request),
        kind=kind,
        idempotency_key="idem-slack-1",
    )


def target() -> NativeTargetResolution:
    ref = ChatRef(
        "chat-ref:v1:slack:slack-1:D1:-", instance(), "T1", "D1", "dm", None, None
    )
    audience = AudienceSnapshot("private", (), "partial", None, 1)
    authorized = AuthorizedChatTarget("target-1", ref, audience, "send", None)
    return NativeTargetResolution(authorized, "resolved", "D1", None, None)


@dataclass
class ByteStream:
    body: bytes
    name: str | None = "artifact.bin"
    media_type: str | None = "application/octet-stream"

    def __post_init__(self) -> None:
        self.size = len(self.body)
        self.sha256 = hashlib.sha256(self.body).hexdigest()
        self.offset = 0
        self.closed = False

    async def read(self, max_bytes: int) -> bytes:
        chunk = self.body[self.offset : self.offset + max_bytes]
        self.offset += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self.closed = True


async def read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _version = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return f"{method} {path}", headers, body


async def send_json(
    writer: asyncio.StreamWriter,
    value: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(value, separators=(",", ":")).encode()
    reason = {
        200: "OK",
        400: "Bad Request",
        429: "Too Many Requests",
        503: "Unavailable",
    }[status]
    extra = "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n".encode()
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()
