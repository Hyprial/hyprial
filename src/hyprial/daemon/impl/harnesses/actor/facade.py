"""Public compatibility facade over the canonical harness runtime actor."""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, cast
from hyprial.daemon.impl.api  import (
    HarnessDelivery,
    HarnessResult,
)
from hyprial.kernel import DesiredStateError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    BindHarnessLivenessCommand,
    DispatchHarnessDeliveryCommand,
    DrainHarnessFailedCommand,
    DrainHarnessProgressCommand,
    DrainHarnessReadinessCommand,
    DrainHarnessResultsCommand,
    EnsureHarnessCommand,
    HarnessAdapterRegistrationProjection,
    HarnessCommand,
    HarnessDeliveryAdmitted,
    HarnessDesiredStateManaged,
    HarnessEvent,
    HarnessFailedDrained,
    HarnessLivenessBound,
    HarnessMutationCompleted,
    HarnessProjectionsRefreshed,
    HarnessProgressObserved,
    HarnessReadinessDrained,
    HarnessReadyObserved,
    HarnessRestoreCompleted,
    HarnessResultObserved,
    HarnessSessionRefsReconciled,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
    HarnessesStopped,
    RemoveHarnessCommand,
    RemoveAdapterRegistrationCommand,
    RefreshHarnessProjectionsCommand,
    ReconcileHarnessSessionRefsCommand,
    RestoreAdapterRegistrationCommand,
    RestoreHarnessesCommand,
    StopHarnessesCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    WaitHarnessReadyCommand,
)
from hyprial.kernel import PortCommandRejected
from hyprial.kernel import ReadinessReport

from .contracts import HarnessProjection, HarnessRestoreSummary, HarnessRuntimeClosed, _AWAIT_SETTLEMENT, _AwaitSettlement, _Reply, _launch_projection

if TYPE_CHECKING:  # pragma: no cover - annotation-only, same package
    from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor


