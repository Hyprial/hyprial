from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.ipc import server as ipc_server
from hyprial.daemon.impl.application import (
    DaemonApplication,
    _LocalPresence,
)
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    InboxAuthorityUnavailable,
    InboxService,
    MemoryDeliveryTransport,
    SubmissionResult,
)


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

from hyprial.daemon.impl.links.ports import LinkGate
from tests.support.links.gate import PermissiveLinkGate

CANONICAL = "agent:op:node-1:alice"


class FakeInbox:
    def shutdown(self) -> None:
        return None

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
        # Non-destructive by contract: list/wait are doorbells, and only the
        # per-id ack (or the batch read) may consume a notice.
        return tuple(item for item in self.notices if item.recipient == actor)

    def dismiss_system_notice(self, message_id: str, *, recipient=None) -> bool:
        matching = [
            item
            for item in self.notices
            if item.message_id == message_id
            and (recipient is None or item.recipient == recipient)
        ]
        if not matching:
            return False
        self.notices = tuple(
            item for item in self.notices if item.message_id != message_id
        )
        return True

    def drain_system_notices(self, actor: str) -> tuple[InboxMessage, ...]:
        notices = tuple(item for item in self.notices if item.recipient == actor)
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
        # The daemon authority keys a submission receipt by a digest that
        # includes the summary (WP3): a retry with a different summary is a
        # different command and must conflict, exactly like a different body.
        return (
            message.conversation_id,
            message.sender,
            message.recipient,
            message.payload,
            message.intent,
            message.lifecycle,
            message.idempotency_key,
            message.summary,
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


class LoopbackTransport(MemoryDeliveryTransport):
    """A real inbox on this node receives every message that is delivered."""

    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).acknowledged


