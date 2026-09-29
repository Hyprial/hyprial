"""Persistent custody and conservative collection of detached harness processes."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.persistent_config import atomic_json_write

from .api import (
    ManagedHarnessProcess,
    ProcessLiveness,
    ProcessLivenessProbeError,
    ProcessLivenessState,
)

_SCHEMA = "hyprial.orphan-processes/v1"
_DEFAULT_UNKNOWN_THRESHOLD_SECONDS = 60.0


@dataclass(slots=True)
class _OrphanEntry:
    orphan_id: str
    harness_id: str
    pid: int | None
    marker: str | None
    observed: bool
    first_seen_ms: int
    owner_id: str | None
    orphan_since_ms: int | None = None
    last_probe_ms: int | None = None
    gc_attempts: int = 0
    state: str = ProcessLivenessState.UNKNOWN.value
    detail: str | None = None
    unknown_since_ms: int | None = None
    last_probe_error: str | None = None


class OrphanProcessRegistry:
    """Persist stop custody until a positive ``dead`` observation retires it."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        identity_reader: Callable[[int], str | None],
        clock_ms: Callable[[], int] | None = None,
        unknown_threshold_seconds: float = _DEFAULT_UNKNOWN_THRESHOLD_SECONDS,
        logger: Callable[..., None] | None = None,
        cleanup_async: bool = True,
    ) -> None:
        self._path = path
        self._identity_reader = identity_reader
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._unknown_threshold_ms = max(0, int(unknown_threshold_seconds * 1000))
        self._logger = logger
        self._cleanup_async = cleanup_async
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._owner_id = uuid4().hex
        self._entries = self._load()
        self._processes: dict[str, ManagedHarnessProcess] = {}
        self._process_keys: dict[int, str] = {}
        self._cleanup_in_flight: set[str] = set()
        if self._entries:
            now_ms = self._clock_ms()
            for entry in self._entries.values():
                entry.owner_id = None
                entry.orphan_since_ms = entry.orphan_since_ms or now_ms
            with self._lock:
                self._save_locked()

    def observe_start(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> str:
        """Persist a live generation so a later daemon can inherit custody."""

        with self._lock:
            orphan_id, entry = self._ensure_entry_locked(
                harness_id, process, pid=pid, marker=marker
            )
            entry.owner_id = self._owner_id
            entry.orphan_since_ms = None
            self._save_locked()
            return orphan_id

    def observe_stop(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> str:
        """Take orphan cleanup custody before a stop can block or fail."""

        with self._lock:
            orphan_id, entry = self._ensure_entry_locked(
                harness_id, process, pid=pid, marker=marker
            )
            entry.owner_id = None
            entry.orphan_since_ms = entry.orphan_since_ms or self._clock_ms()
            self._save_locked()
            return orphan_id

    def _ensure_entry_locked(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> tuple[str, _OrphanEntry]:
        process_key = id(process)
        existing = self._process_keys.get(process_key)
        if existing is not None:
            return existing, self._entries[existing]
        orphan_id = (
            f"{harness_id}:{pid}:{marker}"
            if pid is not None and marker is not None
            else f"{harness_id}:unidentified:{uuid4().hex}"
        )
        entry = self._entries.get(orphan_id)
        if entry is None:
            entry = _OrphanEntry(
                orphan_id=orphan_id,
                harness_id=harness_id,
                pid=pid,
                marker=marker,
                observed=pid is not None,
                first_seen_ms=self._clock_ms(),
                owner_id=self._owner_id,
            )
            self._entries[orphan_id] = entry
        self._processes[orphan_id] = process
        self._process_keys[process_key] = orphan_id
        return orphan_id, entry

    def collect_once(self) -> int:
        """Probe every retained target; retire only a confirmed-dead target."""

        with self._collect_lock:
            return self._collect_once()

    def _collect_once(self) -> int:
        with self._lock:
            # Current-daemon entries are live-process custody records, not
            # orphans.  Their owning harness actor observes exit and calls
            # ``observe_stop`` before cleanup; a daemon restart clears the old
            # owner in ``__init__``.  Scanning them here made the one-second
            # reconcile tick re-run every process liveness/identity probe even
            # when there was nothing to reap (hundreds of ``ps`` calls on
            # macOS).  Only ownerless entries need orphan collection.
            orphan_ids = tuple(
                orphan_id
                for orphan_id, entry in self._entries.items()
                if entry.owner_id != self._owner_id
            )
        retired = 0
        for orphan_id in orphan_ids:
            with self._lock:
                entry = self._entries.get(orphan_id)
                process = self._processes.get(orphan_id)
            if entry is None:
                continue
            owned_by_current_daemon = entry.owner_id == self._owner_id
            now_ms = self._clock_ms()
            try:
                observation = self._observe(entry, process)
            except ProcessLivenessProbeError as error:
                with self._lock:
                    current = self._entries.get(orphan_id)
                    if current is None:
                        continue
                    if not owned_by_current_daemon:
                        current.gc_attempts += 1
                    current.last_probe_ms = now_ms
                    current.state = "probe-failed"
                    current.last_probe_error = str(error)
                    current.detail = str(error)
                    self._save_locked()
                self._log("error", "harness.orphan.probe_failed", entry, str(error))
                continue
            with self._lock:
                current = self._entries.get(orphan_id)
                if current is None:
                    continue
                if not owned_by_current_daemon:
                    current.gc_attempts += 1
                current.last_probe_ms = now_ms
                current.state = observation.state.value
                current.observed = observation.observed
                current.pid = observation.pid if observation.pid is not None else current.pid
                current.marker = (
                    observation.marker
                    if observation.marker is not None
                    else current.marker
                )
                current.detail = observation.detail
                current.last_probe_error = None
                if (
                    not owned_by_current_daemon
                    and observation.state is ProcessLivenessState.UNKNOWN
                ):
                    current.unknown_since_ms = current.unknown_since_ms or now_ms
                else:
                    current.unknown_since_ms = None
                if observation.state is ProcessLivenessState.DEAD:
                    self._entries.pop(orphan_id, None)
                    owned = self._processes.pop(orphan_id, None)
                    if owned is not None:
                        self._process_keys.pop(id(owned), None)
                    self._cleanup_in_flight.discard(orphan_id)
                    self._save_locked()
                    retired += 1
                    self._log("info", "harness.orphan.retired", entry, None)
                    continue
                self._save_locked()
            if (
                not owned_by_current_daemon
                and observation.state is ProcessLivenessState.ALIVE
            ):
                self._request_cleanup(orphan_id, entry, process)
        return retired

    def status(self) -> tuple[dict[str, object], ...]:
        now_ms = self._clock_ms()
        with self._lock:
            entries = tuple(
                entry
                for entry in self._entries.values()
                if entry.owner_id != self._owner_id
            )
        rows: list[dict[str, object]] = []
        for entry in sorted(entries, key=lambda item: item.orphan_id):
            unknown_for = (
                None
                if entry.unknown_since_ms is None
                else max(0, now_ms - entry.unknown_since_ms)
            )
            rows.append(
                {
                    "orphanId": entry.orphan_id,
                    "harnessId": entry.harness_id,
                    "state": entry.state,
                    "observed": entry.observed,
                    "pid": entry.pid,
                    "marker": entry.marker,
                    "firstSeenAtMs": entry.first_seen_ms,
                    "orphanSinceMs": entry.orphan_since_ms,
                    "lastProbeAtMs": entry.last_probe_ms,
                    "gcAttempts": entry.gc_attempts,
                    "unknownSinceMs": entry.unknown_since_ms,
                    "unknownForMs": unknown_for,
                    "unknownOverThreshold": bool(
                        unknown_for is not None
                        and unknown_for >= self._unknown_threshold_ms
                    ),
                    **(
                        {"detail": entry.detail}
                        if entry.detail is not None
                        else {}
                    ),
                    **(
                        {"lastProbeError": entry.last_probe_error}
                        if entry.last_probe_error is not None
                        else {}
                    ),
                }
            )
        return tuple(rows)

    def _observe(
        self,
        entry: _OrphanEntry,
        process: ManagedHarnessProcess | None,
    ) -> ProcessLiveness:
        if process is not None:
            try:
                observation = process.liveness()
            except AttributeError as error:
                raise ProcessLivenessProbeError(
                    "managed process does not implement liveness()"
                ) from error
            if (
                observation.state is not ProcessLivenessState.UNKNOWN
                or not observation.observed
            ):
                return observation
            pid = observation.pid if observation.pid is not None else entry.pid
            marker = (
                observation.marker
                if observation.marker is not None
                else entry.marker
            )
            if pid is None:
                return observation
            if marker is None:
                marker = self._read_identity(pid)
                if marker is None:
                    return observation
            return self._probe_identity(pid, marker)
        if entry.pid is None or entry.marker is None:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=entry.observed,
                pid=entry.pid,
                marker=entry.marker,
                detail="no pid and identity marker are available",
            )
        return self._probe_identity(entry.pid, entry.marker)

    def _read_identity(self, pid: int) -> str | None:
        try:
            return self._identity_reader(pid)
        except Exception as error:
            raise ProcessLivenessProbeError(
                f"identity probe failed for pid {pid}: {error}"
            ) from error

    def _probe_identity(self, pid: int, expected_marker: str) -> ProcessLiveness:
        marker = self._read_identity(pid)
        if marker is not None:
            if marker != expected_marker:
                raise ProcessLivenessProbeError(
                    f"process identity changed for pid {pid}"
                )
            return ProcessLiveness(
                ProcessLivenessState.ALIVE,
                observed=True,
                pid=pid,
                marker=expected_marker,
            )
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return ProcessLiveness(
                ProcessLivenessState.DEAD,
                observed=True,
                pid=pid,
                marker=expected_marker,
            )
        except OSError as error:
            raise ProcessLivenessProbeError(
                f"existence probe failed for pid {pid}: {error}"
            ) from error
        return ProcessLiveness(
            ProcessLivenessState.UNKNOWN,
            observed=True,
            pid=pid,
            marker=expected_marker,
            detail="process exists but its identity cannot be read",
        )

    def _request_cleanup(
        self,
        orphan_id: str,
        entry: _OrphanEntry,
        process: ManagedHarnessProcess | None,
    ) -> None:
        with self._lock:
            if orphan_id in self._cleanup_in_flight:
                return
            self._cleanup_in_flight.add(orphan_id)

        def cleanup() -> None:
            try:
                if process is not None:
                    process.stop()
                elif entry.pid is not None and entry.marker is not None:
                    marker = self._identity_reader(entry.pid)
                    if marker != entry.marker:
                        raise ProcessLivenessProbeError(
                            f"process identity changed before cleanup for pid {entry.pid}"
                        )
                    os.killpg(entry.pid, signal.SIGTERM)
            except Exception as error:
                now_ms = self._clock_ms()
                with self._lock:
                    current = self._entries.get(orphan_id)
                    if current is not None:
                        current.gc_attempts += 1
                        current.last_probe_ms = now_ms
                        current.state = "cleanup-failed"
                        current.last_probe_error = str(error)
                        current.detail = str(error)
                        self._save_locked()
                self._log("error", "harness.orphan.cleanup_failed", entry, str(error))
            finally:
                with self._lock:
                    self._cleanup_in_flight.discard(orphan_id)

        if self._cleanup_async:
            threading.Thread(
                target=cleanup,
                name=f"hyprial-orphan-gc-{orphan_id}",
                daemon=True,
            ).start()
        else:
            cleanup()

    def _load(self) -> dict[str, _OrphanEntry]:
        if self._path is None:
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
            raise ProcessLivenessProbeError("orphan process registry has invalid schema")
        rows = raw.get("orphans")
        if not isinstance(rows, list):
            raise ProcessLivenessProbeError("orphan process registry rows are invalid")
        try:
            return {
                str(item["orphan_id"]): _OrphanEntry(**item)
                for item in rows
                if isinstance(item, dict)
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessLivenessProbeError(
                f"orphan process registry contains invalid input: {error}"
            ) from error

    def _save_locked(self) -> None:
        if self._path is None:
            return
        atomic_json_write(
            self._path,
            {"schema": _SCHEMA, "orphans": [asdict(item) for item in self._entries.values()]},
        )

    def _log(
        self,
        level: str,
        event: str,
        entry: _OrphanEntry,
        detail: str | None,
    ) -> None:
        if self._logger is not None:
            self._logger(
                level,
                "daemon",
                event,
                orphan_id=entry.orphan_id,
                harness_id=entry.harness_id,
                pid=entry.pid,
                detail=detail,
            )


@dataclass(frozen=True, slots=True)
class ProcessCustodyCommand:
    operation_id: str
    generation: int
    kind: str
    harness_id: str = ""
    pid: int | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCustodyCompleted:
    operation_id: str
    generation: int
    orphan_id: str | None
    retired: int
    rows: tuple[tuple[tuple[str, object], ...], ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCustodyProjection:
    generation: int
    version: int
    pending: int
    rejected: int
    failed: int
    rows: tuple[tuple[tuple[str, object], ...], ...]


class ProcessCustodyOverloaded(RuntimeError):
    pass


class _CustodyWaiter:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.result: ProcessCustodyCompleted | None = None
        self.error: BaseException | None = None


class OrphanProcessAuthority:
    """One ordered custody lane owns process references, probes and JSON state.

    The actor admits typed requests and publishes immutable projections.
    Its fixed effect lane is the only caller of the retained registry. Process
    objects stay in local custody under a short lock; commands carry only a
    token, PID and birth marker. Accepted work survives caller timeout.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        identity_reader: Callable[[int], str | None],
        clock_ms: Callable[[], int] | None = None,
        unknown_threshold_seconds: float = _DEFAULT_UNKNOWN_THRESHOLD_SECONDS,
        logger: Callable[..., None] | None = None,
        capacity: int = 64,
        timeout: float = 10.0,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._registry = OrphanProcessRegistry(
            path,
            identity_reader=identity_reader,
            clock_ms=clock_ms,
            unknown_threshold_seconds=unknown_threshold_seconds,
            logger=logger,
            cleanup_async=False,
        )
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._generation = 1
        self._version = 0
        self._capacity = capacity
        self._timeout = timeout
        self._closed = False
        self._pending: dict[str, _CustodyWaiter] = {}
        self._processes: dict[str, ManagedHarnessProcess] = {}
        self._errors: dict[str, BaseException] = {}
        self._rejected = self._failed = 0
        self._rows = self._freeze_rows(self._registry.status())
        self._handle = self._runtime.start(
            ActorSpec(
                name="orphan-process-custody",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects: EffectLane[
            ProcessCustodyCommand, ProcessCustodyCompleted
        ] = EffectLane(
            name="orphan-process-custody-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    @staticmethod
    def _freeze_rows(
        rows: tuple[dict[str, object], ...]
    ) -> tuple[tuple[tuple[str, object], ...], ...]:
        return tuple(tuple(sorted(row.items())) for row in rows)

    def observe_start(
        self, harness_id: str, process: ManagedHarnessProcess, *,
        pid: int | None, marker: str | None,
    ) -> str:
        return self._call("start", harness_id, process, pid, marker).orphan_id or ""

    def observe_stop(
        self, harness_id: str, process: ManagedHarnessProcess, *,
        pid: int | None, marker: str | None,
    ) -> str:
        return self._call("stop", harness_id, process, pid, marker).orphan_id or ""

    def collect_once(self) -> int:
        return self._call("collect").retired

    def status(self) -> tuple[dict[str, object], ...]:
        with self._guard:
            rows = self._rows
        return tuple(dict(row) for row in rows)

    def projection(self) -> ProcessCustodyProjection:
        with self._guard:
            return ProcessCustodyProjection(
                self._generation, self._version, len(self._pending),
                self._rejected, self._failed, self._rows,
            )

    def _call(
        self, kind: str, harness_id: str = "",
        process: ManagedHarnessProcess | None = None,
        pid: int | None = None, marker: str | None = None,
    ) -> ProcessCustodyCompleted:
        operation_id = uuid4().hex
        waiter = _CustodyWaiter()
        command = ProcessCustodyCommand(
            operation_id, self._generation, kind, harness_id, pid, marker
        )
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                self._rejected += 1
                raise ProcessCustodyOverloaded("process custody authority unavailable")
            self._pending[operation_id] = waiter
            if process is not None:
                self._processes[operation_id] = process
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id)
                self._processes.pop(operation_id, None)
                self._rejected += 1
                raise ProcessCustodyOverloaded(
                    f"process custody admission: {admission.value}"
                )
        if not waiter.ready.wait(self._timeout):
            raise TimeoutError(
                f"process custody {operation_id} accepted but still pending"
            )
        if waiter.error is not None:
            raise waiter.error
        assert waiter.result is not None
        return waiter.result

    def _receive(self, command: object) -> None:
        if isinstance(command, ProcessCustodyCommand):
            admission = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is not AdmissionResult.ACCEPTED:
                with self._guard:
                    self._errors[command.operation_id] = ProcessCustodyOverloaded(
                        "process custody I/O lane full"
                    )
                self._settle(ProcessCustodyCompleted(
                    command.operation_id, command.generation,
                    None, 0, self._rows, "ProcessCustodyOverloaded",
                ))
            return
        if isinstance(command, EffectCompleted):
            if command.generation == self._generation:
                result = command.result or ProcessCustodyCompleted(
                    command.operation_id, command.generation,
                    None, 0, self._rows, command.error or "ProcessCustodyEffectError",
                )
                self._settle(result)
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError("unsupported process custody command")

    def _settle(self, result: ProcessCustodyCompleted) -> None:
        with self._guard:
            waiter = self._pending.pop(result.operation_id, None)
            if waiter is None:
                return
            self._processes.pop(result.operation_id, None)
            error = self._errors.pop(result.operation_id, None)
            if error is not None or result.error is not None:
                self._failed += 1
            else:
                self._rows = result.rows
                self._version += 1
            waiter.result = result
            waiter.error = error
            waiter.ready.set()

    def _execute(self, command: ProcessCustodyCommand) -> ProcessCustodyCompleted:
        try:
            if command.kind in {"start", "stop"}:
                with self._guard:
                    process = self._processes[command.operation_id]
                operation = (
                    self._registry.observe_start
                    if command.kind == "start" else self._registry.observe_stop
                )
                orphan_id = operation(
                    command.harness_id, process,
                    pid=command.pid, marker=command.marker,
                )
                retired = 0
            elif command.kind == "collect":
                orphan_id = None
                retired = self._registry.collect_once()
            else:
                raise TypeError("unsupported custody effect")
            return ProcessCustodyCompleted(
                command.operation_id, command.generation, orphan_id, retired,
                self._freeze_rows(self._registry.status()),
            )
        except BaseException as error:
            with self._guard:
                self._errors[command.operation_id] = error
            return ProcessCustodyCompleted(
                command.operation_id, command.generation,
                None, 0, self._freeze_rows(self._registry.status()),
                type(error).__name__,
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
