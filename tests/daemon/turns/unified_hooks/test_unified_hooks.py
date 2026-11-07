"""Unified-hooks daemon lane contract tests.

决策记录 · 2026-11-02 · no-hooks 基线测试冻结服务时钟,消除刷新窗口竞态

CI run 43915(shard4)上 ``test_no_hooks_config_preserves_turn_result_record_resolver_and_threads_exactly``
红:``assert resolver_calls == [RECIPIENT]`` 看到 resolver 被调 2 次。机制:产品按
``HOOK_CONFIG_REFRESH_SECONDS = 1.0`` 周期性重解析配置;10 个 turn 在慢机上一旦
跨过 1.0s 窗口,``_configuration`` 就再调度一次刷新,``config_path_for_agent``
随之重跑。这是产品已文档化的周期重载行为,不是单 turn 内双解析(实测:测试内
拉长循环即可确定性复现;修复后同机制下仍绿)。修法:走 ``_service`` 既有
``monotonic`` 注入缝把服务时钟冻结在常数,测试只度量"一个已结算的刷新期内
恰好解析一次",断言含义不变(不是弱化成 at-least-once),产品零改动。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
)
from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.turn_delivery import InboxTurnHookInvoker
from hyprial.daemon.impl.hooks import DaemonHookService, TurnObserver
from hyprial.daemon.impl.hooks import service as hook_service_module
from hyprial.daemon.impl.inbox.contracts.api import SubmissionResult
from hyprial.identity.impl.agents.state.read_port import AgentReadTimeout
from hyprial.kernel import HookBus, HookEvent
from hyprial.kernel.impl.contracts.ports import PortAdmission


RECIPIENT = "agent:owner:node:worker"
HANDLER = "agent:owner:node:handler"
CALLBACK = "agent:owner:node:_hooks"


class _Consumer:
    def __init__(self, release: threading.Event | None = None) -> None:
        self.events: list[tuple[HookEvent, dict[str, object]]] = []
        self.release = release

    def observe(self, event: HookEvent, options: dict[str, object]) -> None:
        self.events.append((event, dict(options)))
        if self.release is not None:
            assert self.release.wait(5.0)


class _CaptureInbox:
    def __init__(self) -> None:
        self.requests = []

    def submit(self, message, *, now_ms=None):  # type: ignore[no-untyped-def]
        self.requests.append(message)
        return SubmissionResult(message.message_id, accepted=True)


class _Client:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(result="done", is_error=False)


def _delivery() -> HarnessDelivery:
    return HarnessDelivery(
        "delivery-1",
        "conversation-1",
        "agent:owner:node:sender",
        RECIPIENT,
        "password=hunter2 /private/prompt.txt",
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _write_configs(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat().st_mtime_ns if path.exists() else time.time_ns()
    path.write_text(
        json.dumps({"schemaVersion": 2, "hooks": entries}), encoding="utf-8"
    )
    changed = max(time.time_ns(), previous + 1)
    os.utime(path, ns=(changed, changed))


def _write_config(path: Path, entry: dict[str, object]) -> None:
    _write_configs(path, [entry])


def _wait_for_configuration_refresh(
    service: DaemonHookService, actor: str = RECIPIENT
) -> None:
    _wait_until(
        lambda: actor in service._configs  # noqa: SLF001 - async cache seam
        and actor not in service._config_refreshing  # noqa: SLF001
    )


def _prime_configuration(
    service: DaemonHookService,
    event: str = "turn.completed",
    actor: str = RECIPIENT,
) -> None:
    service._has_route(actor, event)  # noqa: SLF001 - async cache seam
    _wait_for_configuration_refresh(service, actor)


def _service(
    tmp_path: Path,
    bus: HookBus,
    consumer: _Consumer | None,
    *,
    inbox: _CaptureInbox | None = None,
    logs: list[tuple[str, dict[str, object]]] | None = None,
    config_path_for_agent: Callable[[str], Path] | None = None,
    consumers: Mapping[str, _Consumer] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> DaemonHookService:
    invoker = InboxTurnHookInvoker(inbox or _CaptureInbox(), callback_actor=CALLBACK)
    return DaemonHookService(
        bus=bus,
        config_path_for_agent=config_path_for_agent
        or (lambda _actor: tmp_path / "agent" / "config" / "hooks.json"),
        consumers=(
            dict(consumers)
            if consumers is not None
            else ({"test": consumer} if consumer is not None else {})
        ),
        handler_notifier=invoker.notify,
        logger=(
            None
            if logs is None
            else lambda _level, _component, event, **fields: logs.append((event, fields))
        ),
        monotonic=monotonic,
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"events": ["turn.completed"], "consumer": "test", "handler": HANDLER},
        {"events": ["turn.completed"]},
        {"events": ["turn.completed"], "consumer": "not-registered"},
    ],
)
def test_invalid_v2_entry_is_logged_disabled_and_daemon_lane_survives(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    bus = HookBus()
    consumer = _Consumer()
    logs: list[tuple[str, dict[str, object]]] = []
    service = _service(tmp_path, bus, consumer, logs=logs)
    try:
        _write_config(tmp_path / "agent/config/hooks.json", entry)
        _prime_configuration(service)
        service.observe_turn(
            _delivery(),
            HarnessResult("delivery-1", RECIPIENT, HarnessResultStatus.COMPLETED, output="ok"),
            started_at_ms=10,
            ended_at_ms=20,
            tool_names=("reader",),
        )
        assert consumer.events == []
        assert [name for name, _fields in logs] == ["hooks.config_invalid"]
        assert sum(row.accepted for row in bus.projection()) == 0
        assert all(row.failed == 0 for row in bus.projection())
    finally:
        assert bus.close(5.0)


def test_mtime_reload_switches_consumer_to_agent_handler_with_documented_payload(
    tmp_path: Path,
) -> None:
    bus = HookBus()
    consumer = _Consumer()
    inbox = _CaptureInbox()
    service = _service(tmp_path, bus, consumer, inbox=inbox)
    path = tmp_path / "agent/config/hooks.json"
    try:
        _write_config(
            path,
            {
                "events": ["turn.completed"],
                "consumer": "test",
                "options": {"sample": True},
            },
        )
        _prime_configuration(service)
        service.observe_turn(
            _delivery(),
            HarnessResult("delivery-1", RECIPIENT, HarnessResultStatus.COMPLETED, output="token=secret"),
            started_at_ms=10,
            ended_at_ms=20,
            tool_names=("reader",),
        )
        _wait_until(lambda: len(consumer.events) == 1)
        assert consumer.events[0][1] == {"sample": True}

        time.sleep(1.05)
        _write_config(
            path,
            {"events": ["turn.failed"], "handler": HANDLER, "timeoutMs": 50},
        )
        _prime_configuration(service, "turn.failed")
        service.observe_turn(
            _delivery(),
            HarnessResult(
                "delivery-1",
                RECIPIENT,
                HarnessResultStatus.FAILED,
                error="failed",
                failure_code="PROVIDER_TRANSIENT_FAILURE",
            ),
            started_at_ms=30,
            ended_at_ms=40,
            tool_names=("reader",),
        )
        _wait_until(lambda: len(inbox.requests) == 1)
        request = inbox.requests[0]
        assert request.recipient == HANDLER and request.intent == "request"
        outer = json.loads(request.payload)
        event = json.loads(outer["message"])
        assert event["event"] == "turn.failed"
        assert event["schemaVersion"] == 2
        assert event["payload"] == {
            "deliveryId": "delivery-1",
            "sender": "agent:owner:node:sender",
            "conversation": "conversation-1",
            "startedAtMs": 30,
            "endedAtMs": 40,
            "status": "failed",
            "toolNames": ["reader"],
            "outputExcerpt": "",
            "promptExcerpt": "password=<redacted> <path>",
            "toolCalls": [{"name": "reader", "ok": False}],
            "failureCode": "PROVIDER_TRANSIENT_FAILURE",
            "collectionMethod": "daemon",
            "fidelity": "inferred",
        }
    finally:
        assert bus.close(5.0)


class _HeldClient:
    async def __aenter__(self) -> _HeldClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        import asyncio

        await asyncio.to_thread(self._release.wait)

    async def interrupt(self) -> None:
        self._release.set()

    async def receive_response(self):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(result="done", is_error=False)


def test_streaming_post_admission_bookkeeping_preserves_existing_tool_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-offer accepted by admission keeps tool names collected so far."""

    release = threading.Event()

    held = _HeldClient()
    held._release = release  # noqa: SLF001
    process = StreamingTurnProcess(
        harness="test",
        label="test",
        client_factory=lambda: held,
        thread_name="hooks-admission-bookkeeping",
        on_turn_completed=lambda *_args, **_kwargs: None,
    )
    delivery = HarnessDelivery(
        "delivery-1", "conversation-1", "sender", RECIPIENT, "body"
    )
    try:
        assert process.wait_ready(timeout=1)
        process._turn_tool_names[delivery.delivery_id] = ["reader"]  # noqa: SLF001
        with monkeypatch.context() as patch:
            patch.setattr(
                process._turn_runtime,  # noqa: SLF001
                "submit",
                lambda _command: PortAdmission.ACCEPTED,
            )
            assert process.enqueue(delivery)
        assert process._turn_tool_names[delivery.delivery_id] == [  # noqa: SLF001
            "reader"
        ]
    finally:
        release.set()
        process.stop()


