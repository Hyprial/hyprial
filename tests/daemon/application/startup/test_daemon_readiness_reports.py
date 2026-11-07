"""Phase ③ as an event: one readiness report per disposition, never interpreted.

③ is not "which state every connector is in" -- this system has no terminal
state to observe -- but "did this round disposition every desired connector
once".  The harness actor appends one report per settled start attempt (plus
already-running ensures and restore targets admission could not take); the
daemon drains them on the reconcile tick, records arrival + verdict per
connector, and flips ping's phase to "reconciled" once the first round
covers the declared set.

The boundary these tests pin: the daemon observes the REPORT, never the
connector's internals.  A connector whose own start failed (verdict
"retrying") still completes daemon readiness, because bringing its harness
up and reaching its external service is the connector's own retry loop, not
the daemon's readiness problem.  And the vocabulary stays honest: failure
and success must look different in every record, while an unknown verdict
value passes through verbatim -- the daemon aggregates, it does not judge.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hyprial.kernel.impl.contracts.daemon.readiness import (
    READINESS_PHASES,
    READINESS_VERDICTS,
    )
from hyprial.kernel import (
    ReadinessReport,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    DrainHarnessReadinessCommand,
    HarnessReadinessDrained,
    RestoreHarnessesCommand,
    HarnessLaunchProjection,
)
from hyprial.daemon.impl.runtime import ReconcileSummary
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class _Process:
    def __init__(self) -> None:
        self.running = True
        self.pid: int | None = None
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class _Launcher:
    """Healthy except for the named connectors, which refuse every start."""

    def __init__(self, *, broken: frozenset[str] = frozenset()) -> None:
        self._broken = broken
        self.processes: list[_Process] = []

    def start(self, spec: HarnessLaunchSpec) -> _Process:
        if spec.name in self._broken:
            raise RuntimeError(f"{spec.name} refuses to start")
        process = _Process()
        self.processes.append(process)
        return process


def _spec(name: str, harness: str = "codex") -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness, name, True)


def _wait_for(predicate: Any, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"{what} never happened"
        time.sleep(0.02)


@pytest.fixture(name="runtimes")
def _runtimes() -> Iterator[list[ManagedHarnessRuntime]]:
    created: list[ManagedHarnessRuntime] = []
    yield created
    for runtime in created:
        runtime.stop(2.0)


def test_restore_emits_one_report_per_target(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """The ③ contract at the producer: per restore round, exactly one report
    per desired target -- a missing report reads as "never desired", a
    duplicate inflates the aggregation, so both directions are pinned."""

    launcher = _Launcher(broken=frozenset({"c1"}))
    managed = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=30.0,  # keep the broken one from retrying mid-test
        failure_budget=3,
        start_timeout_seconds=2.0,
    )
    runtimes.append(managed)
    specs = tuple(_spec(f"c{i}") for i in range(3))

    summary = managed.restore(specs)

    assert summary.attempted == 3
    assert summary.restored == 2
    assert summary.failed == 1
    reports = managed.drain_readiness_reports()
    assert sorted(report.source for report in reports) == [
        "codex:c0",
        "codex:c1",
        "codex:c2",
    ], "one report per desired target, no more, no fewer"
    by_source = {report.source: report for report in reports}
    assert all(report.phase == "connector-up" for report in reports)
    assert by_source["codex:c0"].verdict == "ready"
    assert by_source["codex:c2"].verdict == "ready"
    # U0b (Allen 2026-09-03, spec test ③) retired the OLD assertion
    # `verdict == "retrying"` here: it pinned the pre-U0b contract that a
    # restore-kind start failure stays inside the reconcile budget/backoff
    # and is retried.  The ruling gives a restore-kind start exactly ONE
    # attempt -- the row was "running" before, so a failed bring-back is
    # "previously up, did not come back": verdict "failed", persisted to
    # desired state, no auto-retry.  The verdict still differs from
    # success, which is the whole point of carrying a verdict at all.
    assert by_source["codex:c1"].verdict == "failed"
    # Drained once: the same dispositions must not be reported twice.
    assert managed.drain_readiness_reports() == ()


def test_failed_verdict_is_distinguished_when_the_budget_trips(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """Budget 1 turns the restore-round failure straight into "failed"."""

    launcher = _Launcher(broken=frozenset({"c0"}))
    managed = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=30.0,
        failure_budget=1,
        start_timeout_seconds=2.0,
    )
    runtimes.append(managed)

    summary = managed.restore((_spec("c0"),))

    assert summary.failed == 1
    (report,) = managed.drain_readiness_reports()
    assert report.verdict == "failed"
    assert report.source == "codex:c0"


def test_reconcile_harvest_reports_exactly_once(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """Reconcile-kind starts settle on the tick that harvests them, and that
    harvest is their disposition: one report, written there.  A tick with
    nothing to disposition writes none -- reports are events, not a
    per-tick heartbeat of the fleet."""

    launcher = _Launcher()
    managed = ManagedHarnessRuntime(
        launcher, restart_backoff_seconds=0.0, start_timeout_seconds=2.0
    )
    runtimes.append(managed)
    managed.restore((_spec("c0"),))
    assert [report.verdict for report in managed.drain_readiness_reports()] == ["ready"]

    # Kill the process: the next tick reaps and restarts it, a later tick
    # harvests the completion -- and the harvest writes the second report.
    launcher.processes[0].running = False
    deadline = time.monotonic() + 5.0
    while True:
        restarted = managed.reconcile()
        if restarted == 1:
            break
        assert time.monotonic() < deadline, "restart was never harvested"
        time.sleep(0.02)

    (report,) = managed.drain_readiness_reports()
    assert report.source == "codex:c0"
    assert report.verdict == "ready"

    # Quiet ticks have no dispositions, so they produce no reports.
    managed.reconcile()
    managed.reconcile()
    assert managed.drain_readiness_reports() == ()


def test_ensure_on_a_running_connector_reports_ready(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """ "Already running" is a disposition too: the target was examined and
    needed no action.  Reports are per disposition, not per connector -- the
    explicit start and the no-op ensure each produce exactly one."""

    managed = ManagedHarnessRuntime(_Launcher(), start_timeout_seconds=2.0)
    runtimes.append(managed)

    assert managed.start(_spec("c0")) is True
    assert managed.start(_spec("c0")) is False  # already running, nothing to do

    reports = managed.drain_readiness_reports()
    assert [report.verdict for report in reports] == ["ready", "ready"]
    assert all(report.source == "codex:c0" for report in reports)


def test_restore_defers_targets_a_lifecycle_effect_owns() -> None:
    """A desired connector whose resource is owned by an in-flight lifecycle
    effect cannot be admitted this round.  The round still owes it a report:
    "deferred" -- silence would read as "never desired"."""

    events: list[object] = []
    actor = HarnessRuntimeActor(_Launcher(), event_sink=events.append)
    try:
        actor._lifecycle_effect_resources.add("codex:locked")
        assert (
            actor.submit(
                RestoreHarnessesCommand(
                    "restore-1",
                    (HarnessLaunchProjection("codex", "locked", True),),
                )
            )
            is PortAdmission.ACCEPTED
        )
        assert (
            actor.submit(DrainHarnessReadinessCommand("drain-1"))
            is PortAdmission.ACCEPTED
        )

        def drained() -> bool:
            return any(
                isinstance(event, HarnessReadinessDrained)
                and event.correlation_id == "drain-1"
                for event in events
            )

        _wait_for(drained, what="readiness drain")
        (event,) = [
            item
            for item in events
            if isinstance(item, HarnessReadinessDrained)
            and item.correlation_id == "drain-1"
        ]
        assert event.reports == (
            ReadinessReport(
                phase="connector-up", verdict="deferred", source="codex:locked"
            ),
        )
    finally:
        actor.close_runtime(2.0)


# --------------------------------------------------------------------------- #
# The schema: three required string fields; unknown verdict VALUES pass.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"verdict": "ready", "source": "codex:c0"},  # missing phase
        {"phase": "connector-up", "source": "codex:c0"},  # missing verdict
        {"phase": "connector-up", "verdict": "ready"},  # missing source
        {"phase": "", "verdict": "ready", "source": "codex:c0"},  # empty phase
        {"phase": "connector-up", "verdict": 42, "source": "codex:c0"},  # wrong type
        "not-a-mapping",
    ],
)
def test_report_schema_rejects_missing_or_mistyped_fields(payload: Any) -> None:
    with pytest.raises(ValueError):
        ReadinessReport.from_payload(payload)


def test_report_schema_passes_unknown_verdict_values_through() -> None:
    """The vocabulary grows with reporters, not daemon releases: an unknown
    verdict is still a valid report.  Only missing fields are rejected."""

    report = ReadinessReport.from_payload(
        {"phase": "connector-up", "verdict": "melted", "source": "codex:c0"}
    )
    assert report.verdict == "melted"
    assert ReadinessReport.from_payload(report.to_payload()) == report
    # The documented starting vocabulary: one phase, four verdicts.
    assert "connector-up" in READINESS_PHASES
    assert {"ready", "retrying", "failed", "deferred"} <= set(READINESS_VERDICTS)


# --------------------------------------------------------------------------- #
# The daemon side: aggregation, the ping phase, and the overdue alarm's new
# anchor.  A minimal runtime seam drives one real supervisor tick into
# ``_record_maintenance_outcome``, exactly as the maintenance loop does.
# --------------------------------------------------------------------------- #


class _SupervisorRuntime:
    def __init__(self, supervisor: ManagedHarnessRuntime) -> None:
        self._supervisor = supervisor

    def on_timer(self, _observed_at_ms: int | None = None) -> ReconcileSummary:
        # Same order as DaemonEventBridge.on_timer: reconcile first (the
        # harvest writes reports), then drain.
        restarts = self._supervisor.reconcile()
        return ReconcileSummary(
            harness_restarts=restarts,
            inbox_results=0,
            harness_readiness=self._supervisor.drain_readiness_reports(),
        )


def _app(tmp_path: Path, providers: list[dict[str, Any]]) -> DaemonApplication:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    Path(f"{state / 'desired-state.json'}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": providers,
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="readiness-test",
        hyprial_home=tmp_path / "home",
        **_app_injections(),
    )


def _provider(name: str, harness: str = "codex") -> dict[str, Any]:
    return {
        "provider": harness,
        "name": name,
        "headless": True,
        "args": [],
        "ownership": "managed",
    }


def _tick(app: DaemonApplication, runtime: _SupervisorRuntime) -> None:
    outcome = runtime.on_timer()
    app._record_maintenance_outcome(time.monotonic(), outcome, (), 0, ())


def _log_entries(app: DaemonApplication) -> list[dict[str, Any]]:
    log_path = app.state_dir / "logs" / "daemon.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_readiness_boundary_excludes_connector_internals(
    own_daemon_application, tmp_path: Path
) -> None:
    """THE boundary test: a connector whose own start failed does not hold
    daemon readiness.  Its report arrives with verdict "failed" (U0b: a
    restore-kind failure is terminal after one attempt -- see the note at
    the assertion below), the daemon records and forwards that verbatim,
    and the first round still completes -- what the connector's internals
    are doing is its own business, never the daemon's readiness
    observation."""

    supervisor = ManagedHarnessRuntime(
        _Launcher(broken=frozenset({"broken"})),
        restart_backoff_seconds=30.0,
        failure_budget=3,
        start_timeout_seconds=2.0,
    )
    app = own_daemon_application(_app(tmp_path, [_provider("broken")]))
    app._harnesses = supervisor  # type: ignore[assignment]
    runtime = _SupervisorRuntime(supervisor)

    app._restore_harnesses()

    # The report settled inside the actor; one tick carries it over.
    _tick(app, runtime)

    report = app._readiness_reports["codex:broken"]
    # U0b (Allen 2026-09-03, spec test ③) retired the OLD assertion
    # `report.verdict == "retrying"`: it pinned the pre-U0b contract that
    # restore-kind failures retry inside the reconcile budget.  The ruling
    # makes a restore-kind failure terminal ("failed") after one attempt;
    # the boundary property this test actually pins -- a failing connector
    # does not hold daemon readiness, its report settles the round -- is
    # unchanged.
    assert report.verdict == "failed"
    # Readiness completed WITH a failing connector in the round -- the
    # report's arrival is the event ③ waits for, not the connector's health.
    assert app.handle("ping", {})["phase"] == "reconciled"
    _wait_for(
        lambda: any(
            item.get("event") == "daemon.readiness.reconciled"
            for item in _log_entries(app)
        ),
        what="readiness reconciliation log",
    )
    entries = _log_entries(app)
    first_reports = [e for e in entries if e.get("event") == "daemon.readiness.report"]
    assert len(first_reports) == 1
    assert first_reports[0]["connector"] == "codex:broken"
    assert first_reports[0]["verdict"] == "failed"
    reconciled = [e for e in entries if e.get("event") == "daemon.readiness.reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0]["expected"] == 1
    # Same U0b contract retirement as the report assertion above: a
    # restore-kind failure is "failed", not "retrying".
    assert reconciled[0]["verdicts"] == {"failed": 1}


def test_ping_reports_reconciled_after_first_round(
    own_daemon_application, tmp_path: Path
) -> None:
    """ping's three phases: restore done but no report aggregated yet is
    "serving"; once the first round covers every declared connector the
    phase flips to "reconciled" -- and stays there, because ③ is an event
    that happened, not a state that can lapse."""

    supervisor = ManagedHarnessRuntime(_Launcher(), start_timeout_seconds=2.0)
    app = own_daemon_application(_app(tmp_path, [_provider("c0")]))
    app._harnesses = supervisor  # type: ignore[assignment]
    runtime = _SupervisorRuntime(supervisor)

    app._restore_harnesses()

    # Restore is done (the gate is default-open without a restore thread)
    # but no tick has aggregated the report yet.
    assert app.handle("ping", {})["phase"] == "serving"

    _tick(app, runtime)
    assert app.handle("ping", {})["phase"] == "reconciled"

    # Monotonic: later rounds with nothing to report never revoke it, and
    # the completion event is logged once.
    _tick(app, runtime)
    assert app.handle("ping", {})["phase"] == "reconciled"
    _wait_for(
        lambda: any(
            item.get("event") == "daemon.readiness.reconciled"
            for item in _log_entries(app)
        ),
        what="readiness reconciliation log",
    )
    reconciled = [
        e for e in _log_entries(app) if e.get("event") == "daemon.readiness.reconciled"
    ]
    assert len(reconciled) == 1
    assert reconciled[0]["verdicts"] == {"ready": 1}


def test_aggregation_records_unknown_verdicts_without_interpreting(
    own_daemon_application, tmp_path: Path,
) -> None:
    """A verdict the daemon has never heard of is recorded verbatim, counted
    under its own name, and still completes the round -- the aggregator
    crashing on a newer reporter would put it ahead of the contract."""

    app = own_daemon_application(_app(tmp_path, []))
    app._harnesses = ManagedHarnessRuntime(_Launcher(), start_timeout_seconds=2.0)
    app._readiness_expected = frozenset({"codex:new"})
    outcome = ReconcileSummary(
        harness_restarts=0,
        inbox_results=0,
        harness_readiness=(
            ReadinessReport(phase="connector-up", verdict="melted", source="codex:new"),
        ),
    )

    app._record_maintenance_outcome(time.monotonic(), outcome, (), 0, ())

    assert app._readiness_reports["codex:new"].verdict == "melted"
    assert app.handle("ping", {})["phase"] == "reconciled"
    _wait_for(
        lambda: any(
            item.get("event") == "daemon.readiness.reconciled"
            for item in _log_entries(app)
        ),
        what="unknown-verdict reconciliation log",
    )
    reconciled = [
        e for e in _log_entries(app) if e.get("event") == "daemon.readiness.reconciled"
    ]
    assert reconciled[0]["verdicts"] == {"melted": 1}


# U0BRACE retirement: ``test_overdue_alarm_waits_for_the_first_readiness_report``
# pinned the ``harness.bootstrap.overdue`` alarm -- the "submitted but the
# first report never arrived" warning for the construction-phase bootstrap
# submissions whose audit (``_audit_pending_bootstraps``) was the only
# watcher.  The partition deleted that submission path (it was the second
# owner of every non-Lark running row), so there is nothing left to audit:
# every declared target now settles through the restore batch, whose
# disposition is bounded by F1's derived settlement budget and reported per
# target (ready / failed / deferred).  Retired with the path, not weakened.
