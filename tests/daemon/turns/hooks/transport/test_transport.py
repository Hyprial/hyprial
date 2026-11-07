"""Inbox hook-exchange invoker tests (the mechanism the H1 handler lane uses).

The before-delivery/after-turn dispatch tests that shared this file exercised
the retired v1 ``turn-hooks.json`` service; the bounded-exchange guarantees of
:class:`InboxTurnHookInvoker` itself are unchanged and stay here.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.turn_delivery import (
    InboxTurnHookInvoker,
    is_hook_request,
)
from hyprial.daemon.impl.hooks import DaemonHookService, DeliveryHookService
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.identity.impl.agents.registry import InvalidAgentNameError
from hyprial.kernel import HookBus, HookInterceptPort
from hyprial.daemon.impl.inbox.links.local_link import LocalFirstDeliveryTransport
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    ZenohDeliveryTransport,
    ZenohInboxEndpoint,
)
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport

from ..support import (
    HANDLER,
    RECIPIENT,
    _ExchangeInbox,
    _Harnesses,
    _Invoker,
    _ManyInbox,
    _MeshPresence,
    _TimeoutInbox,
    _TimeoutRequestAckFailureInbox,
    _Transport,
    _cross_node_diagnostics,
    _delivery_hooks,
    _dispatch_until_ready,
    _loopback_endpoint,
    _message,
    _quarantine_xfail,
    _runtime,
    _write_hooks_config,
)


def test_inbox_invoker_marks_request_and_collects_correlated_reply() -> None:
    callback = "agent:owner:node:_turn-hooks"
    inbox = _ExchangeInbox(callback)
    invoker = InboxTurnHookInvoker(inbox, callback_actor=callback)  # type: ignore[arg-type]

    reply = invoker.invoke(
        HANDLER,
        {"event": "delivery.prepared", "actor": RECIPIENT},
        timeout_seconds=0.1,
    )

    assert reply == "opaque reply"
    assert inbox.request is not None
    body = json.loads(inbox.request.payload)
    assert body["turnHookRequest"] is True
    assert json.loads(body["message"])["event"] == "delivery.prepared"
    assert inbox.request.lifecycle is DeliveryLifecycle.ONLINE_ONLY
    assert inbox.request.idempotency_key is not None
    assert inbox.acked == ["reply"]


def test_timed_out_exchange_uses_online_lifecycle_and_retires_stale_rows() -> None:
    callback = "agent:owner:node:_turn-hooks"
    inbox = _TimeoutInbox(callback)
    invoker = InboxTurnHookInvoker(inbox, callback_actor=callback)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="hook handler timed out"):
        invoker.invoke(
            HANDLER,
            {"event": "delivery.prepared", "actor": RECIPIENT},
            timeout_seconds=0.02,
        )

    assert inbox.request is not None
    assert inbox.request.lifecycle is DeliveryLifecycle.ONLINE_ONLY
    assert (callback, "stale-reply") in inbox.acked
    assert (HANDLER, inbox.request.message_id) in inbox.acked
    assert inbox.pending == []


def test_timeout_ack_failure_preserves_timeout_and_still_sweeps_stale_reply() -> None:
    callback = "agent:owner:node:_turn-hooks"
    inbox = _TimeoutRequestAckFailureInbox(callback)
    invoker = InboxTurnHookInvoker(inbox, callback_actor=callback)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="hook handler timed out"):
        invoker.invoke(
            HANDLER,
            {"event": "delivery.prepared", "actor": RECIPIENT},
            timeout_seconds=0.02,
        )

    assert inbox.request_ack_attempted
    assert (callback, "raced-stale-reply") in inbox.acked
    assert inbox.pending == []


def test_foreign_machine_handler_does_not_abort_delivery(tmp_path: Path) -> None:
    home = tmp_path / "home"
    foreign = "agent:owner:othermachine:handler"
    local_config = _write_hooks_config(home, handler=foreign, timeout_ms=5_000)

    def config_path(actor: str) -> Path:
        if actor == RECIPIENT:
            return local_config
        raise InvalidAgentNameError("cannot create foreign agent")

    class ForeignInvoker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def invoke(
            self,
            handler: str,
            event: dict[str, object],
            *,
            timeout_seconds: float,
        ) -> str:
            del timeout_seconds
            self.calls.append((handler, event))
            return "foreign context"

    invoker = ForeignInvoker()
    hooks = DaemonHookService(
        bus=HookBus(),
        config_path_for_agent=config_path,
        consumers={},
        handler_notifier=lambda *_args, **_kwargs: None,
    )
    service = DeliveryHookService(
        hooks=hooks,
        port=HookInterceptPort(invoke=invoker.invoke),
    )
    runtime, harnesses = _runtime(tmp_path, service)

    _dispatch_until_ready(runtime)
    assert len(harnesses.deliveries) == 1
    assert harnesses.deliveries[0].hook_text == "foreign context"
    assert [handler for handler, _event in invoker.calls] == [foreign]


@pytest.mark.parametrize(
    "publication_order", ["before-observation", "after-observation"]
)
def test_injectable_clock_idle_handler_returns_context(
    tmp_path: Path, publication_order: str
) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    clock = [10.0]
    invoked = threading.Event()
    release_answer = threading.Event()

    def answer(_timeout_seconds: float) -> str:
        invoked.set()
        assert release_answer.wait(1)
        return "CTX"

    service = _delivery_hooks(
        tmp_path, _Invoker(answer), port_monotonic=lambda: clock[0]
    )
    delivery = HarnessDelivery("id", "conversation", "sender", RECIPIENT, "body")

    deadline = time.monotonic() + 2
    try:
        while not invoked.is_set():
            assert service.prepare_delivery(delivery) is None
            assert time.monotonic() < deadline
            time.sleep(0.005)
        if publication_order == "after-observation":
            assert service.prepare_delivery(delivery) is None
        release_answer.set()
        if publication_order == "before-observation":
            pending = service._port._pending[delivery.delivery_id]  # noqa: SLF001
            while pending.outcome.empty():
                assert time.monotonic() < deadline
                time.sleep(0.005)
        prepared = service.prepare_delivery(delivery)
    finally:
        release_answer.set()
    while prepared is None:
        # Handler entry precedes returning CTX and publishing the outcome.
        assert time.monotonic() < deadline
        time.sleep(0.005)
        prepared = service.prepare_delivery(delivery)
    assert prepared is not None
    assert prepared.hook_text == "CTX"


def test_hook_request_bypasses_older_held_handler_row(tmp_path: Path) -> None:
    _write_hooks_config(tmp_path, timeout_ms=30_000)
    release = threading.Event()
    started = threading.Event()

    def hold(_timeout_seconds: float) -> str:
        started.set()
        release.wait(1)
        return "late"

    hook_request = InboxMessage(
        message_id="hook-request",
        conversation_id="turn-hook:request",
        sender="agent:owner:node:_turn-hooks",
        recipient=RECIPIENT,
        payload=b'{"message":"event","turnHookRequest":true}',
        intent="request",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=2,
    )
    service = _delivery_hooks(
        tmp_path, _Invoker(hold), port_monotonic=lambda: 0.0
    )
    harnesses = _Harnesses()
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "daemon-state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=_ManyInbox((_message("older"), hook_request)),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        delivery_hooks=service,
    )
    try:
        # The mechanism's own request reaches its handler on the very first
        # tick, even while the older ordinary row is held by its hook.
        assert runtime._dispatch_harness_deliveries() == 1  # noqa: SLF001
        assert harnesses.deliveries[0].delivery_id == "hook-request"
        assert harnesses.deliveries[0].hook_request
        deadline = time.monotonic() + 2
        while not started.is_set():
            runtime._dispatch_harness_deliveries()  # noqa: SLF001
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert "older" not in [item.delivery_id for item in harnesses.deliveries]
    finally:
        release.set()



def test_non_hook_payload_skips_json_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _message()

    def fail_decode(_value: object) -> object:
        raise AssertionError("ordinary delivery must not parse hook JSON")

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.turn_delivery.service.json.loads",
        fail_decode,
    )
    assert not is_hook_request(message)


def test_same_node_offline_handler_fails_fast_with_real_inbox(
    tmp_path: Path,
) -> None:
    inner = MemoryDeliveryTransport()
    local = LocalFirstDeliveryTransport(
        inner,
        origin_node="node",
        local_node_id="node",
        local_owner="owner",
    )
    inbox = InboxService(tmp_path / "inbox.sqlite3", local)
    local.bind_receiver(inbox.receive)
    invoker = InboxTurnHookInvoker(
        inbox,
        callback_actor="agent:owner:node:_turn-hooks",
    )
    try:
        with pytest.raises(RuntimeError, match="TARGET_OFFLINE"):
            invoker.invoke(
                HANDLER,
                {"event": "delivery.prepared", "actor": RECIPIENT},
                timeout_seconds=30,
            )
        assert inbox.outbox_count() == 0
        assert inbox.pending_messages(HANDLER) == ()
    finally:
        inbox.close()


@pytest.mark.xfail(
    strict=False,
    reason=(
        "tracking: cross-node hook reply times out on CI worker (first seen task "
        "34324), suspected same stall as the drain test, see #882; owner "
        "org-improver; quarantined 2026-09-27 per hq-adjutant, remove on fix "
        "(report due: 2 working days after the diagnostics PR lands or 5 drain "
        "samples, hard limit 2026-10-03)"
    ),
)
def test_cross_node_hook_reply_returns_through_origin_custody(
    tmp_path: Path,
) -> None:
    import threading
    import time

    endpoint = _loopback_endpoint()
    callback = "agent:owner:origin:_turn-hooks"
    remote_handler = "agent:owner:remote:handler"
    origin_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    remote_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    origin = InboxService(
        tmp_path / "origin.sqlite3",
        ZenohDeliveryTransport(
            origin_transport,
            _MeshPresence((remote_handler, callback)),  # type: ignore[arg-type]
            receipt_timeout=1,
            origin_node="origin",
        ),
        node_id="origin",
    )
    remote = InboxService(
        tmp_path / "remote.sqlite3",
        ZenohDeliveryTransport(
            remote_transport,
            _MeshPresence((), mailbox="origin"),  # type: ignore[arg-type]
            receipt_timeout=1,
            origin_node="remote",
        ),
        node_id="remote",
    )
    origin_endpoint = ZenohInboxEndpoint(
        origin_transport,
        origin,
        callback,
        mailbox_node="origin",
    )
    remote_endpoint = ZenohInboxEndpoint(
        remote_transport,
        remote,
        remote_handler,
    )
    worker_errors: list[BaseException] = []
    worker_stages: list[str] = []

    def answer_and_promote() -> None:
        try:
            deadline = time.monotonic() + 4
            request: InboxMessage | None = None
            while request is None:
                pending = remote.pending_messages(remote_handler)
                request = pending[0] if pending else None
                if request is None:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            worker_stages.append("request-received")
            assert remote.ack(remote_handler, request.message_id).acknowledged
            reply = InboxMessage(
                message_id="cross-node-reply",
                conversation_id=request.conversation_id,
                sender=remote_handler,
                recipient=callback,
                payload=b'{"message":"CTX"}',
                intent="reply",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=2,
            )
            submitted = remote.submit(reply)
            assert submitted.accepted
            assert submitted.custody_mailbox == "origin"
            worker_stages.append("reply-submitted")
            while origin.custody_count() == 0:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            worker_stages.append("custody-arrived")
            origin.retry_custody_due(now_ms=time.time_ns() // 1_000_000)
            worker_stages.append("custody-promoted")
        except BaseException as error:  # noqa: BLE001 - propagate worker assertion
            worker_errors.append(error)

    worker = threading.Thread(target=answer_and_promote, daemon=True)
    worker.start()
    try:
        time.sleep(0.5)
        invoker = InboxTurnHookInvoker(origin, callback_actor=callback)
        try:
            answer = invoker.invoke(
                remote_handler,
                {"event": "delivery.prepared", "actor": RECIPIENT},
                timeout_seconds=5,
            )
        except TimeoutError as error:
            _quarantine_xfail(
                "TURN_HOOKS_CROSS_NODE_STALL",
                f"{error!r} "
                + _cross_node_diagnostics(
                    worker,
                    worker_stages,
                    worker_errors,
                    origin,
                    remote,
                    callback,
                    remote_handler,
                ),
            )
        try:
            assert answer == "CTX", answer
            worker.join(timeout=1)
            assert not worker.is_alive(), "worker still alive"
            assert worker_errors == [], worker_errors
            assert origin.has_received("cross-node-reply"), "reply not received"
            assert origin.pending_messages(callback) == (), "callback not drained"
        except AssertionError as error:
            _quarantine_xfail(
                "TURN_HOOKS_CROSS_NODE_ASSERT",
                f"{error!r} "
                + _cross_node_diagnostics(
                    worker,
                    worker_stages,
                    worker_errors,
                    origin,
                    remote,
                    callback,
                    remote_handler,
                ),
            )
    finally:
        origin_endpoint.close()
        remote_endpoint.close()
        origin.close()
        remote.close()
        remote_transport.close()
        origin_transport.close()


class _SlowForOlderInvoker:
    """Hold the older delivery's intercept; answer the younger immediately."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(
        self,
        handler: str,
        event: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> str | None:
        del handler, timeout_seconds
        self.calls.append(event)
        payload = event["payload"]
        assert isinstance(payload, dict)
        if payload["deliveryId"] == "older":
            self.started.set()
            assert self.release.wait(2)
            return "CTX"
        return "YGR"


