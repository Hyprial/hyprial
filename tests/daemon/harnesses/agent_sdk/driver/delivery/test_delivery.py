from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
import pytest
import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import ClaudeAgentSdkProcess, IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.kernel.impl.log import Logger

from ..support import (
    BlockingResponse,
    ClientFactory,
    FakeClient,
    delivery,
    process,
    wait_for_results,
)


def _receive_delivery(
    inbox: InboxService,
    item: HarnessDelivery,
    *,
    expires_at_ms: int | None = None,
) -> None:
    now_ms = time.time_ns() // 1_000_000
    received = inbox.receive(
        InboxMessage(
            message_id=item.delivery_id,
            conversation_id=item.conversation_id,
            sender="agent:alice:mac:requester",
            recipient=item.recipient,
            payload=item.message.encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=now_ms,
            expires_at_ms=expires_at_ms,
        ),
        now_ms=now_ms,
    )
    assert received.acknowledged


def _fifo_process(
    tmp_path: Path, client: FakeClient
) -> tuple[ClaudeAgentSdkProcess, InboxService]:
    state_dir = tmp_path / "state"
    inbox = InboxService(state_dir / "inbox.sqlite3", MemoryDeliveryTransport())
    channel = build_worker_channel(
        actor="agent:alice:mac:claude-worker",
        session_ref="session-pending-fence",
        hyprial_home=tmp_path / "home",
        state_dir=state_dir,
    )
    process = ClaudeAgentSdkProcess(
        HarnessLaunchSpec(
            harness="claude",
            name="claude-worker",
            headless=True,
            cwd="/work",
        ),
        client_factory=ClientFactory([client]),
        worker_channel=channel,
        reconnect_delay_seconds=0.01,
    )
    return process, inbox


def _worker_records(state_dir: Path) -> list[dict[str, object]]:
    path = Logger.worker(
        state_dir, runtime="claude", name="claude-worker"
    ).path
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fifo_skips_a_delivery_consumed_during_the_previous_turn(
    tmp_path: Path,
) -> None:
    blocked = BlockingResponse("first interrupted")
    client = FakeClient([blocked, "second must not run"])
    process, inbox = _fifo_process(tmp_path, client)
    first = delivery("delivery-pending-first", "hold the first turn")
    second = delivery("delivery-consumed-second", "unique consumed body")
    _receive_delivery(inbox, first)
    _receive_delivery(inbox, second)
    try:
        assert process.enqueue(first)
        assert process.enqueue(second)
        assert blocked.started.wait(timeout=1.0)
        assert inbox.ack(second.recipient, second.delivery_id).acknowledged

        assert process.interrupt(first.delivery_id, timeout=1.0)
        results = wait_for_results(process, 2)

        assert client.queries == [
            "[Harness Network message from requester to claude-worker]\n"
            "hold the first turn"
        ]
        assert [item.status for item in results] == [
            HarnessResultStatus.INTERRUPTED,
            HarnessResultStatus.INTERRUPTED,
        ]
        assert process._turn_runtime.read_in_flight() is None  # noqa: SLF001
        assert process._turn_runtime.read_results() == ()  # noqa: SLF001
    finally:
        process.stop()
        inbox.close()

    records = _worker_records(tmp_path / "state")
    skipped = [item for item in records if item["event"] == "worker.turn.skipped"]
    assert [(item["deliveryId"], item["reason"]) for item in skipped] == [
        (second.delivery_id, "already-consumed")
    ]
    assert not any(item["event"] == "worker.turn.failed" for item in records)
    assert "unique consumed body" not in json.dumps(records)


def test_fifo_prompts_a_system_notice_without_an_inbox_row(
    tmp_path: Path, monkeypatch,
) -> None:
    client = FakeClient(["notice handled"])
    process, inbox = _fifo_process(tmp_path, client)
    monkeypatch.setattr(
        "hyprial.daemon.impl.inbox.authority.read.delivery_pending",
        lambda *_args, **_kwargs: pytest.fail("notice consulted inbox projection"),
    )
    notice = HarnessDelivery(
        "notice-delivery",
        "notice-conversation",
        "system:hyprial:remote-node",
        "claude-worker",
        '{"message":"delivery failed","systemNotice":true}',
        notice=True,
    )
    try:
        assert process.enqueue(notice)
        assert wait_for_results(process, 1)[0].status is HarnessResultStatus.COMPLETED
        assert client.queries == [
            "[Harness Network message from system:hyprial:remote-node "
            "to claude-worker]\n"
            '{"message":"delivery failed","systemNotice":true}'
        ]
    finally:
        process.stop()
        inbox.close()


