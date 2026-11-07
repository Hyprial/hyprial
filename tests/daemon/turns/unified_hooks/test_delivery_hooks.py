"""delivery.* events and the delivery.prepared intercept (design §4, H3).

Acceptance pinned here:
- an intercept that times out, errors or replies empty FAILS OPEN — the
  message is delivered unchanged;
- a non-empty intercept reply appears verbatim as the delivery's hook text;
- with no hooks.json the behaviour is byte-identical and nothing publishes;
- a slow observer never delays delivery.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    delivery_prompt,
)
from hyprial.daemon.impl.hooks import (
    DaemonHookService,
    DeliveryHookService,
    consumer_intercept_context,
    harness_supports_delivery_intercept,
)
from hyprial.kernel import HookBus, HookEvent, HookInterceptPort

from ..hooks.support import (
    HANDLER,
    RECIPIENT,
    _close_owned_hook_services as _close_owned_hook_services,
    _delivery_hooks,
    _Invoker,
    _prepare_until_decided,
    _write_hooks_config,
)


def _delivery(
    delivery_id: str = "delivery-1", message: str = "original body"
) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id,
        "conversation-1",
        "agent:owner:node:sender",
        RECIPIENT,
        message,
    )


def _result(
    delivery_id: str = "delivery-1",
    status: HarnessResultStatus = HarnessResultStatus.COMPLETED,
    failure_code: str | None = None,
) -> HarnessResult:
    return HarnessResult(delivery_id, RECIPIENT, status, failure_code=failure_code)


class _Recorder:
    def __init__(self, release: threading.Event | None = None) -> None:
        self.events: list[HookEvent] = []
        self.release = release

    def observe(self, event: HookEvent, _options: dict[str, object]) -> None:
        self.events.append(event)
        if self.release is not None:
            assert self.release.wait(5.0)


def _wait_until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _observer_service(
    home: Path,
    invoker: _Invoker,
    consumer: _Recorder,
    *,
    entries: list[dict[str, object]],
) -> DeliveryHookService:
    _write_hooks_config(home, entries=entries)
    return _delivery_hooks(home, invoker, consumers={"test": consumer})


def test_intercept_reply_appears_verbatim(tmp_path: Path) -> None:
    opaque = "  first line\n<opaque>& unchanged  "
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    invoker = _Invoker(opaque)
    service = _delivery_hooks(tmp_path, invoker)
    delivery = _delivery()

    prepared = _prepare_until_decided(service, delivery)
    assert prepared.hook_text == opaque
    assert delivery_prompt(prepared).endswith(
        "<<<HYPRIAL-HOOK>>>\n" + opaque + "\n<<<END-HYPRIAL-HOOK>>>"
    )
    assert len(invoker.calls) == 1


@pytest.mark.parametrize(
    "behavior", ["", None, RuntimeError("handler exploded")]
)
def test_error_and_empty_replies_fail_open(
    tmp_path: Path, behavior: object
) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    service = _delivery_hooks(tmp_path, _Invoker(behavior))
    delivery = _delivery()

    prepared = _prepare_until_decided(service, delivery)
    assert prepared == delivery
    assert prepared.hook_text is None


def test_timeout_fails_open_and_never_blocks_the_caller(tmp_path: Path) -> None:
    _write_hooks_config(tmp_path, timeout_ms=25)
    release = threading.Event()

    def slow(_timeout_seconds: float) -> str:
        release.wait(2)
        return "late"

    service = _delivery_hooks(tmp_path, _Invoker(slow))
    delivery = _delivery()
    try:
        started = time.monotonic()
        prepared = _prepare_until_decided(service, delivery)
        assert time.monotonic() - started < 1.0
        assert prepared == delivery
        assert prepared.hook_text is None
    finally:
        release.set()


def test_no_hooks_config_is_byte_identical_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    invoker = _Invoker("must not run")
    consumer = _Recorder()
    service = _delivery_hooks(tmp_path, invoker, consumers={"test": consumer})
    delivery = _delivery()
    baseline = delivery_prompt(delivery).encode()

    prepared = _prepare_until_decided(service, delivery)
    assert prepared is delivery
    service.mark_dispatched(delivery.delivery_id)
    service.settled(_result())
    assert delivery_prompt(prepared).encode() == baseline
    assert invoker.calls == []
    time.sleep(0.05)
    assert consumer.events == []
    assert sum(row.accepted for row in service._hooks._bus.projection()) == 0  # noqa: SLF001


def test_delivery_events_reach_an_observer_with_the_documented_payload(
    tmp_path: Path,
) -> None:
    invoker = _Invoker("must not run")
    consumer = _Recorder()
    service = _observer_service(
        tmp_path,
        invoker,
        consumer,
        entries=[
            {
                "events": [
                    "delivery.prepared",
                    "delivery.dispatched",
                    "delivery.settled",
                ],
                "consumer": "test",
            }
        ],
    )
    delivery = _delivery(message="password=hunter2 /private/prompt.txt")

    prepared = _prepare_until_decided(service, delivery)
    assert prepared is delivery  # observe-only: delivery itself is untouched
    service.mark_dispatched(delivery.delivery_id)
    service.settled(
        _result(status=HarnessResultStatus.FAILED, failure_code="HARNESS_TRANSIENT_FAILURE")
    )
    _wait_until(lambda: len(consumer.events) == 3)
    prepared_event, dispatched, settled = consumer.events
    assert [event.event for event in consumer.events] == [
        "delivery.prepared",
        "delivery.dispatched",
        "delivery.settled",
    ]
    assert all(event.source == "daemon.delivery" for event in consumer.events)
    assert prepared_event.payload["deliveryId"] == "delivery-1"
    assert prepared_event.payload["sender"] == "agent:owner:node:sender"
    assert prepared_event.payload["conversation"] == "conversation-1"
    # The observe payload is bounded and redacted like turn excerpts.
    assert prepared_event.payload["messageExcerpt"] == "password=<redacted> <path>"
    assert prepared_event.payload["collectionMethod"] == "daemon"
    assert prepared_event.payload["fidelity"] == "observed"
    assert "messageExcerpt" not in dispatched.payload
    assert settled.payload["outcome"] == "failed"
    assert settled.payload["failureCode"] == "HARNESS_TRANSIENT_FAILURE"
    assert settled.payload["sender"] == "agent:owner:node:sender"
    # Settlement released the delivery's hook state.
    assert service._contexts == {}  # noqa: SLF001


def test_prepared_event_is_emitted_once_per_delivery_across_ticks(
    tmp_path: Path,
) -> None:
    consumer = _Recorder()
    service = _observer_service(
        tmp_path,
        _Invoker("must not run"),
        consumer,
        entries=[{"events": ["delivery.prepared"], "consumer": "test"}],
    )
    delivery = _delivery()
    _prepare_until_decided(service, delivery)
    for _tick in range(5):
        assert service.prepare_delivery(delivery) is delivery
    _wait_until(lambda: len(consumer.events) == 1)
    time.sleep(0.05)
    assert len(consumer.events) == 1


def test_blocking_observer_never_delays_delivery(tmp_path: Path) -> None:
    release = threading.Event()
    consumer = _Recorder(release)
    service = _observer_service(
        tmp_path,
        _Invoker("must not run"),
        consumer,
        entries=[
            {
                "events": ["delivery.prepared", "delivery.dispatched"],
                "consumer": "test",
            }
        ],
    )
    delivery = _delivery()
    try:
        started = time.monotonic()
        prepared = _prepare_until_decided(service, delivery)
        service.mark_dispatched(delivery.delivery_id)
        elapsed = time.monotonic() - started
        assert prepared is delivery
        assert elapsed < 0.75, f"delivery waited {elapsed:.3f}s for an observer"
        assert not release.is_set()
    finally:
        release.set()


def test_intercept_request_document_shape_and_options_passthrough(
    tmp_path: Path,
) -> None:
    _write_hooks_config(
        tmp_path, timeout_ms=5_000, options={"recentRecaps": 5}
    )
    invoker = _Invoker("context")
    service = _delivery_hooks(tmp_path, invoker)
    delivery = _delivery(message="password=hunter2 full body")

    _prepare_until_decided(service, delivery)
    assert len(invoker.calls) == 1
    document = invoker.calls[0]
    assert document["event"] == "delivery.prepared"
    assert document["mode"] == "intercept"
    assert document["schemaVersion"] == 2
    assert document["actor"] == RECIPIENT
    assert document["options"] == {"recentRecaps": 5}
    # No provider is registered: the seam yields an empty context, never a
    # blocked delivery (H2's recap consumer fills it at integration).
    assert document["context"] == {}
    # The configured handler composes context for the real delivery, so the
    # request carries the message verbatim, unlike the bounded observe event.
    assert document["payload"]["message"] == "password=hunter2 full body"
    assert document["payload"]["deliveryId"] == "delivery-1"


def test_registered_context_provider_fills_the_intercept_request(
    tmp_path: Path,
) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000, options={"recentRecaps": 2})
    provider_calls: list[tuple[str, dict[str, object]]] = []

    def provider(actor: str, options: dict[str, object]) -> dict[str, object]:
        provider_calls.append((actor, options))
        return {"recentRecaps": [{"event": "turn"}, {"event": "annotation"}]}

    invoker = _Invoker("context")
    service = _delivery_hooks(tmp_path, invoker, intercept_context=provider)

    prepared = _prepare_until_decided(service, _delivery())
    assert prepared.hook_text == "context"
    assert provider_calls == [(RECIPIENT, {"recentRecaps": 2})]
    assert invoker.calls[0]["context"] == {
        "recentRecaps": [{"event": "turn"}, {"event": "annotation"}]
    }


def test_failing_context_provider_keeps_the_intercept_alive(
    tmp_path: Path,
) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    logs: list[str] = []

    def broken_provider(
        _actor: str, _options: dict[str, object]
    ) -> dict[str, object]:
        raise RuntimeError("recap store unavailable")

    invoker = _Invoker("still contexted")
    service = _delivery_hooks(
        tmp_path,
        invoker,
        intercept_context=broken_provider,
        logger=lambda _level, _component, event, **_fields: logs.append(event),
    )

    prepared = _prepare_until_decided(service, _delivery())
    assert prepared.hook_text == "still contexted"
    assert invoker.calls[0]["context"] == {}
    assert "hooks.intercept_context_failed" in logs


def test_unsupported_harness_kind_fails_open_without_invocation(
    tmp_path: Path,
) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    logs: list[str] = []
    invoker = _Invoker("must not run")

    def broken_support(_actor: str) -> bool:
        raise KeyError("undeclared harness")

    service = _delivery_hooks(
        tmp_path,
        invoker,
        supports=broken_support,
        logger=lambda _level, _component, event, **_fields: logs.append(event),
    )
    delivery = _delivery()

    prepared = _prepare_until_decided(service, delivery)
    assert prepared == delivery
    assert invoker.calls == []
    assert "hooks.intercept_unsupported" in logs


def test_invalid_hooks_config_fails_open(tmp_path: Path) -> None:
    config = tmp_path / "config" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text("{", encoding="utf-8")
    invoker = _Invoker("must not run")
    service = _delivery_hooks(tmp_path, invoker)
    delivery = _delivery()

    prepared = _prepare_until_decided(service, delivery)
    assert prepared is delivery
    assert invoker.calls == []


def test_self_handler_intercept_is_disabled_without_invocation(
    tmp_path: Path,
) -> None:
    _write_hooks_config(tmp_path, handler=RECIPIENT, timeout_ms=5_000)
    invoker = _Invoker("must not run")
    service = _delivery_hooks(tmp_path, invoker)

    prepared = _prepare_until_decided(service, _delivery())
    assert prepared.hook_text is None
    assert invoker.calls == []


def test_hook_request_deliveries_are_never_intercepted(tmp_path: Path) -> None:
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    invoker = _Invoker("must not run")
    service = _delivery_hooks(tmp_path, invoker)
    delivery = HarnessDelivery(
        "hook-exchange",
        "turn-hook:x",
        HANDLER,
        RECIPIENT,
        "hook body",
        hook_request=True,
    )

    assert service.prepare_delivery(delivery) is delivery
    time.sleep(0.05)
    assert invoker.calls == []




@pytest.mark.parametrize("harness", ("jev", "user-proxy"))
def test_python_worker_harness_kind_rejects_delivery_intercepts(
    harness: str,
) -> None:
    assert not harness_supports_delivery_intercept(harness)


@pytest.mark.parametrize("harness", ("claude", "codex", "pi", "dsh"))
def test_prompt_harness_kinds_accept_delivery_intercepts(harness: str) -> None:
    assert harness_supports_delivery_intercept(harness)


def test_config_wait_hold_is_bounded_and_fails_open(tmp_path: Path) -> None:
    """A dispatch pump never blocks on hooks.json: it holds, bounded, then
    fails open if the first load cannot complete."""

    entered = threading.Event()
    release = threading.Event()

    def blocked_path(_actor: str) -> Path:
        entered.set()
        assert release.wait(5)
        return tmp_path / "config" / "hooks.json"

    bus = HookBus()
    hooks = DaemonHookService(
        bus=bus,
        config_path_for_agent=blocked_path,
        consumers={},
        handler_notifier=lambda *_args, **_kwargs: None,
    )
    clock = [0.0]
    invoker = _Invoker("must not run")
    service = DeliveryHookService(
        hooks=hooks,
        port=HookInterceptPort(invoke=invoker.invoke),
        monotonic=lambda: clock[0],
        config_wait_seconds=2.0,
    )
    delivery = _delivery()
    try:
        started = time.monotonic()
        assert service.prepare_delivery(delivery) is None
        assert time.monotonic() - started < 0.1
        assert entered.wait(1)
        clock[0] = 3.0  # the bounded config wait expired
        prepared = service.prepare_delivery(delivery)
        assert prepared is delivery
        assert invoker.calls == []
    finally:
        release.set()


def test_intercept_request_carries_recent_recaps_from_the_recap_consumer(
    tmp_path: Path,
) -> None:
    """H2 x H3 seam: the recap consumer's serving reaches the handler.

    The shell wires ``consumer_intercept_context`` over its registered
    ``recap`` consumer (post-H2 that is RecapHookConsumer); the intercept
    request the handler receives then carries ``context.recentRecaps``.
    """

    class _RecapConsumer:
        def __init__(self) -> None:
            self.served: list[tuple[str, int]] = []

        def recent_recaps(self, actor: str, count: int) -> list[object]:
            self.served.append((actor, count))
            return [{"schemaVersion": 1, "deliveryId": "delivery-1"}]

    recap = _RecapConsumer()
    invoker = _Invoker("CTX")
    _write_hooks_config(tmp_path, timeout_ms=5_000)
    service = _delivery_hooks(
        tmp_path,
        invoker,
        consumers={"recap": recap},
        intercept_context=consumer_intercept_context({"recap": recap}),
    )

    prepared = _prepare_until_decided(service, _delivery())

    assert prepared is not None
    assert prepared.hook_text == "CTX"
    assert recap.served == [(RECIPIENT, 5)]
    event = invoker.calls[-1]
    assert event["context"]["recentRecaps"] == [
        {"schemaVersion": 1, "deliveryId": "delivery-1"}
    ]


def test_consumer_intercept_context_without_a_recap_consumer_is_absent() -> None:
    assert consumer_intercept_context({}) is None
    assert consumer_intercept_context(None) is None
    assert consumer_intercept_context({"recap": object()}) is None


def test_consumer_intercept_context_bounds_the_requested_count() -> None:
    class _Reader:
        def __init__(self) -> None:
            self.counts: list[int] = []

        def recent_recaps(self, _actor: str, count: int) -> list[object]:
            self.counts.append(count)
            return []

    reader = _Reader()
    provider = consumer_intercept_context({"recap": reader})
    assert provider is not None
    assert provider(RECIPIENT, {}) == {"recentRecaps": []}
    assert provider(RECIPIENT, {"recentRecaps": 12}) == {"recentRecaps": []}
    assert provider(RECIPIENT, {"recentRecaps": 0}) == {"recentRecaps": []}
    assert provider(RECIPIENT, {"recentRecaps": 999}) == {"recentRecaps": []}
    assert provider(RECIPIENT, {"recentRecaps": True}) == {"recentRecaps": []}
    assert reader.counts == [5, 12, 5, 5, 5]


def test_shell_recap_consumer_serves_the_intercept_context_end_to_end(
    tmp_path: Path,
) -> None:
    """The real composition: shell ``recap`` consumer -> intercept context.

    Assembles the delivery hooks exactly the way the daemon does (shell
    ``hook_consumers`` factory + ``wire_delivery_hooks``): with recaps on
    disk and an intercept entry asking ``options.recentRecaps = 2``, the
    request the handler receives carries the two most recent records.  Goes
    red if the wiring stops passing the recap consumer's serving in as the
    intercept context.
    """

    import json as _json
    from types import SimpleNamespace

    from hyprial.daemon.impl.application.ports import HookConsumersDeps
    from hyprial.daemon.impl.application.wiring.delivery_hooks import (
        wire_delivery_hooks,
    )
    from hyprial.daemon.impl.hooks import DaemonHookService
    from hyprial.kernel import HookBus
    from hyprial.shell.impl.daemon_wiring.hooks import hook_consumers

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    records = [
        {"schemaVersion": 1, "event": "turn", "deliveryId": f"delivery-{index}"}
        for index in range(3)
    ]
    (home / "state" / "recaps.jsonl").write_text(
        "".join(_json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    _write_hooks_config(home, timeout_ms=5_000, options={"recentRecaps": 2})

    def home_for_agent(_actor: str) -> Path:
        return home

    consumers = hook_consumers(
        HookConsumersDeps(home_for_agent=home_for_agent, logger=None)
    )
    assert sorted(consumers) == ["recap"]
    hooks = DaemonHookService(
        bus=HookBus(),
        config_path_for_agent=lambda _actor: home / "config" / "hooks.json",
        consumers=consumers,
        handler_notifier=lambda *_args, **_kwargs: None,
    )
    invoker = _Invoker("CTX")
    service = wire_delivery_hooks(
        daemon_hooks=hooks,
        hook_invoker=invoker,
        hook_consumers=consumers,
        node_id="node",
        desired_state=SimpleNamespace(load=lambda: SimpleNamespace(harnesses=())),
        logger=None,
    )

    prepared = _prepare_until_decided(service, _delivery())

    assert prepared is not None
    assert prepared.hook_text == "CTX"
    document = invoker.calls[-1]
    assert document["context"]["recentRecaps"] == records[-2:]


def test_turn_lifecycle_is_ledgered_without_any_hooks_subscription(
    tmp_path: Path,
) -> None:
    """PR #1463 finding 3: turns leave a durable trace unconditionally.

    The turn.started/completed/failed bus events had no emit site and the
    daemon ledger carried no turn line at all, so "was there a turn" could
    only be inferred from adjacent traffic.  The ledger lines are written
    for every dispatched delivery regardless of hooks.json.
    """

    lines: list[tuple[str, str, str, dict[str, object]]] = []

    def logger(level: str, component: str, event: str, **fields: object) -> None:
        lines.append((level, component, event, fields))

    service = _delivery_hooks(tmp_path, _Invoker("unused"), logger=logger)
    delivery = _delivery(message="do the work")
    assert _prepare_until_decided(service, delivery) is delivery
    service.mark_dispatched(delivery.delivery_id)
    service.settled(_result(status=HarnessResultStatus.FAILED, failure_code="PROVIDER_AUTHENTICATION_FAILED"))

    turns = [entry for entry in lines if entry[2].startswith("turn.")]
    assert [entry[2] for entry in turns] == ["turn.started", "turn.failed"]
    started, failed = turns
    assert started[0] == "info"
    assert started[3]["deliveryId"] == "delivery-1"
    assert started[3]["actor"] == RECIPIENT
    assert started[3]["sender"] == "agent:owner:node:sender"
    assert isinstance(started[3]["startedAtMs"], int)
    assert failed[3]["status"] == "failed"
    assert failed[3]["failureCode"] == "PROVIDER_AUTHENTICATION_FAILED"
    assert failed[3]["startedAtMs"] == started[3]["startedAtMs"]
    assert isinstance(failed[3]["endedAtMs"], int)
    # Identifiers and timing only: the prompt body never enters the ledger.
    assert "do the work" not in str(turns)


def test_turn_bus_events_publish_to_subscribed_lanes(
    tmp_path: Path,
) -> None:
    invoker = _Invoker("must not run")
    consumer = _Recorder()
    service = _observer_service(
        tmp_path,
        invoker,
        consumer,
        entries=[
            {
                "events": ["turn.started", "turn.completed"],
                "consumer": "test",
            }
        ],
    )
    delivery = _delivery(message="hello")
    assert _prepare_until_decided(service, delivery) is delivery
    service.mark_dispatched(delivery.delivery_id)
    service.settled(_result())
    _wait_until(lambda: len(consumer.events) == 2)
    started_event, completed_event = consumer.events
    assert started_event.event == "turn.started"
    assert started_event.source == "daemon.turns"
    assert started_event.payload["status"] == "started"
    assert started_event.payload["deliveryId"] == "delivery-1"
    assert completed_event.event == "turn.completed"
    assert completed_event.payload["status"] == "completed"


def test_settled_without_a_context_ledgers_no_turn(tmp_path: Path) -> None:
    """A result for an unprepared delivery invents no turn lifecycle."""

    lines: list[tuple[str, str, str, dict[str, object]]] = []

    def logger(level: str, component: str, event: str, **fields: object) -> None:
        lines.append((level, component, event, fields))

    service = _delivery_hooks(tmp_path, _Invoker("unused"), logger=logger)
    service.settled(_result(delivery_id="never-dispatched"))

    assert [entry for entry in lines if entry[2].startswith("turn.")] == []
