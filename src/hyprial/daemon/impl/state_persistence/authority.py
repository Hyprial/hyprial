"""StatePersistenceAuthority: the single serialized writer owning desired state and lifecycle journal commits."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
from collections import OrderedDict
from typing import Any, cast, get_args
from uuid import uuid4
from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult, HarnessLaunchSpec
from hyprial.kernel import CallCostCounters
from hyprial.daemon.impl.desired_state  import (
    DesiredStateStore,
)
from hyprial.daemon.impl.lifecycle_manager  import (
    _LifecycleStore,
)
from hyprial.daemon.impl.state_db  import StateDatabase

from .commands import (
    DesiredQuery,
    JournalQuery,
    ReadDesiredQuery,
    ReadJournalQuery,
    StateCommand,
    StateProjection,
    _STATE_COST_KEYS,
    _snapshot_payload,
    _state_cost_key,
)
from .ports import (
    StateCommandCompleted,
    StatePersistenceBusy,
    StatePersistenceTimeout,
    _DesiredPort,
    _Generation,
    _JournalPort,
    _Pending,
    _Projection,
    _SettlementWaitingOwner,
    run_journal_query,
)


class StatePersistenceAuthority:
    """Bounded one-writer authority for the shared lifecycle SQLite file.

    `call_*` returns only after settlement. A timeout does not cancel a
    submitted command; internal I/O owners can join it through
    `settled_result`, or use the typed `settled_desired` / `settled_journal`
    views to wait through that observational deadline on the original cell.
    Abandoned completions retire active capacity into a bounded late-result
    table instead of filling the writer forever.
    """

    def __init__(
        self,
        desired_store: DesiredStateStore,
        *,
        mailbox_capacity: int = 128,
        completion_capacity: int = 256,
        call_timeout: float = 70.0,
    ) -> None:
        if completion_capacity < mailbox_capacity or mailbox_capacity < 1:
            raise ValueError("completion capacity must cover the mailbox")
        self.state_db: StateDatabase = desired_store.state_db
        self._desired_store = desired_store
        self._journal_store = _LifecycleStore(self.state_db)
        self._projection = _Projection(desired_store.load())
        self._pending: dict[str, _Pending] = {}
        self._late_results: OrderedDict[str, StateCommandCompleted] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        self._completion_capacity = completion_capacity
        self._call_timeout = call_timeout
        self._generation = 0
        # Owned by the authority rather than one supervised generation so
        # diagnostics remain monotonic across an actor restart.
        self._command_costs = CallCostCounters(_STATE_COST_KEYS, wall=False)
        # Latency, kept apart from the CPU rows the ipcStats calibration sums:
        # execution wall inside the single writer, and the caller's full
        # round trip (admission + mailbox wait + execution + completion).
        # Round trip minus execution is the time spent queued.
        self._execution_wall = CallCostCounters(_STATE_COST_KEYS, wall=True)
        self._round_trip_wall = CallCostCounters(_STATE_COST_KEYS, wall=True)
        self._mailbox_capacity = mailbox_capacity
        self._runtime = ActorRuntime()

        def factory() -> _Generation:
            self._generation += 1
            return _Generation(
                self._generation,
                self._desired_store,
                self._journal_store,
                self._projection,
                self._complete,
                self._command_costs,
                self._execution_wall,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="state-persistence-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="state_authority",
                undelivered_sink=self._undelivered,
            )
        )
        self.desired = _DesiredPort(self)
        self.journal = _JournalPort(self)
        settlement_owner = _SettlementWaitingOwner(self)
        self.settled_desired = _DesiredPort(settlement_owner)
        self.settled_journal = _JournalPort(settlement_owner)

    @property
    def command_costs(self) -> CallCostCounters:
        return self._command_costs

    def latency_status(self) -> dict[str, Any]:
        """Queue depth and per-command wall time of the single writer (``ps``)."""

        snapshot = self._runtime.snapshot(self._handle)
        with self._lock:
            pending = len(self._pending)
        return {
            "sinceMs": self._round_trip_wall.since_ms,
            "mailbox": {
                "queued": snapshot.queued,
                "inFlight": snapshot.in_flight,
                "capacity": self._mailbox_capacity,
                "pendingCompletions": pending,
            },
            "roundTrip": self._round_trip_wall.snapshot(),
            "execution": self._execution_wall.snapshot(),
        }

    def _undelivered(self, command: object, reason: str) -> None:
        correlation = getattr(command, "correlation_id", None)
        if correlation:
            self._complete(
                correlation, self._generation, None, StatePersistenceBusy(reason)
            )

    def projection(self) -> StateProjection:
        return self._projection.read()

    def harness_spec(
        self, name: str, *, excluding: str | None = None
    ) -> HarnessLaunchSpec | None:
        return self._projection.harness_spec(name, excluding=excluding)

    def call_desired(self, operation: DesiredQuery, *args: object, **kwargs: object) -> Any:
        if any(callable(item) for item in args) or any(callable(item) for item in kwargs.values()):
            raise TypeError("state commands cannot carry executable callbacks")
        if kwargs:
            raise TypeError("desired queries do not accept keyword arguments")
        key_queries = {
            DesiredQuery.HARNESS_LIFECYCLE_RECEIPT,
            DesiredQuery.ADAPTER_REGISTRATION,
        }
        if operation in key_queries:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(f"{operation.value} requires one string key")
            key = args[0]
        elif not args:
            key = None
        else:
            raise TypeError(f"{operation.value} accepts no arguments")
        return self._call(ReadDesiredQuery(
            f"state-desired-{uuid4().hex}", operation, key,
        ))

    def call_journal(self, operation: JournalQuery, *args: object, **kwargs: object) -> Any:
        return self._read_journal(self._journal_query(operation, *args, **kwargs))

    def call_journal_settled(
        self, operation: JournalQuery, *args: object, **kwargs: object
    ) -> Any:
        """A journal read for internal lifecycle owners (same as ``call_journal``)."""

        return self._read_journal(self._journal_query(operation, *args, **kwargs))

    def _read_journal(self, query: ReadJournalQuery) -> Any:
        """Answer a journal read on the caller's thread, not the writer's.

        Reads only need committed state, and each one opens its own SQLite
        connection (WAL readers never block the writer), so queueing them
        behind writes bought nothing and cost a mailbox round trip each -- a
        lifecycle create made dozens (lifecycle root-fix plan S4).  Writes
        still go through the single writer.
        """

        with self._lock:
            if self._closed:
                raise StatePersistenceBusy("state authority is closed")
        return run_journal_query(self._journal_store, query)

    @staticmethod
    def _journal_query(
        operation: JournalQuery, *args: object, **kwargs: object
    ) -> ReadJournalQuery:
        if any(callable(item) for item in args) or any(callable(item) for item in kwargs.values()):
            raise TypeError("state commands cannot carry executable callbacks")
        if kwargs:
            raise TypeError("journal queries do not accept keyword arguments")
        arity = {
            JournalQuery.PENDING: 0,
            JournalQuery.LOAD: 1,
            JournalQuery.STATE: 1,
            JournalQuery.RESULT: 1,
            JournalQuery.COMPLETED_FORWARD: 1,
            JournalQuery.COMPENSABLE_FORWARD: 1,
            JournalQuery.FORWARD_RESOURCE_TOKEN: 2,
            JournalQuery.EFFECT_DONE: 3,
            JournalQuery.EFFECT_RECEIPT: 3,
            JournalQuery.RECEIPT_RETIREMENT: 3,
            JournalQuery.COMPLETED_RECEIPT: 3,
        }[operation]
        if len(args) != arity or any(not isinstance(item, str) for item in args):
            raise TypeError(f"{operation.value} requires {arity} string arguments")
        return ReadJournalQuery(
            f"state-journal-{uuid4().hex}", operation,
            args[0] if arity >= 1 else None,  # type: ignore[arg-type]
            args[1] if arity >= 2 else None,  # type: ignore[arg-type]
            args[2] if arity >= 3 else None,  # type: ignore[arg-type]
        )

    def call_settled(self, command: StateCommand) -> Any:
        """Submit one frozen typed command and join its exact completion.

        This port is for bounded internal I/O lanes. It deliberately waits
        through the public observational deadline after admission; callers
        must not wrap a timeout by constructing and submitting a replacement
        command with a new correlation ID.
        """

        if not isinstance(command, get_args(StateCommand)):
            raise TypeError("call_settled requires a typed StateCommand")
        if not command.correlation_id.strip():
            raise ValueError("state command correlation_id must not be blank")
        frozen = cast(StateCommand, _snapshot_payload(command))
        return self._call(frozen, wait_for_settlement=True)

    def _call(
        self,
        command: StateCommand,
        *,
        wait_for_settlement: bool = False,
    ) -> Any:
        pending = _Pending()
        started = time.perf_counter()
        with self._lock:
            if self._closed:
                raise StatePersistenceBusy("state authority is closed")
            if len(self._pending) >= self._completion_capacity:
                raise StatePersistenceBusy("state completion capacity is full")
            if command.correlation_id in self._pending:
                raise StatePersistenceBusy(
                    f"state correlation is already pending: {command.correlation_id}"
                )
            self._pending[command.correlation_id] = pending
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                del self._pending[command.correlation_id]
                raise StatePersistenceBusy(f"state command admission: {admission.value}")
        if not pending.event.wait(self._call_timeout):
            if wait_for_settlement:
                # Internal I/O owners already hold bounded effect custody.
                # Keep the original pending cell and wait for its exact
                # completion; retrying the command could repeat a mutation.
                pending.event.wait()
            else:
                with self._lock:
                    if pending.generation is None:
                        pending.abandoned = True
                        if self._round_trip_wall.enabled:
                            self._round_trip_wall.record(
                                _state_cost_key(command),
                                cpu_seconds=0.0,
                                wall_seconds=time.perf_counter() - started,
                                error=True,
                            )
                        raise StatePersistenceTimeout(command.correlation_id)
        completion = self.result(command.correlation_id)
        assert completion is not None
        if self._round_trip_wall.enabled:
            self._round_trip_wall.record(
                _state_cost_key(command),
                cpu_seconds=0.0,
                wall_seconds=time.perf_counter() - started,
                error=completion.error is not None,
            )
        if completion.error is not None:
            raise completion.error
        return completion.result

    def result(self, correlation_id: str) -> StateCommandCompleted | None:
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
            if pending is None or pending.generation is None:
                return None
            del self._pending[correlation_id]
            return StateCommandCompleted(
                correlation_id, pending.generation, pending.result, pending.error
            )

    def settled_result(
        self, correlation_id: str, timeout: float | None = None
    ) -> StateCommandCompleted | None:
        """Join an accepted command after its synchronous caller timed out.

        Internal I/O owners use this port to retain the exact mutation receipt.
        Completed abandoned calls move to a bounded late-result table, so they
        no longer consume active command capacity while remaining retrievable.
        """

        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
        if pending is None or not pending.event.wait(timeout):
            return None
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            current = self._pending.get(correlation_id)
            if pending.generation is None:
                return None
            if current is pending:
                del self._pending[correlation_id]
            # A live joiner owns this exact cell even if the bounded late-result
            # cache has already evicted its entry before this waiter resumes.
            return StateCommandCompleted(
                correlation_id, pending.generation, pending.result, pending.error
            )

    def _complete(
        self, correlation_id: str, generation: int,
        result: object, error: Exception | None,
    ) -> None:
        with self._lock:
            pending = self._pending.get(correlation_id)
            if pending is None:
                return
            pending.generation = generation
            pending.result = result
            pending.error = error
            pending.event.set()
            if pending.abandoned:
                del self._pending[correlation_id]
                self._late_results[correlation_id] = StateCommandCompleted(
                    correlation_id, generation, result, error
                )
                while len(self._late_results) > self._completion_capacity:
                    self._late_results.popitem(last=False)

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed = True
        return self._runtime.drain(timeout).complete
