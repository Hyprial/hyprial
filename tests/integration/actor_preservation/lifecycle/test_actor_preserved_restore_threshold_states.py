from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import time
import threading

import pytest
from typer.testing import CliRunner


from hyprial.identity.impl.agents.state.worker_state import (
    RESTORE_THRESHOLD_MS,
    RestorePolicyAuthority,
    RestorePolicyError,
    desired_generation,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager import LifecycleState
from hyprial.daemon.impl.correlation.bounded_cadence import BoundedCadence
from hyprial.daemon.impl.pac.contracts.restore import PacRestoreFacts
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


HOUR_MS = 60 * 60 * 1_000


_Facts = PacRestoreFacts


class _Pac:
    def __init__(self, facts: _Facts = _Facts()) -> None:
        self.facts = facts

    def restore_facts(self, _actor: str) -> _Facts:
        return self.facts

    def close(self, _timeout: float = 5.0) -> bool:
        return True


class _Inbox:
    def __init__(self, pending: bool = False) -> None:
        self.pending = pending

    def has_pending_work(self, _recipient: str) -> bool:
        return self.pending

    def shutdown(self) -> None:
        return None


class _Harnesses:
    drain_complete = True

    def __init__(self) -> None:
        self.eligibility = []
        self.started = []
        self.fail_start = False
        self.eligibility_admission = PortAdmission.ACCEPTED

    def submit_restore_eligibility(self, projection) -> PortAdmission:
        self.eligibility.append(projection)
        return self.eligibility_admission

    def start(self, spec: HarnessLaunchSpec) -> bool:
        if self.fail_start:
            raise RuntimeError("start failed")
        self.started.append(spec)
        return True

    def stop(self) -> None:
        return None


class _LateLifecycle:
    crashed = False

    def __init__(self) -> None:
        self.operations = []
        self.waits = 0

    def result(self, operation_id):
        # These fixtures have no historical CREATE wake operations.
        raise KeyError(operation_id)

    def submit(self, operation) -> PortAdmission:
        self.operations.append(operation)
        return PortAdmission.ACCEPTED

    def wait(self, _operation_id: str, _timeout: float):
        self.waits += 1
        if self.waits == 1:
            raise TimeoutError("accepted operation still settling")
        return SimpleNamespace(
            state=LifecycleState.COMPLETED,
            completed_effects=("harness",),
            error_code=None,
            error=None,
        )


class _CompletedLifecycle:
    crashed = False

    def __init__(self) -> None:
        self.operations = []
        self.native_starts: list[str] = []
        self._completed: set[str] = set()

    def result(self, operation_id):
        # These fixtures have no historical CREATE wake operations.
        raise KeyError(operation_id)

    def submit(self, operation) -> PortAdmission:
        self.operations.append(operation)
        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float):
        if operation_id not in self._completed:
            self._completed.add(operation_id)
            self.native_starts.append(operation_id)
        return SimpleNamespace(
            state=LifecycleState.COMPLETED,
            completed_effects=("harness",),
            error_code=None,
            error=None,
        )


def _app(
    tmp_path: Path, *, now_ms: int, age_ms: int, known_activity: bool = True
) -> tuple[DaemonApplication, HarnessLaunchSpec]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="alice",
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    app.network_isolated = True
    app._agent_registry._clock = lambda: now_ms - age_ms
    app.agents.create("worker")
    if known_activity:
        app.agents.record_activity("worker")
    app._agent_registry._clock = lambda: now_ms
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._pac_graph_authority = _Pac()
    spec = HarnessLaunchSpec("codex", "worker", True)
    app.desired_state.upsert_harness(spec)
    return app, spec


def _close(app: DaemonApplication) -> None:
    app._close()
    app._close_home_lease()


