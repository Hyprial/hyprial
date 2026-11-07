"""Pin authority unavailability must not become authoritative absence."""
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
        channel_actor_id="channel:lark:gateway", transient_retry_budget=budget,
        transient_retry_backoff_initial=0.001, transient_retry_backoff_max=0.002,
    )
    return adapter, routes, state, lark, harness, message


@pytest.mark.parametrize("group", [False, True], ids=["dm", "bot-mention"])
@pytest.mark.parametrize("negative_cache", [False, True], ids=["cold", "previously-unpinned"])
@pytest.mark.parametrize("error_type", [DaemonRestoringError, ConnectionRefusedError])
def test_pin_query_transient_retries_then_forwards_once(
    tmp_path: Path, monkeypatch, group, negative_cache, error_type,
):
    attempts = 0
    seed_cache = negative_cache

    def query(_socket, method, params):
        nonlocal attempts
        assert method == "adapter.pins" and params == {}
        if seed_cache:
            return {"pins": {}}
        attempts += 1
        if attempts <= 2:
            raise error_type("transient pin authority unavailable")
        return {"pins": {"gateway": "agent:probe:local:target"}}

    adapter, routes, state, lark, harness, message = _rig(tmp_path, monkeypatch, query, group=group)
    try:
        if seed_cache:
            assert routes.pinned_actor(message.conversation_id) is None
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


def test_pin_retry_exhaustion_keeps_body_for_recovery_without_no_target(
    tmp_path: Path, monkeypatch,
):
    restoring = True

    def query(*_args):
        if restoring:
            raise DaemonRestoringError("restore still running")
        return {"pins": {"gateway": "agent:probe:local:target"}}

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


def test_successfully_queried_absent_pin_stays_no_target(tmp_path: Path, monkeypatch):
    calls = []

    def query(*_args):
        calls.append(1)
        return {"pins": {}}

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