def _combine(first, second):  # type: ignore[no-untyped-def]
    """Stand-in for the retired CombinedTurnObserver: additive observation."""

    class _Combined:
        def __call__(
            self,
            delivery,  # type: ignore[no-untyped-def]
            result,  # type: ignore[no-untyped-def]
            *,
            started_at_ms: int,
            ended_at_ms: int,
            tool_names: tuple[str, ...] = (),
        ) -> None:
            first(
                delivery,
                result,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                tool_names=tool_names,
            )
            second(
                delivery,
                result,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                tool_names=tool_names,
            )

        def turn_started(self, delivery, *, started_at_ms: int) -> None:  # type: ignore[no-untyped-def]
            started = getattr(second, "turn_started", None)
            if callable(started):
                started(delivery, started_at_ms=started_at_ms)

    return _Combined()


def _run_turn(observer):  # type: ignore[no-untyped-def]
    records: list[dict[str, object]] = []

    def legacy(delivery, result, **fields):  # type: ignore[no-untyped-def]
        records.append(
            {
                "delivery": delivery,
                "result": result,
                **fields,
            }
        )

    combined = observer(legacy) if observer is not None else legacy
    process = StreamingTurnProcess(
        harness="test",
        label="test",
        client_factory=_Client,
        thread_name="unified-hook-turn",
        on_turn_completed=combined,
    )
    delivery = _delivery()
    try:
        assert process.wait_ready(timeout=1.0)
        assert process.enqueue(delivery)
        deadline = time.monotonic() + 1.0
        results = ()
        while not results:
            results = process.drain_results()
            assert time.monotonic() < deadline
            time.sleep(0.005)
        return results, records
    finally:
        process.stop()


