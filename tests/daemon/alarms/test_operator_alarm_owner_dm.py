"""Operator alarms are paged to the daemon owner's existing DM route."""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon import Alarm, AlarmResult
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging.operator_alarm import (
    OperatorAlarmDelivery,
)
from hyprial.daemon.impl.squire import UserDeliveryRequest, UserDeliveryResult
from hyprial.kernel import HarnessLaunchSpec, SmolvmRuntimeSpec
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


class _Emitter:
    def __init__(self, status: str = "delivered", *, claimed: bool | None = None) -> None:
        self.status = status
        self.claimed = status == "delivered" if claimed is None else claimed
        self.alarms: list[Alarm] = []

    def emit(self, alarm: Alarm, *, terminal: bool) -> AlarmResult:
        assert terminal is False
        self.alarms.append(alarm)
        return AlarmResult(  # type: ignore[arg-type]
            self.status,
            alarm.audience,
            claimed=self.claimed,
        )


class _Inbox:
    def __init__(self, emitter: _Emitter) -> None:
        self.alarm_emitter = emitter


class _Delivery:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.requests: list[UserDeliveryRequest] = []
        self.changed = threading.Condition()

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        with self.changed:
            self.requests.append(request)
            self.changed.notify_all()
        if self.delay:
            time.sleep(self.delay)
        return UserDeliveryResult(message_id=request.message_id, accepted=True)

    def wait_for(self, count: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        with self.changed:
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, self.requests
                self.changed.wait(remaining)


def _application(tmp_path: Path) -> DaemonApplication:
    return DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="allen",
        **daemon_dependencies(),
    )


