from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.state.worker_state import BlockingFailureAuthority

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    classify_harness_failure,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.runtime.ports import ClaimedHarnessResult
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
        self.settled_results: list[tuple[str, str]] = []

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

    def settle_result(self, claim_token: str, delivery_id: str) -> bool:
        self.settled_results.append((claim_token, delivery_id))
        return True


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
        # ACTOR->ACTOR dispatch is gated; keep this preservation suite about
        # provider failure settlement with a permissive premise (wp4 T13).
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
    blocking_failure_identity: Any = None,
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
        blocking_failure_identity=blocking_failure_identity,
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
    # A 429 is terminal for the current message but never blocks the agent.
    assert (
        classify_harness_failure("HTTP 429 rate_limit: try again later")
        == "PROVIDER_RATE_LIMITED"
    )
    # The boundary must not widen into every retryable-looking word.
    assert (
        classify_harness_failure("wire disconnected")
        == "HARNESS_TRANSIENT_FAILURE"
    )


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
        # Hard cut (WP5-A, rulings 58/59): a completed turn no longer acks the
        # request on the model's behalf.  With no link gate bound the settle
        # hint is ``keep``; the row stays pending until the model answers or
        # acks it.  Here the model acks, which is what consumes the row.
        assert not inbox.is_acknowledged(message.message_id)
        assert inbox.ack(ACTOR, message.message_id).acknowledged
        assert inbox.is_acknowledged(message.message_id)
        assert inbox.consumption_state(message.message_id) is ConsumptionState.CONSUMED
        # WP5-A: a completed turn drops the retry eligibility (the non-terminal
        # settlement row); the append-only attempt log is the evidence that
        # survives, and it still seeds the next logical cycle below.
        assert inbox.harness_failure_settlement(message.message_id) is None
        assert [item.attempt for item in inbox.harness_failure_attempts(message.message_id)] == [
            1,
            2,
        ]
        assert inbox.dlq_count() == 0

        # Message IDs may be reused only after the ordinary dedup window and
        # successful consumption.  The old evidence survives, but its retry
        # budget must not poison the new logical cycle.
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
    root = Path(tempfile.mkdtemp(prefix="hyprial-pfs-", dir="/tmp"))
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


