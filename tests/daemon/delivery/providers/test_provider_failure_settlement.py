from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    classify_harness_failure,
    harness_failure_is_permanent,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.inbox import (
    ConsumptionState,
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

ACTOR = "agent:op:provider-failure-node:worker"
SENDER = "agent:op:sender-node:dispatcher"


class _Events:
    def publish(self, _event: object) -> None:
        pass


class _Transport:
    def declare_liveliness(self, _key: str) -> object:
        return object()


class _Harnesses:
    def __init__(self) -> None:
        self.dispatched: list[HarnessDelivery] = []
        self.results: list[HarnessResult] = []

    def reconcile(self) -> int:
        return 0

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        # The reconcile tick now persists the native-session projection
        # through the harness authority (daemon/runtime.py); this double
        # stands in for that port, so it has to answer the same call.
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return ("worker",)

    def dispatch(self, _name: str, delivery: HarnessDelivery) -> bool:
        self.dispatched.append(delivery)
        return True

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_results(self) -> tuple[HarnessResult, ...]:
        results = tuple(self.results)
        self.results.clear()
        return results

    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def online_mailboxes(self) -> tuple[()]:
        return ()

    def actor_online(self, _actor: str) -> bool:
        return False


class _NoopRuntime:
    def on_timer(self, _observed_at_ms: int | None = None) -> Any:
        return type(
            "Outcome",
            (),
            {
                "harness_restarts": 0,
                "inbox_results": 0,
                "inbox_pruned": 0,
                "inbox_pruned_items": (),
            },
        )()

    def stop(self) -> None:
        pass


def _desired(path: Path) -> DesiredStateStore:
    desired = path / "desired-state.json"
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DesiredStateStore(desired)


def _message(message_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conversation-{message_id}",
        sender=SENDER,
        recipient=ACTOR,
        payload=b'{"message":"do the work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _inbox(
    database: Path, *, max_inbox_items: int = 10_000
) -> DeliveryCustodyFacade:
    from tests.support.links.gate import permissive_admission_slot

    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        node_id="provider-failure-node",
        max_inbox_items=max_inbox_items,
        # ACTOR->ACTOR dispatch is gated; keep this suite about provider
        # failure settlement with an explicit permissive premise (wp4 T13).
        service_options={"link_admission": permissive_admission_slot()},
    )
    return DeliveryCustodyFacade(coordinator, database)


def _bridge(
    state: Path,
    inbox: DeliveryCustodyFacade,
    harnesses: _Harnesses,
    clock: dict[str, int],
    logs: list[tuple[str, str, str, dict[str, object]]] | None = None,
    blocking_failure_observer: Any = None,
    blocked_actor: Any = None,
) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=state,
        node_id="provider-failure-node",
        desired_state=_desired(state),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda _name: ACTOR,
        clock_ms=lambda: clock["now"],
        logger=(
            None
            if logs is None
            else lambda level, component, event, **fields: logs.append(
                (level, component, event, fields)
            )
        ),
        blocking_failure_observer=blocking_failure_observer,
        blocked_actor=blocked_actor,
    )


def _ipc_call(
    socket_path: Path, method: str, params: dict[str, object]
) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": "probe", "method": method, "params": params}
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def test_usage_limit_is_exactly_classified_as_permanent_provider_failure() -> None:
    assert (
        classify_harness_failure(
            "ERROR: You've hit your usage_limit; exceeded your current quota"
        )
        == "PROVIDER_USAGE_LIMIT"
    )
    # §8.2: a 429/rate-limit is terminal for the current message, not a
    # transient wire fault.  Classifying it transient kept the delivery
    # dispatchable, so the same request reached the provider again on every
    # redelivery.  The turn runtime already stops on the first such attempt;
    # the daemon must agree.
    assert (
        classify_harness_failure("HTTP 429 rate_limit: try again later")
        == "PROVIDER_RATE_LIMITED"
    )
    assert (
        classify_harness_failure("resource_exhausted: too many requests")
        == "PROVIDER_RATE_LIMITED"
    )
    # Quota wording wins over the 429 wording: a vendor that reports real
    # quota exhaustion with a 429 status still needs a human (USAGE_LIMIT,
    # agent blocked), while a bare window rate limit must not block.
    for exhausted in (
        '429 {"error":{"code":"insufficient_quota",'
        '"message":"You exceeded your current quota"}}',
        "Error 429: quota exceeded for model",
        "RESOURCE_EXHAUSTED: Quota exceeded for quota metric",
    ):
        assert classify_harness_failure(exhausted) == "PROVIDER_USAGE_LIMIT", exhausted
    # The rate-limit class is permanent for the delivery but never a reason
    # to block the agent: the window resets, so the next message must flow.
    assert harness_failure_is_permanent("PROVIDER_RATE_LIMITED") is True
    assert harness_failure_is_permanent("PROVIDER_USAGE_LIMIT") is True
    # A genuine network fault stays transient: the boundary must not widen
    # into every retryable-looking word.
    assert (
        classify_harness_failure("wire disconnected")
        == "HARNESS_TRANSIENT_FAILURE"
    )