class HarnessRuntimeFacade:
    """Compatibility facade preserving ``ManagedHarnessRuntime`` behavior."""

    def __init__(
        self,
        actor: HarnessRuntimeActor,
        *,
        reply_timeout: float = 65.0,
    ) -> None:
        self._actor = actor
        self._reply_timeout = reply_timeout
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[type[object], _Reply[object]]] = {}
        self._timer_sequence = 0
        self._last_drain_complete = True
        self._actor.subscribe_events(self._on_event)

    @property
    def projection(self) -> HarnessProjection:
        return self._actor.projection

    def restore(self, desired: tuple[HarnessLaunchSpec, ...]) -> HarnessRestoreSummary:
        """Restore the declared fleet, returning when the batch has settled.

        No partial summary on a timeout: the batch completes when every
        admitted start has settled, each settlement bounded by the per-start
        timer the actor arms at admission.  Progress observation is the
        readiness reports (phase ③), not this summary, so a caller giving
        up early had nothing left to report that the reports do not already
        carry.

        The wait itself carries a derived wall-clock backstop (F1): the
        settlement bound the batch is structurally owed -- admission rounds
        times the per-start timeout, plus a round of margin -- so a lost
        settlement raises ``TimeoutError`` into the daemon's degraded-start
        path (gate opens with ``harness.recovery.failed``) instead of
        hanging the gate closed forever.  With start timers disabled the
        wait stays unlimited (``_AWAIT_SETTLEMENT``): the operator opted
        out of settlement deadlines.  A runtime stop still ends the wait:
        ``_fail_pending`` raises ``HarnessRuntimeClosed`` here.
        """

        targets = tuple(desired)
        budget = self._actor.restore_settlement_budget(len(targets))
        event = self._request(
            RestoreHarnessesCommand(
                uuid.uuid4().hex, tuple(_launch_projection(spec) for spec in targets)
            ),
            HarnessRestoreCompleted,
            timeout=_AWAIT_SETTLEMENT if budget is None else budget,
        )
        return cast(HarnessRestoreCompleted, event).result

    def reconcile(self) -> int:
        with self._pending_lock:
            self._timer_sequence += 1
            timer_sequence = self._timer_sequence
        event = self._request(
            HarnessTimerElapsedCommand(
                uuid.uuid4().hex,
                self._actor.generation,
                timer_sequence,
                int(time.time() * 1000),
            ),
            HarnessTimerCompleted,
        )
        return cast(HarnessTimerCompleted, event).restarted

    def start(self, spec: HarnessLaunchSpec) -> bool:
        event = self._request(
            EnsureHarnessCommand(uuid.uuid4().hex, _launch_projection(spec)),
            HarnessMutationCompleted,
        )
        return cast(HarnessMutationCompleted, event).changed

    def remove(self, harness: str, name: str) -> bool:
        event = self._request(
            RemoveHarnessCommand(uuid.uuid4().hex, harness, name),
            HarnessMutationCompleted,
        )
        return cast(HarnessMutationCompleted, event).changed

    def drain_failed_events(self) -> tuple[str, ...]:
        event = self._request(
            DrainHarnessFailedCommand(uuid.uuid4().hex),
            HarnessFailedDrained,
        )
        return cast(HarnessFailedDrained, event).harness_ids

    def drain_readiness_reports(self) -> tuple[ReadinessReport, ...]:
        event = self._request(
            DrainHarnessReadinessCommand(uuid.uuid4().hex),
            HarnessReadinessDrained,
        )
        return cast(HarnessReadinessDrained, event).reports

    def status(self) -> tuple[dict[str, object], ...]:
        return self.projection.status()

    def streaming_actors(self) -> tuple[str, ...]:
        return self.projection.streaming_actors()

    def session_refs(self) -> dict[tuple[str, str], str]:
        self._request(
            RefreshHarnessProjectionsCommand(uuid.uuid4().hex),
            HarnessProjectionsRefreshed,
        )
        return self.projection.session_refs()

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        event = cast(
            HarnessSessionRefsReconciled,
            self._request(
                ReconcileHarnessSessionRefsCommand(uuid.uuid4().hex),
                HarnessSessionRefsReconciled,
            ),
        )
        if event.error is not None:
            raise DesiredStateError(event.error)
        return {
            (item.harness, item.name): item.session_ref for item in event.refs
        }

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                StageHarnessDesiredCommand(uuid.uuid4().hex, _launch_projection(spec)),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    @staticmethod
    def _raise_managed_error(event: HarnessDesiredStateManaged) -> None:
        """Rebuild public errors only after immutable I/O completion settles."""

        if event.error is not None:
            raise event.error
        if event.error_code is None:
            if event.error_detail is not None:
                raise RuntimeError(event.error_detail)
            return
        if event.error_is_oserror:
            subclasses: dict[str, type[OSError]] = {
                "OSError": OSError,
                "BlockingIOError": BlockingIOError,
                "BrokenPipeError": BrokenPipeError,
                "ChildProcessError": ChildProcessError,
                "ConnectionError": ConnectionError,
                "ConnectionAbortedError": ConnectionAbortedError,
                "ConnectionRefusedError": ConnectionRefusedError,
                "ConnectionResetError": ConnectionResetError,
                "FileExistsError": FileExistsError,
                "FileNotFoundError": FileNotFoundError,
                "InterruptedError": InterruptedError,
                "IsADirectoryError": IsADirectoryError,
                "NotADirectoryError": NotADirectoryError,
                "PermissionError": PermissionError,
                "ProcessLookupError": ProcessLookupError,
                "TimeoutError": TimeoutError,
            }
            kind = subclasses.get(event.error_code or "")
            if kind is None:
                # Calling OSError(errno, ...) can auto-select a *different*
                # builtin subclass. Unknown custom names intentionally fall
                # back to plain OSError with the same frozen public facts.
                fallback = OSError(event.error_detail or "")
                fallback.errno = event.error_errno
                fallback.strerror = event.error_strerror
                fallback.filename = event.error_filename
                fallback.filename2 = event.error_filename2
                raise fallback
            if (
                event.error_errno is not None
                or event.error_strerror is not None
                or event.error_filename is not None
                or event.error_filename2 is not None
            ):
                args: list[object] = [
                    event.error_errno,
                    event.error_strerror or event.error_detail or "",
                ]
                if event.error_filename is not None or event.error_filename2 is not None:
                    args.append(event.error_filename)
                if event.error_filename2 is not None:
                    args.extend((None, event.error_filename2))
                raise kind(*args)
            raise kind(event.error_detail or "")
        if event.operation == "SnapshotAdapterRegistrationCommand":
            if event.error_code == "DesiredStateError":
                raise DesiredStateError(event.error_detail or "")
            if event.error_code == "RuntimeError":
                raise RuntimeError(event.error_detail or "")
        raise RuntimeError(f"{event.error_code}: {event.error_detail}")

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                SnapshotAdapterRegistrationCommand(uuid.uuid4().hex, name),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        assert event.adapter is not None
        return event.adapter

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                RemoveAdapterRegistrationCommand(
                    uuid.uuid4().hex,
                    snapshot.name,
                    snapshot.spec,
                    snapshot.legacy_pin,
                ),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                RestoreAdapterRegistrationCommand(
                    uuid.uuid4().hex,
                    snapshot.name,
                    snapshot.spec,
                    snapshot.legacy_pin,
                ),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        event = self._request(
            WaitHarnessReadyCommand(uuid.uuid4().hex, harness, name, timeout),
            HarnessReadyObserved,
            timeout=max(timeout + 1.0, self._reply_timeout),
        )
        return cast(HarnessReadyObserved, event).ready

    def dispatch(self, name: str, delivery: HarnessDelivery) -> bool:
        event = self._request(
            DispatchHarnessDeliveryCommand(uuid.uuid4().hex, name, delivery),
            HarnessDeliveryAdmitted,
        )
        return cast(HarnessDeliveryAdmitted, event).accepted

    def drain_results(self) -> tuple[HarnessResult, ...]:
        event = self._request(
            DrainHarnessResultsCommand(uuid.uuid4().hex), HarnessResultObserved
        )
        return cast(HarnessResultObserved, event).results

    def drain_progress(self) -> tuple[object, ...]:
        event = self._request(
            DrainHarnessProgressCommand(uuid.uuid4().hex), HarnessProgressObserved
        )
        return cast(HarnessProgressObserved, event).progress

    def bind_liveness(self, harness: str, name: str, binding: object) -> bool:
        event = self._request(
            BindHarnessLivenessCommand(
                uuid.uuid4().hex, harness, name, binding
            ),
            HarnessLivenessBound,
        )
        return cast(HarnessLivenessBound, event).changed

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        command_drained = False
        try:
            event = self._request(
                StopHarnessesCommand(
                    uuid.uuid4().hex,
                    int(deadline * 1000),
                ),
                HarnessesStopped,
                timeout=timeout,
            )
            command_drained = cast(HarnessesStopped, event).drain_complete
        except TimeoutError:
            self._last_drain_complete = False
            return
        if not command_drained:
            self._last_drain_complete = False
            return
        self._fail_pending(HarnessRuntimeClosed("harness runtime stopped"))
        runtime_drained = self._actor.close_runtime(
            max(0.0, deadline - time.monotonic())
        )
        self._last_drain_complete = runtime_drained

    def _request(
        self,
        command: HarnessCommand,
        expected: type[object],
        *,
        timeout: float | _AwaitSettlement | None = None,
    ) -> object:
        reply: _Reply[object] = _Reply()
        with self._pending_lock:
            self._pending[command.correlation_id] = (expected, reply)
        reply.set_cancel(lambda: self._cancel(command.correlation_id))
        self._actor.submit(command)
        if isinstance(timeout, _AwaitSettlement):
            return reply.wait()
        return reply.wait(self._reply_timeout if timeout is None else timeout)

    def _cancel(self, correlation_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(correlation_id, None)
        self._actor.cancel_correlation(correlation_id)

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for _expected, reply in pending:
            reply.fail(error)

    def _on_event(self, event: HarnessEvent) -> None:
        correlation_id = event.correlation_id
        with self._pending_lock:
            pending = self._pending.get(correlation_id)
            if pending is None:
                return
            expected, reply = pending
            if not isinstance(event, (expected, PortCommandRejected)):
                return
            self._pending.pop(correlation_id, None)
        if isinstance(event, PortCommandRejected):
            if event.code == "HARNESS_START_TIMEOUT":
                reply.fail(TimeoutError(event.detail))
            elif event.code in {
                "PORT_CLOSING",
                "PORT_OVERLOADED",
                "HARNESS_COMPLETION_HANDOFF_FAILED",
                "HARNESS_GENERATION_RESTARTED",
                "HARNESS_RUNTIME_STOPPED",
            }:
                reply.fail(HarnessRuntimeClosed(event.detail))
            else:
                failure = RuntimeError(event.detail)
                failure.code = event.code  # type: ignore[attr-defined]
                reply.fail(failure)
            return
        reply.complete(event)
