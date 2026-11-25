"""Routing authority unavailability must not become authoritative absence."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import HarnessReceipt, LarkAdapter, LarkInboundMessage, LarkStateStore
from hyprial.daemon.impl.adapters.lark.worker import daemon_link as worker
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRestoringError


class _Lark:
    def __init__(self, message):
        self.message = message
        self.replies = []

    def bot_open_id(self):
        return "ou_bot"

    def reply(self, message_id, text, *, idempotency_key):
        self.replies.append((message_id, text, idempotency_key))
        return "om_notice"

    def add_reaction(self, *_args):
        pass

    def get_inbound_message(self, message_id):
        assert message_id == self.message.message_id
        return self.message


class _Harness:
    def __init__(self):
        self.requests = []

    def send_request(self, request):
        self.requests.append(request)
        return HarnessReceipt(message_id="harness-message")


def _rig(tmp_path, monkeypatch, query, *, group=False, budget=0.1):
    message = LarkInboundMessage(
        event_id="event-pin-retry", message_id="om_pin_retry", chat_id="oc_chat",
        chat_type="group" if group else "p2p", text="preserve this body",
        sender_id="ou_sender", sender_type="user",
        mention_open_ids=("ou_bot",) if group else (),
    )
    monkeypatch.setattr(worker, "_ipc", query)
    routes = worker._Routes(gateway_name="gateway", socket_path=tmp_path / "daemon.sock", report=lambda _event: None)
    state = LarkStateStore(tmp_path / "lark.sqlite3")
    lark = _Lark(message)
    harness = _Harness()
    adapter = LarkAdapter(
        state=state, lark=lark, harness=harness, routes=routes,
        channel_actor_id="adapter:lark:gateway", transient_retry_budget=budget,
        transient_retry_backoff_initial=0.001, transient_retry_backoff_max=0.002,
    )
    return adapter, routes, state, lark, harness, message


@pytest.mark.parametrize("group", [False, True], ids=["dm", "bot-mention"])
@pytest.mark.parametrize("negative_cache", [False, True], ids=["cold", "previously-unbound"])
@pytest.mark.parametrize("error_type", [DaemonRestoringError, ConnectionRefusedError])
def test_routing_query_transient_retries_then_forwards_once(
    tmp_path: Path, monkeypatch, group, negative_cache, error_type,
):
    attempts = 0
    seed_cache = negative_cache

    def query(_socket, method, params):
        nonlocal attempts
        assert method == "adapter.list" and params == {}
        if seed_cache:
            return {"adapters": [{"name": "gateway", "agent": None}]}
        attempts += 1
        if attempts <= 2:
            raise error_type("transient routing authority unavailable")
        return {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}

    adapter, routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query, group=group)
    try:
        if seed_cache:
            assert routes.routed_actor(message.conversation_id) is None
            seed_cache = False
        outcome = adapter.handle_inbound(message)
        assert outcome.status == "forwarded"
        assert attempts == 3
        assert len(harness.requests) == 1
        assert harness.requests[0].to.actor_id == "agent:probe:local:target"
        assert harness.requests[0].text == message.text
        assert state.dead_letters() == ()
        assert lark.replies == []
        assert adapter.handle_inbound(replace(message, event_id="redelivered-event")).status == "duplicate"
        assert len(harness.requests) == 1
    finally:
        adapter.close()


def test_routing_retry_exhaustion_keeps_body_for_recovery_without_no_target(
    tmp_path: Path, monkeypatch,
):
    restoring = True

    def query(*_args):
        if restoring:
            raise DaemonRestoringError("restore still running")
        return {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}

    adapter, _routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query, budget=0)
    try:
        outcome = adapter.handle_inbound(message)
        assert outcome.status == "error"
        assert outcome.ack_error == "TransientHarnessRetryExhausted"
        assert harness.requests == []
        [letter] = state.dead_letters()
        assert letter.reason == "forward-retry-exhausted"
        assert letter.detail == "DAEMON_RESTORING"
        assert letter.text == message.text
        assert not state.seen(message.event_id)
        assert not state.seen_message(message.message_id)
        assert len(lark.replies) == 1
        assert "DAEMON_RESTORING" in lark.replies[0][1]
        assert "No agent is bound" not in lark.replies[0][1]
        restoring = False
        assert adapter.recover_message(message.message_id).status == "forwarded"
        assert len(harness.requests) == 1
        assert state.dead_letters() == ()
    finally:
        adapter.close()


def test_successfully_queried_absent_binding_stays_no_target(tmp_path: Path, monkeypatch):
    calls = []

    def query(*_args):
        calls.append(1)
        return {"adapters": []}

    adapter, _routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query)
    try:
        assert adapter.handle_inbound(message).status == "no-target"
        assert len(calls) == 1
        assert harness.requests == []
        [letter] = state.dead_letters()
        assert letter.reason == "no-target"
        assert "No agent is bound" in lark.replies[0][1]
    finally:
        adapter.close()


def _cell_runtime_unavailable():
    from hyprial.kernel import DaemonRequestError

    return DaemonRequestError("DAEMON_NOT_READY", "cell runtime is unavailable")


def test_unavailable_cell_runtime_keeps_the_last_binding(tmp_path: Path, monkeypatch):
    available = True

    def query(*_args):
        if available:
            return {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}
        raise _cell_runtime_unavailable()

    adapter, routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query)
    try:
        assert routes.routed_actor(message.conversation_id).actor_id == "agent:probe:local:target"
        available = False
        assert adapter.handle_inbound(message).status == "forwarded"
        assert [request.to.actor_id for request in harness.requests] == ["agent:probe:local:target"]
        assert state.dead_letters() == ()
        assert lark.replies == []
    finally:
        adapter.close()


def test_unavailable_cell_runtime_without_cache_retries_then_forwards(tmp_path: Path, monkeypatch):
    attempts = 0

    def query(*_args):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _cell_runtime_unavailable()
        return {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}

    adapter, _routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query)
    try:
        assert adapter.handle_inbound(message).status == "forwarded"
        assert attempts == 3
        assert [request.to.actor_id for request in harness.requests] == ["agent:probe:local:target"]
        assert state.dead_letters() == ()
        assert lark.replies == []
    finally:
        adapter.close()


def test_unavailable_cell_runtime_exhaustion_keeps_the_body_for_recovery(tmp_path: Path, monkeypatch):
    available = False

    def query(*_args):
        if not available:
            raise _cell_runtime_unavailable()
        return {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}

    adapter, _routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query, budget=0)
    try:
        outcome = adapter.handle_inbound(message)
        assert outcome.status == "error"
        assert outcome.ack_error == "TransientHarnessRetryExhausted"
        assert harness.requests == []
        [letter] = state.dead_letters()
        assert letter.reason == "forward-retry-exhausted"
        assert letter.detail == "DAEMON_NOT_READY"
        assert letter.text == message.text
        assert not state.seen_message(message.message_id)
        assert all("No agent is bound" not in reply[1] for reply in lark.replies)
        available = True
        assert adapter.recover_message(message.message_id).status == "forwarded"
        assert len(harness.requests) == 1
        assert state.dead_letters() == ()
    finally:
        adapter.close()
