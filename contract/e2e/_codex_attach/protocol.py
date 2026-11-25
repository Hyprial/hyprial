"""Local Unix-WebSocket client and thread projection helpers for the gate."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import time
from pathlib import Path
from typing import Any


class ProbeFailure(RuntimeError):
    """A measured product or protocol assertion failed."""


def remaining(deadline: float | None, stage: str, cap: float = 30.0) -> float:
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise ProbeFailure(f"GLOBAL_TIMEOUT stage={stage}")
    return min(cap, left)


class UnixWebSocket:
    """Tiny RFC 6455 client for Codex's local Unix WebSocket endpoint."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.default_timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self.sock.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ProbeFailure(f"WebSocket handshake failed: {response[:200]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expected not in response:
            raise ProbeFailure("WebSocket handshake accept mismatch")

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ProbeFailure("WebSocket closed during handshake")
            data.extend(chunk)
        return bytes(data)

    def send_json(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
        self.sock.sendall(header + mask + masked)

    def recv_json(self, timeout: float | None = None) -> dict[str, Any]:
        self.sock.settimeout(self.default_timeout if timeout is None else timeout)
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._recv_exact(4) if masked else b""
            payload = bytearray(self._recv_exact(length))
            if masked:
                for index in range(length):
                    payload[index] ^= mask[index % 4]
            if opcode == 0x9:
                self._send_control(0xA, bytes(payload))
                continue
            if opcode == 0x8:
                raise ProbeFailure("WebSocket closed while awaiting JSON")
            if opcode != 0x1:
                continue
            value = json.loads(payload.decode())
            if not isinstance(value, dict):
                raise ProbeFailure("app-server WebSocket message was not an object")
            return value

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(masked)]) + mask + masked)

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ProbeFailure("WebSocket closed unexpectedly")
            data.extend(chunk)
        return bytes(data)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        request_id: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.send_json({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + (
            self.default_timeout if timeout is None else timeout
        )
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProbeFailure(f"RPC_TIMEOUT method={method} id={request_id}")
            message = self.recv_json(timeout=left)
            if message.get("id") == request_id:
                return message

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except OSError:
            pass
        self.sock.close()


def response_result(response: dict[str, Any], method: str) -> dict[str, Any]:
    if "error" in response:
        raise ProbeFailure(f"{method} RPC error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ProbeFailure(f"{method} returned no object result: {response}")
    return result


def thread_candidates(
    client: UnixWebSocket,
    cwd: str,
    global_deadline: float | None = None,
) -> tuple[list[dict[str, Any]], str]:
    rpc_timeout = remaining(global_deadline, "thread-discovery")
    loaded = response_result(
        client.request("thread/loaded/list", {}, 2, timeout=rpc_timeout),
        "thread/loaded/list",
    )
    loaded_ids = set(loaded.get("data", []))
    listed = response_result(
        client.request(
            "thread/list",
            {
                "cwd": cwd,
                "limit": 50,
                "sourceKinds": ["cli", "vscode", "appServer"],
            },
            3,
            timeout=remaining(global_deadline, "thread-discovery"),
        ),
        "thread/list",
    )
    data = listed.get("data")
    if not isinstance(data, list):
        raise ProbeFailure(f"thread/list returned invalid data: {listed}")
    listed_candidates = [item for item in data if isinstance(item, dict) and item.get("cwd") == cwd]
    candidates = [
        item for item in listed_candidates
        if item.get("canAcceptDirectInput") is True
        and (not loaded_ids or item.get("id") in loaded_ids)
    ]
    loaded_summaries: list[dict[str, Any]] = []
    if not candidates and loaded_ids:
        loaded_candidates: list[dict[str, Any]] = []
        for index, thread_id in enumerate(sorted(str(item) for item in loaded_ids), 10):
            read = response_result(
                client.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": False},
                    index, timeout=remaining(global_deadline, "thread-discovery"),
                ),
                "thread/read",
            )
            thread = read.get("thread")
            if isinstance(thread, dict):
                loaded_summaries.append({
                    "id": thread.get("id"), "cwd": thread.get("cwd"),
                    "source": thread.get("source"), "status": thread.get("status"),
                    "canAcceptDirectInput": thread.get("canAcceptDirectInput"),
                })
            if (isinstance(thread, dict) and thread.get("cwd") == cwd
                    and thread.get("canAcceptDirectInput") is True):
                loaded_candidates.append(thread)
        if loaded_candidates:
            return loaded_candidates, "loaded/list + thread/read fallback"
    if not candidates:
        raise ProbeFailure(
            "thread discovery returned no candidate: "
            + json.dumps({
                "loadedIds": sorted(str(item) for item in loaded_ids),
                "listedCount": len(data),
                "cwdCandidates": [
                    {"id": item.get("id"), "source": item.get("source"),
                     "status": item.get("status"),
                     "canAcceptDirectInput": item.get("canAcceptDirectInput"),
                     "cwd": item.get("cwd")} for item in listed_candidates
                ],
                "loadedRead": loaded_summaries,
            }, separators=(",", ":"))
        )
    return candidates, "thread/list"


def thread_read(
    client: UnixWebSocket, thread_id: str, request_id: int,
    timeout: float | None = None,
) -> dict[str, Any]:
    return response_result(
        client.request("thread/read", {"threadId": thread_id, "includeTurns": True},
                       request_id, timeout=timeout),
        "thread/read",
    )


def thread_text(thread: dict[str, Any]) -> str:
    parts: list[str] = []
    nested = thread.get("thread") if isinstance(thread.get("thread"), dict) else thread
    turns = nested.get("turns", []) if isinstance(nested, dict) else []
    for turn in turns if isinstance(turns, list) else []:
        for item in turn.get("items", []) if isinstance(turn, dict) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "userMessage":
                content = item.get("content", [])
                for entry in content if isinstance(content, list) else []:
                    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                        parts.append(entry["text"])
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)