@pytest.mark.parametrize(
    ("policy", "age_ms", "override", "restored"),
    [
        ("active", 12 * HOUR_MS, None, True),
        ("active", 17 * HOUR_MS, None, False),
        ("always", 17 * HOUR_MS, None, True),
        ("never", 1 * HOUR_MS, None, False),
        ("never", 17 * HOUR_MS, "pending", True),
        ("never", 17 * HOUR_MS, "blocked", True),
        ("never", 17 * HOUR_MS, "pac", True),
        ("never", 17 * HOUR_MS, "remote-return", True),
    ],
)
def test_restore_eligibility_matrix_active_always_never_default_threshold(
    tmp_path: Path,
    policy: str,
    age_ms: int,
    override: str | None,
    restored: bool,
) -> None:
    now_ms = 2_000_000_000_000
    app, spec = _app(tmp_path, now_ms=now_ms, age_ms=age_ms)
    try:
        if policy != "active":
            app._restore_policy.set_agent("worker", policy)
        if override == "pending":
            app._inbox.pending = True  # type: ignore[union-attr]
        elif override == "blocked":
            app.agents.block_agent("worker", reason="provider-quota")
        elif override == "pac":
            app._pac_graph_authority.facts = _Facts(requested=True)
        elif override == "remote-return":
            app._pac_graph_authority.facts = _Facts(remote_return=True)
        selected = app._classify_restore_specs((spec,))
        assert bool(selected) is restored
        assert RESTORE_THRESHOLD_MS == 12 * HOUR_MS
        assert app._harnesses.eligibility[-1].suppressed is (not restored)  # type: ignore[union-attr]
    finally:
        _close(app)


def test_deferred_restore_eligibility_drops_old_entity_before_retry(
    tmp_path: Path,
) -> None:
    app, spec = _app(
        tmp_path,
        now_ms=2_000_000_000_000,
        age_ms=17 * HOUR_MS,
    )
    harnesses = app._harnesses
    assert isinstance(harnesses, _Harnesses)
    harnesses.eligibility_admission = PortAdmission.OVERLOADED
    try:
        original = app.agents.require("worker")
        assert app._classify_restore_specs((spec,)) == ()
        pending = app._pending_restore_eligibility["worker"]
        assert pending.entity_token == original.entity_token
        submitted = len(harnesses.eligibility)

        assert app.agents.destroy("worker")
        successor = app.agents.create("worker")
        assert successor.entity_token != original.entity_token
        harnesses.eligibility_admission = PortAdmission.ACCEPTED
        app._retry_restore_eligibility()

        assert len(harnesses.eligibility) == submitted
        assert app._pending_restore_eligibility == {}
    finally:
        _close(app)


def test_restore_policy_corruption_publishes_fail_open_projection(tmp_path: Path) -> None:
    path = tmp_path / "agent-restore-policy.json"
    authority = RestorePolicyAuthority(path, normalize=lambda value: value)
    try:
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(RestorePolicyError):
            authority.refresh(wait=True)
        projection = authority.projection()
        assert projection.degraded is not None
        assert projection.policy.threshold_ms == RESTORE_THRESHOLD_MS
    finally:
        assert authority.close(1.0)