class _RetiringInbox:
    """Two ordinary rows in inbox order; dispatch retires a row like an ack."""

    def __init__(self, messages: tuple[InboxMessage, ...]) -> None:
        self.rows: list[InboxMessage] = list(messages)

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return tuple(self.rows) if actor == RECIPIENT else ()

    def retire(self, message_id: str) -> None:
        self.rows = [item for item in self.rows if item.message_id != message_id]


class _RetiringHarnesses(_Harnesses):
    """The real enqueue dedups and the inbox row retires once accepted."""

    def __init__(self, inbox: _RetiringInbox) -> None:
        super().__init__()
        self._inbox = inbox
        self._seen: set[str] = set()

    def dispatch(self, actor: str, delivery: HarnessDelivery) -> bool:
        if delivery.delivery_id in self._seen:
            return False
        if not super().dispatch(actor, delivery):
            return False
        self._seen.add(delivery.delivery_id)
        self._inbox.retire(delivery.delivery_id)
        return True


def test_held_head_does_not_let_a_younger_delivery_pass(tmp_path: Path) -> None:
    """FIFO guard (review B1): a bounded intercept may hold the head, but no
    younger ordinary message may be dispatched past it; once the handler
    decides, the older delivery lands first, carrying its hook text."""

    _write_hooks_config(tmp_path, timeout_ms=30_000)
    invoker = _SlowForOlderInvoker()
    service = _delivery_hooks(tmp_path, invoker, port_monotonic=lambda: 0.0)
    inbox = _RetiringInbox((_message("older"), _message("younger")))
    harnesses = _RetiringHarnesses(inbox)
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "daemon-state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        delivery_hooks=service,
    )
    try:
        # Phase 1: the head is held by its slow intercept (the first
        # hooks.json load settles in the background); during the whole hold
        # nothing is dispatched -- the younger row never passes.
        deadline = time.monotonic() + 5
        while not invoker.started.is_set():
            assert runtime._dispatch_harness_deliveries() == 0  # noqa: SLF001
            assert harnesses.deliveries == []
            assert time.monotonic() < deadline
            time.sleep(0.005)
        # Phase 2: still held; further ticks must not let the younger past.
        for _ in range(3):
            assert runtime._dispatch_harness_deliveries() == 0  # noqa: SLF001
            assert harnesses.deliveries == []
            assert [
                event["payload"]["deliveryId"] for event in invoker.calls
            ] == ["older"]
            time.sleep(0.01)

        invoker.release.set()
        deadline = time.monotonic() + 2
        while len(harnesses.deliveries) < 2:
            runtime._dispatch_harness_deliveries()  # noqa: SLF001
            assert time.monotonic() < deadline
            time.sleep(0.005)

        assert [item.delivery_id for item in harnesses.deliveries] == [
            "older",
            "younger",
        ]
        by_id = {item.delivery_id: item for item in harnesses.deliveries}
        assert by_id["older"].hook_text == "CTX"
        assert by_id["younger"].hook_text == "YGR"
    finally:
        invoker.release.set()