def _close(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()


def _owner_message_for_spec(tmp_path: Path, spec: HarnessLaunchSpec) -> str:
    from types import SimpleNamespace

    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    real_store = application.desired_state
    application.desired_state = SimpleNamespace(  # type: ignore[assignment]
        # Deliberately hostile historical alarm projection, not valid new intent.
        load=lambda: SimpleNamespace(harnesses=(spec,))
    )
    try:
        application._raise_harness_failed_alarm(f"{spec.harness}:{spec.name}")
        delivery.wait_for(1)
        [request] = delivery.requests
        return request.message
    finally:
        application.desired_state = real_store  # type: ignore[assignment]
        _close(application)


def _smolvm_runtime() -> SmolvmRuntimeSpec:
    return SmolvmRuntimeSpec(
        "/opt/smolvm",
        "a" * 64,
        "/opt/rootfs.ext4",
        "b" * 64,
        "/opt/resize2fs",
        "c" * 64,
        ("/usr/bin/codex",),
        "/usr/bin/python3",
        "d" * 64,
    )


def test_harness_and_state_writer_alarms_each_deliver_one_owner_dm(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    emitter = _Emitter()
    delivery = _Delivery()
    application._inbox = _Inbox(emitter)  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        application._raise_harness_failed_alarm("codex:worker-7")
        application._raise_harness_failed_alarm(
            "sample-9",
            kind="state-writer-slow",
            reason="STATE_WRITER_SLOW",
            text="utilization=0.97, queued=41",
        )
        delivery.wait_for(2)
        harness, writer = delivery.requests
        assert harness.owner == writer.owner == application.owner
        assert harness.message == (
            "托管 worker codex:worker-7 已失败，重启预算已用完，不会再自动重启。"
            f"节点：{application.node_id}。"
            "处理：用 hyprial daemon status 查看它的状态和最后的错误。"
        )
        assert writer.message.startswith("daemon 状态写入者饱和，")
        assert f"节点：{application.node_id}。" in writer.message
        assert "utilization=0.97" in writer.message
        assert "queued=41" in writer.message
        assert "hyprial-cli-developer" not in writer.message
        assert "持续超过 10 分钟请检查最近的部署" in writer.message
        assert harness.sender == writer.sender == "daemon-alarm"
        assert harness.conversation_id == "daemon-harness-failed"
        assert writer.conversation_id == "daemon-state-writer-slow"
        assert harness.idempotency_key.startswith(emitter.alarms[0].correlation_id + ":")
        assert writer.idempotency_key.startswith(emitter.alarms[1].correlation_id + ":")
    finally:
        _close(application)


def test_start_precondition_alarm_names_no_retry_and_next_step(tmp_path: Path) -> None:
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        next_step = (
            "Pi P2 headless launch requires explicit --approve trust. "
            "Run: `hyprial start pi --name worker --headless -- --approve`."
        )
        application._raise_harness_failed_alarm(
            "pi:worker",
            kind="harness-start-precondition",
            reason="HARNESS_START_PRECONDITION_FAILED:pi:worker",
            text=next_step,
        )
        delivery.wait_for(1)

        request = delivery.requests[0]
        assert "启动前置检查未通过" in request.message
        assert "未自动重试，也未消耗重启预算" in request.message
        assert next_step in request.message
        assert request.conversation_id == "daemon-harness-start-precondition"
    finally:
        _close(application)


def test_the_same_harness_failing_again_is_a_new_delivery(tmp_path: Path) -> None:
    # Review 832: the receiver's ledger keeps every outcome by key, so a key
    # reused for the same harness answered "duplicate" (or replayed an old
    # refusal) forever -- one page per harness per lifetime.
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        application._raise_harness_failed_alarm("codex:worker-7")
        application._raise_harness_failed_alarm("codex:worker-7")
        delivery.wait_for(2)
        first, second = delivery.requests
        assert first.idempotency_key != second.idempotency_key
        assert first.message_id != second.message_id
    finally:
        _close(application)


def test_throttled_operator_alarm_does_not_deliver_owner_dm(tmp_path: Path) -> None:
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter("throttled"))  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        application._raise_harness_failed_alarm("codex:worker-8")
        # Drain the lane before asserting: a submission that slipped through
        # would only reach the transport on the lane's own thread.
        lane = application._operator_alarm_delivery
        assert lane is not None
        deadline = time.monotonic() + 2.0
        while lane.snapshot().outstanding and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lane.snapshot().outstanding == 0
        assert delivery.requests == []
    finally:
        _close(application)


def test_failed_operator_alarm_emit_does_not_bypass_the_emitter_throttle(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(  # type: ignore[assignment]
        _Emitter("failed", claimed=False)
    )
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        application._raise_harness_failed_alarm("codex:worker-emitter-failed")
        lane = application._operator_alarm_delivery
        assert lane is not None
        assert lane.snapshot().outstanding == 0
        assert delivery.requests == []
    finally:
        _close(application)


def test_failed_after_claim_operator_alarm_still_delivers_owner_dm(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(  # type: ignore[assignment]
        _Emitter("failed", claimed=True)
    )
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        application._raise_harness_failed_alarm("codex:worker-after-claim")
        delivery.wait_for(1)
        assert len(delivery.requests) == 1
    finally:
        _close(application)


def test_slow_owner_dm_transport_never_blocks_harness_alarm_raiser(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    delivery = _Delivery(delay=3.0)
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    try:
        started = time.monotonic()
        application._raise_harness_failed_alarm("codex:worker-9")
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        delivery.wait_for(1)
    finally:
        _close(application)


def test_missing_user_delivery_logs_once_without_raising(tmp_path: Path) -> None:
    application = _application(tmp_path)
    logs: list[tuple[str, str, dict[str, Any]]] = []
    changed = threading.Condition()

    def record(level: str, component: str, event: str, **fields: Any) -> None:
        with changed:
            logs.append((level, event, fields))
            changed.notify_all()

    application._log = record  # type: ignore[method-assign]
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = None
    try:
        application._raise_harness_failed_alarm("codex:worker-10")
        deadline = time.monotonic() + 2.0
        with changed:
            while not logs:
                remaining = deadline - time.monotonic()
                assert remaining > 0
                changed.wait(remaining)
        assert [(level, event) for level, event, _ in logs] == [
            ("warn", "daemon.operator_alarm.user_delivery_unavailable")
        ]
    finally:
        _close(application)


def test_daemon_close_drains_operator_alarm_lane_without_worker_leak(
    tmp_path: Path,
) -> None:
    # Only this test's lane threads count: an earlier test's lane on the same
    # xdist worker may still be winding down (CI task 51424 saw one).
    before = set(threading.enumerate())
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]
    lane = application._operator_alarm_delivery
    try:
        application._raise_harness_failed_alarm("codex:worker-11")
        delivery.wait_for(1)
    finally:
        _close(application)

    assert lane.snapshot().closed is True
    assert lane.snapshot().outstanding == 0
    leaked = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("operator-alarm-delivery-")
        and thread not in before
    ]
    assert leaked == []


def test_operator_alarm_leak_guard_keeps_thread_objects_not_reusable_ids() -> None:
    source = inspect.getsource(
        test_daemon_close_drains_operator_alarm_lane_without_worker_leak
    )
    assert "before = set(threading.enumerate())" in source
    assert "thread not in before" in source
    assert "id(thread)" not in source


def test_operator_alarm_lane_overflow_is_counted_and_logged_without_blocking() -> None:
    before = set(threading.enumerate())
    entered = threading.Event()
    release = threading.Event()
    logs: list[tuple[str, dict[str, Any]]] = []

    class BlockingDelivery:
        def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
            entered.set()
            assert release.wait(2.0)
            return UserDeliveryResult(message_id=request.message_id, accepted=True)

    delivery = BlockingDelivery()
    lane = OperatorAlarmDelivery(
        delivery=lambda: delivery,
        log=lambda level, component, event, **fields: logs.append((event, fields)),
        capacity=1,
    )

    def alarm(key: str) -> Alarm:
        return Alarm(
            correlation_id=f"harness-failed:{key}",
            message_id=f"harness-failed:{key}",
            conversation_id="daemon-harness:node-1",
            sender="node-1",
            recipient="allen",
            reason=f"HARNESS_FAILED:{key}",
            audience="operator",
        )

    try:
        lane.submit(alarm("worker-1"), kind="harness-failed", key="worker-1", owner="allen", node="node-1")
        assert entered.wait(1.0)
        started = time.monotonic()
        lane.submit(alarm("worker-2"), kind="harness-failed", key="worker-2", owner="allen", node="node-1")
        assert time.monotonic() - started < 0.5
        assert lane.snapshot().rejected == 1
        assert logs == [
            (
                "daemon.operator_alarm.delivery_overloaded",
                {
                    "kind": "harness-failed",
                    "correlationId": "harness-failed:worker-2",
                    "admission": "overloaded",
                    "rejected": 1,
                },
            )
        ]
    finally:
        release.set()
        assert lane.close(2.0)
    leaked = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("operator-alarm-delivery-")
        and thread not in before
    ]
    assert leaked == []


def test_a_known_harness_failure_names_its_restart_command(tmp_path: Path) -> None:
    spec = HarnessLaunchSpec(
        harness="codex",
        name="worker seven",
        cwd=str(tmp_path / "working directory"),
        headless=True,
        nickname="operator's worker",
        args=("--dangerously-bypass-approvals-and-sandbox", "prompt with spaces"),
        model_provider="openai compatible",
        model="model's name",
        session_ref="session ref",
    )
    message = _owner_message_for_spec(tmp_path, spec)
    assert message.endswith(
        "处理：确认原因后执行 hyprial agent start codex --name 'worker seven' "
        "--nickname 'operator'\"'\"'s worker' --cwd "
        f"'{tmp_path / 'working directory'}' --headless "
        "--provider 'openai compatible' --model 'model'\"'\"'s name' "
        "--resume 'session ref' -- "
        "--dangerously-bypass-approvals-and-sandbox 'prompt with spaces'。"
    )


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(
            HarnessLaunchSpec(
                harness="codex",
                name="worker",
                headless=True,
                args=("--dangerously-bypass-approvals-and-sandbox",),
                execution_runtime=_smolvm_runtime(),
            ),
            id="smolvm",
        ),
        pytest.param(
            HarnessLaunchSpec(
                harness="codex",
                name="worker",
                headless=True,
                containerized=True,
            ),
            id="containerized",
        ),
        pytest.param(
            HarnessLaunchSpec(
                harness="codex",
                name="worker",
                headless=True,
                pinned_owner="owner-node",
            ),
            id="pinned-owner",
        ),
        pytest.param(
            HarnessLaunchSpec(
                harness="user-proxy",
                name="worker",
                headless=True,
                command=("/usr/bin/proxy", "--route", "route:lark:owner"),
            ),
            id="user-proxy-route",
        ),
        pytest.param(
            HarnessLaunchSpec(
                harness="lark",
                name="worker",
                headless=True,
            ),
            id="unsupported-lark",
        ),
    ],
)
def test_isolated_or_routed_harness_uses_generic_restart_guidance(
    tmp_path: Path,
    spec: HarnessLaunchSpec,
) -> None:
    message = _owner_message_for_spec(tmp_path, spec)
    assert message.endswith(
        "处理：运行 hyprial daemon status 查看并按原隔离方式重启 worker。"
    )
    assert "hyprial agent start" not in message


