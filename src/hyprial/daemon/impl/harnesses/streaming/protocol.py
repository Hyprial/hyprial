"""Streaming-turn protocol vocabulary and timeout resolution."""


import math
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Self

from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
)


if TYPE_CHECKING:
    from hyprial.identity import AgentRuntimeContext



#: Observer seam for turn failures, with the worker's spec context attached
#: (provider/model/name are not recoverable from the failure text alone).
#: Implemented by ``provider_auth.ProviderAuthCoordinator.handle_turn_failure``;
#: its contract is never-raises, so the pump does not defend against it.
class TurnFailureSpecObserver(Protocol):
    def __call__(
        self,
        failure: str,
        *,
        harness: str,
        provider: str | None,
        model: str | None,
        worker: str,
        runtime_context: "AgentRuntimeContext | None" = None,
    ) -> AdmissionResult | None: ...

class TurnCompletedObserver(Protocol):
    """Never-raises observer for a terminal headless turn."""

    def __call__(
        self,
        delivery: HarnessDelivery,
        result: HarnessResult,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        tool_names: tuple[str, ...],
    ) -> None: ...

@dataclass(frozen=True, slots=True)
class ProgressObservation:
    """A harness-side progress signal before the pump stamps delivery context.

    Turn clients yield these from ``receive_response`` alongside (never
    instead of) the terminal outcome.  The pump owns the delivery-scoped
    fields -- ``delivery_id``/``conversation_id``/``actor``/``harness`` come
    from the in-flight delivery, ``seq`` and ``emitted_at_ms`` are assigned
    here -- so a client only reports what the harness actually said.
    """

    phase: str
    summary: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    detail: dict[str, Any] | None = None
    terminal: bool = False

class TurnClient(Protocol):
    """One harness conversation that turns a prompt into a terminal result.

    ``receive_response`` yields two disjoint object shapes: outcome objects
    carrying ``result`` (str) and ``is_error`` (bool) -- the last such value
    is the turn outcome -- and :class:`ProgressObservation` side-channel
    values, which never affect the outcome.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> bool | None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[object]: ...

    async def interrupt(self) -> None: ...

TurnClientFactory = Callable[[], TurnClient]

TurnStartedObserver = Callable[[HarnessDelivery, TurnClient], None]

_STOP = object()

#: Per-actor bound on queued progress events (route C backpressure layer 1).
_PROGRESS_QUEUE_MAX = 256

_TURN_DELIVERY_MAX = 128

#: Bound on the fire-and-forget turn-failure observer queue.  The pump only
#: enqueues (never awaits) so a slow/hung owner notifier cannot stall the
#: turn's completion; when the observer drains slower than failures arrive,
#: the oldest excess is dropped and counted (review r2 final).
_TURN_FAILURE_OBSERVER_QUEUE_MAX = 16

#: Sentinel that asks the observer drain thread to exit.
_OBSERVER_STOP = object()

#: Retired wall-clock cap override (#277: no timeout kills a turn).  Still
#: read where old code paths resolve the legacy value, but nothing
#: enforces it.
TURN_TIMEOUT_ENV = "HYPRIAL_TURN_TIMEOUT_SECONDS"

#: Operator override for the producer-local quiet-period REPORT
#: sensitivity (worker.turn.stalled / worker.turn.resumed); 0 disables
#: reporting.  Nothing is killed on expiry.
TURN_IDLE_TIMEOUT_ENV = "HYPRIAL_TURN_IDLE_TIMEOUT_SECONDS"

#: Observation phases the pump also persists to the worker JSONL log, so
#: a stalled turn is visible to `hyprial top` and survives a restarted
#: consumer.  The phase names describe the CONDITION, not the detector --
#: connector-side steer probing (#277) can emit the same phases later.
_TURN_CONDITION_EVENTS = {
    "turn-stalled": "worker.turn.stalled",
    "turn-resumed": "worker.turn.resumed",
}

def resolve_turn_timeout_seconds(
    configured: float | None,
    *,
    default: float,
    env_var: str = TURN_TIMEOUT_ENV,
) -> float:
    """Timeout precedence: launch spec, then environment, then default.

    ``0`` disables the timeout (returned as ``math.inf``): the hard cap is a
    resource insurance an operator may explicitly waive, not a liveness
    check.  A malformed or negative value fails loudly at worker start;
    silently falling back would resurrect issue #270's invisible cap.
    """

    if configured is not None:
        if configured < 0:
            raise ValueError("turn timeout must not be negative (0 disables)")
        return math.inf if configured == 0 else configured
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return math.inf if default == 0 else default
    try:
        parsed = float(raw)
    except ValueError as error:
        raise ValueError(f"{env_var} must be a number, got {raw!r}") from error
    if parsed < 0:
        raise ValueError(
            f"{env_var} must not be negative (0 disables), got {raw!r}"
        )
    return math.inf if parsed == 0 else parsed
