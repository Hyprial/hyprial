"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig

from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    HarnessDelivery,
)
class _WorkerProcess(Protocol):
    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int: ...

    @property
    def readiness(self) -> str: ...

    @property
    def last_sdk_output(self) -> str | None: ...

    @property
    def error(self) -> str | None: ...

    @property
    def health(self) -> dict[str, object]: ...

    def wait_ready(self, timeout: float | None = None) -> str: ...

    def drain_health_events(self) -> tuple[dict[str, object], ...]: ...

    def deliver_reply(self, delivery: HarnessDelivery) -> bool: ...

    def deliver_alarm(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> bool: ...

    def stop(self, timeout: float = 5.0) -> None: ...


class _Launcher(Protocol):
    startup_timeout: float

    def spawn(self, gateway: LarkGatewayConfig) -> _WorkerProcess: ...


class AdapterDesiredStatePort(Protocol):
    """Blocking persistence effect owned by the Adapter actor operation."""

    def activate(self, name: str) -> bool: ...

    def deactivate(self, name: str) -> bool: ...


class _NullDesiredStatePort:
    """Compatibility boundary for isolated AdapterRuntime unit tests."""

    def activate(self, name: str) -> bool:
        del name
        return False

    def deactivate(self, name: str) -> bool:
        del name
        return False


@dataclass(frozen=True, slots=True)
class AdapterRestoreSummary:
    attempted: int
    restored: int
    failed: int


@dataclass(frozen=True, slots=True)
class _Observation:
    running: bool
    readiness: str | None
    pid: int | None
    error: str | None
    attempt_token: str | None = None
    health: Mapping[str, object] = field(default_factory=dict)
    events: tuple[dict[str, object], ...] = ()
    last_sdk_output: str | None = None


@dataclass(frozen=True, slots=True)
class _IoResult:
    correlation_id: str
    generation: int
    version: int
    name: str
    operation: str
    succeeded: bool
    attempt_token: str | None = None
    observation: _Observation | None = None
    value: object = None
    code: str | None = None
    detail: str | None = None


#: Failure codes meaning the resource is still held: the rollback itself did
#: not succeed, so the worker is alive even though the operation failed.
_UNSETTLED_CODES = frozenset({"WORKER_STOP_FAILED"})


def _released(result: _IoResult) -> bool:
    """Whether the worker this result concerns is actually gone.

    The effect side already keeps a worker that refused to stop both
    registered and running -- see the compensating-stop path.  Completion then
    has to honour that: discarding the desired entry for a worker that is
    still alive makes the projection report "should not be running" while the
    process runs, and reconcile finds nothing to adopt because desired is
    empty.  Cleanup must not delete the record before the resource is really
    released; a failed rollback degrades the entry to unsettled, never to
    absent.
    """

    return result.code not in _UNSETTLED_CODES


@dataclass(frozen=True, slots=True)
class _Start:
    correlation_id: str
    name: str
    result: Future[bool]
    explicit: bool = True


@dataclass(frozen=True, slots=True)
class _Remove:
    correlation_id: str
    name: str
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _Reload:
    channels: ChannelConfiguration
    result: Future[dict[str, list[str]]]


@dataclass(frozen=True, slots=True)
class _Restore:
    names: tuple[str, ...]
    result: Future[AdapterRestoreSummary]


@dataclass(frozen=True, slots=True)
class _Reconcile:
    result: Future[int]


@dataclass(frozen=True, slots=True)
class _Refresh:
    result: Future[None]


@dataclass(frozen=True, slots=True)
class _DrainEvents:
    result: Future[tuple[dict[str, object], ...]]


@dataclass(frozen=True, slots=True)
class _DeliverReply:
    correlation_id: str
    name: str
    delivery: HarnessDelivery
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _DeliverAlarm:
    correlation_id: str
    name: str
    alarm_correlation_id: str
    text: str
    idempotency_key: str
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _Shutdown:
    correlation_id: str
    result: Future[None]
    deadline: float


@dataclass(slots=True)
class _Aggregate:
    kind: str
    future: Future[Any]
    remaining: set[str]
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    restarted: int = 0
