"""Server-request policy on the Codex app-server connection.

Decision record: the rel0553 regression showed the app-server delivering its
consent surfaces (``mcpServer/elicitation/request``) to the daemon connection
once the carrier joins the TUI thread with ``thread/resume``; the PR1 ``-32601``
refusal then denies the tool call in the user's TUI without the user ever
answering. These tests pin the contract: consent requests are left for the TUI,
every other server request keeps the refusal, and ordinary RPC responses keep
flowing past a deferred consent request.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import websockets

from hyprial.daemon.impl.harnesses.codex.transport import open_app_server_connection

_DEFER_WINDOW_SECONDS = 1.5


class _RecordingAppServer:
    """Minimal RFC 6455 unix app-server: records answers, echoes one probe."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = Path(socket_path)
        self.incoming: list[dict[str, object]] = []
        self._clients: set[object] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: object | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._server is not None and self.socket_path.exists():
                return
            time.sleep(0.01)
        raise TimeoutError("recording app-server did not start")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
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
            self._clients.add(socket)
        try:
            async for raw in socket:  # type: ignore[attr-defined]
                message = json.loads(raw)
                with self._lock:
                    self.incoming.append(message)
                if message.get("method") == "transport-probe":
                    await socket.send(
                        json.dumps({"id": message.get("id"), "result": {"pong": True}})
                    )
        finally:
            with self._lock:
                self._clients.discard(socket)

    def send(self, payload: dict[str, object]) -> None:
        loop = self._loop
        assert loop is not None

        async def _deliver() -> None:
            with self._lock:
                clients = list(self._clients)
            for socket in clients:
                await socket.send(json.dumps(payload))

        future = asyncio.run_coroutine_threadsafe(_deliver(), loop)
        future.result(timeout=10.0)

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self.incoming)

    def wait_for_incoming(self, *, timeout: float = 5.0) -> dict[str, object] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.snapshot()
            if rows:
                return rows[0]
            time.sleep(0.01)
        return None


def _elicitation(message_id: int) -> dict[str, object]:
    return {
        "method": "mcpServer/elicitation/request",
        "id": message_id,
        "params": {
            "threadId": "thread-t",
            "turnId": "turn-t",
            "message": "Allow the harness-bridge MCP server to run tool?",
            "_meta": {"codex_approval_kind": "mcp_tool_call"},
        },
    }


def test_consent_request_is_left_for_the_tui_client(socket_root: Path) -> None:
    server = _RecordingAppServer(socket_root / "app.sock")
    server.start()
    connection = open_app_server_connection(server.socket_path, timeout=5.0)
    try:
        server.send(_elicitation(0))
        time.sleep(_DEFER_WINDOW_SECONDS)
        assert server.snapshot() == []
    finally:
        connection.close()
        server.stop()


def test_other_server_requests_keep_the_refusal(socket_root: Path) -> None:
    server = _RecordingAppServer(socket_root / "app.sock")
    server.start()
    connection: object | None = None
    try:
        connection = open_app_server_connection(server.socket_path, timeout=5.0)
        server.send({"method": "unhandled/serverProbe", "id": 7, "params": {}})
        refusal = server.wait_for_incoming()
        assert refusal is not None
        assert refusal.get("id") == 7
        error = refusal.get("error")
        assert isinstance(error, dict) and error.get("code") == -32601
    finally:
        if connection is not None:
            connection.close()
        server.stop()


def test_rpc_responses_flow_past_a_deferred_consent(socket_root: Path) -> None:
    server = _RecordingAppServer(socket_root / "app.sock")
    server.start()
    connection: object | None = None
    try:
        connection = open_app_server_connection(server.socket_path, timeout=5.0)
        server.send(_elicitation(0))
        response = connection.request("transport-probe", {}, 1)
        assert response.get("result") == {"pong": True}
    finally:
        if connection is not None:
            connection.close()
        server.stop()