def test_a_vendor_context_limit_rejection_is_named_for_what_it_is() -> None:
    """A conversation over the model's window is not a malformed request.

    2026-10-17 runtime incident: a Claude session on a model id CC cannot
    size ran to the vendor's 1,048,576 limit.  The rejection carries the
    vendor's own prose, usually wrapped in ``invalid_request_error``, so the
    generic rule caught it first and the failure was reported as
    PROVIDER_INVALID_REQUEST -- a code that reads as "your request was
    malformed" instead of "this conversation outgrew the window".  The
    wordings that name the limit are classified as the context exhaustion
    they are.

    The last check is the load-bearing half: CONTEXT_EXHAUSTED and
    PROVIDER_INVALID_REQUEST are both permanent, so this mapping cannot
    change delivery.  The settlement tests above are what prove the
    consequence (terminal on the first attempt, no replay).
    """

    for prose in (
        "API Error: 400 invalid_request_error: prompt is too long: "
        "1017000 tokens > 1048576 maximum",
        "This model's maximum context length is 1048576 tokens. However, "
        "you requested 1049000 tokens",
        "error: context_length_exceeded (requested 1049000 > 1048576)",
    ):
        code = classify_harness_failure(prose)
        assert code == "CONTEXT_EXHAUSTED", prose
        assert harness_failure_is_permanent(code)

    # The generic mapping is untouched for prose that does not name the limit.
    assert (
        classify_harness_failure("invalid_request_error: bad tool schema")
        == "PROVIDER_INVALID_REQUEST"
    )
    assert (
        classify_harness_failure("invalid request: context length unsupported")
        == "PROVIDER_INVALID_REQUEST"
    )


