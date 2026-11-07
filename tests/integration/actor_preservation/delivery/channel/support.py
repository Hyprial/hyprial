from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

import hyprial.daemon.impl.ipc.server as ipc_server
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from tests.support.links.gate import PermissiveLinkGate

from hyprial.daemon.impl.application import DaemonApplication,_LocalPresence
from hyprial.daemon.impl.correlation.correlation import CompletionReceipt, CorrelationEventRouter
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports import DropRouteCommand
from hyprial.daemon.impl.route_registration import (
    RouteRegistrationIo,
)
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    InboxAuthorityUnavailable,
    SubmissionResult,
)

CANONICAL = "agent:op:node-1:alice"


class FakeInbox:
    def __init__(
        self, pending: InboxMessage, notices: tuple[InboxMessage, ...] = ()
    ) -> None:
        self.pending = pending
        self.notices = notices
        self.submitted: list[InboxMessage] = []
        self.acks: list[tuple[str, str]] = []
        self.fetches: list[str] = []

    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        return (self.pending,) if actor == self.pending.recipient else ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def pending_all(self):  # type: ignore[no-untyped-def]
        return (self.pending,)

    def fetch_pending(
        self, actor: str, *, now_ms=None, only=None  # type: ignore[no-untyped-def]
    ):  # type: ignore[no-untyped-def]
        self.fetches.append(actor)
        messages = self.pending_messages(actor)
        if only is None:
            return messages
        return tuple(item for item in messages if item.message_id in only)

    def system_notices(self, actor: str) -> tuple[InboxMessage, ...]:
        return tuple(item for item in self.notices if item.recipient == actor)

    def drain_system_notices(self, actor: str) -> tuple[InboxMessage, ...]:
        notices = self.system_notices(actor)
        self.notices = tuple(item for item in self.notices if item.recipient != actor)
        return notices

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        self.acks.append((actor, message_id))
        return AckResult(message_id, acknowledged=True)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class _RouteRegistration:
    def __init__(self, *, close_failures: int = 0) -> None:
        self.closed = False
        self.close_failures = close_failures
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("injected route close failure")
        self.closed = True


class _RouteTransport:
    def declare_liveliness(self, _key: str) -> _RouteRegistration:
        return _RouteRegistration()

    def declare_queryable(self, _key: str, _handler: object) -> _RouteRegistration:
        return _RouteRegistration()


class _FailOnceCloseRouteTransport(_RouteTransport):
    def __init__(self) -> None:
        self.failed_registration: _RouteRegistration | None = None

    def declare_queryable(self, key: str, _handler: object) -> _RouteRegistration:
        item = _RouteRegistration(
            close_failures=(1 if "alice" in key and self.failed_registration is None else 0)
        )
        if "alice" in key and self.failed_registration is None:
            self.failed_registration = item
        return item


class _RecordingRouteIo(RouteRegistrationIo):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.drop_attempts: list[str] = []
        super().__init__(*args, **kwargs)

    def submit(self, command: object):  # type: ignore[no-untyped-def]
        payload = (
            command.payload if isinstance(command, LifecycleMutationRequest) else command
        )
        if isinstance(payload, DropRouteCommand):
            self.drop_attempts.append(payload.attempt_token)
        return super().submit(command)


class _HeldApplicationRoute:
    def __init__(self, router: CorrelationEventRouter, held_route: str) -> None:
        self.router = router
        self.held_route = held_route
        self.block = False
        self.held = threading.Event()
        self.release = threading.Event()

    def publish(self, event: object) -> CompletionReceipt:
        if self.block and getattr(event, "route_id", None) == self.held_route:
            self.held.set()
            if not self.release.is_set():
                return CompletionReceipt.STALE
        return self.router.publish(event)  # type: ignore[arg-type]


class DurableReplyInbox(FakeInbox):
    """Model one durable reply receipt surviving inbound acknowledgement."""

    def __init__(self, pending: InboxMessage) -> None:
        super().__init__(pending)
        self.visible = True
        self.receipt: SubmissionResult | None = None

    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        if not self.visible:
            return ()
        return super().pending_messages(actor)

    def harness_failure_original(self, message_id: str) -> InboxMessage | None:
        return self.pending if self.pending.message_id == message_id else None

    def reply_submission_result(self, message_id: str) -> SubmissionResult | None:
        if self.receipt is None or self.receipt.message_id != message_id:
            return None
        return self.receipt

    @staticmethod
    def _command_identity(message: InboxMessage) -> tuple[object, ...]:
        return (
            message.conversation_id,
            message.sender,
            message.recipient,
            message.payload,
            message.intent,
            message.lifecycle,
            message.idempotency_key,
        )

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        if not self.submitted:
            self.submitted.append(message)
            self.receipt = SubmissionResult(message.message_id, True, queued=True)
            return self.receipt
        if self._command_identity(message) != self._command_identity(self.submitted[0]):
            raise InboxAuthorityUnavailable(
                "SUBMISSION_RECEIPT_CONFLICT: correlation_id is already "
                "settled for a different inbox command"
            )
        assert self.receipt is not None
        return self.receipt

    def settle(self, *, accepted: bool, code: str | None = None) -> None:
        assert self.receipt is not None
        self.receipt = SubmissionResult(
            self.receipt.message_id,
            accepted,
            queued=False,
            code=code,
        )
        self.visible = False
        if accepted:
            self.acks.append((self.pending.recipient, self.pending.message_id))


