"""The agent-effect worker executing session effects off the actor thread."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
from collections.abc import Callable
from queue import Empty, Full, Queue
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.desired_state  import (
    PendingSessionAgentEffect,
)
from hyprial.kernel import CallCostCounters

from .internals import (
    _AgentEffectUnavailable,
    _CommandSubmitter,
    _EFFECT_ADMISSION_COST_KEYS,
    _EffectWork,
    _agent_command,
)


class _AgentEffectWorker:
    """Bounded Agent admission lane; it never executes Agent state itself."""

    def __init__(
        self,
        commands: _CommandSubmitter | None,
        completion_sink: Callable[[object], bool],
        submission_failed: Callable[[str, str], None],
        *,
        capacity: int,
        deadline: float,
        backoff: tuple[float, ...],
    ) -> None:
        if capacity < 1:
            raise ValueError("agent effect capacity must be at least 1")
        if deadline <= 0:
            raise ValueError("agent effect deadline must be positive")
        if not backoff or any(delay <= 0 for delay in backoff):
            raise ValueError("agent effect backoff must contain positive delays")
        self._commands = commands
        self._completion_sink = completion_sink
        self._submission_failed = submission_failed
        self._deadline = deadline
        self._backoff = backoff
        self._queue: Queue[_EffectWork | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        # Its own side of the ipc_stats attribution rule: the admission work
        # (building the Agent command, submitting it, backing off) runs on
        # this thread only.  Every effect here is session-originated.
        self.admission_costs = CallCostCounters(
            _EFFECT_ADMISSION_COST_KEYS, wall=False
        )
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-session-agent-effects",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        effect: PendingSessionAgentEffect,
        *,
        custody_token: str,
        generation: int,
        agent_correlation_id: str,
    ) -> bool:
        work = _EffectWork(
            effect, custody_token, generation, agent_correlation_id
        )
        with self._condition:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(work)
            except Full:
                self._submission_failed(effect.effect_id, custody_token)
                self._completion_sink(
                    _AgentEffectUnavailable(
                        effect.effect_id,
                        custody_token,
                        "AGENT_EFFECT_QUEUE_OVERLOADED",
                        "Agent effect queue is full; durable effect remains pending",
                    )
                )
                return False
            self._pending += 1
            return True

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                work = self._queue.get(timeout=0.1)
            except Empty:
                with self._condition:
                    if self._closed and self._pending == 0:
                        return
                continue
            if work is None:
                return
            self._timed_admit(work)
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()

    def _timed_admit(self, work: _EffectWork) -> None:
        costs = self.admission_costs
        if not costs.enabled:
            self._admit(work)
            return
        failed = True
        # thread_time excludes the backoff sleeps: only CPU is charged.
        started_cpu = time.thread_time()
        try:
            self._admit(work)
            failed = False
        finally:
            costs.record(
                work.effect.operation,
                cpu_seconds=time.thread_time() - started_cpu,
                error=failed,
            )

    def _admit(self, work: _EffectWork) -> None:
        if self._commands is None:
            self._fail(
                work,
                "AGENT_EFFECT_UNAVAILABLE",
                "SessionActor has no Agent command sink; durable effect remains pending",
            )
            return
        command = _agent_command(work)
        deadline = time.monotonic() + self._deadline
        attempt = 0
        while True:
            try:
                admission = self._commands.submit(command)
            except Exception as error:
                self._fail(
                    work,
                    "AGENT_EFFECT_SUBMIT_FAILED",
                    f"Agent command submit raised {type(error).__name__}",
                )
                return
            if admission is PortAdmission.ACCEPTED:
                return
            if admission is PortAdmission.CLOSING:
                self._fail(
                    work,
                    "AGENT_EFFECT_PORT_CLOSED",
                    "Agent command port is closed; durable effect remains pending",
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail(
                    work,
                    "AGENT_EFFECT_ADMISSION_DEADLINE",
                    "Agent command port stayed overloaded; durable effect remains pending",
                )
                return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            time.sleep(min(delay, remaining))

    def _fail(self, work: _EffectWork, code: str, detail: str) -> None:
        self._submission_failed(work.effect.effect_id, work.custody_token)
        self._completion_sink(
            _AgentEffectUnavailable(
                work.effect.effect_id, work.custody_token, code, detail
            )
        )