def test_fifo_reports_an_expired_delivery_with_its_own_reason(tmp_path: Path) -> None:
    client = FakeClient(["expired delivery must not run"])
    process, inbox = _fifo_process(tmp_path, client)
    item = delivery("delivery-expired", "unique expired body")
    _receive_delivery(inbox, item, expires_at_ms=2)
    try:
        assert process.enqueue(item)
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.INTERRUPTED
        assert result.failure_code == "expired"
        assert client.queries == []
    finally:
        process.stop()
        inbox.close()

    records = _worker_records(tmp_path / "state")
    skipped = [item for item in records if item["event"] == "worker.turn.skipped"]
    assert [(item["deliveryId"], item["reason"]) for item in skipped] == [
        (item.delivery_id, "expired")
    ]


def test_fifo_still_prompts_a_pending_followup_delivery(tmp_path: Path) -> None:
    blocked = BlockingResponse("first interrupted")
    client = FakeClient([blocked, "second completed"])
    process, inbox = _fifo_process(tmp_path, client)
    first = delivery("delivery-pending-control-first", "hold control turn")
    second = delivery("delivery-pending-control-second", "prompt pending body")
    _receive_delivery(inbox, first)
    _receive_delivery(inbox, second)
    try:
        assert process.enqueue(first)
        assert process.enqueue(second)
        assert blocked.started.wait(timeout=1.0)

        assert process.interrupt(first.delivery_id, timeout=1.0)
        results = wait_for_results(process, 2)

        assert client.queries == [
            "[Harness Network message from requester to claude-worker]\n"
            "hold control turn",
            "[Harness Network message from requester to claude-worker]\n"
            "prompt pending body",
        ]
        assert results[1].status is HarnessResultStatus.COMPLETED
        assert results[1].output == "second completed"
    finally:
        process.stop()
        inbox.close()


def test_fifo_runs_the_turn_when_the_inbox_projection_is_absent(
    tmp_path: Path,
) -> None:
    """UNKNOWN fails open: no inbox database means the turn is prompted."""

    client = FakeClient(["completed without a projection"])
    process, inbox = _fifo_process(tmp_path, client)
    inbox.close()
    (tmp_path / "state" / "inbox.sqlite3").unlink()
    item = delivery("delivery-unknown-projection", "run me anyway")
    try:
        assert process.enqueue(item)
        result = wait_for_results(process, 1)[0]
        assert client.queries == [
            "[Harness Network message from requester to claude-worker]\n"
            "run me anyway"
        ]
        assert result.status is HarnessResultStatus.COMPLETED
    finally:
        process.stop()


def test_worker_turn_emits_correlated_start_and_completion_events(
    tmp_path: Path,
) -> None:
    factory = ClientFactory([FakeClient(["turn complete"])])
    channel = build_worker_channel(
        actor="agent:alice:mac:claude-worker",
        session_ref="session-trajectory",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )
    sdk = ClaudeAgentSdkProcess(
        HarnessLaunchSpec(
            harness="claude",
            name="claude-worker",
            headless=True,
            cwd="/work",
        ),
        client_factory=factory,
        worker_channel=channel,
        reconnect_delay_seconds=0.01,
    )
    inbox = InboxService(
        channel.state_dir / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    item = delivery("msg-turn-1", "do the work")
    _receive_delivery(inbox, item)
    try:
        assert sdk.enqueue(item)
        assert wait_for_results(sdk, 1)[0].status is HarnessResultStatus.COMPLETED
    finally:
        sdk.stop()
        inbox.close()

    logger = Logger.worker(channel.state_dir, runtime="claude", name="claude-worker")
    records = [json.loads(line) for line in logger.path.read_text().splitlines()]
    turn_records = [
        item for item in records if item["event"].startswith("worker.turn.")
    ]
    assert [item["event"] for item in turn_records] == [
        "worker.turn.started",
        "worker.turn.completed",
    ]
    for record in turn_records:
        assert record["messageId"] == "msg-turn-1"
        assert record["correlationId"] == "msg-turn-1"
        assert record["conversationId"] == "conversation-1"
        assert record["node"] == "worker-turn"
        assert record["actorId"] == "claude-worker"
        assert record["deliveryId"] == "msg-turn-1"
        assert record["sender"] == "requester"
        assert record["recipient"] == "claude-worker"
        assert isinstance(record["ts"], str)


@pytest.mark.anyio
async def test_packaged_worker_forwards_interrupt_without_ending_session(
    tmp_path: Path,
) -> None:
    fake_package = tmp_path / "claude_agent_sdk"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        """
import asyncio

class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

class ResultMessage:
    def __init__(self, result, is_error=False):
        self.result = result
        self.is_error = is_error

class ClaudeSDKClient:
    def __init__(self, options):
        self.interrupted = asyncio.Event()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def query(self, prompt):
        self.prompt = prompt
    async def receive_response(self):
        await self.interrupted.wait()
        self.interrupted.clear()
        yield ResultMessage(self.prompt + ' interrupted')
    async def interrupt(self):
        self.interrupted.set()
""",
        encoding="utf-8",
    )
    worker = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(sys.executable, str(worker)),
        env={"PYTHONPATH": str(tmp_path)},
    )

    async with client:
        await client.query("long turn")

        async def collect():  # type: ignore[no-untyped-def]
            return [message async for message in client.receive_response()]

        response = asyncio.create_task(collect())
        await asyncio.sleep(0.03)
        await client.interrupt()
        messages = await asyncio.wait_for(response, timeout=1.0)

        await client.query("second turn")
        second = asyncio.create_task(collect())
        await asyncio.sleep(0.03)
        await client.interrupt()
        second_messages = await asyncio.wait_for(second, timeout=1.0)

    assert messages[-1].result == "long turn interrupted"
    assert messages[-1].is_error is False
    assert second_messages[-1].result == "second turn interrupted"
    assert second_messages[-1].is_error is False


