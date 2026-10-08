"""Actor-owned turn-hook coordinator."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from uuid import uuid4

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    HookServicePort,
    _GetPrepareData,
    _PrepareData,
    _PrepareHook,
    _MarkHook,
    _ForgetHook,
    _ForgetMissingHooks,
    _TurnObserved,
)

class _HookReply:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.result: object | None = None
        self.error: BaseException | None = None



class HookCoordinator:
    """One mailbox owns pending before-hook state and delivery decisions."""

    def __init__(self, service: HookServicePort, *, capacity: int = 64) -> None:
        self._service = service
        self._guard = threading.Lock()
        self._replies: dict[str, _HookReply] = {}
        self._closed = False
        self._capacity = capacity
        self._outstanding_turns = 0
        self._active_turn_ids: set[str] = set()
        self._deferred_turns: deque[_TurnObserved] = deque()
        self._prepare_data: dict[str, _PrepareData] = {}
        self._preparing: set[str] = set()
        self._prepare_operations: dict[str, tuple[str, str]] = {}
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="turn-hook-coordinator",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._turn_effects: EffectLane[_TurnObserved, str] = EffectLane(
            name="turn-hook-observation",
            execute=self._observe_turn,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=2,
        )
        self._config_effects: EffectLane[_GetPrepareData, _PrepareData] = EffectLane(
            name="turn-hook-config",
            execute=self._read_prepare_data,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=2,
        )

    def prepare_data(self, delivery: HarnessDelivery) -> _PrepareData | None:
        value = self.call(
            _GetPrepareData(
                f"config:{uuid4().hex}", delivery.delivery_id, delivery.recipient
            )
        )
        assert value is None or isinstance(value, _PrepareData)
        return value

    def _read_prepare_data(self, command: _GetPrepareData) -> _PrepareData:
        home, config = self._service._configuration(command.agent)
        recaps = (
            self._service._recent_recaps(home, config.recent_recaps)
            if home is not None and config is not None
            and "before-delivery" in config.events
            else []
        )
        return _PrepareData(
            command.delivery_id, command.agent, home, config,
            json.dumps(recaps, ensure_ascii=False),
        )

    def submit_turn(self, command: _TurnObserved) -> AdmissionResult:
        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
            if self._outstanding_turns >= self._capacity:
                return AdmissionResult.OVERLOADED
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                self._outstanding_turns += 1
                self._active_turn_ids.add(command.operation_id)
            return admission

    def _observe_turn(self, command: _TurnObserved) -> str:
        self._service._observe_turn_effect(
            command.delivery, command.result,
            started_at_ms=command.started_at_ms,
            ended_at_ms=command.ended_at_ms,
            tool_names=command.tool_names,
        )
        return command.operation_id

    def call(self, command: object, *, timeout: float = 2.0) -> object | None:
        operation_id = command.operation_id  # type: ignore[attr-defined]
        reply = _HookReply()
        with self._guard:
            if self._closed or len(self._replies) >= 64:
                raise TimeoutError("hook coordinator is closed or overloaded")
            self._replies[operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._replies.pop(operation_id)
                raise TimeoutError(f"hook coordinator admission {admission.value}")
        if not reply.ready.wait(max(0.0, timeout)):
            raise TimeoutError("accepted hook decision is still pending")
        if reply.error is not None:
            raise reply.error
        return reply.result

    def _receive(self, command: object) -> None:
        if isinstance(command, _GetPrepareData):
            cached = self._prepare_data.get(command.delivery_id)
            if cached is not None:
                result: object | None = cached
            else:
                result = None
                if command.delivery_id not in self._preparing:
                    self._preparing.add(command.delivery_id)
                    admitted = self._config_effects.submit(
                        EffectRequest(command.operation_id, 1, command)
                    )
                    if admitted is AdmissionResult.ACCEPTED:
                        self._prepare_operations[command.operation_id] = (
                            command.delivery_id, command.agent
                        )
                    else:
                        self._preparing.discard(command.delivery_id)
                        self._prepare_data[command.delivery_id] = _PrepareData(
                            command.delivery_id, command.agent, None, None, "[]"
                        )
                        result = self._prepare_data[command.delivery_id]
            with self._guard:
                reply = self._replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.result = result
                    reply.ready.set()
            return
        if isinstance(command, _TurnObserved):
            if command.result.status is HarnessResultStatus.COMPLETED:
                self._service._forget_delivery_owned(command.delivery.delivery_id)
            admission = self._turn_effects.submit(
                EffectRequest(command.operation_id, 1, command)
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._deferred_turns.append(command)
            return
        if isinstance(command, EffectCompleted):
            if command.operation_id.startswith("config:"):
                result = command.result
                target = self._prepare_operations.pop(command.operation_id, None)
                if target is not None:
                    delivery_id, agent = target
                    if not isinstance(result, _PrepareData):
                        result = _PrepareData(delivery_id, agent, None, None, "[]")
                    self._prepare_data[delivery_id] = result
                    self._preparing.discard(delivery_id)
                    if len(self._prepare_data) > 256:
                        self._prepare_data.pop(next(iter(self._prepare_data)))
                self._config_effects.acknowledge(
                    command.operation_id, command.generation
                )
                return
            with self._guard:
                if command.operation_id not in self._active_turn_ids:
                    self._turn_effects.acknowledge(
                        command.operation_id, command.generation
                    )
                    return
                self._active_turn_ids.remove(command.operation_id)
                self._outstanding_turns -= 1
            self._turn_effects.acknowledge(command.operation_id, command.generation)
            if command.error and self._service._logger is not None:
                self._service._logger(
                    "warn", "daemon", "turn_hook.observation_failed",
                    errorType=command.error,
                )
            while self._deferred_turns:
                deferred = self._deferred_turns[0]
                admitted = self._turn_effects.submit(
                    EffectRequest(deferred.operation_id, 1, deferred)
                )
                if admitted is not AdmissionResult.ACCEPTED:
                    break
                self._deferred_turns.popleft()
            return
        operation_id = command.operation_id  # type: ignore[attr-defined]
        try:
            if isinstance(command, _PrepareHook):
                result = self._service._prepare_after_config(
                    command.delivery,
                    command.home,
                    command.config,
                    json.loads(command.recent_recaps_json),
                )
            elif isinstance(command, _MarkHook):
                self._service._mark_dispatched_owned(command.delivery_id)
                result = None
            elif isinstance(command, _ForgetHook):
                self._service._forget_delivery_owned(command.delivery_id)
                result = None
            elif isinstance(command, _ForgetMissingHooks):
                self._service._forget_missing_owned(
                    command.recipient, command.present_ids
                )
                result = None
            else:
                raise TypeError("unsupported hook coordinator command")
            error = None
        except BaseException as caught:
            result = None
            error = caught
        with self._guard:
            reply = self._replies.pop(operation_id, None)
            if reply is not None:
                reply.result = result
                reply.error = error
                reply.ready.set()

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                outstanding = self._outstanding_turns
            if outstanding == 0:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._turn_effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not self._config_effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
