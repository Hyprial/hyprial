from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected
from hyprial.contracts.readiness import ReadinessReport

from .api import HarnessDelivery, HarnessResult


@dataclass(frozen=True, slots=True)
class HarnessLaunchProjection:
    harness: str
    name: str
    headless: bool
    args: tuple[str, ...] = ()
    ownership: str = "managed"
    nickname: str | None = None
    cwd: str | None = None
    endpoint: str | None = None
    session_ref: str | None = None
    command: tuple[str, ...] = ()
    turn_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None
    containerized: bool = False
    pinned_owner: str | None = None
    container_image: str | None = None
    model_provider: str | None = None
    model: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "provider": self.harness,
            "name": self.name,
            "headless": self.headless,
            "args": list(self.args),
            "ownership": self.ownership,
            **({"nickname": self.nickname} if self.nickname is not None else {}),
            **({"cwd": self.cwd} if self.cwd is not None else {}),
            **({"endpoint": self.endpoint} if self.endpoint is not None else {}),
            **(
                {"sessionRef": self.session_ref} if self.session_ref is not None else {}
            ),
            **({"command": list(self.command)} if self.command else {}),
            **(
                {"turnTimeoutSeconds": self.turn_timeout_seconds}
                if self.turn_timeout_seconds is not None
                else {}
            ),
            **(
                {"idleTimeoutSeconds": self.idle_timeout_seconds}
                if self.idle_timeout_seconds is not None
                else {}
            ),
            **({"containerized": True} if self.containerized else {}),
            **(
                {"pinnedOwner": self.pinned_owner}
                if self.pinned_owner is not None
                else {}
            ),
            **(
                {"containerImage": self.container_image}
                if self.container_image is not None
                else {}
            ),
            **(
                {"modelProvider": self.model_provider}
                if self.model_provider is not None
                else {}
            ),
            **({"model": self.model} if self.model is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class EnsureHarnessCommand:
    correlation_id: str
    spec: HarnessLaunchProjection


@dataclass(frozen=True, slots=True)
class RemoveHarnessCommand:
    correlation_id: str
    harness: str
    name: str
    interruption_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessTimerElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class StopHarnessesCommand:
    correlation_id: str
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class RestoreHarnessesCommand:
    correlation_id: str
    specs: tuple[HarnessLaunchProjection, ...]


@dataclass(frozen=True, slots=True)
class DrainHarnessFailedCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DrainHarnessReadinessCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class WaitHarnessReadyCommand:
    correlation_id: str
    harness: str
    name: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DispatchHarnessDeliveryCommand:
    correlation_id: str
    name: str
    delivery: HarnessDelivery


@dataclass(frozen=True, slots=True)
class DrainHarnessResultsCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DrainHarnessProgressCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class BindHarnessLivenessCommand:
    correlation_id: str
    harness: str
    name: str
    binding: object


@dataclass(frozen=True, slots=True)
class HarnessStartTimerElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    harness_id: str


@dataclass(frozen=True, slots=True)
class RefreshHarnessProjectionsCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ReconcileHarnessSessionRefsCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class StageHarnessDesiredCommand:
    correlation_id: str
    spec: HarnessLaunchProjection


@dataclass(frozen=True, slots=True)
class SnapshotAdapterRegistrationCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class RemoveAdapterRegistrationCommand:
    correlation_id: str
    name: str
    expected_spec: HarnessLaunchProjection | None
    expected_legacy_pin: str | None


@dataclass(frozen=True, slots=True)
class RestoreAdapterRegistrationCommand:
    correlation_id: str
    name: str
    spec: HarnessLaunchProjection | None
    legacy_pin: str | None


HarnessCommand: TypeAlias = (
    EnsureHarnessCommand
    | RemoveHarnessCommand
    | HarnessTimerElapsedCommand
    | StopHarnessesCommand
    | RestoreHarnessesCommand
    | DrainHarnessFailedCommand
    | DrainHarnessReadinessCommand
    | WaitHarnessReadyCommand
    | DispatchHarnessDeliveryCommand
    | DrainHarnessResultsCommand
    | DrainHarnessProgressCommand
    | BindHarnessLivenessCommand
    | HarnessStartTimerElapsedCommand
    | RefreshHarnessProjectionsCommand
    | ReconcileHarnessSessionRefsCommand
    | StageHarnessDesiredCommand
    | SnapshotAdapterRegistrationCommand
    | RemoveAdapterRegistrationCommand
    | RestoreAdapterRegistrationCommand
)


@dataclass(frozen=True, slots=True)
class HarnessStatusProjection:
    version: int
    harness_id: str
    runtime: str
    name: str
    running: bool
    pid: int | None = None
    starting: bool = False
    # Retry posture derived from the actor's failure counter: "retrying"
    # while failures accumulate inside the budget, "failed" once it is spent
    # (terminal; only an explicit start revives), None while never failed.
    state: str | None = None
    error: str | None = None
    endpoint: str | None = None
    # DSH-only: the private per-worker DSH_HOME the current generation spawned
    # against.  Reported so an operator (and E2E-014) can locate the real home
    # instead of the retired shared ``~/.dsh`` default.
    dsh_home: str | None = None
    max_in_flight: int | None = None
    in_flight: int | None = None
    queue_depth: int | None = None

    @property
    def quarantined(self) -> bool:
        """Deprecated alias for ``state == "failed"``, kept one version for
        the existing ``ps`` ``connectors[]`` consumers; delete next major."""

        return self.state == "failed"

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.harness_id,
            "runtime": self.runtime,
            "name": self.name,
            "running": self.running,
            "pid": self.pid,
            # Absent means ``None``: the payload only names failure accounting
            # when a harness is actually in it.
            **({"state": self.state} if self.state is not None else {}),
            **({"starting": True} if self.starting else {}),
            # Deprecated wire key: mapped from ``state`` for one version.
            **({"quarantined": True} if self.quarantined else {}),
            **({"error": self.error} if self.error is not None else {}),
            **({"endpoint": self.endpoint} if self.endpoint is not None else {}),
            **({"dshHome": self.dsh_home} if self.dsh_home is not None else {}),
            **({"maxInFlight": self.max_in_flight} if self.max_in_flight is not None else {}),
            **({"inFlight": self.in_flight} if self.in_flight is not None else {}),
            **({"queueDepth": self.queue_depth} if self.queue_depth is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class HarnessStreamingProjection:
    version: int
    actors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessSessionRefProjection:
    harness: str
    name: str
    session_ref: str


@dataclass(frozen=True, slots=True)
class HarnessSessionRefsProjection:
    version: int
    refs: tuple[HarnessSessionRefProjection, ...]


@dataclass(frozen=True, slots=True)
class HarnessRestoreProjection:
    attempted: int
    restored: int
    failed: int
    # Targets the round dispositioned as "deferred" -- a lifecycle effect
    # owned the resource, so no start attempt of this round settled them.
    # Carried explicitly so `attempted` never has an unexplained remainder
    # (attempted - restored - failed - deferred == 0 for every batch).
    deferred: int = 0


@dataclass(frozen=True, slots=True)
class HarnessMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    changed: bool


@dataclass(frozen=True, slots=True)
class HarnessRestoreCompleted:
    correlation_id: str
    generation: int
    version: int
    result: HarnessRestoreProjection


@dataclass(frozen=True, slots=True)
class HarnessTimerCompleted:
    correlation_id: str
    generation: int
    version: int
    restarted: int


@dataclass(frozen=True, slots=True)
class HarnessFailedDrained:
    correlation_id: str
    generation: int
    version: int
    harness_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessReadinessDrained:
    correlation_id: str
    generation: int
    version: int
    reports: tuple[ReadinessReport, ...]


@dataclass(frozen=True, slots=True)
class HarnessReadyObserved:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    ready: bool


@dataclass(frozen=True, slots=True)
class HarnessDeliveryAdmitted:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    delivery_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class HarnessProgressObserved:
    correlation_id: str
    generation: int
    version: int
    progress: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class HarnessResultObserved:
    correlation_id: str
    generation: int
    version: int
    results: tuple[HarnessResult, ...]


@dataclass(frozen=True, slots=True)
class HarnessLivenessBound:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    changed: bool


@dataclass(frozen=True, slots=True)
class HarnessProjectionsRefreshed:
    correlation_id: str
    generation: int
    version: int


@dataclass(frozen=True, slots=True)
class HarnessSessionRefsReconciled:
    correlation_id: str
    generation: int
    version: int
    refs: tuple[HarnessSessionRefProjection, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessAdapterRegistrationProjection:
    name: str
    spec: HarnessLaunchProjection | None
    legacy_pin: str | None


@dataclass(frozen=True, slots=True)
class HarnessDesiredStateManaged:
    correlation_id: str
    generation: int
    version: int
    operation: str
    changed: bool
    adapter: HarnessAdapterRegistrationProjection | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class HarnessProcessStarted:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    pid: int | None
    process: object | None = None
    identity_marker: str | None = None
    error: BaseException | None = None
    processed_receipt: object | None = None


@dataclass(frozen=True, slots=True)
class HarnessProcessExited:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    exit_code: int | None
    expected: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessStopIoCompleted:
    correlation_id: str
    generation: int
    version: int
    harness_id: str
    stopped: bool
    detail: str | None = None
    processed_receipt: object | None = None


@dataclass(frozen=True, slots=True)
class HarnessCallIoCompleted:
    correlation_id: str
    generation: int
    version: int
    value: object | None = None
    error: BaseException | None = None
    processed_receipt: object | None = None


@dataclass(frozen=True, slots=True)
class HarnessesStopped:
    correlation_id: str
    generation: int
    version: int
    remaining: tuple[str, ...]
    drain_complete: bool = True


HarnessEvent: TypeAlias = (
    HarnessMutationCompleted
    | HarnessRestoreCompleted
    | HarnessTimerCompleted
    | HarnessFailedDrained
    | HarnessReadinessDrained
    | HarnessReadyObserved
    | HarnessDeliveryAdmitted
    | HarnessProgressObserved
    | HarnessResultObserved
    | HarnessLivenessBound
    | HarnessProjectionsRefreshed
    | HarnessDesiredStateManaged
    | HarnessSessionRefsReconciled
    | HarnessProcessStarted
    | HarnessProcessExited
    | HarnessStopIoCompleted
    | HarnessCallIoCompleted
    | HarnessesStopped
    | PortCommandRejected
)
HarnessCommandSink: TypeAlias = CommandSink[HarnessCommand]
HarnessEventSink: TypeAlias = EventSink[HarnessEvent]


class HarnessProjectionPort(Protocol):
    def read_harness(self, harness_id: str) -> HarnessStatusProjection | None: ...

    def read_harnesses(self) -> tuple[HarnessStatusProjection, ...]: ...

    def read_streaming(self) -> HarnessStreamingProjection: ...

    def read_session_refs(self) -> HarnessSessionRefsProjection: ...

    def read_worker_session_refs(self) -> HarnessSessionRefsProjection: ...