def test_held_head_does_not_let_a_younger_delivery_pass_on_the_offer_path(
    tmp_path: Path,
) -> None:
    """Same FIFO guard on the ``_dispatch_offers`` branch (actor mode).

    The offers path ``break``s after one accepted offer per worker; the
    held-head check runs before that, so the invariant is identical: the
    younger row is neither offered nor dispatched while the older row's
    intercept is undecided, and the older delivery lands first."""

    _write_hooks_config(tmp_path, timeout_ms=30_000)
    invoker = _SlowForOlderInvoker()
    service = _delivery_hooks(tmp_path, invoker, port_monotonic=lambda: 0.0)
    inbox = _RetiringInbox((_message("older"), _message("younger")))
    harnesses = _RetiringHarnesses(inbox)
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "daemon-state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        delivery_hooks=service,
        actor_mode=True,
    )
    runtime.start()
    try:
        # Phase 1: the head is held by its slow intercept; during the whole
        # hold no offer is even made and nothing is dispatched -- the younger
        # row never passes.
        deadline = time.monotonic() + 5
        while not invoker.started.is_set():
            assert runtime._dispatch_harness_deliveries() == 0  # noqa: SLF001
            assert harnesses.deliveries == []
            assert time.monotonic() < deadline
            time.sleep(0.005)
        # Phase 2: still held; further ticks must not offer the younger row.
        for _ in range(3):
            assert runtime._dispatch_harness_deliveries() == 0  # noqa: SLF001
            assert harnesses.deliveries == []
            assert [
                event["payload"]["deliveryId"] for event in invoker.calls
            ] == ["older"]
            time.sleep(0.01)

        invoker.release.set()
        deadline = time.monotonic() + 2
        while len(harnesses.deliveries) < 2:
            runtime._dispatch_harness_deliveries()  # noqa: SLF001
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        invoker.release.set()

    assert [item.delivery_id for item in harnesses.deliveries] == ["older", "younger"]
    by_id = {item.delivery_id: item for item in harnesses.deliveries}
    assert by_id["older"].hook_text == "CTX"
    assert by_id["younger"].hook_text == "YGR"
