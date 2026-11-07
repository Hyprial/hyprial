"""Fake Codex app-server over a Unix socket, plus a carrier-level daemon stub.

The fake speaks the real transport (RFC 6455 over ``unix://``) so tests exercise
the production connection, request and notification paths. It is a protocol
fixture, never a model: it answers app-server RPC and sends notifications when a
test asks it to.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import websockets

from hyprial.daemon.impl.harnesses.codex.transport import open_app_server_connection

THREAD_ID = "thread-whole-terminal"
ACTOR = "agent:op:node-1:codex-whole-terminal"


class FakeAppServer:
    """One in-process app-server; tests drive notifications explicitly."""

    def __init__(self, socket_path: Path, *, thread_id: str = THREAD_ID) -> None:
        self.socket_path = Path(socket_path)
        self.thread_id = thread_id
        self.open_turn: str | None = None
        self.turns: list[dict[str, object]] = []
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.clients: set[object] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: object | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        #: Emit a native turn/started before answering turn/start, so a client
        #: that starts the turn is raced by its own notification.
        self.emit_native_events = False
        #: optional hook: called on the server thread for turn/start requests.
        self.on_turn_start: Callable[[str], None] | None = None

    def start(self) -> Path:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._server is not None:
                return self.socket_path
            time.sleep(0.01)
        raise TimeoutError("fake app-server did not start")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=10.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def serve() -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            self.socket_path.unlink(missing_ok=True)
            self._server = await websockets.serve(
                self._handle, unix=True, path=str(self.socket_path)
            )

        loop.run_until_complete(serve())
        loop.run_forever()
        server = self._server
        self._server = None
        try:
            if server is not None:
                server.close(close_connections=True)
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    async def _handle(self, socket: object) -> None:
        with self._lock:
            self.clients.add(socket)
        try:
            async for raw in socket:  # type: ignore[attr-defined]
                message = json.loads(raw)
                await self._dispatch(socket, message)
        finally:
            with self._lock:
                self.clients.discard(socket)

    async def _dispatch(self, socket: object, message: dict[str, object]) -> None:
        method = message.get("method")
        identifier = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        with self._lock:
            if isinstance(method, str):
                self.requests.append((method, params))
        if identifier is None:
            return
        result = self._result(method, params)
        if method == "turn/start" and self.emit_native_events:
            turn = result["turn"]
            await self._broadcast(json.dumps({
                "method": "turn/started",
                "params": {"threadId": self.thread_id, "turn": turn},
            }))
        await socket.send(json.dumps({"id": identifier, "result": result}))

    def thread_payload(self) -> dict[str, object]:
        return {
            "id": self.thread_id,
            "status": (
                {"type": "active", "activeFlags": []}
                if self.open_turn is not None
                else {"type": "idle"}
            ),
            "turns": list(self.turns),
        }

    def _result(self, method: object, params: dict[str, object]) -> object:
        if method == "initialize":
            return {"userAgent": "fake-codex-app-server", "codexHome": "/isolated"}
        if method == "thread/start":
            return {"thread": self.thread_payload()}
        if method == "thread/resume":
            return {"thread": self.thread_payload()}
        if method == "thread/read":
            return {"thread": self.thread_payload()}
        if method == "turn/start":
            turn_id = f"turn-native-{len(self.turns) + 1}"
            self.open_turn = turn_id
            self.turns.append({"id": turn_id, "status": "inProgress", "items": []})
            hook = self.on_turn_start
            if hook is not None:
                hook(turn_id)
            return {"turn": {"id": turn_id, "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(f"fake app-server does not implement {method!r}")

    def broadcast(self, method: str, params: dict[str, object]) -> None:
        assert self._loop is not None
        payload = json.dumps({"method": method, "params": params})
        future = asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)
        future.result(timeout=10.0)

    async def _broadcast(self, payload: str) -> None:
        with self._lock:
            clients = list(self.clients)
        for client in clients:
            await client.send(payload)

    def begin_tui_turn(self, turn_id: str) -> None:
        self.open_turn = turn_id
        self.turns.append({"id": turn_id, "status": "inProgress", "items": []})
        self.broadcast("thread/status/changed", {
            "threadId": self.thread_id,
            "status": {"type": "active", "activeFlags": []},
        })
        self.broadcast("turn/started", {
            "threadId": self.thread_id,
            "turn": {"id": turn_id, "status": "inProgress", "items": []},
        })

    def end_turn(self, turn_id: str, status: str = "completed") -> None:
        self.open_turn = None
        for turn in self.turns:
            if turn["id"] == turn_id:
                turn["status"] = status
                if status == "completed":
                    turn["items"] = [
                        {
                            "id": f"{turn_id}-final",
                            "type": "agentMessage",
                            "text": "probe final",
                            "phase": "final_answer",
                        }
                    ]
        self.broadcast("turn/completed", {
            "threadId": self.thread_id,
            "turn": {"id": turn_id, "status": status, "items": []},
        })
        self.broadcast("thread/status/changed", {
            "threadId": self.thread_id,
            "status": {"type": "idle"},
        })

    def methods(self) -> list[str]:
        with self._lock:
            return [method for method, _ in self.requests]

    def wait_for_request(self, method: str, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if method in self.methods():
                return True
            time.sleep(0.01)
        return False


class RecordingDaemon:
    """Minimal daemon_request stub recording interactive carrier traffic."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._pending: list[dict[str, object]] = []
        self._acknowledged: set[str] = set()
        self._lock = threading.Lock()

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        with self._lock:
            self.calls.append((method, dict(params)))
        if method == "session.refresh":
            return {"ok": True, "sessionRef": params.get("sessionRef")}
        if method == "session.heartbeat":
            return {"ok": True}
        if method == "session.turn.started":
            return {"ok": True, "startedAtMs": 1}
        if method == "session.turn.ended":
            return {"ok": True, "turnCount": 1}
        if method == "message.pending.list":
            return {"ok": True, "messages": self.pending_rows()}
        if method == "message.pending.batch":
            rows = self.pending_rows()
            text = "\n".join(str(row["messageId"]) for row in rows) or "empty"
            return {
                "ok": True,
                "schemaVersion": 1,
                "format": "inbox-batch-v1",
                "cursor": "fake-codex-cursor",
                "daemonEpoch": "fake-epoch",
                "actor": ACTOR,
                "itemCount": len(rows),
                "totalBodyBytes": 0,
                "inlineLimitBytes": 16384,
                "items": rows,
                "delivery": {
                    "mode": "inline",
                    "text": text,
                    "spillPath": None,
                    "spillBytes": None,
                    "spillSha256": None,
                },
            }
        if method == "message.pending.wait":
            hold_ms = params.get("holdMs")
            deadline = time.monotonic() + min(
                0.2, (float(hold_ms) / 1000.0) if isinstance(hold_ms, (int, float)) else 0.2
            )
            while time.monotonic() < deadline and not self.pending_rows():
                time.sleep(0.01)
            rows = self.pending_rows()
            return {"ok": True, "messages": rows, "held": True, "changed": bool(rows)}
        if method == "message.settle_hint":
            return {"ok": True, "hint": "ack", "pending": False}
        if method == "message.ack":
            with self._lock:
                self._acknowledged.add(str(params.get("messageId")))
            return {"ok": True, "acknowledged": True}
        raise AssertionError(f"unexpected daemon method {method!r}")

    def add_pending(self, message: dict[str, object]) -> None:
        with self._lock:
            self._pending.append(dict(message))

    def pending_rows(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                dict(row)
                for row in self._pending
                if str(row["messageId"]) not in self._acknowledged
            ]

    def of(self, method: str) -> list[dict[str, object]]:
        with self._lock:
            return [params for name, params in self.calls if name == method]

    def wait_for(self, predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


def carrier_app_server(socket_path: Path, *, cwd: Path):
    """A real app-server object that also reports the PID a carrier expects."""

    from hyprial.daemon.impl.harnesses.codex import CodexInteractiveAppServer

    class _CarrierServer(CodexInteractiveAppServer):
        pid = 4242

    server = _CarrierServer(socket_path, cwd=cwd)
    server._socket = open_app_server_connection(socket_path, timeout=5.0)
    server._discovered_thread_id = THREAD_ID
    return server