def test_terminal_pac_skips_restore_and_unknown_activity_restores_once(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    terminal_app, terminal_spec = _app(
        tmp_path / "terminal", now_ms=now_ms, age_ms=17 * HOUR_MS
    )
    try:
        terminal_app._pac_graph_authority.facts = _Facts(  # type: ignore[union-attr]
            terminal=True,
            requested=True,
        )
        assert terminal_app._classify_restore_specs((terminal_spec,)) == ()
    finally:
        _close(terminal_app)

    unknown_app, unknown_spec = _app(
        tmp_path / "unknown",
        now_ms=now_ms,
        age_ms=17 * HOUR_MS,
        known_activity=False,
    )
    try:
        assert unknown_app._classify_restore_specs((unknown_spec,)) == (
            unknown_spec,
        )
        assert "worker" in unknown_app._restore_activity_unknown
        assert unknown_app.agents.require("worker").last_active_at_ms == now_ms
    finally:
        _close(unknown_app)


def test_dormant_wake_retains_custody_across_lifecycle_overload_and_late_completion(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    app, spec = _app(tmp_path, now_ms=now_ms, age_ms=17 * HOUR_MS)
    try:
        assert app._classify_restore_specs((spec,)) == ()
        assert app.agents.projection("worker").restore_disposition is not None  # type: ignore[union-attr]
        app._inbox.pending = True  # type: ignore[union-attr]
        lifecycle = _LateLifecycle()
        app._lifecycle_manager = lifecycle  # type: ignore[assignment]
        app._restore_wake_scan(now_ms)
        assert app.agents.projection("worker").restore_disposition is not None  # type: ignore[union-attr]

        app._restore_wake_scan(now_ms + 1)
        assert app.agents.projection("worker").restore_disposition is None  # type: ignore[union-attr]
        assert len(lifecycle.operations) == 2
        assert (
            lifecycle.operations[0].operation_id
            == lifecycle.operations[1].operation_id
        )
    finally:
        app._lifecycle_manager = None
        _close(app)


def test_completed_restore_wake_cannot_replay_for_new_incarnation_or_episode(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    app, spec = _app(tmp_path, now_ms=now_ms, age_ms=17 * HOUR_MS)
    lifecycle = _CompletedLifecycle()
    try:
        assert app._classify_restore_specs((spec,)) == ()
        original = app.agents.projection("worker")
        assert original is not None and original.restore_disposition is not None
        original_disposition = original.restore_disposition
        app.agents.suppress_restore(
            "worker",
            desired_generation=desired_generation(spec),
            last_active_at_ms=1,
            idle_age_ms=10,
            restore_threshold_ms=20,
        )
        observed_again = app.agents.projection("worker")
        assert observed_again is not None
        assert observed_again.restore_disposition is not None
        assert (
            observed_again.restore_disposition.disposition_token
            == original_disposition.disposition_token
        )
        app._lifecycle_manager = lifecycle  # type: ignore[assignment]

        assert app._wake_dormant_agent("worker", reason="pending-work")
        assert len(lifecycle.native_starts) == 1

        assert app.agents.destroy("worker")
        successor = app.agents.create("worker")
        app.agents.suppress_restore(
            "worker",
            desired_generation=desired_generation(spec),
            last_active_at_ms=1,
            idle_age_ms=10,
            restore_threshold_ms=20,
        )
        successor_projection = app.agents.projection("worker")
        assert (
            successor_projection is not None
            and successor_projection.restore_disposition is not None
        )
        successor_disposition = successor_projection.restore_disposition
        assert successor.entity_token != original.entity_token
        assert (
            successor_disposition.disposition_token
            != original_disposition.disposition_token
        )

        assert app._wake_dormant_agent("worker", reason="pending-work")
        assert len(lifecycle.native_starts) == 2
        assert (
            lifecycle.operations[0].operation_id
            != lifecycle.operations[1].operation_id
        )
        assert app.agents.projection("worker").restore_disposition is None  # type: ignore[union-attr]

        app.agents.suppress_restore(
            "worker",
            desired_generation=desired_generation(spec),
            last_active_at_ms=1,
            idle_age_ms=10,
            restore_threshold_ms=20,
        )
        repeated_projection = app.agents.projection("worker")
        assert (
            repeated_projection is not None
            and repeated_projection.restore_disposition is not None
        )
        repeated = repeated_projection.restore_disposition
        assert (
            repeated.disposition_token
            != successor_disposition.disposition_token
        )
        assert not app.agents.clear_restore_disposition(
            "worker",
            expected_entity_token=successor.entity_token,
            expected_desired_generation=desired_generation(spec),
            expected_disposition_token=(
                successor_disposition.disposition_token
            ),
        )
        still_repeated = app.agents.projection("worker")
        assert still_repeated is not None
        assert still_repeated.restore_disposition is not None
        assert (
            still_repeated.restore_disposition.disposition_token
            == repeated.disposition_token
        )
        assert app._wake_dormant_agent("worker", reason="pending-work")
        assert len(lifecycle.native_starts) == 3
        assert len({item.operation_id for item in lifecycle.operations}) == 3
    finally:
        app._lifecycle_manager = None
        _close(app)


def test_owner_alert_retries_after_committed_block_with_stable_key(
    tmp_path: Path, monkeypatch,
) -> None:
    app, _spec = _app(
        tmp_path / "owner-alert-outcome",
        now_ms=2_000_000_000_000,
        age_ms=HOUR_MS,
    )
    agent = app.agents.require("worker")
    notices = []
    release_delivery = threading.Event()

    def notify_owner(**kwargs):
        projection = app.agents.projection("worker")
        assert projection is not None and projection.block is not None
        delivered = release_delivery.is_set()
        notices.append(
            (kwargs["text"], kwargs["idempotency_key"], delivered)
        )
        return SimpleNamespace(delivered=delivered)

    monkeypatch.setattr(
        "hyprial.daemon.impl.autoupdate.alert.notify_owner", notify_owner
    )
    try:
        assert app._blocking_failures.submit(
            agent.uri, "PROVIDER_USAGE_LIMIT", agent.entity_token
        ).value == "accepted"
        deadline = time.monotonic() + 1
        while not notices and time.monotonic() < deadline:
            time.sleep(0.01)
        assert notices and notices[0][2] is False
        assert not app._blocking_failures.close(0.02)
        release_delivery.set()
        assert app._blocking_failures.close(1.0)
        projection = app.agents.projection("worker")
        assert projection is not None and projection.block is not None
        assert projection.block.reason == "provider-quota"
        assert len(notices) >= 2
        assert {item[1] for item in notices} == {
            f"agent-blocked:{agent.entity_token}:provider-quota"
        }
        assert [item[2] for item in notices].count(True) == 1
        assert tuple(app._blocking_failures._notified) == (
            f"agent-blocked:{agent.entity_token}:provider-quota",
        )
    finally:
        release_delivery.set()
        _close(app)


def test_slow_blocking_observation_cannot_block_recreated_agent(
    tmp_path: Path,
) -> None:
    app, _spec = _app(
        tmp_path / "blocking-incarnation",
        now_ms=2_000_000_000_000,
        age_ms=HOUR_MS,
    )
    original = app.agents.require("worker")
    entered = threading.Event()
    release = threading.Event()
    notices: list[tuple[str, str]] = []
    commit = app._blocking_failures._block

    def slow_commit(
        recipient: str, reason: str, expected_entity_token: str
    ):
        entered.set()
        assert release.wait(1.0)
        return commit(recipient, reason, expected_entity_token)

    app._blocking_failures._block = slow_commit
    app._blocking_failures._notify = (
        lambda text, *, idempotency_key: notices.append((text, idempotency_key))
    )
    try:
        assert app._blocking_failures.submit(
            original.uri,
            "PROVIDER_USAGE_LIMIT",
            original.entity_token,
        ).value == "accepted"
        assert entered.wait(1.0)

        assert app.agents.destroy("worker")
        successor = app.agents.create("worker")
        app.agents.suppress_restore(
            "worker",
            desired_generation="successor-desired",
            last_active_at_ms=1,
            idle_age_ms=10,
            restore_threshold_ms=20,
        )
        assert successor.entity_token != original.entity_token

        release.set()
        assert app._blocking_failures.close(1.0)
        projection = app.agents.projection("worker")
        assert projection is not None
        assert projection.entity_token == successor.entity_token
        assert projection.block is None
        assert projection.restore_disposition is not None
        assert (
            projection.restore_disposition.desired_generation
            == "successor-desired"
        )
        assert notices == []
    finally:
        release.set()
        _close(app)


def test_slow_policy_inbox_pac_effects_do_not_block_harness_or_maintenance_tick(
    tmp_path: Path,
) -> None:
    app, _spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    entered = threading.Event()
    release = threading.Event()

    def slow_restore_scan(_observed_at_ms: int) -> None:
        entered.set()
        assert release.wait(2)

    cadence = BoundedCadence("slow-restore-test", slow_restore_scan)
    app._restore_wake_cadence = cadence
    app._flush_agent_activity = lambda: None  # type: ignore[method-assign]
    app._retry_restore_eligibility = lambda: None  # type: ignore[method-assign]
    app._run_scheduled_domains = lambda _started: (  # type: ignore[method-assign]
        None,
        (),
        0,
        (),
    )
    app._record_maintenance_outcome = lambda *_args: None  # type: ignore[method-assign]
    app._schedule_maintenance = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    try:
        started = time.monotonic()
        app._scheduled_maintenance(app._maintenance_generation)
        assert time.monotonic() - started < 0.1
        assert entered.wait(1)
    finally:
        release.set()
        assert cadence.close(1.0)
        app._restore_wake_cadence = None
        _close(app)


def test_restore_policy_threshold_and_unblock_cli_contract(monkeypatch) -> None:
    calls = []

    def request(method: str, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    assert runner.invoke(
        _cli_root_app, ["agent", "restore", "worker", "never", "--json"]
    ).exit_code == 0
    assert runner.invoke(
        _cli_root_app, ["agent", "restore", "--threshold", "12h", "--json"]
    ).exit_code == 0
    assert runner.invoke(
        _cli_root_app, ["agent", "unblock", "worker", "--json"]
    ).exit_code == 0
    assert calls == [
        ("agent.restore-policy", {"name": "worker", "policy": "never"}),
        ("agent.restore-threshold", {"thresholdMs": 12 * HOUR_MS}),
        ("agent.unblock", {"name": "worker"}),
    ]