def test_context_exhaustion_is_terminal_on_the_first_delivery_attempt(
    tmp_path: Path,
) -> None:
    assert harness_failure_is_permanent("CONTEXT_EXHAUSTED")
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    message = _message("context-exhausted-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    try:
        settlement = inbox.settle_harness_failure(
            ACTOR,
            message.message_id,
            "CONTEXT_EXHAUSTED",
            permanent=harness_failure_is_permanent("CONTEXT_EXHAUSTED"),
            max_attempts=3,
            backoff_ms=(1_000, 5_000),
            now_ms=2,
        )

        assert settlement.attempts == 1
        assert settlement.max_attempts == 3
        assert settlement.terminal is True
        assert settlement.permanent is True
        assert settlement.next_attempt_ms is None
        assert settlement.terminal_reason == "CONTEXT_EXHAUSTED"
        assert len(inbox.harness_failure_attempts(message.message_id)) == 1
        assert inbox.dispatchable_messages(ACTOR, now_ms=10**15) == ()
    finally:
        inbox.close()


def test_context_exhaustion_raises_the_existing_fail_loud_notice(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    runtime = _bridge(tmp_path, inbox, harnesses, clock)
    message = _message("context-exhausted-loud-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 1
        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="pi context exhausted: stopReason=length; usage(output=0)",
                failure_code="CONTEXT_EXHAUSTED",
            )
        )

        assert runtime.on_timer().harness_results == 1
        assert runtime.on_timer().harness_deliveries == 0
        assert len(harnesses.dispatched) == 1
        settlement = inbox.harness_failure_settlement(message.message_id)
        assert settlement is not None
        assert settlement.terminal is True
        assert settlement.attempts == 1
        notices = [
            json.loads(item.message.payload)
            for item in inbox.outbox_items()
            if item.message.recipient == SENDER
        ]
        assert len(notices) == 1
        assert notices[0]["notification"] == "provider-unavailable"
        assert notices[0]["failureCode"] == "CONTEXT_EXHAUSTED"
        assert notices[0]["terminal"] is True
        assert notices[0]["attempts"] == 1
    finally:
        runtime.stop()
        inbox.close()


def test_permanent_failure_mints_terminal_tombstone_without_reply_ack_or_redispatch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database, max_inbox_items=1)
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    logs: list[tuple[str, str, str, dict[str, object]]] = []
    runtime = _bridge(tmp_path, inbox, harnesses, clock, logs)
    message = _message("permanent-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 1
        assert [item.delivery_id for item in harnesses.dispatched] == [message.message_id]

        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="You've hit your usage limit",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        outcome = runtime.on_timer()
        assert outcome.harness_results == 1
        assert outcome.harness_deliveries == 0
        assert runtime.on_timer().harness_deliveries == 0
        assert len(harnesses.dispatched) == 1

        settlement = inbox.harness_failure_settlement(message.message_id)
        assert settlement is not None
        assert settlement.failure_code == "PROVIDER_USAGE_LIMIT"
        assert settlement.cycle == 1
        assert settlement.attempts == 1
        assert settlement.terminal is True
        assert settlement.permanent is True
        assert settlement.terminal_reason == "PROVIDER_USAGE_LIMIT"
        assert settlement.terminal_at_ms == clock["now"]
        assert [item.failure_code for item in inbox.harness_failure_attempts(message.message_id)] == [
            "PROVIDER_USAGE_LIMIT"
        ]

        assert inbox.harness_failure_original(message.message_id) == message
        assert inbox.pending_all() == ()
        assert inbox.pending_messages(ACTOR) == ()
        assert inbox.dispatchable_messages(ACTOR, now_ms=10**15) == ()
        assert inbox.fetch_pending(ACTOR, now_ms=2_000) == ()
        assert inbox.next(ACTOR) is None
        assert inbox.pending_count(ACTOR) == 0
        assert inbox.pending_recipient_counts() == ()
        assert inbox.pending_recipient_stats() == ()
        assert inbox.consumption_state(message.message_id) is ConsumptionState.FAILED
        assert inbox.ack(ACTOR, message.message_id).acknowledged is False
        assert inbox.refresh_hold(message.message_id, now_ms=3_000) is False
        # While unexpired the sweep retains the row, tombstone or not; once
        # past the deadline it is evicted as TERMINAL_SETTLED (pinned in
        # test_inbox_ttl.py) -- the zombie accumulation the old assertion
        # here froze in is over.
        assert inbox.prune_inbox(now_ms=clock["now"]) == ()
        assert inbox.dlq_count() == 1
        # 2026-09-21: a terminal provider failure now also emits one fail-loud
        # notice to the waiting sender ("terminal" means the harness gave up;
        # it never meant the sender should be left uninformed).  The assertion
        # that matters is that the outbox holds exactly that notice -- no
        # fabricated business reply, and nothing addressed to the worker.
        outbox = [item.message for item in inbox.outbox_items()]
        assert len(outbox) == 1
        assert outbox[0].intent == "reply"
        assert outbox[0].recipient == message.sender
        assert b"provider-unavailable" in outbox[0].payload
        assert message.message_id.encode() in outbox[0].payload

        # Terminal evidence is retained but no longer consumes live inbox
        # capacity; otherwise enough permanent provider failures would turn
        # the tombstone requirement into a node-wide admission outage.
        capacity_probe = _message("capacity-after-terminal")
        assert inbox.receive(capacity_probe, now_ms=4_000).acknowledged
        assert inbox.pending_messages(ACTOR, now_ms=4_000) == (capacity_probe,)
        assert inbox.ack(ACTOR, capacity_probe.message_id).acknowledged

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM inbox WHERE message_id = ?",
                (message.message_id,),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT reason, payload FROM dlq WHERE message_id = ?",
                (message.message_id,),
            ).fetchone() == ("PROVIDER_USAGE_LIMIT", message.payload)

        terminal_logs = [
            fields
            for _level, _component, event, fields in logs
            if event == "harness.delivery.terminal_failed"
        ]
        assert terminal_logs == [
            {
                "messageId": message.message_id,
                "recipient": ACTOR,
                "failureCode": "PROVIDER_USAGE_LIMIT",
                "attempts": 1,
                "maxAttempts": 3,
                "permanent": True,
                "terminal": True,
                "terminalReason": "PROVIDER_USAGE_LIMIT",
            }
        ]

        # Reverse mutation: removing only the durable terminal bit makes the
        # retained row dispatchable again.  This proves the quiet next tick
        # above was caused by the tombstone, not by an in-memory harness dedup.
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE harness_failure_settlements "
                "SET terminal = 0, terminal_reason = NULL, terminal_at_ms = NULL "
                "WHERE message_id = ?",
                (message.message_id,),
            )
        assert runtime.on_timer().harness_deliveries == 1
        assert len(harnesses.dispatched) == 2
    finally:
        runtime.stop()
        inbox.close()