def test_no_hooks_config_preserves_turn_result_record_resolver_and_threads_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.harnesses.streaming import process as process_module

    monkeypatch.setattr(process_module.time, "time_ns", lambda: 10_000_000)
    baseline_results, baseline_records = _run_turn(None)
    handler_threads_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("hook-lane-agent-handlers-")
    }
    resolver_calls: list[str] = []
    logs: list[tuple[str, dict[str, object]]] = []
    consumer = _Consumer()
    payload_calls: list[dict[str, object]] = []
    real_turn_event_payload = hook_service_module.turn_event_payload

    def spy_turn_event_payload(**fields):  # type: ignore[no-untyped-def]
        payload_calls.append(fields)
        return real_turn_event_payload(**fields)

    monkeypatch.setattr(
        hook_service_module, "turn_event_payload", spy_turn_event_payload
    )

    def resolve(actor: str) -> Path:
        resolver_calls.append(actor)
        return tmp_path / "agent" / "config" / "hooks.json"

    bus = HookBus()
    service = _service(
        tmp_path,
        bus,
        consumer,
        logs=logs,
        config_path_for_agent=resolve,
        monotonic=lambda: 0.0,
    )
    try:
        turns = [
            _run_turn(lambda legacy: _combine(legacy, TurnObserver(service)))
            for _index in range(10)
        ]
        _wait_for_configuration_refresh(service)
        wired_results, wired_records = turns[0]
        assert wired_results == baseline_results
        assert wired_records == baseline_records
        assert resolver_calls == [RECIPIENT]
        assert logs == []
        assert consumer.events == []
        assert payload_calls == []
        assert sum(row.accepted for row in bus.projection()) == 0
        assert {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("hook-lane-agent-handlers-")
        } == handler_threads_before
    finally:
        assert bus.close(5.0)