def pending_message() -> InboxMessage:
    return InboxMessage(
        message_id="message-1",
        conversation_id="conversation-1",
        sender="agent:bob",
        recipient=CANONICAL,
        payload=json.dumps({"message": "durable body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def system_notice() -> InboxMessage:
    return InboxMessage(
        message_id="notice-1",
        conversation_id="conversation-1",
        sender="system:hyprial:node-1",
        recipient=CANONICAL,
        payload=json.dumps(
            {
                "message": "System notice: delivery failed for message original-1; "
                "reason=TARGET_NOT_FOUND.",
                "originalMessageId": "original-1",
                "reason": "TARGET_NOT_FOUND",
                "systemNotice": True,
            }
        ).encode(),
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=2,
    )


class _StubPresence:
    """Dumb presence double: every actor is unconditionally online.

    Most tests in this module only care that `ps`/`targets` expose whatever
    is registered, not about heartbeat-expiry semantics -- that real
    liveness-gating behavior (dead connector -> offline) is covered
    separately below against the real `_LocalPresence`.
    """

    def actor_online(self, actor: str) -> bool:
        return True

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def application(tmp_path: Path, inbox: FakeInbox) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox
    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    # Explicit premise (wp2.md §4.9 option a): these reply-settlement
    # scenarios are not about links, so a permissive gate keeps agent replies
    # delivering instead of holding on the unbound slot (ruling 54).
    app.link_admission.bind(PermissiveLinkGate())
    _TEST_APPLICATIONS.append(app)
    return app


ROUTE_PROGRESS_SECONDS = 2.0


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_application_domains():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        # Protocol stubs own no resources; the real daemon authorities do.
        if not callable(getattr(app._harnesses, "stop", None)):
            app._harnesses = None
        if not callable(getattr(app._runtime, "stop", None)):
            app._runtime = None
        if isinstance(app._inbox, FakeInbox):
            app._inbox = None
        if not callable(getattr(app._transport, "close", None)):
            app._transport = None
        try:
            app._close()
        finally:
            app._close_home_lease()


def register(app: DaemonApplication, session_ref: str = "session-1") -> None:
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": session_ref,
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


#: Budget for waiting on a state these tests EXPECT to arrive.  ⛔ Not a
#: latency claim: every wait below returns the instant the state lands, so a
#: green run does not get slower when this number grows.  The literals it
#: replaces (2.0 / 1.0) were independent numbers SMALLER than the daemon's own
#: patience, so a loaded box could make the TEST give up while the daemon was
#: still serving normally -- CI tasks 16029 / 16121 / 16150.  One ruler with
#: the product: a local IPC client may go quiet for ``_IPC_CLIENT_IDLE_TIMEOUT``
#: before the daemon stops waiting on it, so neither may a test.
IPC_WAIT_SECONDS = ipc_server._IPC_CLIENT_IDLE_TIMEOUT


def _ipc_session_request(
    app: DaemonApplication, request_id: str, method: str, session_ref: str
) -> tuple[threading.Thread, socket.socket]:
    server, client = socket.socketpair()
    params: dict[str, object] = {
        "actor": "alice",
        "sessionRef": session_ref,
    }
    if method == "session.register":
        params.update(
            {
                "cwd": "/work/alice",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
            }
        )
    client.sendall(
        json.dumps(
            {"version": 1, "id": request_id, "method": method, "params": params}
        ).encode()
        + b"\n"
    )
    worker = threading.Thread(target=app._serve_client, args=(server,), daemon=True)
    worker.start()
    return worker, client


def _finish_ipc_request(
    worker: threading.Thread, client: socket.socket
) -> dict[str, object]:
    try:
        client.settimeout(IPC_WAIT_SECONDS)
        response = json.loads(client.makefile("rb").readline())
        worker.join(timeout=IPC_WAIT_SECONDS)
        assert not worker.is_alive()
        return response
    finally:
        client.close()


# --- Connector liveness: a dead MCP connector must stop reporting online ---
#
# _ensure_interactive_route declares an interactive session's zenoh
# liveliness token on the DAEMON's own transport session, not the
# connector's (see its docstring in application.py). That token therefore
# survives for as long as the daemon runs, regardless of whether the
# connector process (Claude Code's stdio MCP child) is still alive -- `ps`
# and `targets` used to trust that raw token alone and so reported a dead
# connector as `channelConfirmed=true` / `status=online` forever. These
# tests wire the real `_LocalPresence` (the tests above all inject a dumb
# always-online `_StubPresence`, which cannot exercise this) against a fake
# liveliness directory that reproduces exactly that "daemon-owned token never
# expires on its own" defect, and prove a real per-contact heartbeat closes
# the gap.


class _FakeLivelinessDirectory:
    """A raw zenoh liveliness view that never expires tokens on its own.

    This mirrors production `_ensure_interactive_route`: once `add()` is
    called (standing in for `transport.declare_liveliness`, which the
    application-level tests never exercise -- see `_ensure_interactive_route`'s
    "Unit-level application.handle seams do not start transport" guard), the
    actor stays "online" forever from the raw token's point of view. Only
    `_LocalPresence.interactive_liveness` can tell a dead connector apart
    from a live one on top of this.
    """

    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def add(self, actor: str) -> None:
        self._tokens.add(actor)

    def actor_online(self, actor: str) -> bool:
        return actor in self._tokens

    def online_actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._tokens))

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _wire_real_presence(app: DaemonApplication) -> _FakeLivelinessDirectory:
    """Swap the dumb `_StubPresence` for the real heartbeat-gating logic."""

    directory = _FakeLivelinessDirectory()
    app._presence = _LocalPresence(  # type: ignore[assignment]
        directory, app.node_id, interactive_liveness=app._interactive_actor_liveness
    )
    return directory


def _register_channel_session(
    app: DaemonApplication, directory: _FakeLivelinessDirectory, actor: str = "alice"
) -> None:
    app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
        },
    )
    # Stand-in for the real daemon declaring the raw token via
    # _ensure_interactive_route -> transport.declare_liveliness.
    directory.add(app._canonical_interactive_actor(actor))
