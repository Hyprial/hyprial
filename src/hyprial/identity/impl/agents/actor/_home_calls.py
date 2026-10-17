"""In-process home-call custody, not durable migration recovery authority.

Only explicit submission reserves a call. Lookup never submits work. After a
process restart or completed-result eviction, lookup reports unknown: P2 must
reconcile its durable intent against identity/home receipts before deciding on
another submission. Pending calls are never aged out or capacity-evicted.
"""

from __future__ import annotations

import math
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from uuid import uuid4

from ._base import (
    AgentHomeAccessError, AgentHomeAccessPending, _HomeCall, _LegacyRuntimeResult,
    _PrepareRuntimeCommand, _PrepareWorkspaceCommand,
    _MaterialiseLandingHomeCommand, _ConfirmLandingHomeCommand, _VerifyLandingPayloadCommand,
)
from ..runtime.context import AgentRuntimeContext


class HomeOperationUnknown(AgentHomeAccessError):
    """No in-process receipt; this is not evidence that work did not happen."""

    disposition = "unknown-operation"


class HomeOperationConflict(AgentHomeAccessError):
    """The same operation id was presented with a different immutable input."""

    disposition = "operation-fingerprint-mismatch"


def home_operation_id(value: str | None) -> str:
    """Only omission mints an ID; invalid explicit IDs never become new work."""
    if value is None:
        return uuid4().hex
    if not isinstance(value, str) or not value.strip():
        raise ValueError("operation_id must be a non-empty string")
    return value


def command_fingerprint(
    command: _PrepareRuntimeCommand | _PrepareWorkspaceCommand | _MaterialiseLandingHomeCommand | _ConfirmLandingHomeCommand | _VerifyLandingPayloadCommand,
) -> str:
    payload = asdict(command)
    payload.pop("correlation_id")
    encoded = json.dumps(
        {"kind": type(command).__name__, "payload": payload},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def await_home_call(call: _HomeCall, operation_id: str, timeout: float):
    """Wait on retained custody outside the owner; a timeout never cancels it."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("home wait timeout must be finite and non-negative")
    if not call.event.wait(timeout):
        raise AgentHomeAccessPending(f"Agent home access remains unsettled: {operation_id}")
    if call.error is not None:
        raise call.error
    return call.value


@dataclass(frozen=True, slots=True)
class _Entry:
    fingerprint: str
    call: _HomeCall


class HomeCallRegistry:
    """Serialize registration and settlement; never execute an effect here.

    Defaults retain at most 128 completed results for 300 seconds from settlement,
    plus at most 32 pending calls (the existing admission budget). Completion age
    never extends on reads/replays. These are memory retention bounds, not a
    filesystem timeout, cancellation lease, or permission to retry an operation.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic,
        pending_capacity: int = 32, settled_capacity: int = 128,
        retention_seconds: float = 300.0,
    ) -> None:
        if pending_capacity <= 0 or settled_capacity <= 0:
            raise ValueError("home call capacities must be positive")
        if not math.isfinite(retention_seconds) or retention_seconds <= 0:
            raise ValueError("home result retention must be positive and finite")
        self._clock = clock
        self._pending_capacity = pending_capacity
        self._settled_capacity = settled_capacity
        self._retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, _Entry] = {}
        self._settled: OrderedDict[str, tuple[float, _Entry]] = OrderedDict()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _expire(self) -> None:
        now = self._clock()
        while self._settled:
            _operation, (completed_at, _entry) = next(iter(self._settled.items()))
            if now - completed_at < self._retention_seconds:
                break
            self._settled.popitem(last=False)

    def _find(self, operation_id: str) -> _Entry | None:
        pending = self._pending.get(operation_id)
        if pending is not None:
            return pending
        settled = self._settled.get(operation_id)
        return None if settled is None else settled[1]

    def reserve(self, operation_id: str, fingerprint: str) -> tuple[_HomeCall, bool]:
        """Explicit submission: attach if retained, otherwise reserve once.

        This method cannot distinguish a never-seen id from an evicted id. It is
        not the recovery query. A caller that previously submitted must use
        lookup and reconcile unknown instead of blindly invoking reserve again.
        After durable authority confirms absent, P2 may explicitly submit the
        same deterministic ID as NEW work. Fingerprint conflict detection only
        applies while an entry is retained; there are no persistent tombstones.
        """
        with self._lock:
            self._expire()
            entry = self._find(operation_id)
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    raise HomeOperationConflict(f"home operation input changed: {operation_id}")
                return entry.call, False
            if len(self._pending) >= self._pending_capacity:
                raise AgentHomeAccessError("Agent home access capacity is full")
            call = _HomeCall()
            self._pending[operation_id] = _Entry(fingerprint, call)
            return call, True

    def lookup(self, operation_id: str) -> _HomeCall:
        """Return existing custody/result; never reserve, restart or retry."""
        with self._lock:
            self._expire()
            entry = self._find(operation_id)
            if entry is None:
                raise HomeOperationUnknown(f"unknown home operation: {operation_id}")
            return entry.call

    def abandon_unstarted(self, operation_id: str, error: BaseException) -> None:
        """Release unaccepted/never-begun work and wake every attached caller.

        This is NOT cancellation of a running effect. Only the admission result
        or actor runtime's never-begun notification authorizes this path. No
        result is retained, so an explicit later submission can reserve anew.
        """
        with self._lock:
            entry = self._pending.pop(operation_id, None)
            if entry is None:
                return
            entry.call.error = error
            entry.call.event.set()

    def settle(
        self, operation_id: str,
        value: str | AgentRuntimeContext | _LegacyRuntimeResult | None,
        error: BaseException | None,
    ) -> None:
        with self._lock:
            entry = self._pending.pop(operation_id, None)
            if entry is None:
                return  # Duplicate/unknown completion cannot replace a result.
            self._expire()
            entry.call.value = value
            entry.call.error = error
            self._settled[operation_id] = (self._clock(), entry)
            while len(self._settled) > self._settled_capacity:
                self._settled.popitem(last=False)
            entry.call.event.set()