def _observe_completed(service: DaemonHookService, delivery: HarnessDelivery) -> None:
    service.observe_turn(
        delivery,
        HarnessResult(
            delivery.delivery_id,
            delivery.recipient,
            HarnessResultStatus.COMPLETED,
            output="ok",
        ),
        started_at_ms=10,
        ended_at_ms=20,
    )


def test_handler_equal_to_emitting_agent_does_not_recur(tmp_path: Path) -> None:
    path = tmp_path / "agent/config/hooks.json"
    resolver_calls = 0

    def resolve(_actor: str) -> Path:
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls > 1:
            raise RuntimeError("back-pointer lookup unavailable")
        return path

    bus = HookBus()
    inbox = _CaptureInbox()
    service = _service(
        tmp_path,
        bus,
        None,
        inbox=inbox,
        config_path_for_agent=resolve,
    )
    _write_config(
        path,
        {"events": ["turn.completed"], "handler": RECIPIENT},
    )
    try:
        _prime_configuration(service)
        _observe_completed(service, _delivery())
        time.sleep(0.1)
        assert inbox.requests == []
    finally:
        assert bus.close(5.0)


def test_handler_back_pointer_does_not_ping_pong(tmp_path: Path) -> None:
    peer = "agent:owner:node:peer"
    paths = {
        RECIPIENT: tmp_path / "worker/config/hooks.json",
        peer: tmp_path / "peer/config/hooks.json",
    }
    _write_config(
        paths[RECIPIENT],
        {"events": ["turn.completed"], "handler": peer},
    )
    _write_config(
        paths[peer],
        {"events": ["turn.completed"], "handler": RECIPIENT},
    )
    bus = HookBus()
    inbox = _CaptureInbox()
    service = _service(
        tmp_path,
        bus,
        None,
        inbox=inbox,
        config_path_for_agent=paths.__getitem__,
    )
    try:
        _prime_configuration(service)
        _observe_completed(service, _delivery())
        time.sleep(0.1)
        assert inbox.requests == []
    finally:
        assert bus.close(5.0)


def test_hook_caused_turn_emits_no_handler_request(tmp_path: Path) -> None:
    bus = HookBus()
    inbox = _CaptureInbox()
    service = _service(tmp_path, bus, None, inbox=inbox)
    _write_config(
        tmp_path / "agent/config/hooks.json",
        {"events": ["turn.completed"], "handler": HANDLER},
    )
    try:
        _prime_configuration(service)
        _observe_completed(service, replace(_delivery(), hook_request=True))
        time.sleep(0.1)
        assert inbox.requests == []
    finally:
        assert bus.close(5.0)


