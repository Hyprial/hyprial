"""S2: a reply inherits the envelope of the message it answers, on the replier.

Both reply builders on the replying node are covered: ``message.reply``
(``send.py``) and the daemon's mechanism answers (``runtime/forwards.py``).
The IPC response of ``message.reply`` gains no envelope key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.daemon.delivery.message import test_message_history as history
from tests.daemon.delivery.message.test_message_history import (
    CONVERSATION,
    SESSION,
    _HistoryHarness,
)

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.runtime.forwards import _BridgeForwardsMixin
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

history_harness = history.harness
THREAD = mint_thread_id(b"envelope-replies-thread")
CELL = mint_cell_id(b"envelope-replies-cell")
# The mechanism test runs no daemon, so plain URIs stand for the two sides;
# the message.reply test uses the harness's registered (id-form) addresses.
READER = "agent:op:node-1:reader"
ACTOR = "agent:op:node-1:worker-b"


def _request(message_id: str, *, sender: str = READER, recipient: str = ACTOR) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=CONVERSATION,
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"message": "please answer"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1000,
    ).with_envelope(thread_id=THREAD, conversation_cell_id=CELL)


def test_message_reply_inherits_the_original_envelope(
    history_harness: _HistoryHarness,
) -> None:
    harness = history_harness
    assert harness.inbox.receive(
        _request("m-stamped", sender=harness.reader, recipient=harness.actor)
    ).accepted
    replied = harness.app.handle(
        "message.reply",
        {
            "from": harness.actor,
            "sessionRef": SESSION,
            "messageId": "m-stamped",
            "message": "the answer",
            "summary": "the answer",
        },
    )
    assert replied["replied"] is True
    assert not {"threadId", "thread_id", "conversationCellId", "messageClass"} & set(
        replied
    )
    (reply,) = [
        row
        for row in harness.inbox.pending_messages(harness.reader)
        if row.message_id == replied["replyMessageId"]
    ]
    assert (reply.thread_id, reply.conversation_cell_id) == (THREAD, CELL)
    assert reply.message_class is None


class _RecordingInbox:
    def __init__(self) -> None:
        self.submitted: list[InboxMessage] = []

    def submit(self, message: InboxMessage) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, recipient: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)


def _runtime() -> SimpleNamespace:
    runtime = SimpleNamespace(inbox=_RecordingInbox())
    runtime._confirm_mechanism_answer = (
        lambda original, reply: _BridgeForwardsMixin._confirm_mechanism_answer(
            runtime, original, reply  # type: ignore[arg-type]
        )
    )
    # Release (UUID addressing D8): a mechanism answer speaks from the
    # recipient's official address; with no resolver the row key is kept.
    runtime._official_recipient = (
        lambda recipient: _BridgeForwardsMixin._official_recipient(
            runtime, recipient  # type: ignore[arg-type]
        )
    )
    return runtime


def test_mechanism_answers_inherit_the_original_envelope() -> None:
    original = _request("m-mechanism", sender=READER, recipient=ACTOR).with_envelope(
        message_class=MessageClass.SYSTEM
    )
    result = HarnessResult(
        delivery_id="m-mechanism",
        recipient=ACTOR,
        status=HarnessResultStatus.COMPLETED,
        output="done",
    )
    for answer in (
        _BridgeForwardsMixin._answer_mechanism_result,
        _BridgeForwardsMixin._answer_hook_request,
    ):
        runtime = _runtime()
        assert answer(runtime, original, result)  # type: ignore[arg-type]
        (reply,) = runtime.inbox.submitted
        assert reply.idempotency_key == "reply:m-mechanism"
        assert (reply.thread_id, reply.conversation_cell_id, reply.message_class) == (
            THREAD,
            CELL,
            MessageClass.SYSTEM,
        )
