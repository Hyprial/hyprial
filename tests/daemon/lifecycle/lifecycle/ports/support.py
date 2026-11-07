from __future__ import annotations

import threading
import time
import inspect

from dataclasses import dataclass

from hyprial.identity.impl.agents.actor.ports import (
    CreateAgentCommand,
    DestroyAgentCommand,
)
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition import (
    AgentSessionDomains,
    HarnessPortClient,
)
from hyprial.daemon.impl.lifecycle_manager import LifecycleProcessManager
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
    LifecycleMutationFailed,
    )
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest


# Every wait budget below is READ from the product's own signatures or
# named constants -- never a literal chosen by this file.  If the product
# renames or drops one of these parameters, the KeyError fails loudly here
# instead of the test silently keeping a stale criterion.
#
# Operation-terminal waits must outlast the operation deadline so a
# genuinely stuck operation surfaces as its own coded failure instead of a
# test-side timeout (same derivation as
# tests/test_lifecycle_process_manager.py:454).
TERMINAL_WAIT_SECONDS = (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS
)
# Agent/session lifecycle domain events: the composition layer waits for
# exactly these events with this timeout (composition.py
# AgentSessionDomains.call_agent/call_session; wait_agent_lifecycle and
# wait_session_lifecycle use the same value).
DOMAIN_EVENT_WAIT_SECONDS = inspect.signature(
    AgentSessionDomains.call_session
).parameters["timeout"].default
# Intermediate observables (attempt records, reconcile ticks, admission
# rejections, test-double coordination): bounded at the manager's per-step
# wait (lifecycle_manager.py LifecycleProcessManager.__init__
# completion_timeout; also documented in lifecycle_budgets.py's docstring).
# This is an UPPER BOUND, not an expectation -- the healthy path lands in
# milliseconds; if a probe ever needs longer, that is a product-side
# signal, not a budget to raise.
PER_STEP_WAIT_SECONDS = inspect.signature(
    LifecycleProcessManager.__init__
).parameters["completion_timeout"].default
# The product's harness domain port waits on the system edge with
# HarnessPortClient's timeout (composition.py HarnessPortClient.__init__;
# its wait_lifecycle passes self._timeout).
HARNESS_PORT_WAIT_SECONDS = inspect.signature(
    HarnessPortClient.__init__
).parameters["timeout"].default


class _Events:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[object] = []

    def publish(self, event: object) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    def _wait_for(
        self,
        event_type: type,
        correlation: str,
        timeout: float,
        label: str,
    ) -> object:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events:
                    if (
                        isinstance(event, event_type)
                        and event.correlation_id == correlation
                    ):
                        return event
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"no {label} for {correlation}"
                self._condition.wait(remaining)

    def wait(
        self, correlation: str, timeout: float = TERMINAL_WAIT_SECONDS
    ) -> LifecycleMutationCompleted:
        return self._wait_for(  # type: ignore[return-value]
            LifecycleMutationCompleted,
            correlation,
            timeout,
            "lifecycle completion",
        )

    def wait_failed(
        self, correlation: str, timeout: float = TERMINAL_WAIT_SECONDS
    ) -> LifecycleMutationFailed:
        return self._wait_for(  # type: ignore[return-value]
            LifecycleMutationFailed,
            correlation,
            timeout,
            "lifecycle failure",
        )

    def wait_event(
        self,
        event_type: type,
        correlation: str,
        timeout: float = TERMINAL_WAIT_SECONDS,
    ) -> object:
        return self._wait_for(
            event_type, correlation, timeout, f"{event_type.__name__} event"
        )


def _request(
    operation: str,
    ordinal: int,
    payload: object,
    *,
    expected: str | None = None,
) -> LifecycleMutationRequest:
    attempt = f"{operation}:forward:{ordinal}"
    correlation = f"lifecycle:{attempt}"
    return LifecycleMutationRequest(
        correlation,
        attempt,
        operation,
        expected,
        payload,
    )


def _agent_request(
    operation: str,
    ordinal: int,
    command: type[CreateAgentCommand] | type[DestroyAgentCommand],
    *,
    expected: str | None = None,
) -> LifecycleMutationRequest:
    correlation = f"lifecycle:{operation}:forward:{ordinal}"
    return _request(
        operation,
        ordinal,
        command(correlation, "foo"),
        expected=expected,
    )
















@dataclass
class _Process:
    running: bool = True
    pid: int | None = None
    stop_failures: int = 0
    stop_calls: int = 0

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_failures:
            self.stop_failures -= 1
            raise RuntimeError("injected stop failure")
        self.running = False


class _Launcher:
    def __init__(self, *, start_failures: int = 0, stop_failures: int = 0) -> None:
        self.start_failures = start_failures
        self.stop_failures = stop_failures
        self.start_calls = 0
        self.processes: list[_Process] = []

    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        self.start_calls += 1
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError("injected start failure")
        process = _Process(stop_failures=self.stop_failures)
        self.processes.append(process)
        return process






















































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'DOMAIN_EVENT_WAIT_SECONDS',
    'HARNESS_PORT_WAIT_SECONDS',
    'PER_STEP_WAIT_SECONDS',
    'TERMINAL_WAIT_SECONDS',
    '_Events',
    '_Launcher',
    '_Process',
    '_agent_request',
    '_request',
]
