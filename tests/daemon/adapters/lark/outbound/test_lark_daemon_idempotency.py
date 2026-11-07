"""Cross-layer Lark replay idempotency against the real daemon IPC seam."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _Harness
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class _LoopbackTransport:
    def __init__(self) -> None:
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()


class _DroppingDaemonSocket:
    """Real AF_UNIX framing; process the first request but discard its reply."""

    def __init__(self, path: Path, app: DaemonApplication) -> None:
        self.path = path
        self.app = app
        self.requests: list[dict[str, Any]] = []
        self._drop_next = True
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._server.bind(str(self.path))
        self._server.listen(8)
        self._server.settimeout(0.05)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()
        self.path.unlink(missing_ok=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._server.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                if b"\n" not in buffer:
                    continue
                line = bytes(buffer.partition(b"\n")[0])
                self.requests.append(json.loads(line))
                response = self.app._response(line)
                if self._drop_next:
                    self._drop_next = False
                    continue
                connection.sendall(json.dumps(response).encode() + b"\n")


class _Lark:
    def __init__(self, message: LarkInboundMessage) -> None:
        self.message = message

    def list_chat_messages(self, chat_id: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        assert chat_id == self.message.chat_id
        return (self.message,)

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        pass

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        pass

    def reply(
        self, message_id: str, text: str, *, idempotency_key: str
    ) -> str:
        del idempotency_key
        return "om_reply"

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        return "om_owner"

    def get_message(self, message_id: str):  # type: ignore[no-untyped-def]
        return None

    def get_inbound_message(self, message_id: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        return self.message


class _Routes:
    # The actor id must be a canonical four-segment URI: bare actor names
    # are node-shaped and the send-time admission gate rejects them.
    target = ActorTarget(
        "agent:test-owner:lark-idempotency-node:target-agent",
        "target-agent",
        "target-agent",
    )

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return self.target

    def resolve_mention(self, name: str) -> ActorTarget | None:
        return None

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        return self.target

    def list_runtime_targets(self):  # type: ignore[no-untyped-def]
        return ()

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]:
        return (self.target,) if token == "target-agent" else ()


def test_lark_reconcile_after_lost_daemon_response_creates_one_durable_message(
    tmp_path: Path,
) -> None:
    transport = _LoopbackTransport()
    inbox = InboxService(tmp_path / "inbox.sqlite3", transport)
    transport.service = inbox
    # AF_UNIX sun_path is 104 bytes on macOS and pytest's default tmp_path
    # (under /var/folders/...) already exceeds it for this test's long node
    # name, so the real daemon socket lives in a short private root -- the
    # same workaround the E2E-001 local runner documents.
    socket_dir = Path(tempfile.mkdtemp(prefix="hyprial-idem-"))
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=socket_dir / "d.sock",
        node_id="lark-idempotency-node",
        owner="test-owner",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox
    app._presence = _Presence()
    app._harnesses = object()
    app._adapters = None
    app.handle("agent.create", {"name": "target-agent"})
    server = _DroppingDaemonSocket(app.socket_path, app)
    server.start()
    message = LarkInboundMessage(
        event_id="evt-live",
        message_id="om_native_stable",
        chat_id="oc_dm",
        chat_type="p2p",
        text="/hyprial ask target-agent accepted before the response vanished",
        sender_id="ou_sender",
        sender_type="user",
        create_time="1720000002000",
        message_type="text",
    )
    state_path = tmp_path / "lark-state.sqlite3"
    try:
        first = LarkAdapter(
            state=LarkStateStore(state_path),
            lark=_Lark(message),
            harness=_Harness(app.socket_path),
            routes=_Routes(),
            channel_actor_id="channel:lark:test",
        )
        outcome = first.handle_inbound(message)
        assert outcome.status == "error"

        recovered = LarkAdapter(
            state=LarkStateStore(state_path),
            lark=_Lark(message),
            harness=_Harness(app.socket_path),
            routes=_Routes(),
            channel_actor_id="channel:lark:test",
        )
        report = recovered.reconcile_recent(
            lookback_seconds=900, now_ms=1_720_000_005_000
        )

        durable = inbox.pending_messages(
            "agent:test-owner:lark-idempotency-node:target-agent"
        )
        assert report.forwarded == 1
        assert len(durable) == 1
        assert len(server.requests) == 2
        assert {
            request["params"]["idempotencyKey"] for request in server.requests
        } == {"lark-inbound:channel:lark:test:om_native_stable"}
        assert recovered.dead_letters() == ()
    finally:
        server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)
