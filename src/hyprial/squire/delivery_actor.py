"""User-delivery claims and receipts owned by a bounded actor.

Ownership resolution, durable ledger calls and native channel operations execute
on isolated workers. One canonical owner/idempotency key has one active delivery;
other requests join its completion instead of repeating native effects.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.contracts import ipc_errors
from .ledger_actor import UserDeliveryLedgerAuthority

if TYPE_CHECKING:
    from .addressing import (
        ReceiverUserDelivery,
        UserDeliveryRequest,
        UserDeliveryResult,
    )


@dataclass(frozen=True, slots=True)
class DeliverToUser:
    operation_id: str
    generation: int
    request: UserDeliveryRequest


@dataclass(frozen=True, slots=True)
class PrepareDelivery:
    command: DeliverToUser


@dataclass(frozen=True, slots=True)
class DeliveryPrepared:
    canonical_owner: str | None


@dataclass(frozen=True, slots=True)
class ExecuteDelivery:
    command: DeliverToUser


@dataclass(frozen=True, slots=True)
class MarkJoinedDelivery:
    command: DeliverToUser
    result: UserDeliveryResult


@dataclass(frozen=True, slots=True)
class UserDeliveryActorStatus:
    pending: int
    rejected: int
    receipts: int
    closed: bool
    failed: int = 0


class UserDeliveryActor:
    def __init__(
        self, receiver: ReceiverUserDelivery, *, capacity=128, receipt_capacity=4096
    ):
        from .addressing import ReceiverUserDelivery

        if capacity < 1 or receipt_capacity < 1:
            raise ValueError("delivery capacities must be positive")
        self._ledger_owner = None
        if isinstance(receiver, ReceiverUserDelivery):
            self._receiver = copy.copy(receiver)
            self._ledger_owner = UserDeliveryLedgerAuthority(
                receiver.ledger, capacity=capacity
            )
            self._receiver.ledger = self._ledger_owner
        else:
            self._receiver = receiver
        self._capacity = receipt_capacity
        self._guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(capacity)
        self._closed = False
        self._persistence_retries = 0
        self._persistence_pace = threading.Event()
        self._generation = 1
        self._rejected = self._failed = 0
        self._pending: dict[str, DeliverToUser] = {}
        self._claims: dict[tuple[str, str], str] = {}
        self._claim_keys = {}
        self._followers: dict[str, list[str]] = {}
        self._leader_results = {}
        self._effect_owners = {}
        self._receipts: OrderedDict[str, UserDeliveryResult] = OrderedDict()
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="user-delivery",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                undelivered_sink=self._undelivered,
            )
        )
        self._effects = EffectLane(
            name="user-delivery-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=4,
        )

    def admit(self, request: UserDeliveryRequest) -> AdmissionResult:
        with self._guard:
            if self._closed:
                self._rejected += 1
                return AdmissionResult.CLOSED
            if not self._slots.acquire(blocking=False):
                self._rejected += 1
                return AdmissionResult.OVERLOADED
            command = DeliverToUser(
                request.attempt_id or uuid4().hex, self._generation, request
            )
            result = self._runtime.tell(self._handle, command)
            if result is not AdmissionResult.ACCEPTED:
                self._slots.release()
                self._rejected += 1
            return result

    def _undelivered(self, command, reason):
        # EffectLane retries completion until ACK; only never-begun ingress
        # needs explicit retirement here. Native work is never replayed.
        if isinstance(command, DeliverToUser):
            from .addressing import UserDeliveryResult

            self._publish(command.operation_id, UserDeliveryResult(
                command.request.message_id, False, code=reason,
                message="receiver restarted before delivery began",
                definitely_not_sent=True,
            ))
            self._slots.release()
            with self._guard:
                self._rejected += 1

    def _execute(self, effect):
        request = effect.command.request
        if isinstance(effect, PrepareDelivery):
            if not self._receiver.owns(request.owner):
                return DeliveryPrepared(None)
            profiles = getattr(self._receiver, "profiles", None)
            profile = profiles.resolve(request.owner) if profiles is not None else None
            return DeliveryPrepared(
                profile.owner_key if profile is not None else request.owner
            )
        if isinstance(effect, ExecuteDelivery):
            if self._ledger_owner is None:
                return self._receiver.handle(request)
            result = self._receiver._deliver_unrecorded(request)
            # This bounded I/O slot retains the native result and canonical
            # claim until durability succeeds. Retrying a ledger commit must
            # never call the native adapter again or publish a false refusal.
            while True:
                try:
                    return self._ledger_owner.record(request.idempotency_key, result)
                except Exception:
                    with self._guard:
                        self._persistence_retries += 1
                    self._persistence_pace.wait(0.05)
        if isinstance(effect, MarkJoinedDelivery):
            stored = (
                self._ledger_owner.mark_duplicate(request.idempotency_key)
                if self._ledger_owner is not None
                else None
            )
            return stored or replace(effect.result, duplicate=True)
        raise TypeError("unsupported user delivery effect")

    def _offer_effect(self, operation, effect, phase):
        token = uuid4().hex
        self._effect_owners[token] = (operation, phase)
        admitted = self._effects.submit(EffectRequest(token, self._generation, effect))
        if admitted is not AdmissionResult.ACCEPTED:
            self._effect_owners.pop(token, None)
            from .addressing import UserDeliveryResult

            with self._guard:
                self._rejected += 1
                pending = self._pending[operation]
            self._finish(
                operation,
                UserDeliveryResult(
                    pending.request.message_id,
                    False,
                    code="USER_DELIVERY_OVERLOADED",
                    message="receiver delivery capacity exhausted",
                ),
            )

    def _receive(self, command):
        from .addressing import UserDeliveryResult

        if isinstance(command, DeliverToUser):
            with self._guard:
                duplicate = (
                    command.operation_id in self._pending
                    or command.operation_id in self._receipts
                )
                if not duplicate and command.generation == self._generation:
                    self._pending[command.operation_id] = command
            if duplicate or command.generation != self._generation:
                self._slots.release()
                return
            self._offer_effect(
                command.operation_id, PrepareDelivery(command), "prepare"
            )
            return
        if not isinstance(command, EffectCompleted):
            raise TypeError("unsupported user delivery command")
        owned = self._effect_owners.pop(command.operation_id, None)
        self._effects.acknowledge(command.operation_id, command.generation)
        if owned is None or command.generation != self._generation:
            return
        operation, phase = owned
        with self._guard:
            pending = self._pending.get(operation)
            self._failed += command.error is not None
        if pending is None:
            return
        if phase == "joined":
            original = self._leader_results.pop(operation)
            duplicate = (
                command.result
                if command.error is None
                else replace(original, duplicate=True)
            )
            self._finish(operation, original, duplicate)
            return
        if command.error is not None:
            self._finish(
                operation,
                UserDeliveryResult(
                    pending.request.message_id,
                    False,
                    code=ipc_errors.USER_DELIVERY_FAILED,
                    message=command.error,
                ),
            )
            return
        if phase == "prepare":
            canonical_owner = command.result.canonical_owner
            if canonical_owner is None:
                self._finish(operation, None)
                return
            key = (canonical_owner, pending.request.idempotency_key)
            leader = self._claims.get(key)
            if leader is not None:
                self._followers.setdefault(leader, []).append(operation)
                return
            self._claims[key] = operation
            self._claim_keys[operation] = key
            self._offer_effect(operation, ExecuteDelivery(pending), "execute")
            return
        result = command.result
        if result is not None and self._followers.get(operation):
            self._leader_results[operation] = result
            self._offer_effect(operation, MarkJoinedDelivery(pending, result), "joined")
        else:
            self._finish(operation, result)

    def _finish(self, operation, result, duplicate=None):
        key = self._claim_keys.pop(operation, None)
        if key is not None and self._claims.get(key) == operation:
            self._claims.pop(key)
        followers = self._followers.pop(operation, [])
        self._leader_results.pop(operation, None)
        for index, token in enumerate((operation, *followers)):
            with self._guard:
                pending = self._pending.pop(token, None)
            if pending is None:
                continue
            if result is not None:
                receipt = (
                    result
                    if index == 0
                    else (duplicate or replace(result, duplicate=True))
                )
                self._publish(token, receipt)
            self._slots.release()

    def _publish(self, attempt, result):
        with self._guard:
            self._receipts[attempt] = result
            self._receipts.move_to_end(attempt)
            while len(self._receipts) > self._capacity:
                self._receipts.popitem(last=False)

    def receipt(self, attempt):
        with self._guard:
            return self._receipts.get(attempt)

    def snapshot(self):
        with self._guard:
            return UserDeliveryActorStatus(
                len(self._pending),
                self._rejected,
                len(self._receipts),
                self._closed,
                self._failed,
            )

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            snapshot = self._runtime.snapshot(self._handle)
            with self._guard:
                pending = bool(self._pending)
            if not pending and snapshot.queued == snapshot.in_flight == 0:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if self._ledger_owner is not None and not self._ledger_owner.close(
            max(0.0, deadline - time.monotonic())
        ):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
