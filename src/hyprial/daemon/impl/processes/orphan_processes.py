"""Persistent custody and conservative collection of detached harness processes."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from hyprial.daemon.impl.mcp.channel.ownership import (
    _process_identities_comparable,
    _process_identities_match,
)
from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest
from hyprial.kernel import atomic_json_write

from hyprial.kernel  import (
    ManagedHarnessProcess,
    ProcessLivenessProbeError,
)

from hyprial.kernel import ProcessLiveness, ProcessLivenessState

from .orphan.models import (
    ProcessCustodyCommand as ProcessCustodyCommand,
    ProcessCustodyCompleted as ProcessCustodyCompleted,
    ProcessCustodyProjection as ProcessCustodyProjection,
    _OrphanEntry as _OrphanEntry,
)
from .orphan.recovery import eviction_key, load_entries
from .process_owner import ProcessOwner
from .storage.journal import CustodyJournal
_DEFAULT_UNKNOWN_THRESHOLD_SECONDS = 60.0
_DEFAULT_MAX_ENTRIES = 4096
_DEFAULT_PROBE_BACKOFF_BASE_MS = 1_000
_DEFAULT_PROBE_BACKOFF_CAP_MS = 15 * 60 * 1_000

#: Detail stamped on a custody record retired because the pid it names now
#: belongs to a different process. Kept as one literal so the retirement log
#: and the probe share the same wording instead of drifting apart.
_IDENTITY_REPLACED_DETAIL = "process identity replaced (pid reused)"


def _process_error(process: ManagedHarnessProcess | None) -> str | None:
    """Best-effort child-process failure text for a retirement record.

    A retirement says only that the probe observed ``dead``; without the
    managed process's own last error the log cannot distinguish "the
    harness crashed" from "the harness never launched".  Reading it here
    keeps the custody registry free of any harness-specific knowledge.
    """

    if process is None:
        return None
    error = getattr(process, "last_error", None)
    if error is None:
        return None
    text = str(error)
    return text[:400] if text else None


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
        collect_batch_size: int = 64,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        probe_backoff_base_ms: int = _DEFAULT_PROBE_BACKOFF_BASE_MS,
        probe_backoff_cap_ms: int = _DEFAULT_PROBE_BACKOFF_CAP_MS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if probe_backoff_base_ms < 1 or probe_backoff_cap_ms < probe_backoff_base_ms:
            raise ValueError("probe backoff must satisfy 0 < base <= cap")
        self._path = path
        self._journal = CustodyJournal(path, atomic_json_write)
        self._collect_batch_size = max(1, collect_batch_size)
        self._collect_cursor = 0
        self._max_entries = max_entries
        self._probe_backoff_base_ms = probe_backoff_base_ms
        self._probe_backoff_cap_ms = probe_backoff_cap_ms
        self._retired_harnesses: dict[str, None] = {}
        self._identity_reader = identity_reader
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._unknown_threshold_ms = max(0, int(unknown_threshold_seconds * 1000))
        self._logger = logger
        self._cleanup_async = cleanup_async
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._owner_id = uuid4().hex
        self._entries, self._pending_evictions = load_entries(self._journal.rows)
        self._processes: dict[str, ManagedHarnessProcess] = {}
        self._process_keys: dict[int, str] = {}
        self._entry_keys: dict[str, set[int]] = {}
        self._cleanup_in_flight: set[str] = set()
        if self._entries:
            now_ms = self._clock_ms()
            for entry in self._entries.values():
                entry.owner_id = None
                entry.orphan_since_ms = entry.orphan_since_ms or now_ms
            with self._lock:
                self._evict_to_capacity_locked()
                self._save_locked(*self._entries)

    def observe_start(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> str:
        """Persist live custody; return an empty ID if capacity rejects it."""

        with self._lock:
            self._retired_harnesses.pop(harness_id, None)
            orphan_id, entry = self._ensure_entry_locked(
                harness_id, process, pid=pid, marker=marker
            )
            if entry is not None:
                entry.owner_id = self._owner_id
                entry.orphan_since_ms = None
            self._save_locked(orphan_id)
            return orphan_id if entry is not None else ""

    def refresh_pid(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int,
        marker: str | None,
    ) -> None:
        """Backfill a late-discovered pid onto the process's custody entry.

        Async-spawn harnesses start with an unknown pid; the launch captured
        ``unidentified`` custody at a moment when the handle could not answer.
        Once the handle exposes the real pid, the entry must carry it:
        liveness probes, the stop fence and the status projection all read the
        entry, not the handle.  The orphan id (keyed at first observation)
        stays as-is; the fields are what everything downstream reads.
        """

        with self._lock:
            orphan_id = self._process_keys.get(id(self._source(process)))
            entry = self._entries.get(orphan_id) if orphan_id else None
            if entry is None or entry.pid is not None:
                return
            entry.pid = pid
            if marker is not None and entry.marker is None:
                entry.marker = marker
            entry.observed = True
            self._save_locked(orphan_id)

    def observe_stop(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> str:
        """Take stop custody; return an empty ID if capacity rejects it."""

        with self._lock:
            orphan_id, entry = self._ensure_entry_locked(
                harness_id, process, pid=pid, marker=marker
            )
            if entry is not None:
                entry.owner_id = None
                entry.orphan_since_ms = entry.orphan_since_ms or self._clock_ms()
            self._save_locked(orphan_id)
            return orphan_id if entry is not None else ""

    def _ensure_entry_locked(
        self,
        harness_id: str,
        process: ManagedHarnessProcess,
        *,
        pid: int | None,
        marker: str | None,
    ) -> tuple[str, _OrphanEntry | None]:
        process_key = id(self._source(process))
        existing = self._process_keys.get(process_key)
        if existing is not None:
            # Transfer the one logical handle to its I/O owner, never back to raw.
            if isinstance(process, ProcessOwner):
                self._processes[existing] = process
            return existing, self._entries[existing]
        orphan_id = (
            f"{harness_id}:{pid}:{marker}"
            if pid is not None and marker is not None
            else f"{harness_id}:unidentified"
        )
        entry = self._entries.get(orphan_id)
        if entry is None and pid is not None and marker is not None:
            # Recovery/backfill may retain this identity under the harness key.
            canonical = f"{harness_id}:unidentified"
            retained = self._entries.get(canonical)
            if retained is not None and (retained.pid, retained.marker) == (pid, marker):
                orphan_id, entry = canonical, retained
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
            self._evict_to_capacity_locked()
            if orphan_id not in self._entries:
                return orphan_id, None
        else:
            # One harness-level unidentified record: a later observation
            # merges into it instead of minting another random key.
            entry.pid = pid if pid is not None else entry.pid
            entry.marker = marker if marker is not None else entry.marker
            entry.observed = entry.observed or pid is not None
        self._processes[orphan_id] = process
        self._process_keys[process_key] = orphan_id
        self._entry_keys.setdefault(orphan_id, set()).add(process_key)
        return orphan_id, entry

    @staticmethod
    def _source(process):
        return process.custody_source if isinstance(process, ProcessOwner) else process

    def _retire_locked(self, orphan_id):
        self._entries.pop(orphan_id, None)
        self._processes.pop(orphan_id, None)
        for key in self._entry_keys.pop(orphan_id, ()):
            self._process_keys.pop(key, None)
        self._cleanup_in_flight.discard(orphan_id)

    def _evict_to_capacity_locked(self) -> None:
        overflow = len(self._entries) - self._max_entries
        if overflow <= 0:
            return
        ordered = sorted(self._entries.values(), key=eviction_key)
        for entry in ordered[:overflow]:
            self._retire_locked(entry.orphan_id)
            self._pending_evictions.append(entry.orphan_id)
            self._log(
                "warn",
                "harness.orphan.capacity_evicted",
                entry,
                f"registry over capacity {self._max_entries}",
            )

    def retire_harness(self, harness_id: str) -> int:
        """Drop a destroyed harness's custody and remember it is gone.

        A later observation for the same harness is removed by the next
        collection pass instead of resurrecting an entry nobody owns. Only
        the most recent ``max_entries`` retired harness IDs are remembered.
        """

        with self._lock:
            self._retired_harnesses.pop(harness_id, None)
            self._retired_harnesses[harness_id] = None
            if len(self._retired_harnesses) > self._max_entries:
                self._retired_harnesses.pop(next(iter(self._retired_harnesses)))
            doomed = [
                orphan_id
                for orphan_id, entry in self._entries.items()
                if entry.harness_id == harness_id
            ]
            for orphan_id in doomed:
                entry = self._entries.get(orphan_id)
                if entry is not None:
                    self._retire_locked(orphan_id)
                    self._log(
                        "warn", "harness.orphan.harness_retired", entry, None
                    )
            if doomed:
                self._save_locked(*doomed)
            return len(doomed)

    def persistence_stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._journal.stats)

    def collect_once(self) -> int:
        """Probe every retained target; retire only a confirmed-dead target."""

        with self._collect_lock:
            return self._collect_once()

    def _collect_once(self) -> int:
        with self._lock:
            vanished = [
                orphan_id
                for orphan_id, entry in self._entries.items()
                if entry.harness_id in self._retired_harnesses
            ]
            for orphan_id in vanished:
                entry = self._entries.get(orphan_id)
                if entry is not None:
                    self._retire_locked(orphan_id)
                    self._log(
                        "warn", "harness.orphan.harness_gone_retired", entry, None
                    )
            if vanished:
                self._save_locked(*vanished)
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
            # Round robin prevents permanently unknown entries starving later ones.
            if orphan_ids:
                start = self._collect_cursor % len(orphan_ids)
                orphan_ids = (orphan_ids[start:] + orphan_ids[:start])[:self._collect_batch_size]
                self._collect_cursor = start + len(orphan_ids)
        retired = len(vanished)
        # One durable delta per bounded pass. A retirement not yet committed
        # after a crash is simply observed and retired again.
        dirty: set[str] = set()
        for orphan_id in orphan_ids:
            with self._lock:
                entry = self._entries.get(orphan_id)
                process = self._processes.get(orphan_id)
            if entry is None:
                continue
            owned_by_current_daemon = entry.owner_id == self._owner_id
            if (
                not owned_by_current_daemon
                and process is None
                and (entry.pid is None or entry.marker is None)
            ):
                # Nothing can ever observe this target: no live handle in
                # this daemon and no pid+marker identity to probe.  Keeping
                # it only re-probed an unanswerable UNKNOWN every second, for
                # weeks (gcAttempts in the hundreds of thousands).  Retire it,
                # loudly, with whatever pid we have for a manual check.
                if self._clock_ms() - entry.first_seen_ms < self._unknown_threshold_ms:
                    continue
                with self._lock:
                    if orphan_id in self._entries:
                        self._retire_locked(orphan_id)
                        dirty.add(orphan_id)
                        retired += 1
                self._log(
                    "warn",
                    "harness.orphan.unprobeable_retired",
                    entry,
                    "no live handle and no pid+identity marker to probe",
                )
                continue
            now_ms = self._clock_ms()
            if entry.next_probe_ms is not None and now_ms < entry.next_probe_ms:
                continue
            try:
                observation = self._observe(entry, process)
            except ProcessLivenessProbeError as error:
                with self._lock:
                    current = self._entries.get(orphan_id)
                    if current is None:
                        continue
                    if not owned_by_current_daemon:
                        current.gc_attempts += 1
                    current.probe_failures += 1
                    current.last_probe_ms = now_ms
                    exponent = min(current.probe_failures - 1, 32)
                    delay = min(
                        self._probe_backoff_base_ms << exponent,
                        self._probe_backoff_cap_ms,
                    )
                    current.next_probe_ms = now_ms + delay
                    current.state = "probe-failed"
                    current.last_probe_error = str(error)
                    current.detail = str(error)
                    dirty.add(orphan_id)
                self._log("error", "harness.orphan.probe_failed", entry, str(error))
                continue
            with self._lock:
                current = self._entries.get(orphan_id)
                if current is None:
                    continue
                if not owned_by_current_daemon:
                    current.gc_attempts += 1
                current.probe_failures = 0
                current.next_probe_ms = None
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
                    self._retire_locked(orphan_id)
                    dirty.add(orphan_id)
                    retired += 1
                    fields = {
                        "state": str(observation.state.value),
                        "pidKnown": entry.pid is not None,
                        "markerKnown": entry.marker is not None,
                        "lifetimeMs": (
                            now_ms - entry.first_seen_ms
                            if entry.first_seen_ms is not None
                            else None
                        ),
                        "processError": _process_error(process),
                    }
                    detail = observation.detail or entry.detail
                    if observation.detail == _IDENTITY_REPLACED_DETAIL:
                        # A terminal replacement, not a confirmed exit: say so
                        # at warn level so the reason survives in the journal.
                        # The same structured fields ride along, because the
                        # reader needs the child's own error in this case too.
                        self._log(
                            "warn",
                            "harness.orphan.identity_replaced",
                            entry,
                            detail,
                            **fields,
                        )
                    else:
                        self._log(
                            "info",
                            "harness.orphan.retired",
                            entry,
                            detail,
                            **fields,
                        )
                    continue
                dirty.add(orphan_id)
            if (
                not owned_by_current_daemon
                and observation.state is ProcessLivenessState.ALIVE
            ):
                self._request_cleanup(orphan_id, entry, process)
        if dirty:
            with self._lock:
                self._save_locked(*dirty)
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
            if _process_identities_match(expected_marker, marker):
                return ProcessLiveness(
                    ProcessLivenessState.ALIVE,
                    observed=True,
                    pid=pid,
                    marker=expected_marker,
                )
            if _process_identities_comparable(expected_marker, marker):
                # A shared birth component exists and differs: the pid now
                # belongs to a different process, so the one this record holds
                # custody for is gone. That is a terminal observation, not an
                # error to retry once a second: comparing the raw strings here
                # is what produced the 2026-10-09 24h ``probe_failed`` storm
                # (83,865 events) for a process that had never died.
                return ProcessLiveness(
                    ProcessLivenessState.DEAD,
                    observed=True,
                    pid=pid,
                    marker=expected_marker,
                    detail=_IDENTITY_REPLACED_DETAIL,
                )
            raise ProcessLivenessProbeError(
                f"process identity changed for pid {pid}"
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

    def _retire_replaced(
        self, orphan_id: str, entry: _OrphanEntry, detail: str
    ) -> None:
        """Retire custody whose pid now belongs to a different process."""

        with self._lock:
            if self._entries.pop(orphan_id, None) is None:
                return
            owned = self._processes.pop(orphan_id, None)
            if owned is not None:
                self._process_keys.pop(id(owned), None)
            self._cleanup_in_flight.discard(orphan_id)
            self._save_locked()
        self._log("warn", "harness.orphan.identity_replaced", entry, detail)

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
                    if marker is not None and _process_identities_match(
                        entry.marker, marker
                    ):
                        os.killpg(entry.pid, signal.SIGTERM)
                    elif marker is not None and _process_identities_comparable(
                        entry.marker, marker
                    ):
                        # The pid is a different process now: there is nothing
                        # of ours left to stop and signalling it would hit an
                        # innocent one. Retire the custody instead of failing.
                        self._retire_replaced(
                            orphan_id, entry, _IDENTITY_REPLACED_DETAIL
                        )
                    else:
                        raise ProcessLivenessProbeError(
                            f"process identity changed before cleanup for pid {entry.pid}"
                        )
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
                        self._save_locked(orphan_id)
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

    def _save_locked(self, *orphan_ids: str) -> None:
        evicted = self._pending_evictions
        self._pending_evictions = []
        if orphan_ids or evicted:
            keys = tuple(orphan_ids) + tuple(evicted)
        else:
            keys = tuple(self._entries)
        self._journal.commit({
            key: asdict(self._entries[key]) if key in self._entries else None
            for key in keys
        })

    def _log(
        self,
        level: str,
        event: str,
        entry: _OrphanEntry,
        detail: str | None,
        **fields: object,
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
                **fields,
            )


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
        self._write_stats = self._registry.persistence_stats()
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

    def refresh_pid(
        self, harness_id: str, process: ManagedHarnessProcess, *,
        pid: int, marker: str | None,
    ) -> None:
        self._call("refresh-pid", harness_id, process, pid, marker)

    def collect_once(self) -> int:
        return self._call("collect").retired

    def retire_harness(self, harness_id: str) -> int:
        return self._call("retire-harness", harness_id).retired

    def status(self) -> tuple[dict[str, object], ...]:
        with self._guard:
            rows = self._rows
        return tuple(dict(row) for row in rows)

    def persistence_stats(self) -> dict[str, int]:
        with self._guard:
            return dict(self._write_stats)

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
            elif command.kind == "refresh-pid":
                # ProcessCustodyCommand carries harness_id -- pass it through:
                # the registry signature is (harness_id, process, *, pid,
                # marker). Dropping it here is exactly how #1370's first
                # verdict caught the launch blowing up on the authority lane.
                with self._guard:
                    process = self._processes.get(command.operation_id)
                if process is not None and command.pid is not None:
                    self._registry.refresh_pid(
                        command.harness_id,
                        process,
                        pid=command.pid,
                        marker=command.marker,
                    )
                    with self._guard:
                        self._processes.pop(command.operation_id, None)
                orphan_id = None
                retired = 0
            elif command.kind == "collect":
                orphan_id = None
                retired = self._registry.collect_once()
            elif command.kind == "retire-harness":
                orphan_id = None
                retired = self._registry.retire_harness(command.harness_id)
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

        finally:
            stats = self._registry.persistence_stats()
            with self._guard:
                self._write_stats = stats

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