def test_transient_backoff_and_budget_survive_actor_and_daemon_rebuild(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    runtime = _bridge(tmp_path, inbox, harnesses, clock)
    message = _message("transient-restart-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 1
        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="connection reset",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        runtime.on_timer()
        first = inbox.harness_failure_settlement(message.message_id)
        assert first is not None and first.next_attempt_ms == 2_000
        assert runtime.on_timer().harness_deliveries == 0

        clock["now"] = 2_000
        assert runtime.on_timer().harness_deliveries == 1
        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error="connection reset again",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            )
        )
        runtime.on_timer()
        second = inbox.harness_failure_settlement(message.message_id)
        assert second is not None
        assert second.attempts == 2
        assert second.next_attempt_ms == 7_000
    finally:
        runtime.stop()
        inbox.close()

    # Rebuild both state actor and daemon bridge over the same SQLite file.
    inbox = _inbox(database)
    harnesses = _Harnesses()
    clock["now"] = 6_999
    runtime = _bridge(tmp_path, inbox, harnesses, clock)
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 0
        clock["now"] = 7_000
        assert runtime.on_timer().harness_deliveries == 1
        assert len(harnesses.dispatched) == 1

        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.COMPLETED,
                output="recovered",
            )
        )
        assert runtime.on_timer().harness_results == 1
        # WP5-A: a completed headless turn asks the gate instead of replying;
        # with no gate bound the hint is keep, so the row stays pending until
        # an explicit ack.  The successful turn does clear the non-terminal
        # retry settlement while the append-only attempt log survives.
        assert not inbox.is_acknowledged(message.message_id)
        assert (
            inbox.consumption_state(message.message_id) is ConsumptionState.PENDING
        )
        assert inbox.harness_failure_settlement(message.message_id) is None
        assert [(item.attempt, item.cycle) for item in inbox.harness_failure_attempts(message.message_id)] == [
            (1, 1),
            (2, 1),
        ]
        assert inbox.dlq_count() == 0

        # Message IDs may be reused only after the ordinary dedup window and
        # successful consumption.  The kept request is settled explicitly
        # (the carrier contract's ack path); the old evidence survives, but
        # its retry budget must not poison the new logical cycle.
        assert inbox.ack(ACTOR, message.message_id).acknowledged
        replacement = _message(message.message_id)
        assert inbox.receive(replacement, now_ms=90_000_000).acknowledged
        replacement_failure = inbox.settle_harness_failure(
            ACTOR,
            message.message_id,
            "HARNESS_TRANSIENT_FAILURE",
            permanent=False,
            max_attempts=3,
            backoff_ms=(1_000, 5_000),
            now_ms=90_000_001,
        )
        assert replacement_failure.cycle == 2
        assert replacement_failure.attempts == 1
        assert [
            (item.cycle, item.attempt)
            for item in inbox.harness_failure_attempts(message.message_id)
        ] == [(1, 1), (1, 2), (2, 1)]
    finally:
        runtime.stop()
        inbox.close()