def test_bad_entry_disables_only_it_and_good_entry_still_fires(
    tmp_path: Path,
) -> None:
    bus = HookBus()
    consumer = _Consumer()
    logs: list[tuple[str, dict[str, object]]] = []
    service = _service(tmp_path, bus, consumer, logs=logs)
    _write_configs(
        tmp_path / "agent/config/hooks.json",
        [
            {"events": ["turn.completed"], "handler": HANDLER, "consumer": "test"},
            {"events": ["turn.completed"], "consumer": "test"},
        ],
    )
    try:
        _prime_configuration(service)
        _observe_completed(service, _delivery())
        _wait_until(lambda: len(consumer.events) == 1)
        assert [name for name, _fields in logs] == ["hooks.config_invalid"]
    finally:
        assert bus.close(5.0)


def test_config_resolution_error_is_cached_and_log_is_rate_limited(
    tmp_path: Path,
) -> None:
    bus = HookBus()
    logs: list[tuple[str, dict[str, object]]] = []
    resolver_calls = 0

    def fail_resolution(_actor: str) -> Path:
        nonlocal resolver_calls
        resolver_calls += 1
        raise RuntimeError("resolver unavailable")

    service = _service(
        tmp_path,
        bus,
        None,
        logs=logs,
        config_path_for_agent=fail_resolution,
    )
    try:
        for _index in range(3):
            _observe_completed(service, _delivery())
            _wait_for_configuration_refresh(service)
        assert resolver_calls == 1
        assert [name for name, _fields in logs] == ["hooks.config_invalid"]
    finally:
        assert bus.close(5.0)


def test_config_invalid_log_is_rate_limited_across_refreshes(
    tmp_path: Path,
) -> None:
    bus = HookBus()
    logs: list[tuple[str, dict[str, object]]] = []
    now = 0.0

    def fail_resolution(_actor: str) -> Path:
        raise RuntimeError("resolver unavailable")

    service = _service(
        tmp_path,
        bus,
        None,
        logs=logs,
        config_path_for_agent=fail_resolution,
        monotonic=lambda: now,
    )
    try:
        for now in (0.0, 1.1, 2.2):
            _observe_completed(service, _delivery())
            _wait_for_configuration_refresh(service)
        assert [name for name, _fields in logs] == ["hooks.config_invalid"]
    finally:
        assert bus.close(5.0)


def test_config_invalid_rate_limit_groups_distinct_timeout_ids(
    tmp_path: Path,
) -> None:
    bus = HookBus()
    logs: list[tuple[str, dict[str, object]]] = []
    service = _service(tmp_path, bus, None, logs=logs)
    try:
        for correlation_id in ("timeout-a", "timeout-b", "timeout-c"):
            service._log_config_invalid(  # noqa: SLF001 - mutation boundary
                RECIPIENT, AgentReadTimeout(correlation_id)
            )
        assert [name for name, _fields in logs] == ["hooks.config_invalid"]
    finally:
        assert bus.close(5.0)


def test_config_invalid_tracking_is_capacity_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hook_service_module,
        "HOOK_CONFIG_ERROR_TRACKING_CAPACITY",
        2,
        raising=False,
    )
    bus = HookBus()
    service = _service(tmp_path, bus, None)
    try:
        for index in range(3):
            service._log_config_invalid(  # noqa: SLF001 - capacity boundary
                f"agent:owner:node:worker-{index}", RuntimeError("unavailable")
            )
        assert len(service._config_error_logged_at) <= 2  # noqa: SLF001
    finally:
        assert bus.close(5.0)