def test_idle_sdk_session_accepts_a_later_delivery_and_starts_a_turn() -> None:
    client = FakeClient(["idle wake completed"])
    sdk = process(ClientFactory([client]))
    try:
        assert sdk.wait_ready(timeout=1.0)
        time.sleep(0.03)
        assert client.queries == []

        assert sdk.enqueue(delivery("delivery-1", "wake from idle"))
        [result] = wait_for_results(sdk, 1)

        assert client.queries == [
            "[Harness Network message from requester to claude-worker]\nwake from idle"
        ]
        assert result.delivery_id == "delivery-1"
        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == "idle wake completed"
    finally:
        sdk.stop()


def test_multiple_deliveries_are_injected_and_completed_in_fifo_order() -> None:
    client = FakeClient(["result one", "result two", "result three"])
    sdk = process(ClientFactory([client]))
    try:
        assert sdk.wait_ready(timeout=1.0)
        for index in range(1, 4):
            assert sdk.enqueue(delivery(f"delivery-{index}", f"message {index}"))

        results = wait_for_results(sdk, 3)

        assert client.queries == [
            "[Harness Network message from requester to claude-worker]\nmessage 1",
            "[Harness Network message from requester to claude-worker]\nmessage 2",
            "[Harness Network message from requester to claude-worker]\nmessage 3",
        ]
        assert [item.delivery_id for item in results] == [
            "delivery-1",
            "delivery-2",
            "delivery-3",
        ]
        assert [item.output for item in results] == [
            "result one",
            "result two",
            "result three",
        ]
    finally:
        sdk.stop()


def test_interrupt_marks_current_delivery_non_terminal_for_daemon_retry() -> None:
    blocked = BlockingResponse("ignored after interrupt")
    client = FakeClient([blocked])
    sdk = process(ClientFactory([client]))
    try:
        assert sdk.wait_ready(timeout=1.0)
        assert sdk.enqueue(delivery("delivery-1", "long task"))
        assert blocked.started.wait(timeout=1.0)

        assert sdk.interrupt("delivery-1", timeout=1.0)
        [result] = wait_for_results(sdk, 1)

        assert client.interrupts == 1
        assert result.delivery_id == "delivery-1"
        assert result.status is HarnessResultStatus.INTERRUPTED
        assert result.output == ""
        # A non-terminal outcome releases the dedupe key for durable redelivery.
        assert sdk.enqueue(delivery("delivery-1", "long task"))
    finally:
        sdk.stop()


def test_connection_loss_reconnects_and_retries_head_before_next_delivery() -> None:
    disconnected = FakeClient([ConnectionError("sdk transport lost")])
    recovered = FakeClient(["first recovered", "second completed"])
    sdk = process(ClientFactory([disconnected, recovered]))
    try:
        assert sdk.wait_ready(timeout=1.0)
        assert sdk.enqueue(delivery("delivery-1", "first"))
        assert sdk.enqueue(delivery("delivery-2", "second"))

        results = wait_for_results(sdk, 2)

        assert disconnected.queries == [
            "[Harness Network message from requester to claude-worker]\nfirst"
        ]
        assert recovered.queries == [
            "[Harness Network message from requester to claude-worker]\nfirst",
            "[Harness Network message from requester to claude-worker]\nsecond",
        ]
        assert [item.delivery_id for item in results] == [
            "delivery-1",
            "delivery-2",
        ]
        assert disconnected.exited == 1
        assert recovered.entered == 1
    finally:
        sdk.stop()