def test_transient_budget_exhaustion_is_terminal_and_preserves_all_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = _inbox(database)
    message = _message("transient-exhausted-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    try:
        for now in (10, 20, 30):
            settlement = inbox.settle_harness_failure(
                ACTOR,
                message.message_id,
                "HARNESS_TRANSIENT_FAILURE",
                permanent=False,
                max_attempts=3,
                backoff_ms=(1_000, 5_000),
                now_ms=now,
            )
        assert settlement.terminal is True
        assert settlement.permanent is False
        assert settlement.attempts == 3
        assert settlement.terminal_reason == (
            "HARNESS_TRANSIENT_FAILURE_RETRY_EXHAUSTED"
        )
        assert [item.failed_at_ms for item in inbox.harness_failure_attempts(message.message_id)] == [
            10,
            20,
            30,
        ]
        assert inbox.harness_failure_original(message.message_id) == message
        assert inbox.pending_messages(ACTOR) == ()
        # The settlement is terminal, so the long-expired row is swept as
        # TERMINAL_SETTLED; the settlement row itself -- the durable
        # tombstone -- is retained.
        pruned = inbox.prune_inbox(now_ms=10**15)
        assert [item.message_id for item in pruned] == [message.message_id]
        assert pruned[0].reason == "TERMINAL_SETTLED"
        assert inbox.harness_failure_settlement(message.message_id) == settlement
        assert inbox.dlq_count() == 1

        # A replay after terminal is idempotent: no fourth audit row or DLQ.
        # The pruned-away inbox row does not change that -- the early return
        # keys on the retained settlement, not on the row.
        replay = inbox.settle_harness_failure(
            ACTOR,
            message.message_id,
            "HARNESS_TRANSIENT_FAILURE",
            permanent=False,
            max_attempts=99,
            backoff_ms=(1,),
            now_ms=40,
        )
        assert replay == settlement
        assert len(inbox.harness_failure_attempts(message.message_id)) == 3
        assert inbox.dlq_count() == 1
    finally:
        inbox.close()


def test_terminal_read_and_ack_semantics_survive_real_socket_daemon_rebuild() -> None:
    root = Path(tempfile.mkdtemp(prefix="hyprial-pfs-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    state.mkdir(parents=True)
    database = state / "inbox.sqlite3"
    message = _message("socket-rebuild-1")
    first = _inbox(database)
    assert first.receive(message, now_ms=1).acknowledged
    settlement = first.settle_harness_failure(
        ACTOR,
        message.message_id,
        "PROVIDER_AUTHENTICATION_FAILED",
        permanent=True,
        max_attempts=3,
        backoff_ms=(1_000, 5_000),
        now_ms=2,
    )
    assert settlement.terminal
    first.close()

    _desired(state)
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "d.sock",
        node_id="provider-failure-node",
        hyprial_home=root / "home",
    **_deep_module_daemon_dependencies())
    second = _inbox(database)
    app._inbox = second
    app._inbox_coordinator = second._coordinator
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    app._runtime = _NoopRuntime()  # type: ignore[assignment]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        pending = _ipc_call(
            app.socket_path,
            "message.pending.list",
            {"actor": ACTOR},
        )
        assert pending["result"]["messages"] == []
        acknowledged = _ipc_call(
            app.socket_path,
            "message.ack",
            {"actor": ACTOR, "messageId": message.message_id},
        )
        assert acknowledged["result"] == {
            "ok": False,
            "messageId": message.message_id,
            "acknowledged": False,
            "code": "MESSAGE_ACK_UNAVAILABLE",
        }
        ps = _ipc_call(app.socket_path, "ps", {})
        assert ps["result"]["pendingMessages"] == []
        assert second.harness_failure_original(message.message_id) == message
        assert second.consumption_state(message.message_id) is ConsumptionState.FAILED
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


def _fail(harnesses: _Harnesses, message: InboxMessage, code: str) -> None:
    harnesses.results.append(
        HarnessResult(
            message.message_id,
            ACTOR,
            HarnessResultStatus.FAILED,
            error="provider said no",
            failure_code=code,
        )
    )


def test_blocking_failures_are_classified_and_gate_automatic_retry(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    blocked: set[str] = set()
    observed: list[tuple[str, str]] = []

    def observe(actor: str, code: str) -> None:
        observed.append((actor, code))
        blocked.add(actor)

    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocking_failure_observer=observe,
        blocked_actor=lambda actor: actor in blocked,
    )
    message = _message("limited-block-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        runtime.on_timer()
        _fail(harnesses, message, "PROVIDER_USAGE_LIMIT")
        assert runtime.on_timer().harness_results == 1
        assert observed == [(ACTOR, "PROVIDER_USAGE_LIMIT")]
        next_message = _message("after-block-1")
        clock["now"] = time.time_ns() // 1_000_000
        assert inbox.receive(next_message, now_ms=clock["now"]).acknowledged
        assert [
            item.message_id
            for item in inbox.dispatchable_messages(ACTOR, now_ms=clock["now"])
        ] == [next_message.message_id]
        dispatches_before = len(harnesses.dispatched)
        runtime.on_timer()
        assert len(harnesses.dispatched) == dispatches_before

        blocked.clear()
        runtime.on_timer()
        assert len(harnesses.dispatched) == dispatches_before + 1
    finally:
        runtime.stop()
        inbox.close()