def test_slow_config_resolver_never_blocks_turn_and_next_turn_observes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent/config/hooks.json"
    _write_config(
        path,
        {"events": ["turn.completed"], "consumer": "test"},
    )
    resolver_started = threading.Event()
    resolver_finished = threading.Event()

    def slow_resolver(_actor: str) -> Path:
        resolver_started.set()
        time.sleep(1.0)
        resolver_finished.set()
        return path

    bus = HookBus()
    consumer = _Consumer()
    service = _service(
        tmp_path,
        bus,
        consumer,
        config_path_for_agent=slow_resolver,
    )
    try:
        started = time.monotonic()
        _observe_completed(service, _delivery())
        elapsed = time.monotonic() - started
        assert elapsed < 0.1, f"turn waited {elapsed:.3f}s for config resolution"
        assert resolver_started.wait(0.5)
        assert resolver_finished.wait(1.5)
        _wait_for_configuration_refresh(service)
        _observe_completed(service, _delivery())
        _wait_until(lambda: len(consumer.events) == 1)
    finally:
        assert bus.close(5.0)


def test_handler_back_pointer_cache_tracks_handler_config_signature(
    tmp_path: Path,
) -> None:
    peer = "agent:owner:node:peer"
    paths = {
        RECIPIENT: tmp_path / "worker/config/hooks.json",
        peer: tmp_path / "peer/config/hooks.json",
    }
    _write_config(
        paths[RECIPIENT],
        {"events": ["turn.completed"], "handler": peer},
    )
    _write_config(
        paths[peer],
        {"events": ["turn.completed"], "handler": RECIPIENT},
    )
    bus = HookBus()
    inbox = _CaptureInbox()
    service = _service(
        tmp_path,
        bus,
        None,
        inbox=inbox,
        config_path_for_agent=paths.__getitem__,
    )
    try:
        _prime_configuration(service)
        _observe_completed(service, _delivery())
        assert inbox.requests == []

        _write_configs(paths[peer], [])
        time.sleep(1.05)
        _prime_configuration(service)
        _observe_completed(service, _delivery())
        _wait_until(lambda: len(inbox.requests) == 1)
        assert len(inbox.requests) == 1
    finally:
        assert bus.close(5.0)


def test_blocking_observer_never_delays_turn_completion(tmp_path: Path) -> None:
    release = threading.Event()
    bus = HookBus()
    consumer = _Consumer(release)
    service = _service(tmp_path, bus, consumer)
    _write_config(
        tmp_path / "agent/config/hooks.json",
        {"events": ["turn.completed"], "consumer": "test"},
    )
    _prime_configuration(service)
    started = time.monotonic()
    try:
        results, _records = _run_turn(
            lambda legacy: _combine(legacy, TurnObserver(service))
        )
        elapsed = time.monotonic() - started
        assert results[0].status is HarnessResultStatus.COMPLETED
        assert elapsed < 0.75, f"turn waited {elapsed:.3f}s for hook observer"
        _wait_until(lambda: len(consumer.events) == 1)
    finally:
        release.set()
        assert bus.close(5.0)


def test_turn_runtime_start_emits_the_versioned_started_payload(tmp_path: Path) -> None:
    bus = HookBus()
    consumer = _Consumer()
    service = _service(tmp_path, bus, consumer)
    _write_config(
        tmp_path / "agent/config/hooks.json",
        {"events": ["turn.started"], "consumer": "test"},
    )
    try:
        _prime_configuration(service, "turn.started")
        results, _records = _run_turn(
            lambda legacy: _combine(legacy, TurnObserver(service))
        )
        assert results[0].status is HarnessResultStatus.COMPLETED
        _wait_until(lambda: len(consumer.events) == 1)
        event = consumer.events[0][0]
        assert event.event == "turn.started"
        assert event.schema_version == 2
        assert event.payload["status"] == "started"
        assert event.payload["promptExcerpt"] == "password=<redacted> <path>"
        assert event.payload["toolCalls"] == []
        assert event.payload["collectionMethod"] == "daemon"
        assert event.payload["fidelity"] == "observed"
    finally:
        assert bus.close(5.0)


class _DaemonConsumer:
    """A daemon-level lane (quota-shaped): no options, every event recorded."""

    def __init__(self) -> None:
        self.events: list[HookEvent] = []

    def observe(self, event: HookEvent) -> None:
        self.events.append(event)


