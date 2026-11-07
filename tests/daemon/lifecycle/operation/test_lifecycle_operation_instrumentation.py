"""The lifecycle start path's only post-mortem trace.

2026-09-22: a start that was accepted and then never settled raised with no log
line at all -- ``logs/daemon.jsonl`` held nothing for the failing operation, so
neither "how long did it run", "what budget was it given" nor "how often does
this happen" could be answered after the fact, and the failure could not be
dated at all.  These tests pin the events that answer those three questions.

They matter more than usual because the call sites are best-effort by design
(``_log_lifecycle_operation`` swallows a broken sink so it cannot mask the
failure it describes).  That trade is right, but it means a typo'd event name
degrades to silence rather than to a red test -- so the events are asserted
here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecycleResult,
    LifecycleSpec,
    LifecycleState,
)
from hyprial.daemon.impl.network.route_ports import RouteSpec


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))

    def named(self, event: str) -> dict[str, Any] | None:
        for _level, name, fields in self.events:
            if name == event:
                return fields
        return None


def _operation() -> LifecycleOperation:
    return LifecycleOperation.create(
        "op-1",
        LifecycleSpec(
            agent_name="worker",
            actor="agent:owner:node-1:worker",
            harness=HarnessLaunchProjection("pi", "worker", True),
            route=RouteSpec("agent:owner:node-1:worker", "live/worker", "inbox/worker"),
        ),
    )


def _app(tmp_path: Path, manager: Any) -> tuple[DaemonApplication, _Recorder]:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        **_app_injections(),
    )
    app._lifecycle_manager = manager
    recorder = _Recorder()
    app._log = recorder  # type: ignore[method-assign]
    return app, recorder


def _close_app(app: DaemonApplication) -> None:
    # The injected manager is an observation seam, not a lifecycle owner.
    app._lifecycle_manager = None
    try:
        app._close()
    finally:
        app._close_home_lease()


def test_every_operation_records_its_start_and_budget(tmp_path: Path) -> None:
    """The begin half: without it a duration has no denominator."""

    class _Refusing:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.CLOSING

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            raise AssertionError("a refused operation is never waited on")

    app, recorder = _app(tmp_path, _Refusing())
    try:
        _raises_lifecycle_error(app)
        submitted = recorder.named("daemon.lifecycle.operation.submitted")
        assert submitted is not None, recorder.events
        assert submitted["operationId"] == "op-1"
        assert submitted["kind"] == "create"
        # The declared budget must be on the begin line: the caller's error
        # carries an operation id but no budget, so this is the only place a
        # reader can learn what the operation was allowed to take.
        assert submitted["budgetMs"] == 80000
    finally:
        _close_app(app)


def test_a_refused_admission_says_so_and_how_fast(tmp_path: Path) -> None:
    class _Refusing:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.CLOSING

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            raise AssertionError("a refused operation is never waited on")

    app, recorder = _app(tmp_path, _Refusing())
    try:
        _raises_lifecycle_error(app)
        refused = recorder.named("daemon.lifecycle.operation.refused")
        assert refused is not None, recorder.events
        assert refused["admission"] == "closing"
        assert isinstance(refused["elapsedMs"], int)
    finally:
        _close_app(app)


def test_the_settle_timeout_is_distinguishable_from_a_refusal(tmp_path: Path) -> None:
    """The 2026-09-22 shape: accepted, then the deadline expired."""

    class _Slow:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            raise TimeoutError(f"lifecycle operation still running: {operation_id}")

    app, recorder = _app(tmp_path, _Slow())
    try:
        _raises_lifecycle_error(app)
        unsettled = recorder.named("daemon.lifecycle.operation.unsettled")
        assert unsettled is not None, recorder.events
        assert unsettled["operationId"] == "op-1"
        assert unsettled["budgetMs"] == 80000
        assert isinstance(unsettled["elapsedMs"], int)
    finally:
        _close_app(app)


def test_a_terminal_failure_records_the_state_and_the_code(tmp_path: Path) -> None:
    class _Failing:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.FAILED,
                completed_effects=(),
                compensated_effects=(),
                error="boom",
                error_code="SOMETHING_FAILED",
            )

    app, recorder = _app(tmp_path, _Failing())
    try:
        _raises_lifecycle_error(app)
        failed = recorder.named("daemon.lifecycle.operation.failed")
        assert failed is not None, recorder.events
        assert failed["state"] == "failed"
        assert failed["errorCode"] == "SOMETHING_FAILED"
    finally:
        _close_app(app)


def test_a_settled_operation_records_how_long_it_took(tmp_path: Path) -> None:
    """The reading that separates "stuck" from "close to the budget"."""

    class _Completing:
        crashed = False

        def submit(self, _operation: LifecycleOperation) -> PortAdmission:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
            )

    app, recorder = _app(tmp_path, _Completing())
    try:
        app._ensure_agent = lambda *a, **k: None  # type: ignore[method-assign]
        app._run_lifecycle_operation(_operation())
        settled = recorder.named("daemon.lifecycle.operation.settled")
        assert settled is not None, recorder.events
        assert settled["operationId"] == "op-1"
        assert isinstance(settled["elapsedMs"], int)
    finally:
        _close_app(app)


def _raises_lifecycle_error(app: DaemonApplication) -> None:
    """Drive one failing call; the code it raises is pinned elsewhere."""

    import pytest

    with pytest.raises(Exception):
        app._run_lifecycle_operation(_operation())
