"""PR #332 F3: Lark inbound during the daemon restore window.

While the daemon's restore gate is closed (`_restore_done` unset) every
heavy IPC method -- including the adapter's ``message.send`` forward -- is
refused at dispatch with ``DAEMON_RESTORING`` (application.py ``handle``).
Websocket events are delivered exactly once, so before F3 an inbound that
landed in that window was permanently dead-lettered on the FIRST refusal:
no retry, and the sender never learned the message was not delivered.

F3 contract pinned here:

- a ``DAEMON_RESTORING`` refusal (and a daemon-restart transport blip) is
  retried in place, with backoff, inside a bounded budget -- the gate
  opens and the message is delivered, with no dead letter;
- a permanent error is dead-lettered immediately AND the sender gets a
  best-effort native "not delivered" reply (the dead-letter store keeps
  the body for replay; the reply keeps the sender informed -- both);
- a transient error that outlives the retry budget falls to the
  dead-letter trail with a distinct reason, and the sender is still
  notified.

The harness doubles raise ``DaemonRequestError`` -- the real production
error type the worker's ``_ipc`` raises for a daemon error envelope -- so
the stubs share the wire shape instead of inventing one.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import socket
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    HarnessReceipt,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _ipc
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRestoringError

RESTORING_MESSAGE = (
    "daemon is restoring its connectors; only the light methods "
    "(ping, shutdown) are served until restore completes"
)


def _actor() -> ActorTarget:
    return ActorTarget(
        actor_id="agent:alice:codex:main",
        actor_key="alice/codex/main",
        display_name="alice/codex/main",
    )


class FakeLark:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str, str]] = []

    def reply(self, message_id: str, text: str, *, idempotency_key: str) -> str:
        self.replies.append((message_id, text, idempotency_key))
        return "om_notify"

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        pass

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        pass


class GateHarness:
    """HarnessPort double modelling the daemon's restore gate.

    ``refusals`` consecutive ``send_request`` calls raise the same
    DAEMON_RESTORING envelope the daemon's dispatch gate emits; after that
    (the gate opening) the request is accepted.
    """

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.attempts = 0
        self.requests = []

    def send_request(self, request):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.attempts <= self.refusals:
            raise DaemonRestoringError(RESTORING_MESSAGE)
        self.requests.append(request)
        return HarnessReceipt(message_id=f"msg_harness_{len(self.requests)}")

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        pass

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        pass


class PermanentHarness:
    """Every call is refused with a permanent (non-transient) code."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.attempts = 0

    def send_request(self, request):  # type: ignore[no-untyped-def]
        del request
        self.attempts += 1
        raise DaemonRequestError(self.code, "fixture permanent refusal")

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        pass

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        pass


class TransportBlipHarness:
    """Connection refused (daemon restarting), twice, then accepted."""

    def __init__(self) -> None:
        self.attempts = 0
        self.requests = []

    def send_request(self, request):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.attempts <= 2:
            raise ConnectionRefusedError("daemon socket not listening yet")
        self.requests.append(request)
        return HarnessReceipt(message_id="msg_harness_blip")

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        pass

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        pass


class PinRoutes:
    """RouteDirectory double: one pinned DM plus an ask-target resolver."""

    def __init__(self, actor: ActorTarget, *, resolve_refusals: int = 0) -> None:
        self.actor = actor
        self.pins: dict[str, ActorTarget] = {"lark:oc_dm:oc_dm": actor}
        self.resolve_refusals = resolve_refusals
        self.resolve_attempts = 0

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return self.actor if actor_key == self.actor.actor_key else None

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        return self.pins.get(conversation_id)

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]:
        self.resolve_attempts += 1
        if self.resolve_attempts <= self.resolve_refusals:
            raise DaemonRestoringError(RESTORING_MESSAGE)
        if token == self.actor.actor_id:
            return (self.actor,)
        return ()

    def list_runtime_targets(self):  # type: ignore[no-untyped-def]
        return ()

    def list_agent_directory(self):  # type: ignore[no-untyped-def]
        return ()


def _make_adapter(
    tmp_path: Path,
    harness,
    routes: PinRoutes,  # type: ignore[no-untyped-def]
    *,
    budget: float = 2.0,
) -> tuple[LarkAdapter, FakeLark]:
    lark = FakeLark()
    adapter = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:test",
        transient_retry_budget=budget,
        transient_retry_backoff_initial=0.01,
        transient_retry_backoff_max=0.02,
    )
    return adapter, lark


