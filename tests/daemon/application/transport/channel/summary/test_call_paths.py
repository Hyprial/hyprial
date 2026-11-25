"""T19/T20: every internal send path supplies a summary, and the two new
methods are routable.

``message.send``/``message.reply`` must not become a surface only the CLI can
use: the Lark worker and the managed worker channel both pass through the
same boundary and must carry a summary.  I7a (contract i7-0 v2 section 4):
another owner's copy is posted to the user agent cell's chats and does not
re-enter ``message.send``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from hyprial.kernel import (
    DaemonRequestError,
    IPC_MESSAGE_READ_BODY,
    IPC_MESSAGE_SETTLE_HINT,
    ipc_errors,
)
from hyprial.kernel.impl.contracts.channels import worker_channel
from hyprial.daemon.impl.adapters.lark.contracts.wire import ActorTarget, HarnessRequest
from hyprial.daemon.impl.adapters.lark.worker import daemon_link
from hyprial.daemon.impl.inbox import InboxMessage
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS

from ..support import (
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
    register,
)

TARGET = "agent:op:node-1:bob"


def _lark_request(text: str) -> HarnessRequest:
    return HarnessRequest(
        message_id="lark-1",
        from_actor_id="channel:lark:e2e",
        to=ActorTarget("agent:op:node-1:bob", "bob", "bob"),
        text=text,
        conversation_id="c-lark",
        provider_metadata={"provider": "lark"},
    )


def test_the_lark_worker_sends_a_derived_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _lark_request("a person's long message")
    captured: dict[str, object] = {}

    def fake_ipc(socket_path, method, params, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["params"] = params
        operation_id = f"lark-inbound:{request.from_actor_id}:{request.message_id}"
        expected = str(
            uuid5(
                NAMESPACE_URL,
                f"hyprial:send:{operation_id}:{request.to.actor_id}:0",
            )
        )
        return {
            "ok": True,
            "operationId": operation_id,
            "conversationId": request.conversation_id,
            "messageId": expected,
            "deliveries": [
                {
                    "target": request.to.actor_id,
                    "messageId": expected,
                    "accepted": True,
                    "queued": False,
                }
            ],
        }

    monkeypatch.setattr(daemon_link, "_ipc", fake_ipc)
    daemon_link._Harness(tmp_path / "daemon.sock").send_request(request)

    from hyprial.daemon.impl.inbox.contracts.summary import summary_from_text

    assert captured["method"] == "message.send"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["summary"] == summary_from_text(request.text)


def test_the_worker_channel_admits_summary_and_the_two_new_methods() -> None:
    binding = worker_channel.WorkerBinding(
        actor="agent:op:node-1:worker",
        session_ref="session-worker",
        daemon_epoch="epoch-1",
    )

    method, forwarded = worker_channel.bound_request(
        {
            "version": 1,
            "id": "request-1",
            "method": "message.send",
            "params": {"to": [TARGET], "message": "hi", "summary": "s"},
        },
        binding,
    )
    assert method == "message.send"
    assert forwarded["summary"] == "s"

    for method_name in (IPC_MESSAGE_READ_BODY, IPC_MESSAGE_SETTLE_HINT):
        method, forwarded = worker_channel.bound_request(
            {
                "version": 1,
                "id": None,
                "method": method_name,
                "params": {"messageId": "m-1"},
            },
            binding,
        )
        assert method == method_name
        assert forwarded["messageId"] == "m-1"

    with pytest.raises(worker_channel.WorkerChannelError):
        worker_channel.bound_request(
            {
                "version": 1,
                "id": "request-2",
                "method": "message.send",
                "params": {"to": [TARGET], "message": "hi", "resourcePaths": ["/etc/passwd"]},
            },
            binding,
        )


def test_the_worker_method_names_are_the_seam_constants() -> None:
    assert worker_channel.METHOD_PARAMS["message.send"] >= {"summary"}
    assert worker_channel.METHOD_PARAMS["message.reply"] >= {"summary", "mentions", "noMention"}
    assert worker_channel.METHOD_PARAMS[IPC_MESSAGE_READ_BODY] == {"messageId"}
    assert worker_channel.METHOD_PARAMS[IPC_MESSAGE_SETTLE_HINT] == {"messageId"}


def test_the_worker_channel_forwards_reply_mentions_and_no_mention() -> None:
    binding = worker_channel.WorkerBinding(
        actor="agent:op:node-1:worker",
        session_ref="session-worker",
        daemon_epoch="epoch-1",
    )

    method, forwarded = worker_channel.bound_request(
        {
            "version": 1,
            "id": "reply-1",
            "method": "message.reply",
            "params": {
                "messageId": "m-1",
                "message": "done",
                "summary": "s",
                "mentions": ["a.alphaaaaaaaaaaaaaaaaaaaa"],
                "noMention": True,
            },
        },
        binding,
    )

    assert method == "message.reply"
    assert forwarded["mentions"] == ["a.alphaaaaaaaaaaaaaaaaaaaa"]
    assert forwarded["noMention"] is True


def test_the_two_new_methods_are_routed_and_counted(tmp_path: Path) -> None:
    from hyprial.daemon.impl.application.messaging.visibility import _CONSUMER_METHODS

    assert IPC_MESSAGE_READ_BODY in _IPC_STATS_METHODS
    assert IPC_MESSAGE_SETTLE_HINT in _IPC_STATS_METHODS
    assert IPC_MESSAGE_READ_BODY in _CONSUMER_METHODS
    assert IPC_MESSAGE_SETTLE_HINT in _CONSUMER_METHODS

    app = application(tmp_path, FakeInbox(pending_message()))
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            IPC_MESSAGE_READ_BODY,
            {"actor": "alice", "sessionRef": "session-1", "messageId": "unknown"},
        )
    assert error.value.code != ipc_errors.METHOD_NOT_FOUND


def test_link_governed_exempts_an_internal_actor_in_both_directions() -> None:
    """Rulings 45/51: reserved daemon actor names stay exempt on any node."""

    from hyprial.daemon.impl.application.messaging.inbox_surface.send_receipts import (
        _is_governed_agent_pair,
    )
    from hyprial.kernel import RESERVED_AGENT_ACTOR_NAMES

    worker = "agent:op:node-1:worker"
    peer = "agent:op:node-2:peer"
    internal_uris = (
        "agent:op:node-1:dispatch-coordinator",
        "agent:op:node-2:dispatch-coordinator",
        "agent:op:node-2:_turn-hooks",
    )
    near_name = "agent:op:node-2:_named-worker"

    assert RESERVED_AGENT_ACTOR_NAMES == frozenset({"dispatch-coordinator", "_turn-hooks"})
    for internal_uri in internal_uris:
        assert _is_governed_agent_pair(worker, internal_uri) is False
        assert _is_governed_agent_pair(internal_uri, worker) is False
    assert _is_governed_agent_pair(worker, peer) is True
    assert _is_governed_agent_pair(worker, near_name) is True
    assert _is_governed_agent_pair(near_name, worker) is True


# --- B3: the fail-loud notices are messages too; they carry a summary ------


def _loud_identity(sender: str = "agent:op:node-1:driver"):  # type: ignore[no-untyped-def]
    from hyprial.daemon import AttemptIdentity

    return AttemptIdentity(
        delivery_id="req-loud",
        conversation_id="conv-loud",
        sender=sender,
        worker="agent:op:node-1:bob",
        harness="pi",
        generation=1,
        observed_at_ms=1_000,
    )


def test_the_two_loud_notices_carry_a_derived_summary(tmp_path: Path) -> None:
    from hyprial.daemon import no_progress_notice, unavailable_notice
    from hyprial.daemon.impl.inbox.contracts.summary import (
        SUMMARY_MAX_BYTES,
        summary_from_text,
    )

    inbox = FakeInbox(pending_message())
    notices = (
        unavailable_notice(
            _loud_identity(),
            failure_code="PROVIDER_USAGE_LIMIT",
            terminal=True,
            attempts=1,
            max_attempts=3,
        ),
        no_progress_notice(_loud_identity(), waited_ms=500_000, budget_ms=480_000),
    )
    for notice in notices:
        inbox.submit(notice)

    for notice, submitted in zip(notices, inbox.submitted):
        text = json.loads(notice.payload)["message"]
        assert submitted.summary == summary_from_text(text)
        assert isinstance(submitted.summary, str) and submitted.summary.strip()
        assert len(submitted.summary.encode("utf-8")) <= SUMMARY_MAX_BYTES


def test_the_human_loud_path_delivers_the_notice_text_unchanged() -> None:
    from hyprial.daemon import unavailable_notice
    from hyprial.daemon.impl.runtime.forwards import _BridgeForwardsMixin
    from hyprial.daemon.impl.runtime.settlement import _owner_facing_notice_text

    notice = unavailable_notice(
        _loud_identity(sender="adapter:lark:chat-1"),
        failure_code="PROVIDER_USAGE_LIMIT",
        terminal=True,
        attempts=1,
        max_attempts=3,
    )
    delivered: list[tuple[InboxMessage, str]] = []

    class _HumanLoud(_BridgeForwardsMixin):
        pass

    stub = _HumanLoud()
    stub._in_channel_notice_keys = set()  # type: ignore[attr-defined]
    stub._pending_notices = {}  # type: ignore[attr-defined]
    stub._notice_failures_logged = set()  # type: ignore[attr-defined]
    stub._logger = None  # type: ignore[attr-defined]
    stub._in_channel_notice_delivery = (  # type: ignore[attr-defined]
        lambda message, text: delivered.append((message, text)) or True
    )

    assert stub._submit_loud(notice) is True  # type: ignore[attr-defined]
    assert len(delivered) == 1
    message, text = delivered[0]
    assert message is notice
    assert text == _owner_facing_notice_text(notice)
    # The summary rides along on the message without changing the chat text.
    assert notice.summary is not None