def _wait_for(predicate: Any, *, timeout: float) -> bool:
    """Give an asynchronous effect the same grace production gives it."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_blocking_failure_handoff_retained_until_agent_owner_ack(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    calls: list[tuple[str, str, str]] = []
    blocked: set[str] = set()
    identity = {"token": "entity-before-overload"}

    def observe(actor: str, code: str, entity_token: str) -> AdmissionResult:
        calls.append((actor, code, entity_token))
        if len(calls) == 1:
            return AdmissionResult.OVERLOADED
        blocked.add(actor)
        return AdmissionResult.ACCEPTED

    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocking_failure_observer=observe,
        blocking_failure_identity=lambda _actor: identity["token"],
        blocked_actor=blocked.__contains__,
    )
    message = _message("limited-block")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        runtime.on_timer()
        identity["token"] = "entity-after-dispatch"
        _fail(harnesses, message, "PROVIDER_USAGE_LIMIT")
        assert runtime.on_timer().harness_results == 1
        assert calls == [
            (ACTOR, "PROVIDER_USAGE_LIMIT", "entity-before-overload")
        ]
        identity["token"] = "entity-after-overload"
        runtime.on_timer()
        assert calls == [
            (ACTOR, "PROVIDER_USAGE_LIMIT", "entity-before-overload"),
            (ACTOR, "PROVIDER_USAGE_LIMIT", "entity-before-overload"),
        ]
        assert ACTOR in blocked

        next_message = _message("after-block")
        clock["now"] = time.time_ns() // 1_000_000
        assert inbox.receive(next_message, now_ms=clock["now"]).acknowledged
        before = len(harnesses.dispatched)
        runtime.on_timer()
        assert len(harnesses.dispatched) == before
    finally:
        runtime.stop()
        inbox.close()


def test_rate_limited_failure_settles_terminal_without_blocking_the_agent(
    tmp_path: Path,
) -> None:
    """A 429 stops THIS message; it must never disable the agent.

    The window resets on its own, so the failure is the delivery's, not the
    agent's: the current message spends one attempt and one notice and is not
    redelivered (a 429 cannot heal inside the redelivery window and a second
    turn would multiply the provider calls the runtime just refused to
    repeat), while the next message still reaches the worker.  A registry
    block has no expiry -- only ``agent unblock`` clears it -- so mapping a
    429 onto it would silence the agent until a human intervened.
    """

    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="op",
        machine="provider-failure-node",
        hyprial_home=tmp_path / "home",
    )
    registry.create("worker", cwd=str(tmp_path))
    owner_notices: list[tuple[str, str]] = []
    authority = BlockingFailureAuthority(
        block=lambda recipient, reason, expected_token: registry.block_agent(
            recipient, reason=reason, expected_entity_token=expected_token
        ),
        notify=lambda text, *, idempotency_key: (
            owner_notices.append((text, idempotency_key))
            or SimpleNamespace(delivered=True)
        ),
    )

    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocking_failure_observer=authority.submit,
        blocking_failure_identity=lambda actor: (
            item.entity_token
            if (item := registry.get(actor)) is not None
            else None
        ),
        blocked_actor=registry.is_blocked,
    )
    message = _message("rate-limited-1")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 1
        assert [item.delivery_id for item in harnesses.dispatched] == [
            message.message_id
        ]
        harnesses.results.append(
            HarnessResult(
                message.message_id,
                ACTOR,
                HarnessResultStatus.FAILED,
                error='429: {"type":"rate_limit_error","message":"slow down"}',
            )
        )
        assert runtime.on_timer().harness_results == 1

        settlement = inbox.harness_failure_settlement(message.message_id)
        assert settlement is not None
        assert settlement.failure_code == "PROVIDER_RATE_LIMITED"
        assert settlement.terminal is True
        assert settlement.attempts == 1
        assert settlement.permanent is True
        assert settlement.terminal_reason == "PROVIDER_RATE_LIMITED"
        assert [
            item.failure_code
            for item in inbox.harness_failure_attempts(message.message_id)
        ] == ["PROVIDER_RATE_LIMITED"]
        outbox = [item.message for item in inbox.outbox_items()]
        assert len(outbox) == 1
        assert b"provider-unavailable" in outbox[0].payload
        assert b"PROVIDER_RATE_LIMITED" in outbox[0].payload
        # Terminal: nothing is redelivered and the provider is not called
        # again for this message.
        assert runtime.on_timer().harness_deliveries == 0
        assert len(harnesses.dispatched) == 1

        # Give an asynchronous block the chance production would give it and
        # prove the rate-limited code never reaches the blocking authority.
        assert _wait_for(lambda: registry.is_blocked(ACTOR), timeout=0.5) is False
        assert owner_notices == []

        # The agent was not disabled: the next message flows on the same tick.
        next_message = _message("rate-limited-next")
        clock["now"] = time.time_ns() // 1_000_000
        assert inbox.receive(next_message, now_ms=clock["now"]).acknowledged
        dispatched_before = len(harnesses.dispatched)
        assert runtime.on_timer().harness_deliveries == 1
        assert len(harnesses.dispatched) == dispatched_before + 1
    finally:
        runtime.stop()
        authority.close(1.0)
        inbox.close()
        registry.close()


def test_blocking_failure_capacity_retains_parent_claim_before_settlement(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    accepting = {"value": False}
    calls: list[tuple[str, str, str]] = []

    def observe(actor: str, code: str, entity_token: str) -> AdmissionResult:
        calls.append((actor, code, entity_token))
        return (
            AdmissionResult.ACCEPTED
            if accepting["value"]
            else AdmissionResult.OVERLOADED
        )

    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocking_failure_observer=observe,
        blocking_failure_identity=lambda _actor: "target-entity",
    )
    runtime._blocking_failure_capacity = 1
    message = _message("limited-capacity")
    assert inbox.receive(message, now_ms=1).acknowledged
    runtime.start()
    try:
        runtime.on_timer()
        assert runtime._observe_blocking_failure(
            "agent:op:provider-failure-node:occupied",
            "PROVIDER_USAGE_LIMIT",
            "occupied-entity",
        ) is AdmissionResult.ACCEPTED
        assert len(runtime._pending_blocking_failures) == 1

        _fail(harnesses, message, "PROVIDER_USAGE_LIMIT")
        result = harnesses.results.pop()
        claim = ClaimedHarnessResult("claim-1", "worker", 1, result)
        assert runtime._settle_claimed_result(claim) is False
        assert harnesses.settled_results == []
        assert message.message_id in runtime._inflight
        assert inbox.harness_failure_attempts(message.message_id) == ()

        accepting["value"] = True
        runtime._retry_blocking_failures()
        assert runtime._pending_blocking_failures == {}
        assert runtime._settle_claimed_result(claim) is True
        assert harnesses.settled_results == [
            ("claim-1", message.message_id)
        ]
        attempts = inbox.harness_failure_attempts(message.message_id)
        assert len(attempts) == 1
        assert attempts[0].failure_code == "PROVIDER_USAGE_LIMIT"
        assert calls[-1] == (
            ACTOR,
            "PROVIDER_USAGE_LIMIT",
            "target-entity",
        )
    finally:
        runtime.stop()
        inbox.close()


def test_stop_retains_overloaded_blocking_failure_until_transfer(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": 1_000}
    capacity_released = {"value": False}
    blocks: list[tuple[str, str, str]] = []
    notices: list[tuple[str, str]] = []

    def block(recipient: str, reason: str, entity_token: str):
        blocks.append((recipient, reason, entity_token))
        return (
            SimpleNamespace(
                actor="worker",
                entity_token=entity_token,
                uri=recipient,
            ),
            True,
            reason,
        )

    def notify(text: str, *, idempotency_key: str):
        notices.append((text, idempotency_key))
        return SimpleNamespace(delivered=True)

    authority = BlockingFailureAuthority(
        block=block,
        notify=notify,
        capacity=1,
    )

    def observe(
        recipient: str, code: str, entity_token: str
    ) -> AdmissionResult:
        if not capacity_released["value"]:
            return AdmissionResult.OVERLOADED
        return authority.submit(recipient, code, entity_token)

    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocking_failure_observer=observe,
        blocking_failure_identity=lambda _actor: "entity-1",
    )
    runtime.start()
    try:
        assert runtime._observe_blocking_failure(
            ACTOR,
            "PROVIDER_USAGE_LIMIT",
            "entity-1",
        ) is AdmissionResult.ACCEPTED
        assert len(runtime._pending_blocking_failures) == 1

        with pytest.raises(
            RuntimeError,
            match="accepted blocking failure observations did not transfer",
        ):
            runtime.stop(timeout=0.02)
        assert runtime._started
        assert len(runtime._pending_blocking_failures) == 1
        assert blocks == []
        assert notices == []

        capacity_released["value"] = True
        runtime.stop(timeout=1.0)
        assert not runtime._started
        assert runtime._pending_blocking_failures == {}
        assert authority.close(1.0)
        assert blocks == [(ACTOR, "provider-quota", "entity-1")]
        assert len(notices) == 1
        assert notices[0][1] == "agent-blocked:entity-1:provider-quota"
    finally:
        capacity_released["value"] = True
        if runtime._started:
            runtime.stop(timeout=1.0)
        authority.close(1.0)
        inbox.close()


def test_block_projection_gates_dispatch_and_unblock_releases_without_sqlite(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = _Harnesses()
    clock = {"now": time.time_ns() // 1_000_000}
    blocked = {ACTOR}
    runtime = _bridge(
        tmp_path,
        inbox,
        harnesses,
        clock,
        blocked_actor=blocked.__contains__,
    )
    message = _message("blocked-projection")
    assert inbox.receive(message, now_ms=clock["now"]).acknowledged
    runtime.start()
    try:
        runtime.on_timer()
        assert harnesses.dispatched == []
        blocked.clear()
        runtime.on_timer()
        assert [item.delivery_id for item in harnesses.dispatched] == [
            message.message_id
        ]
    finally:
        runtime.stop()
        inbox.close()