def _letters(tmp_path: Path) -> list:  # type: ignore[no-untyped-def]
    return LarkStateStore(tmp_path / "lark-state.sqlite3").dead_letters()


def _dm(message_id: str, text: str = "deploy is red"):  # type: ignore[no-untyped-def]
    return LarkInboundMessage(
        event_id=f"evt-{message_id}",
        message_id=message_id,
        chat_id="oc_dm",
        chat_type="p2p",
        text=text,
        sender_id="ou_sender",
        sender_type="user",
        create_time="1720000001000",
        message_type="text",
        content_status="supported",
    )


# ---------------------------------------------------------------------------
# Test 1: the gate stays closed for two refusals, then opens.
# ---------------------------------------------------------------------------


def test_restore_window_inbound_is_retried_not_dead_lettered(
    tmp_path: Path,
) -> None:
    harness = GateHarness(refusals=2)
    adapter, lark = _make_adapter(tmp_path, harness, PinRoutes(_actor()))
    try:
        outcome = adapter.handle_inbound(_dm("om_restore_window"))
    finally:
        adapter.close(0.0)

    assert outcome.status == "forwarded"
    assert outcome.harness_message_id == "msg_harness_1"
    # Two gate refusals, then the accepted third attempt.
    assert harness.attempts == 3
    assert len(harness.requests) == 1
    # No dead letter was created and no failure reply was sent.
    assert _letters(tmp_path) == ()
    assert lark.replies == []


# ---------------------------------------------------------------------------
# Test 2: a transport blip while the daemon restarts is also transient.
# ---------------------------------------------------------------------------


def test_daemon_restart_transport_blip_is_retried(tmp_path: Path) -> None:
    harness = TransportBlipHarness()
    adapter, lark = _make_adapter(tmp_path, harness, PinRoutes(_actor()))
    try:
        outcome = adapter.handle_inbound(_dm("om_restart_blip"))
    finally:
        adapter.close(0.0)

    assert outcome.status == "forwarded"
    assert harness.attempts == 3
    assert _letters(tmp_path) == ()
    assert lark.replies == []


# ---------------------------------------------------------------------------
# Test 3: a permanent error dead-letters immediately AND notifies the sender.
# ---------------------------------------------------------------------------


def test_permanent_error_dead_letters_and_notifies_sender(tmp_path: Path) -> None:
    harness = PermanentHarness(ipc_errors.UNSUPPORTED_TARGET)
    adapter, lark = _make_adapter(tmp_path, harness, PinRoutes(_actor()))
    try:
        outcome = adapter.handle_inbound(_dm("om_permanent"))
    finally:
        adapter.close(0.0)

    assert outcome.status == "error"
    # Permanent: no retry budget burned on a refusal that will not change.
    assert harness.attempts == 1
    letters = _letters(tmp_path)
    assert len(letters) == 1
    letter = letters[0]
    assert letter.message_id == "om_permanent"
    assert letter.reason == "forward-error"
    assert letter.detail == ipc_errors.UNSUPPORTED_TARGET
    assert letter.text == "deploy is red"
    # The sender is told the message was not delivered (best effort, once
    # per dead letter, keyed so a replay of the same message cannot resend).
    assert len(lark.replies) == 1
    replied_to, body, idempotency_key = lark.replies[0]
    assert replied_to == "om_permanent"
    assert idempotency_key == "om_permanent:dead-letter:forward-error"
    assert "not delivered" in body
    assert ipc_errors.UNSUPPORTED_TARGET in body


# ---------------------------------------------------------------------------
# Test 4: a transient refusal that outlives the budget still preserves the
# body and still notifies the sender -- with a distinct reason.
# ---------------------------------------------------------------------------


