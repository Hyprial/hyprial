"""Process I/O port for the harness actor.

Potentially blocking launcher/process operations are handed to this port and
reported back as generation-fenced completions; the actor remains the only
writer of lifecycle state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from typing import cast
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.api  import (
    DaemonInterruptibleHarnessProcess,
    HarnessLauncher,
    )
from hyprial.kernel  import (
    ManagedHarnessProcess,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.processes.orphan_processes  import OrphanProcessRegistry, OrphanProcessAuthority
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    HarnessCallIoCompleted,
    HarnessProcessStarted,
    HarnessStopIoCompleted,
)

from .contracts import HarnessRuntimeClosed, ProcessIdentity, _CompletionReceipt, _completion_succeeded, _default_identity_reader, _failed_completion

class ProcessIoPort:
    """Bounded blocking-I/O pool returning typed, generation-fenced events."""

    def __init__(
        self,
        launcher: HarnessLauncher,
        *,
        emit: Callable[[object], tuple[AdmissionResult, int]],
        delivery_failed: Callable[[object, BaseException], None],
        generation_reader: Callable[[], int],
        identity_reader: Callable[[int], str | None] = _default_identity_reader,
        orphan_processes: OrphanProcessRegistry | None = None,
        max_workers: int = 4,
        observe: Callable[[object], AdmissionResult] | None = None,
    ) -> None:
        self._launcher = launcher
        self._emit = emit
        self._observe = observe or (lambda event: self._emit(event)[0])
        self._delivery_failed = delivery_failed
        self._generation_reader = generation_reader
        self._identity_reader = identity_reader
        self._owns_orphan_processes = orphan_processes is None
        self._orphan_processes = orphan_processes or OrphanProcessAuthority(
            identity_reader=identity_reader
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="hyprial-harness-io",
        )
        self._closed = False
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._in_flight = 0
        self._process_owners: set[object] = set()
        self._slots = threading.BoundedSemaphore(max(1, max_workers) * 4)

    def start(
        self,
        correlation_id: str,
        harness_id: str,
        generation: int,
        version: int,
        spec: HarnessLaunchSpec,
        *,
        replace: tuple[ManagedHarnessProcess, ProcessIdentity | None] | None = None,
    ) -> None:
        def operation() -> HarnessProcessStarted:
            if replace is not None:
                old_process, old_identity = replace
                stopped, detail = self._stop_checked(
                    old_process, old_identity, harness_id=harness_id
                )
                if not stopped:
                    return HarnessProcessStarted(
                        correlation_id=correlation_id,
                        generation=generation,
                        version=version,
                        harness_id=harness_id,
                        pid=None,
                        error=RuntimeError(detail or "incumbent harness did not stop"),
                    )
            try:
                from hyprial.daemon.impl.processes.process_owner  import own_process, ProcessFactsObserved

                raw_process = self._launcher.start(spec)
                identity = self._capture_identity(raw_process)
                self._orphan_processes.observe_start(
                    harness_id,
                    raw_process,
                    pid=identity.pid,
                    marker=identity.marker,
                )
                try:
                    process = own_process(
                        raw_process,
                        observer=lambda token, facts: self._observe(
                            ProcessFactsObserved(harness_id, token, facts.running)
                        ),
                    )
                except BaseException:
                    self._stop_checked(raw_process, identity, harness_id=harness_id)
                    raise
                self._orphan_processes.observe_start(
                    harness_id, process, pid=identity.pid, marker=identity.marker
                )
                with self._lock:
                    self._process_owners.add(process)
            except BaseException as error:  # completion carries failures to actor
                return HarnessProcessStarted(
                    correlation_id=correlation_id,
                    generation=generation,
                    version=version,
                    harness_id=harness_id,
                    pid=None,
                    error=error,
                )
            return HarnessProcessStarted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                pid=identity.pid,
                process=process,
                identity_marker=identity.marker,
            )

        self._submit(
            operation,
            overload=HarnessProcessStarted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                pid=None,
                error=HarnessRuntimeClosed("process I/O port is overloaded"),
            ),
            orphan_start=True,
        )

    def stop(
        self,
        correlation_id: str,
        harness_id: str,
        generation: int,
        version: int,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
    ) -> None:
        def operation() -> HarnessStopIoCompleted:
            stopped, detail = self._stop_checked(
                process, identity, harness_id=harness_id
            )
            return HarnessStopIoCompleted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                stopped=stopped,
                detail=detail,
            )

        self._submit(
            operation,
            overload=HarnessStopIoCompleted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                stopped=False,
                detail="process I/O port is overloaded",
            ),
        )

    def call(
        self,
        correlation_id: str,
        generation: int,
        version: int,
        operation: Callable[[], object],
    ) -> None:
        def run() -> HarnessCallIoCompleted:
            try:
                return HarnessCallIoCompleted(
                    correlation_id, generation, version, operation()
                )
            except BaseException as error:
                return HarnessCallIoCompleted(
                    correlation_id, generation, version, error=error
                )

        self._submit(
            run,
            overload=HarnessCallIoCompleted(
                correlation_id,
                generation,
                version,
                error=HarnessRuntimeClosed("process I/O port is overloaded"),
            ),
        )

    def close(self, deadline: float | None = None) -> bool:
        with self._condition:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        if deadline is None:
            deadline = time.monotonic()
        with self._condition:
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            owners = tuple(self._process_owners)
        complete = True
        for owner in owners:
            if owner.detach(max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._process_owners.discard(owner)
            else:
                complete = False
        if complete and self._owns_orphan_processes:
            complete = self._orphan_processes.close(max(0.0, deadline - time.monotonic()))
        return complete

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def _submit(
        self,
        operation: Callable[[], object],
        *,
        overload: object,
        orphan_start: bool = False,
    ) -> None:
        with self._lock:
            if self._closed:
                raise HarnessRuntimeClosed("process I/O port is closed")
            if not self._slots.acquire(blocking=False):
                self._delivery_failed(
                    overload,
                    HarnessRuntimeClosed("process I/O port is overloaded"),
                )
                return
            try:
                future = self._executor.submit(operation)
            except BaseException as error:
                self._slots.release()
                self._delivery_failed(overload, error)
                return
            self._in_flight += 1

        def settle() -> None:
            try:
                completion = future.result()
            except CancelledError:
                completion = _failed_completion(
                    overload,
                    HarnessRuntimeClosed("process I/O operation was cancelled"),
                )
            except BaseException as error:
                completion = _failed_completion(overload, error)
            receipt = _CompletionReceipt()
            completion = replace(completion, processed_receipt=receipt)
            try:
                processed = self._handoff(completion, receipt)
                if (
                    not processed
                    and orphan_start
                    and isinstance(completion, HarnessProcessStarted)
                    and completion.process is not None
                ):
                    self._stop_checked(
                        cast(ManagedHarnessProcess, completion.process),
                        ProcessIdentity(completion.pid, completion.identity_marker),
                        harness_id=completion.harness_id,
                    )
            finally:
                self._slots.release()
                with self._condition:
                    self._in_flight -= 1
                    self._condition.notify_all()

        def schedule_settlement(_future: object) -> None:
            # ``Future.add_done_callback`` runs inline when a fast operation
            # already finished.  Never let receipt waiting block the actor
            # thread that submitted the I/O work.
            threading.Thread(
                target=settle,
                name="hyprial-harness-completion",
                daemon=True,
            ).start()

        future.add_done_callback(schedule_settlement)

    def _handoff(self, completion: object, receipt: _CompletionReceipt) -> bool:
        while True:
            if receipt.processed:
                return True
            generation = self._generation_reader()
            if receipt.claimed_by(generation):
                # Exactly one mailbox copy may be pending in a generation.
                # A long actor stall is not evidence of failure and cannot
                # release external-I/O custody.
                receipt.wait(0.01)
                continue
            admission, admitted_generation = self._emit(completion)
            if admission is AdmissionResult.ACCEPTED:
                receipt.claim(admitted_generation)
                receipt.wait(0.01)
                continue
            if admission is AdmissionResult.CLOSED:
                with self._lock:
                    permanently_closed = self._closed
                if permanently_closed:
                    if (
                        isinstance(completion, HarnessProcessStarted)
                        and completion.process is not None
                    ):
                        # A late child start can be safely compensated: it was
                        # never adopted by actor state, so stop that exact
                        # process identity before releasing worker custody.
                        self._delivery_failed(
                            completion,
                            HarnessRuntimeClosed(
                                "late harness start was not adopted"
                            ),
                        )
                        return False
                    if _completion_succeeded(completion):
                        # A successful external effect has no truthful failure
                        # translation.  Keep worker custody and let bounded
                        # drain report incomplete rather than inviting replay.
                        receipt.wait(0.05)
                        continue
                    self._delivery_failed(
                        completion,
                        HarnessRuntimeClosed(
                            "harness actor closed before I/O completion"
                        ),
                    )
                    return False
                # Guardian replacement has a short CLOSED interval.  Custody
                # stays with this worker and the exact completion is retried
                # against the replacement generation.
            time.sleep(0.002)

    def _capture_identity(self, process: ManagedHarnessProcess) -> ProcessIdentity:
        pid = getattr(process, "pid", None)
        marker = self._identity_reader(pid) if isinstance(pid, int) else None
        return ProcessIdentity(pid if isinstance(pid, int) else None, marker)

    def _recorded_identity_verdict(self, identity: ProcessIdentity) -> str:
        """Classify a recorded identity before stop signals anything.

        ``"reused"`` -- a live PID whose marker disagrees -- is the only
        refusal; ``"dead"`` (PID missing) must proceed, because a harness may
        have replaced its own child generation and a missing PID is not reuse.
        The component-wise owner fence is shared with ``mcp.channel`` so a
        marker-format skew is not read as reuse.

        ⚠️ Stop-path exception: the fence's own fail-safe for ``UNKNOWN``
        (PID alive but its marker cannot be read: EPERM, a vanished ps/procfs
        source, or no shared scheme) is *do not act*.  Here ``UNKNOWN`` maps to
        ``"alive"`` and stop proceeds.  That is safe ONLY because the actual
        signal gate is :meth:`OwnedProcessGroup.signal`, which re-reads the
        generation's PID + birth identity immediately before ``killpg`` and
        refuses a mismatch; ``_stop_checked`` is bookkeeping around that gate,
        not the gate itself.  If stop ever stops routing through
        ``OwnedProcessGroup``, UNKNOWN must become a refusal again.
        """

        from hyprial.daemon.impl.mcp.channel.ownership import _OwnerProcessStatus, _owner_process_status

        status = _owner_process_status(
            identity.pid,
            identity.marker,
            read_identity=self._identity_reader,
        )
        if status is _OwnerProcessStatus.IDENTITY_MISMATCH:
            return "reused"
        if status is _OwnerProcessStatus.PID_MISSING:
            return "dead"
        return "alive"

    def _stop_checked(
        self,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
        *,
        harness_id: str,
        interruption_reason: str | None = None,
    ) -> tuple[bool, str | None]:
        from hyprial.daemon.impl.processes.process_owner  import ProcessOwner

        if (
            identity is not None
            and identity.pid is not None
            and identity.marker is not None
        ):
            verdict = self._recorded_identity_verdict(identity)
            if verdict == "reused":
                if isinstance(process, ProcessOwner):
                    if process.detach(1.0):
                        with self._lock:
                            self._process_owners.discard(process)
                return False, "PID_REUSED: refusing to stop a different process identity"
            if verdict == "dead":
                # The recorded generation is gone (it may have been replaced by
                # a respawn inside the same harness process).  A missing PID is
                # not PID reuse: stop whatever this process owns now.  The
                # generation's own PID+birth-identity fence remains the
                # authority on which PID it may signal.
                identity = self._capture_identity(process)
            # "alive" AND the UNKNOWN case both fall through to process.stop().
            # UNKNOWN means "the marker could not be read", which the shared
            # fence treats as fail-safe-do-not-act; proceeding is safe only
            # because OwnedProcessGroup.signal() re-checks the birth identity
            # before killpg (see _recorded_identity_verdict).
        self._orphan_processes.observe_stop(
            harness_id,
            process,
            pid=None if identity is None else identity.pid,
            marker=None if identity is None else identity.marker,
        )
        try:
            if interruption_reason is not None and isinstance(
                process, DaemonInterruptibleHarnessProcess
            ):
                process.prepare_daemon_interruption(interruption_reason)
            process.stop()
            if isinstance(process, ProcessOwner):
                with self._lock:
                    self._process_owners.discard(process)
        except BaseException as error:
            self._orphan_processes.collect_once()
            return False, str(error)
        self._orphan_processes.collect_once()
        return True, None
