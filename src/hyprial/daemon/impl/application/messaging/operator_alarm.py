"""Bounded off-thread delivery of daemon operator alarms to the owner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import shlex
from typing import Any
from uuid import uuid4

from hyprial.daemon import Alarm
from hyprial.daemon.impl.squire import (
    UserDeliveryPort,
    UserDeliveryRequest,
    UserDeliveryResult,
)
from hyprial.kernel import (
    AdmissionResult,
    EffectCompleted,
    EffectLane,
    EffectRequest,
    HarnessLaunchSpec,
)


_RESTARTABLE_HARNESSES = frozenset({"claude", "pi", "codex", "dsh", "jev"})
_SECRET_NAME = re.compile(r"(?:^|[._-])(key|token|secret|password)(?:$|[._-])")


@dataclass(frozen=True, slots=True)
class _OperatorAlarmRequest:
    alarm: Alarm
    kind: str
    key: str
    owner: str
    node: str
    occurrence: str
    harness: HarnessLaunchSpec | None = None


@dataclass(frozen=True, slots=True)
class _OperatorAlarmOutcome:
    accepted: bool
    unavailable: bool = False
    code: str | None = None
    definitely_not_sent: bool = False


class OperatorAlarmDelivery:
    """Run the potentially blocking owner-DM transport outside maintenance."""

    def __init__(
        self,
        *,
        delivery: Callable[[], UserDeliveryPort | None],
        log: Callable[..., None],
        capacity: int = 16,
    ) -> None:
        self._delivery = delivery
        self._log = log
        self._effects: EffectLane[_OperatorAlarmRequest, _OperatorAlarmOutcome] = (
            EffectLane(
                name="operator-alarm-delivery",
                execute=self._execute,
                complete=self._complete,
                capacity=capacity,
            )
        )

    def submit(
        self,
        alarm: Alarm,
        *,
        kind: str,
        key: str,
        owner: str,
        node: str,
        harness: HarnessLaunchSpec | None = None,
    ) -> None:
        # One identity per occurrence: the receiver's delivery ledger keeps
        # every outcome by key, so a key reused for the same harness's next
        # failure would answer "duplicate" -- or replay an old refusal --
        # forever (review 832 on #1151).  Throttling is unaffected: the
        # emitter claims by (conversation, reason), not by this key.
        occurrence = uuid4().hex[:12]
        admission = self._effects.submit(
            EffectRequest(
                operation_id=f"{alarm.correlation_id}:{occurrence}",
                generation=1,
                payload=_OperatorAlarmRequest(
                    alarm, kind, key, owner, node, occurrence, harness
                ),
            )
        )
        if admission is AdmissionResult.ACCEPTED:
            return
        snapshot = self._effects.snapshot()
        self._safe_log(
            "warn",
            "daemon.operator_alarm.delivery_overloaded",
            kind=kind,
            correlationId=alarm.correlation_id,
            admission=admission.value,
            rejected=snapshot.rejected,
        )

    def _execute(self, request: _OperatorAlarmRequest) -> _OperatorAlarmOutcome:
        delivery = self._delivery()
        if delivery is None:
            return _OperatorAlarmOutcome(False, unavailable=True)
        result: UserDeliveryResult = delivery.deliver(
            UserDeliveryRequest(
                message_id=f"{request.alarm.message_id}:{request.occurrence}",
                idempotency_key=f"{request.alarm.correlation_id}:{request.occurrence}",
                owner=request.owner,
                sender="daemon-alarm",
                message=self._message(request),
                conversation_id=f"daemon-{request.kind}",
            )
        )
        return _OperatorAlarmOutcome(
            result.accepted,
            code=result.code,
            definitely_not_sent=result.definitely_not_sent,
        )

    @staticmethod
    def _message(request: _OperatorAlarmRequest) -> str:
        """Plain Chinese for the owner: conclusion, node, numbers, what to do."""

        alarm = request.alarm
        if request.kind == "harness-failed":
            if request.harness is not None:
                name = request.harness.name
                action = _restart_action(request.harness)
            else:
                name = request.key
                action = "用 hyprial ps 查看它的状态和最后的错误"
            return (
                f"托管 worker {name} 已失败，重启预算已用完，不会再自动重启。"
                f"节点：{request.node}。"
                f"处理：{action}。"
            )
        if request.kind == "state-writer-slow":
            numbers = alarm.text or f"原因 {alarm.reason}"
            return (
                "daemon 状态写入者饱和，会话注册、刷新和心跳会超时，唤醒可能中断。"
                f"节点：{request.node}。"
                f"读数：{numbers}。"
                "处理：用 hyprial ps 查看 daemon.statePersistenceLatency，"
                "找出占用最多的命令；持续超过 10 分钟请检查最近的部署。"
            )
        if alarm.text:
            return f"{alarm.text}（节点：{request.node}）"
        return f"daemon 告警 {request.kind}，原因 {alarm.reason}。节点：{request.node}。"

    def _complete(self, event: EffectCompleted[_OperatorAlarmOutcome]) -> AdmissionResult:
        try:
            outcome = event.result
            if event.error is not None:
                self._safe_log(
                    "error",
                    "daemon.operator_alarm.delivery_failed",
                    correlationId=event.operation_id,
                    errorType=event.error,
                )
            elif outcome is None:
                self._safe_log(
                    "error",
                    "daemon.operator_alarm.delivery_failed",
                    correlationId=event.operation_id,
                    errorType="missing-outcome",
                )
            elif outcome.unavailable:
                self._safe_log(
                    "warn",
                    "daemon.operator_alarm.user_delivery_unavailable",
                    correlationId=event.operation_id,
                )
            elif not outcome.accepted:
                self._safe_log(
                    "error",
                    "daemon.operator_alarm.delivery_rejected",
                    correlationId=event.operation_id,
                    code=outcome.code,
                    definitelyNotSent=outcome.definitely_not_sent,
                )
            else:
                self._safe_log(
                    "info",
                    "daemon.operator_alarm.delivered",
                    correlationId=event.operation_id,
                )
        finally:
            self._effects.acknowledge(event.operation_id, event.generation)
        return AdmissionResult.ACCEPTED

    def _safe_log(self, level: str, event: str, **fields: Any) -> None:
        try:
            self._log(level, "daemon", event, **fields)
        except Exception:
            pass

    def snapshot(self) -> Any:
        return self._effects.snapshot()

    def close(self, timeout: float = 5.0) -> bool:
        return self._effects.close(timeout)


def _restart_action(spec: HarnessLaunchSpec) -> str:
    if spec.nickname is not None and spec.nickname.startswith("pac:"):
        return (
            "该 worker 属于 PAC graph；运行 hyprial workflow inspect "
            "查看图状态并由 PAC 处理重启"
        )
    command = _restart_command(spec)
    if command is None:
        return f"运行 hyprial ps 查看并按原隔离方式重启 {spec.name}"
    return f"确认原因后执行 {command}"


def _restart_command(spec: HarnessLaunchSpec) -> str | None:
    """Render the persisted user intent as one shell-safe start command.

    ``tier`` is already resolved into harness/provider/model before the launch
    spec is persisted, and ``endpoint`` is a runtime result rather than a
    ``start`` input. Runtime arguments resume after the explicit ``--``.
    """

    route_in_command = any(
        value == "--route" or value.startswith("--route=")
        for value in spec.command
    )
    if (
        spec.harness not in _RESTARTABLE_HARNESSES
        or spec.execution_runtime is not None
        or spec.containerized
        or spec.container_image is not None
        or spec.pinned_owner is not None
        or route_in_command
    ):
        return None

    command = ["hyprial", "start", spec.harness, "--name", spec.name]
    if spec.nickname is not None:
        command.extend(("--nickname", spec.nickname))
    if spec.cwd is not None:
        command.extend(("--cwd", spec.cwd))
    if spec.headless:
        command.append("--headless")
    if spec.model_provider is not None:
        command.extend(("--provider", spec.model_provider))
    if spec.model is not None:
        command.extend(("--model", spec.model))
    if spec.session_ref is not None:
        command.extend(("--resume", spec.session_ref))
    if spec.args:
        command.append("--")
        command.extend(_redacted_args(spec.args))
    return shlex.join(command)


def _redacted_args(args: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "-c" and index + 1 < len(args):
            redacted.append(value)
            override = args[index + 1]
            key, separator, _secret = override.partition("=")
            redacted.append(
                f"{key}=REDACTED"
                if separator and _is_secret_name(key)
                else override
            )
            index += 2
            continue
        flag, separator, _secret = value.partition("=")
        if value.startswith("-") and separator and _is_secret_name(flag):
            redacted.append(f"{flag}=REDACTED")
            index += 1
            continue
        redacted.append(value)
        if (
            value.startswith("-")
            and _is_secret_name(value)
            and index + 1 < len(args)
        ):
            redacted.append("REDACTED")
            index += 2
            continue
        index += 1
    return tuple(redacted)


def _is_secret_name(value: str) -> bool:
    return _SECRET_NAME.search(value.lower().lstrip("-")) is not None