def test_transient_retry_exhaustion_dead_letters_and_notifies(
    tmp_path: Path,
) -> None:
    harness = GateHarness(refusals=10**9)
    adapter, lark = _make_adapter(tmp_path, harness, PinRoutes(_actor()), budget=0.05)
    try:
        outcome = adapter.handle_inbound(_dm("om_exhausted"))
    finally:
        adapter.close(0.0)

    assert outcome.status == "error"
    assert harness.attempts >= 2
    letters = _letters(tmp_path)
    assert len(letters) == 1
    letter = letters[0]
    assert letter.message_id == "om_exhausted"
    assert letter.reason == "forward-retry-exhausted"
    assert letter.detail == ipc_errors.DAEMON_RESTORING
    assert letter.text == "deploy is red"
    assert len(lark.replies) == 1
    replied_to, body, idempotency_key = lark.replies[0]
    assert replied_to == "om_exhausted"
    assert idempotency_key == "om_exhausted:dead-letter:forward-retry-exhausted"
    assert "not delivered" in body


# ---------------------------------------------------------------------------
# Test 5: the /hyprial ask routing lookup is inside the restore window too.
# ---------------------------------------------------------------------------


def test_ask_target_resolution_survives_the_restore_window(
    tmp_path: Path,
) -> None:
    actor = _actor()
    harness = GateHarness(refusals=0)
    routes = PinRoutes(actor, resolve_refusals=2)
    adapter, lark = _make_adapter(tmp_path, harness, routes)
    try:
        outcome = adapter.handle_inbound(
            _dm("om_ask_restore", text=f"/hyprial ask {actor.actor_id} please deploy")
        )
    finally:
        adapter.close(0.0)

    assert outcome.status == "forwarded"
    assert routes.resolve_attempts == 3
    assert len(harness.requests) == 1
    assert harness.requests[0].text == "please deploy"
    assert _letters(tmp_path) == ()
    assert lark.replies == []


# ---------------------------------------------------------------------------
# The worker's IPC seam keeps the envelope's stable code on the raised
# error -- the adapter's transient classification reads it.  A bare
# RuntimeError carrying only the message is the pre-F3 shape in which
# every refusal looked permanent.
# ---------------------------------------------------------------------------


class _EnvelopeIpcServer:
    """One-shot AF_UNIX double answering with the DAEMON_RESTORING envelope.

    Construction binds *and listens* before the serving thread starts, so a
    client that connects the moment the socket path exists cannot win the
    bind->listen window.  The previous inline server started the thread first
    and probed ``socket_path.exists()``, which only proves ``bind`` ran: the
    client could be refused with ``ConnectionRefusedError`` while the path was
    already visible (CI task 13636/14142/14635/15085/15480).  ``close`` also
    releases the listener when no client ever connected -- the old form only
    closed it on the accept path, so that same refused connect leaked one
    descriptor and the fd-hygiene fixture reported it.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    client, _ = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with client:
                    buffer = bytearray()
                    while b"\n" not in buffer:
                        chunk = client.recv(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                    if not buffer:
                        continue
                    frame = json.loads(buffer.partition(b"\n")[0])
                    client.sendall(
                        json.dumps(
                            {
                                "version": 1,
                                "id": frame.get("id"),
                                "error": {
                                    "code": ipc_errors.DAEMON_RESTORING,
                                    "message": RESTORING_MESSAGE,
                                },
                            },
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )
                return
        finally:
            self._stopped.set()

    def close(self) -> None:
        self._stop.set()
        self._stopped.wait(2.0)
        self._server.close()


def test_worker_ipc_raises_the_envelope_code() -> None:
    # pytest's tmp_path exceeds the AF_UNIX path limit on this host; use a
    # short /tmp root like the CLI IPC tests do.
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hyprial-ipc-", dir=hyprial_test_tmp_root()))
    server = _EnvelopeIpcServer(root / "daemon.sock")
    try:
        with pytest.raises(DaemonRequestError) as caught:
            _ipc(server.socket_path, "message.send", {"actor": "adapter:lark:test"})
        assert caught.value.code == ipc_errors.DAEMON_RESTORING
        assert RESTORING_MESSAGE in str(caught.value)
    finally:
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_envelope_ipc_server_releases_its_listener_without_a_client() -> None:
    # The refused-connect half of the CI flake: when no client reaches the
    # accept path, close() must still release the listener.  Before the fix the
    # serving thread stayed parked in accept() and the fd-hygiene fixture
    # reported one leaked descriptor for the rest of the run.
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hyprial-ipc-", dir=hyprial_test_tmp_root()))
    server = _EnvelopeIpcServer(root / "daemon.sock")
    try:
        assert server._server.fileno() != -1
    finally:
        server.close()
    assert server._server.fileno() == -1, "close() left the listener open"
    shutil.rmtree(root, ignore_errors=True)
