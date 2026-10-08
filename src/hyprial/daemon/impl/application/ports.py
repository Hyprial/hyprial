"""Injection seams for upper-layer (biz/shell) capabilities.

The daemon domain must not import ``hyprial.biz``: these Protocol ports are
required dependencies that the shell composition injects at
``DaemonApplication``/``from_environment`` construction.  They deliberately
carry no defaults, no lazy fallbacks and no biz types; shell-side adapters
own the biz classes and translate their errors into the port errors defined
here.  Exact factory signatures and call-site lists live in
docs/evidence/refactor-joint-2026-10-02/tasks/daemon-runtime/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from hyprial.kernel import PortAdmission, ipc_errors


# --------------------------------------------------------------------------- #
# Usage collection (biz usage actor) and quota watchdog
# --------------------------------------------------------------------------- #


@runtime_checkable
class UsageCollection(Protocol):
    """The usage cache the daemon starts, observes and reports from."""

    def snapshots(self) -> tuple[Any, ...]: ...

    def set_refresh_observer(self, observer: Callable[[], None]) -> None: ...

    def start(self) -> None: ...

    def stop(self, *, timeout: float = 2.0) -> bool: ...

    def snapshot_payload(self, now_ms: int) -> dict[str, Any]: ...


UsageCollectionFactory = Callable[[], "UsageCollection | None"]


@runtime_checkable
class QuotaEvaluatorPort(Protocol):
    """Upper-layer quota policy injected into the daemon's async watchdog.

    The daemon never imports the biz implementation; shell composition
    constructs the real evaluator and passes it in.  The authority mutates
    ``_deliver``, ``_clock_ms`` and snapshots ``_state`` around each batch.
    """

    _state: dict[str, Any]
    _path: Path
    _deliver: Callable[[str, str], bool]
    _clock_ms: Callable[[], int]
    _readings: Callable[[], tuple[Any, ...]]

    def _save(self) -> None: ...

    def observe_readings(self) -> list[Any]: ...

    def observe_usage_limit_failure(self, actor: str) -> Any | None: ...

    def flush_failures(self) -> Any | None: ...


@dataclass(frozen=True)
class QuotaWatchdogDeps:
    """Daemon-owned inputs of the quota watchdog the shell adapter wires."""

    state_dir: Path
    deliver: Callable[[str, str], Any]
    readings: Callable[[], tuple[Any, ...]]
    clock_ms: Callable[[], int]
    on_alert: Callable[[Any], None]


QuotaEvaluatorFactory = Callable[[QuotaWatchdogDeps], QuotaEvaluatorPort]
"""Builds the real upper-layer evaluator from daemon-owned runtime inputs."""


@runtime_checkable
class QuotaWatchdogPort(Protocol):
    """Keeps the original watchdog contract: close() and late callbacks."""

    def close(self) -> None: ...


QuotaWatchdogFactory = Callable[[QuotaWatchdogDeps], "QuotaWatchdogPort | None"]
"""Returns None only when the watchdog cannot start (loudly logged, as today)."""


# --------------------------------------------------------------------------- #
# Routine runtime (biz routine service / PAC dispatch / coordinator)
# --------------------------------------------------------------------------- #


class RoutinePortError(RuntimeError):
    """Port translation of a routine service/coordinator failure."""

    def __init__(self, code: str, detail: str, data: Any = None) -> None:
        super().__init__(detail)
        self.code = code
        self.data = data


class RoutineSchemaPortError(RoutinePortError):
    """Port translation of a routine schema validation failure."""


class RoutinePortTimeout(RoutinePortError):
    """An accepted routine operation did not settle inside its wait."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(
            ipc_errors.ROUTINE_UNAVAILABLE,
            f"routine operation {operation_id} remains accepted",
        )
        self.operation_id = operation_id


@runtime_checkable
class RoutineSpecPort(Protocol):
    """The parsed routine spec fields the daemon reads."""

    @property
    def name(self) -> str: ...

    @property
    def actor(self) -> str | None: ...

    @property
    def produces(self) -> str | None: ...


RoutineTextLoader = Callable[[str], "RoutineSpecPort"]
"""Parses routine YAML; raises RoutineSchemaPortError on invalid text."""

RoutineTemplateRenderer = Callable[..., str]
"""Renders a routine template by name with keyword substitutions."""


@runtime_checkable
class RoutineServicePort(Protocol):
    """The routine service surface the daemon drives."""

    @property
    def migrated_u3(self) -> dict[str, Any]: ...

    def recover(self) -> int: ...

    def submit_timer(self, observed_at_ms: int | None = None) -> PortAdmission: ...

    def close(self) -> None: ...

    def list(self) -> dict[str, Any]: ...

    def status(self, *, name: str) -> dict[str, Any]: ...

    def add(self, *, yaml_text: str, owner: str, enabled: bool) -> dict[str, Any]: ...

    def remove(self, *, name: str, reservation_id: str | None = None) -> dict[str, Any]: ...

    def reserve_remove(self, *, name: str, reservation_id: str, enforce_last: bool) -> dict[str, Any]: ...

    def cancel_remove(self, *, name: str, reservation_id: str) -> None: ...

    def pause(self, *, name: str) -> dict[str, Any]: ...

    def resume(self, *, name: str, align_schedule: bool = False) -> dict[str, Any]: ...

    def set(self, *, name: str, yaml_text: str) -> dict[str, Any]: ...

    def address_migrations(self) -> dict[str, Any]: ...


@runtime_checkable
class RoutineCoordinatorPort(Protocol):
    """The serialized routine command bus the daemon submits through."""

    def stats(self) -> Any: ...

    def begin_add(
        self,
        *,
        operation_id: str,
        name: str,
        yaml_text: str,
        owner: str,
        produces: str | None,
        agent_compensation: Any | None = None,
    ) -> Any: ...

    def begin_remove(self, *, operation_id: str, name: str, enforce_last: bool) -> Any: ...

    def wait(self, operation_id: str, *, timeout: float) -> Any: ...

    def close(self, timeout: float = 5.0) -> bool: ...


@dataclass(frozen=True)
class RoutineRuntimeDeps:
    """Daemon-owned inputs of the routine runtime the shell adapter wires."""

    state_dir: Path
    hyprial_home: Path
    owner: str
    logger: Any
    alarm: Any
    deliver_task: Any
    clock_ms: Callable[[], int]
    resolve_principal: Callable[[str], str]
    migrate_address: Callable[[str], "str | None"]
    workflow_service: Any | None = None
    graph_authority: Any | None = None
    ensure_coordinator: Any = None
    retire_coordinator: Any = None
    close_graph: Any = None
    compensate_agent: Any = None


@dataclass
class RoutineRuntime:
    """What the factory hands back: service, coordinator and a store probe."""

    service: RoutineServicePort
    coordinator: RoutineCoordinatorPort | None = None
    routine_exists: Callable[[str], bool] | None = None


RoutineRuntimeFactory = Callable[[RoutineRuntimeDeps], "RoutineRuntime | None"]
"""Returns None when the routine runtime cannot start (degraded, as today)."""