def test_restart_command_redacts_secret_like_runtime_arguments(
    tmp_path: Path,
) -> None:
    spec = HarnessLaunchSpec(
        harness="codex",
        name="redaction-worker",
        headless=True,
        args=(
            "--api-key",
            "sensitive-one",
            "--access-token=sensitive-two",
            "-c",
            "client_secret=sensitive-three",
            "-c",
            "theme=dark",
            "--password",
            "sensitive-four",
            "--ordinary",
            "visible",
        ),
    )
    message = _owner_message_for_spec(tmp_path, spec)
    for secret in (
        "sensitive-one",
        "sensitive-two",
        "sensitive-three",
        "sensitive-four",
    ):
        assert secret not in message
    assert "--api-key REDACTED" in message
    assert "--access-token=REDACTED" in message
    assert "client_secret=REDACTED" in message
    assert "-c theme=dark" in message
    assert "--ordinary visible" in message


def test_pac_owned_worker_points_at_workflow_inspection(tmp_path: Path) -> None:
    spec = HarnessLaunchSpec(
        harness="codex",
        name="pac-worker",
        headless=True,
        nickname="pac:graph:node:1:effect",
    )
    message = _owner_message_for_spec(tmp_path, spec)
    assert "属于 PAC graph" in message
    assert "hyprial workflow inspect" in message
    assert "hyprial agent start" not in message


def test_failed_harness_spec_lookup_still_delivers_generic_owner_dm(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    delivery = _Delivery()
    application._inbox = _Inbox(_Emitter())  # type: ignore[assignment]
    application._user_delivery = delivery  # type: ignore[assignment]

    def lookup_failed(_key: str) -> None:
        raise OSError("desired state unavailable")

    application._failed_harness_spec = lookup_failed  # type: ignore[method-assign]
    try:
        application._raise_harness_failed_alarm("codex:worker-lookup-failed")
        delivery.wait_for(1)
        [request] = delivery.requests
        assert "worker codex:worker-lookup-failed 已失败" in request.message
        assert "用 hyprial daemon status 查看它的状态和最后的错误" in request.message
    finally:
        _close(application)