def test_daemon_consumer_hears_turn_failed_without_any_hooks_json(
    tmp_path: Path,
) -> None:
    """Unified hooks H4: the quota lane must hear every usage-limit turn even
    when no agent opted in -- deleting the settlement callback must not leave
    the watchdog silently deaf."""

    bus = HookBus()
    consumer = _DaemonConsumer()
    service = _service(tmp_path, bus, None)
    service.register_consumer(
        "quota", consumer, ("turn.failed", "usage.refreshed")
    )
    try:
        service.observe_turn(
            _delivery(),
            HarnessResult(
                "delivery-1",
                RECIPIENT,
                HarnessResultStatus.FAILED,
                error="quota",
                failure_code="PROVIDER_USAGE_LIMIT",
            ),
            started_at_ms=10,
            ended_at_ms=20,
        )
        _wait_until(lambda: len(consumer.events) == 1)
        event = consumer.events[0]
        assert event.event == "turn.failed"
        assert event.actor == RECIPIENT
        assert event.payload["failureCode"] == "PROVIDER_USAGE_LIMIT"

        # Started and completed turns stay per-agent gated: no hooks.json, no
        # daemon subscriber for them, nothing published (H1 acceptance).
        _observe_completed(service, _delivery())
        service.observe_started(
            SimpleNamespace(
                delivery_id="delivery-1",
                sender="agent:owner:node:sender",
                recipient=RECIPIENT,
                conversation_id="conversation-1",
                message="m",
                hook_request=False,
            ),
            started_at_ms=1,
        )
        time.sleep(0.05)
        assert len(consumer.events) == 1
        assert sum(row.accepted for row in bus.projection()) == 1
    finally:
        assert bus.close(5.0)


def test_usage_refreshed_publishes_to_daemon_lanes_only(tmp_path: Path) -> None:
    bus = HookBus()
    consumer = _DaemonConsumer()
    per_agent = _Consumer()
    service = _service(tmp_path, bus, per_agent)
    _write_config(
        tmp_path / "agent/config/hooks.json",
        {"events": ["usage.refreshed"], "consumer": "test"},
    )
    service.register_consumer(
        "quota", consumer, ("turn.failed", "usage.refreshed")
    )
    try:
        service.observe_usage_refreshed()
        _wait_until(lambda: len(consumer.events) == 1)
        event = consumer.events[0]
        assert event.event == "usage.refreshed"
        assert event.actor is None
        assert event.payload == {
            "collectionMethod": "daemon",
            "fidelity": "observed",
        }
        # An actor-less fact never matches a per-agent registration: the
        # hooks.json entry above names an agent-scoped consumer that cannot
        # hear the daemon's own refresh.
        time.sleep(0.05)
        assert per_agent.events == []
    finally:
        assert bus.close(5.0)


def test_a_raising_daemon_consumer_never_breaks_the_turn(tmp_path: Path) -> None:
    """Allen 09-17 for the bus era: an exploding quota lane is counted as a
    failed delivery on its own lane; the turn result is unchanged."""

    class _Exploding:
        def observe(self, _event: HookEvent) -> None:
            raise RuntimeError("quota lane bug")

    bus = HookBus()
    service = _service(tmp_path, bus, None)
    service.register_consumer(
        "quota", _Exploding(), ("turn.failed", "usage.refreshed")
    )
    try:
        service.observe_turn(
            _delivery(),
            HarnessResult(
                "delivery-1",
                RECIPIENT,
                HarnessResultStatus.FAILED,
                error="quota",
                failure_code="PROVIDER_USAGE_LIMIT",
            ),
            started_at_ms=10,
            ended_at_ms=20,
        )
        _wait_until(
            lambda: next(
                row for row in bus.projection() if row.name == "quota"
            ).failed == 1
        )
        quota_lane = next(row for row in bus.projection() if row.name == "quota")
        assert quota_lane.delivered == 0
    finally:
        assert bus.close(5.0)
