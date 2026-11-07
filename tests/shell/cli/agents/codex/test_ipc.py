"""cli_codex_interactive: ipc scenarios."""

from __future__ import annotations
from tests.support.runtime._tmp import hyprial_test_tmp_root
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.harnesses import codex

from .support import (
    batch_route,
    ACTOR,
    THREAD_ID,
    _delay_actor_turn_projection,
    _legacy_runtime_context as _legacy_runtime_context,
    _prompt,
)


def test_codex_interactive_carrier_settles_through_public_unix_ipc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "turn-ipc"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "ipc final",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    message = {
        "messageId": "m-ipc",
        "conversationId": "c-ipc",
        "from": "adapter:lark:test",
        "intent": "request",
        "message": "ipc question",
    }
    # Keep AF_UNIX below macOS' path limit while remaining valid on Linux CI.
    root = Path(tempfile.mkdtemp(prefix="hyprial-cx-ipc-", dir=hyprial_test_tmp_root()))
    socket_path = root / "daemon.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()
    listener.settimeout(0.1)
    stopped = threading.Event()
    frames: list[dict[str, object]] = []
    first_attempt = threading.Event()
    settled = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stopped.is_set():
                    return
                raise
            with connection:
                with connection.makefile("rb") as reader:
                    raw = reader.readline()
                frame = json.loads(raw)
                frames.append(frame)
                method = frame["method"]
                if method in {"session.refresh", "session.heartbeat"}:
                    result = {"ok": True}
                elif method == "message.pending.list":
                    result = {"ok": True, "messages": [] if settled.is_set() else [message]}
                elif method == "message.settle_hint":
                    result = {"ok": True, "hint": "ack", "pending": True}
                elif method in {"session.turn.started", "session.turn.ended"}:
                    result = {"ok": True}
                elif method == "message.ack":
                    reply_attempts = sum(
                        frame.get("method") == "message.ack" for frame in frames
                    )
                    if reply_attempts == 1:
                        first_attempt.set()
                        result = {
                            "ok": False,
                            "messageId": "m-ipc",
                            "acknowledged": False,
                        }
                    else:
                        settled.set()
                        result = {
                            "ok": True,
                            "messageId": "m-ipc",
                            "acknowledged": True,
                        }
                else:
                    raise AssertionError(method)
                connection.sendall(
                    json.dumps(
                        {"version": 1, "id": frame["id"], "result": result},
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )

    daemon_thread = threading.Thread(target=serve, name="fake-daemon-ipc")
    daemon_thread.start()

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(socket_path))
            identifier = f"request-{len(frames) + 1}"
            client.sendall(
                json.dumps(
                    {
                        "version": 1,
                        "id": identifier,
                        "method": method,
                        "params": params,
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            with client.makefile("rb") as reader:
                response = json.loads(reader.readline())
            assert response["id"] == identifier
            return response["result"]
        finally:
            client.close()

    server = FakeServer()
    state_path = root / "carrier.sqlite3"
    first = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=root,
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        poll_seconds=0.01,
        state_path=state_path,
        settlement_retry_seconds=5.0,
    )
    _delay_actor_turn_projection(first, monkeypatch)
    second: codex.CodexInteractiveCarrier | None = None
    try:
        first.start()
        assert first_attempt.wait(timeout=2.0)
        first.stop()

        second = codex.CodexInteractiveCarrier(
            server,
            actor=ACTOR,
            session_ref=THREAD_ID,
            cwd=root,
            command=["codex", "--remote"],
            daemon_request=batch_route(daemon_request),
            poll_seconds=0.01,
            state_path=state_path,
        )
        second.start()
        assert settled.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not any(
            frame.get("method") == "message.ack" for frame in frames
        ):
            time.sleep(0.01)
        second.stop()
    finally:
        if second is not None and second._store is not None:
            second.stop()
        if first._store is not None:
            first.stop()
        stopped.set()
        listener.close()
        daemon_thread.join(timeout=2.0)
        shutil.rmtree(root, ignore_errors=True)

    assert not daemon_thread.is_alive()
    assert server.prompts == [_prompt(message, sender="adapter:lark:test")]
    replies = [frame for frame in frames if frame["method"] == "message.ack"]
    assert len(replies) == 2
    assert replies[0]["params"] == replies[1]["params"] == {
        "actor": ACTOR,
        "sessionRef": THREAD_ID,
        "messageId": "m-ipc",
    }