def durable_application(
    tmp_path: Path, *, actors: tuple[str, ...] = ("driver", "reader")
) -> DaemonApplication:
    """A DaemonApplication wired to a real SQLite InboxService.

    The pending/read paths under test stamp rows, retire sender receipts and
    publish receipts, so a state dummy cannot stand in for the inbox.  Every
    home, socket and database stays under ``tmp_path``.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    transport = LoopbackTransport()
    # One admission slot for the whole daemon, exactly like composition: the
    # inbox and the application share the object tests bind a gate into.
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", transport, link_admission=app.link_admission
    )
    transport.service = inbox
    app._inbox = inbox  # type: ignore[assignment]
    app._harnesses = Providers()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    _TEST_APPLICATIONS.append(app)
    _TEST_INBOXES.append(inbox)
    for name in actors:
        if app.agents.get(name) is None:
            app.handle("agent.create", {"name": name})
    return app


def register_actor(
    app: DaemonApplication,
    name: str,
    session_ref: str = "session-1",
    *,
    cwd: str | None = None,
) -> None:
    app.handle(
        "session.register",
        {
            "actor": name,
            "sessionRef": session_ref,
            "cwd": cwd or f"/work/{name}",
            "command": ["test"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


class FakeLinkGate:
    """A LinkGate double with observable admission and settle answers."""

    def __init__(self, *, admit: bool = True, hint: str = "keep") -> None:
        self.admit = admit
        self.hint = hint
        self.admitted: list[str] = []
        self.hints: list[str] = []
        #: Every admission call's full argument set (intent/reply_to matter:
        #: the read path must forward them, never guess reply-ness itself).
        self.calls: list[dict[str, object]] = []
        #: One entry per distinct message id: the gate's W reservation is
        #: idempotent by (recipient, message_id), so a repeated read must not
        #: consume a second slot.
        self.reserved: list[str] = []

    def admit_send(self, **kwargs: object):  # type: ignore[no-untyped-def]
        from hyprial.daemon.impl.links.ports import Allow

        return Allow()

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        return True

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        self.admitted.append(message_id)
        self.calls.append(
            {
                "recipient": recipient,
                "sender": sender,
                "message_id": message_id,
                "intent": intent,
                "reply_to": reply_to,
            }
        )
        if message_id not in self.reserved:
            self.reserved.append(message_id)
        return self.admit

    def settle_hint(self, *, actor: str, message_id: str):  # type: ignore[no-untyped-def]
        self.hints.append(message_id)
        return self.hint


class Providers:
    drain_complete = True

    def stop(self) -> None:
        return None

    def status(self) -> tuple[()]:
        return ()


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




def application(
    tmp_path: Path, inbox: FakeInbox, *, gate: LinkGate | None = None
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    app._inbox = inbox
    app._harnesses = Providers()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    # Explicit premise (wp2.md §4.9 option a): these channel scenarios are not
    # about links, so a permissive gate keeps agent replies delivering instead
    # of holding on the unbound slot (ruling 54).
    # A test that needs its own gate passes it here: the slot takes one bind
    # only (ruling 67), so the fixture must not pre-bind a second one.
    app.link_admission.bind(gate if gate is not None else PermissiveLinkGate())
    _TEST_APPLICATIONS.append(app)
    return app


_TEST_APPLICATIONS: list[DaemonApplication] = []
_TEST_INBOXES: list[InboxService] = []


@pytest.fixture(autouse=True)
def _close_application_domains():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        app._close()
        app._close_home_lease()
    while _TEST_INBOXES:
        inbox = _TEST_INBOXES.pop()
        try:
            inbox.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the test
            pass


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
ROUTE_PROGRESS_SECONDS = 2.0


class _RouteLockProbe:
    """Wrap the interactive-route lock and report who BLOCKS on it.

    These tests prove a mutual exclusion: while one request owns the route
    lock, a second request must not reach state mutation.  The old form
    asserted that absence after ``time.sleep(0.05)`` -- a guess at how long
    the second request needs to get there, squeezed between two opposing
    requirements.  It had to be large enough that a BROKEN mutex would have
    had time to show (otherwise the assertion passes vacuously: a false
    green), and small enough that the whole main-thread segment still fit
    inside the paused handler's release budget (otherwise a false red).

    This reports the event the sleep stood in for: a thread found the lock
    already held.  Waiting for that is exact -- it ends when the second
    request is genuinely queued behind the first and never earlier -- and it
    keeps the detection power in the same place, because a mutex that no
    longer excludes produces no blocked thread at all.
    """

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self._blocked: set[threading.Thread] = set()
        self._changed = threading.Condition()

    def acquire(self, blocking: bool = True, timeout: float = -1):  # type: ignore[no-untyped-def]
        # Re-entrant acquisition by the owner succeeds here, so the probe
        # never reports the holder's own nested ``with`` as contention.
        if self._inner.acquire(blocking=False):
            return True
        with self._changed:
            self._blocked.add(threading.current_thread())
            self._changed.notify_all()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()

    def __enter__(self):  # type: ignore[no-untyped-def]
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:  # type: ignore[no-untyped-def]
        self.release()

    def wait_until_blocked(self, thread: threading.Thread, timeout: float) -> bool:
        """True once ``thread`` is parked on this lock."""

        with self._changed:
            return self._changed.wait_for(lambda: thread in self._blocked, timeout)


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














# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'CANONICAL',
    'DurableReplyInbox',
    'FakeInbox',
    'FakeLinkGate',
    'IPC_WAIT_SECONDS',
    'LoopbackTransport',
    'Providers',
    'ROUTE_PROGRESS_SECONDS',
    '_FakeLivelinessDirectory',
    '_RouteLockProbe',
    '_StubPresence',
    '_TEST_APPLICATIONS',
    '_close_application_domains',
    '_finish_ipc_request',
    '_ipc_session_request',
    '_register_channel_session',
    '_wire_real_presence',
    'application',
    'durable_application',
    'pending_message',
    'register',
    'register_actor',
    'system_notice',
]
