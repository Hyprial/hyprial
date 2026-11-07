"""Restart restore threshold and durable worker-state acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from hyprial.identity import is_inactive
from hyprial.identity.impl.agents.state.worker_state import (
    RESTORE_THRESHOLD_MS,
    PacRestoreFacts,
    desired_generation,
)
from hyprial.identity.impl.agents.state.restore_migration import (
    LegacyKeepMigrationError,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.shell.impl.cli.commands.doctor.commands import _restore_doctor_check
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


HOUR_MS = 60 * 60 * 1_000


def test_shared_inactivity_boundary_is_strictly_past_threshold() -> None:
    now_ms = 2_000_000_000_000
    last_activity_at_ms = now_ms - 12 * HOUR_MS

    assert not is_inactive(
        last_activity_at_ms,
        now_ms=now_ms,
        threshold_ms=12 * HOUR_MS,
    )
    assert is_inactive(
        last_activity_at_ms - 1,
        now_ms=now_ms,
        threshold_ms=12 * HOUR_MS,
    )


class _RestoreCapture:
    drain_complete = True

    def stop(self) -> None:
        return None

    def __init__(self) -> None:
        self.batches: list[tuple[HarnessLaunchSpec, ...]] = []
        self.started: list[HarnessLaunchSpec] = []

    def restore(self, specs: tuple[HarnessLaunchSpec, ...]) -> SimpleNamespace:
        self.batches.append(specs)
        return SimpleNamespace(
            attempted=len(specs),
            restored=len(specs),
            failed=0,
            deferred=0,
        )

    def start(self, spec: HarnessLaunchSpec) -> bool:
        self.started.append(spec)
        return True


class _PendingInbox:
    def shutdown(self) -> None:
        return None

    def __init__(self, pending: bool = False) -> None:
        self.pending = pending

    def pending_messages(self, _recipient: str) -> tuple[object, ...]:
        return (object(),) if self.pending else ()


def _new_application(tmp_path: Path) -> DaemonApplication:
    return DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="restore-node",
        owner="restore-owner",
        **_deep_module_daemon_dependencies(),
    )


def _application(
    tmp_path: Path,
    *,
    created_at_ms: int,
    now_ms: int | None = None,
    known_activity: bool = True,
) -> DaemonApplication:
    app = _new_application(tmp_path)
    app._agent_registry._clock = lambda: created_at_ms  # noqa: SLF001
    app.agents.create("worker")
    if known_activity:
        assert app.agents.record_activity("worker") is True
    app._agent_registry._clock = lambda: (  # noqa: SLF001
        created_at_ms if now_ms is None else now_ms
    )
    app._inbox = _PendingInbox()  # type: ignore[assignment]
    app._harnesses = _RestoreCapture()  # type: ignore[assignment]
    app._presence = SimpleNamespace()  # type: ignore[assignment]
    app._pac_graph_authority = SimpleNamespace(
        restore_facts=lambda _actor: PacRestoreFacts(), close=lambda *_args: True
    )
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="codex", name="worker", headless=True)
    )
    return app


def _close(app: DaemonApplication) -> None:
    app._close()
    app._close_home_lease()


def test_idle_seventeen_hour_agent_is_suppressed_before_restore(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    try:
        app._restore_harnesses()
        assert capture.batches == []
    finally:
        _close(app)


def test_restore_diagnostics_keep_suppression_and_wake_events(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms)
    app._restore_now_ms = lambda: now_ms
    events = []
    app._log = lambda level, component, event, **fields: events.append(
        (level, component, event, fields)
    )
    try:
        app._restore_harnesses()
        suppressed = [item for item in events if item[2] == "harness.restore.idle_suppressed"]
        assert len(suppressed) == 1
        assert suppressed[0][3] == {
            "actor": app.agents.require("worker").uri,
            "idleAgeMs": 17 * HOUR_MS,
            "restoreThresholdMs": 12 * HOUR_MS,
        }
        assert app._wake_dormant_agent("worker", reason="pending-inbox")
        woken = [item for item in events if item[2] == "harness.restore.woken"]
        assert len(woken) == 1
        assert woken[0][3] == {
            "actor": app.agents.require("worker").uri,
            "reason": "pending-inbox",
        }
    finally:
        _close(app)


@pytest.mark.parametrize(
    ("age_ms", "restored"),
    [
        (11 * HOUR_MS + 59 * 60 * 1_000, True),
        (12 * HOUR_MS, True),
        (17 * HOUR_MS, False),
    ],
)
def test_restore_threshold_boundaries(
    tmp_path: Path, age_ms: int, restored: bool
) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - age_ms, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    try:
        app._restore_harnesses()
        assert bool(capture.batches) is restored
        assert RESTORE_THRESHOLD_MS == 12 * HOUR_MS
    finally:
        _close(app)


def test_legacy_keep_migration_is_idempotent(tmp_path: Path) -> None:
    keep_path = tmp_path / "agent-keep.json"
    policy_path = tmp_path / "agent-restore-policy.json"
    keep_path.write_text(json.dumps(["worker"]), encoding="utf-8")
    policy_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "restoreThresholdMs": 321,
                "agents": {"other": "never"},
            }
        ),
        encoding="utf-8",
    )

    first = _new_application(tmp_path)
    try:
        assert not keep_path.exists()
        assert first._restore_policy.projection().policy.to_json() == {  # noqa: SLF001
            "schemaVersion": 1,
            "restoreThresholdMs": 321,
            "agents": {"other": "never", "worker": "always"},
        }
    finally:
        _close(first)
    migrated = policy_path.read_bytes()

    second = _new_application(tmp_path)
    try:
        assert not keep_path.exists()
        assert policy_path.read_bytes() == migrated
    finally:
        _close(second)


def test_restart_restores_the_same_set_after_legacy_keep_migration(
    tmp_path: Path,
) -> None:
    now_ms = 2_000_000_000_000
    old_ms = now_ms - 17 * HOUR_MS
    keep_path = tmp_path / "agent-keep.json"
    keep_path.write_text(json.dumps(["kept-worker"]), encoding="utf-8")
    (tmp_path / "agent-restore-policy.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "restoreThresholdMs": 12 * HOUR_MS,
                "agents": {"kept-worker": "never", "never-worker": "never"},
            }
        ),
        encoding="utf-8",
    )

    first = _new_application(tmp_path)
    try:
        first._agent_registry._clock = lambda: old_ms  # noqa: SLF001
        for name in ("kept-worker", "never-worker"):
            first.agents.create(name)
            assert first.agents.record_activity(name) is True
            first.desired_state.upsert_harness(
                HarnessLaunchSpec(harness="codex", name=name, headless=True)
            )
    finally:
        _close(first)

    restarted = _new_application(tmp_path)
    capture = _RestoreCapture()
    restarted._harnesses = capture  # type: ignore[assignment]
    restarted._inbox = _PendingInbox()  # type: ignore[assignment]
    restarted._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    restarted._pac_graph_authority = SimpleNamespace(
        restore_facts=lambda _actor: PacRestoreFacts(), close=lambda *_args: True
    )
    try:
        restarted._restore_harnesses()
        restored = {
            spec.name for batch in capture.batches for spec in batch
        }
        legacy_restore_set = {"kept-worker"}
        assert restored == legacy_restore_set
        assert not keep_path.exists()
    finally:
        _close(restarted)


def test_legacy_never_plus_keep_migrates_to_always_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_path = tmp_path / "agent-keep.json"
    keep_path.write_text(json.dumps(["worker"]), encoding="utf-8")
    (tmp_path / "agent-restore-policy.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "restoreThresholdMs": RESTORE_THRESHOLD_MS,
                "agents": {"worker": "never"},
            }
        ),
        encoding="utf-8",
    )
    events: list[tuple[str, dict[str, object]]] = []
    original_log = DaemonApplication._log  # noqa: SLF001

    def record_log(self, level, component, event, **fields):
        events.append((event, fields))
        return original_log(self, level, component, event, **fields)

    monkeypatch.setattr(DaemonApplication, "_log", record_log)
    app = _new_application(tmp_path)
    try:
        assert app._restore_policy.projection().policy.policy_for("worker") == "always"  # noqa: SLF001
        assert not keep_path.exists()
        assert (
            "agent.keep_list_migrated",
            {"agents": ["worker"], "overrodeNever": ["worker"]},
        ) in events
    finally:
        _close(app)


def test_failed_legacy_keep_migration_preserves_the_source(tmp_path: Path) -> None:
    keep_path = tmp_path / "agent-keep.json"
    keep_path.write_text(json.dumps(["worker"]), encoding="utf-8")
    (tmp_path / "agent-restore-policy.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(LegacyKeepMigrationError, match="cannot migrate"):
        _new_application(tmp_path)

    assert json.loads(keep_path.read_text(encoding="utf-8")) == ["worker"]


def test_restored_legacy_keep_file_cannot_override_never(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    app._restore_policy.set_agent("worker", "never")
    (tmp_path / "agent-keep.json").write_text(
        json.dumps(["worker"]), encoding="utf-8"
    )
    try:
        app._restore_harnesses()
        assert capture.batches == []
    finally:
        _close(app)


@pytest.mark.parametrize("override", ["pending", "always", "blocked"])
def test_restore_overrides_old_activity(tmp_path: Path, override: str) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    if override == "pending":
        app._inbox = _PendingInbox(True)  # type: ignore[assignment]
    elif override == "always":
        app._restore_policy.set_agent("worker", "always")
    else:
        app.agents.block_agent("worker", reason="provider-quota")  # noqa: SLF001
    try:
        app._restore_harnesses()
        assert len(capture.batches) == 1
        assert [spec.name for spec in capture.batches[0]] == ["worker"]
    finally:
        _close(app)


def test_open_request_restores_and_terminal_workflow_never_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    try:
        monkeypatch.setattr(
            app._pac_graph_authority, "restore_facts",
            lambda *_args: PacRestoreFacts(requested=True),
        )
        app._restore_harnesses()
        assert len(capture.batches) == 1

        capture.batches.clear()
        monkeypatch.setattr(
            app._pac_graph_authority, "restore_facts",
            lambda *_args: PacRestoreFacts(terminal=True, requested=True),
        )
        app._restore_harnesses()
        assert capture.batches == []
    finally:
        _close(app)


def test_null_activity_restores_once_and_reports_unknown(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path,
        created_at_ms=now_ms - 17 * HOUR_MS,
        now_ms=now_ms,
        known_activity=False,
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    try:
        app._restore_harnesses()
        assert len(capture.batches) == 1
        assert app._restore_agent_projection(  # noqa: SLF001
            app.agents.require("worker"), now_ms=now_ms
        )["activityUnknown"] is True
        assert app.agents.require("worker").last_active_at_ms == now_ms

        capture.batches.clear()
        app._restore_harnesses()
        assert len(capture.batches) == 1
        assert app._restore_activity_unknown == set()  # noqa: SLF001
    finally:
        _close(app)


def test_malformed_restore_policy_fails_open(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    app._restore_now_ms = lambda: now_ms  # type: ignore[method-assign]
    app._restore_policy.path.write_text("not-json", encoding="utf-8")  # noqa: SLF001
    try:
        app._restore_harnesses()
        assert len(capture.batches) == 1
        assert app._restore_policy_degraded is not None  # noqa: SLF001
    finally:
        _close(app)


def test_desired_generation_change_does_not_inherit_suppression(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    original = app.desired_state.load().harnesses[0]
    app.agents.suppress_restore(  # noqa: SLF001
        "worker",
        desired_generation=desired_generation(original),
        last_active_at_ms=now_ms - 17 * HOUR_MS,
        idle_age_ms=17 * HOUR_MS,
        restore_threshold_ms=12 * HOUR_MS,
    )
    replacement = HarnessLaunchSpec(
        harness="codex", name="worker", headless=True, args=("--new",)
    )
    try:
        assert app._is_restore_suppressed_spec(original) is True  # noqa: SLF001
        assert app._is_restore_suppressed_spec(replacement) is False  # noqa: SLF001
    finally:
        _close(app)


def test_same_name_new_incarnation_does_not_inherit_suppression(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path, created_at_ms=1_000, now_ms=2_000)
    spec = app.desired_state.load().harnesses[0]
    first = app.agents.require("worker")
    app.agents.suppress_restore(  # noqa: SLF001
        "worker",
        desired_generation=desired_generation(spec),
        last_active_at_ms=1_000,
        idle_age_ms=1_000,
        restore_threshold_ms=12 * HOUR_MS,
    )
    try:
        assert app.agents.destroy("worker") is True
        replacement = app.agents.create("worker")
        assert replacement.entity_token != first.entity_token
        assert app._agent_registry.restore_disposition("worker") is None  # noqa: SLF001
    finally:
        _close(app)


def test_pending_inbox_clears_suppression_and_wakes_worker(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    capture = _RestoreCapture()
    app._harnesses = capture  # type: ignore[assignment]
    spec = app.desired_state.load().harnesses[0]
    app.agents.suppress_restore(  # noqa: SLF001
        "worker",
        desired_generation=desired_generation(spec),
        last_active_at_ms=now_ms - 17 * HOUR_MS,
        idle_age_ms=17 * HOUR_MS,
        restore_threshold_ms=12 * HOUR_MS,
    )
    app._inbox = _PendingInbox(True)  # type: ignore[assignment]
    try:
        app._wake_pending_dormant_agents()  # noqa: SLF001
        assert app._agent_registry.restore_disposition("worker") is None  # noqa: SLF001
        assert [item.name for item in capture.started] == ["worker"]
    finally:
        _close(app)


def test_dormant_and_blocked_ps_projection(tmp_path: Path) -> None:
    now_ms = 2_000_000_000_000
    app = _application(
        tmp_path, created_at_ms=now_ms - 17 * HOUR_MS, now_ms=now_ms
    )
    spec = app.desired_state.load().harnesses[0]
    app.agents.suppress_restore(  # noqa: SLF001
        "worker",
        desired_generation=desired_generation(spec),
        last_active_at_ms=now_ms - 17 * HOUR_MS,
        idle_age_ms=17 * HOUR_MS,
        restore_threshold_ms=12 * HOUR_MS,
    )
    try:
        dormant = app._restore_agent_projection(  # noqa: SLF001
            app.agents.require("worker"), now_ms=now_ms, spec=spec
        )
        assert dormant == {
            **dormant,
            "restoreStatus": "idle-suppressed",
            "lastActiveAtMs": now_ms - 17 * HOUR_MS,
            "idleAgeMs": 17 * HOUR_MS,
            "restoreThresholdMs": 12 * HOUR_MS,
            "restoreOverride": "none",
            "activityUnknown": False,
            "status": "dormant (idle 17h; auto-restore threshold 12h)",
        }

        app.agents.block_agent("worker", reason="provider-quota")  # noqa: SLF001
        blocked = app._restore_agent_projection(  # noqa: SLF001
            app.agents.require("worker"), now_ms=now_ms, spec=spec
        )
        assert blocked["workerState"] == "blocked"
        assert blocked["blockReason"] == "provider-quota"
        assert blocked["idleAgeMs"] is None
    finally:
        _close(app)


def test_doctor_reports_restore_counts_and_exact_remediation() -> None:
    check = _restore_doctor_check(
        {
            "restore": {
                "suppressedCount": 2,
                "blockedCount": 1,
                "unknownActivityCount": 3,
                "oldestIdleAgeMs": 17 * HOUR_MS,
                "degraded": False,
            }
        }
    )
    assert check is not None
    assert check["status"] == "warn"
    assert (
        check["action"]["command"]
        == "hyprial agent restore <name> always"
    )
    assert "hyprial agent unblock <name>" in check["action"]["description"]


class _Process:
    def __init__(self) -> None:
        self.running = True
        self.pid = None

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def __init__(self) -> None:
        self.processes: list[_Process] = []

    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        process = _Process()
        self.processes.append(process)
        return process


def test_later_reconcile_cannot_bypass_suppression() -> None:
    allowed = {"value": True}
    launcher = _Launcher()
    runtime = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0,
        start_timeout_seconds=2,
        automatic_restore_allowed=lambda _spec: allowed["value"],
    )
    try:
        assert runtime.start(HarnessLaunchSpec("codex", "worker", True)) is True
        assert len(launcher.processes) == 1
        launcher.processes[0].running = False
        allowed["value"] = False
        runtime.reconcile()
        runtime.reconcile()
        assert len(launcher.processes) == 1
    finally:
        runtime.stop(2)


@pytest.mark.parametrize(
    ("failure_code", "reason"),
    [
        ("PROVIDER_USAGE_LIMIT", "provider-quota"),
        ("PROVIDER_AUTHENTICATION_FAILED", "credential-invalid"),
    ],
)
def test_recognised_failure_block_is_durable_and_notified_once(
    tmp_path: Path, failure_code: str, reason: str
) -> None:
    app = _application(tmp_path, created_at_ms=1_000, now_ms=5_000)
    notices: list[tuple[str, str]] = []
    app._owner_alert_notifier = (  # type: ignore[method-assign]
        lambda text, *, idempotency_key: notices.append((text, idempotency_key))
    )
    try:
        app._on_blocking_failure(  # noqa: SLF001
            app.agents.require("worker").uri, failure_code
        )
        app._on_blocking_failure(  # noqa: SLF001
            app.agents.require("worker").uri, failure_code
        )
        block = app._agent_registry.agent_block("worker")  # noqa: SLF001
        assert block is not None and block.reason == reason
        assert len(notices) == 1
    finally:
        _close(app)


def test_unrecognised_failure_never_blocks(tmp_path: Path) -> None:
    app = _application(tmp_path, created_at_ms=1_000, now_ms=5_000)
    try:
        app._on_blocking_failure(  # noqa: SLF001
            app.agents.require("worker").uri, "PROVIDER_TRANSIENT_FAILURE"
        )
        assert app._agent_registry.agent_block("worker") is None  # noqa: SLF001
    finally:
        _close(app)


def test_agent_unblock_is_an_explicit_idempotent_action(tmp_path: Path) -> None:
    app = _application(tmp_path, created_at_ms=1_000)
    try:
        assert app.handle("agent.unblock", {"name": "worker"}) == {
            "actor": "worker",
            "changed": False,
        }
        app.agents.block_agent("worker", reason="provider-quota")  # noqa: SLF001
        assert app.handle("agent.unblock", {"name": "worker"}) == {
            "actor": "worker",
            "changed": True,
        }
        assert app.handle("agent.unblock", {"name": "worker"}) == {
            "actor": "worker",
            "changed": False,
        }
    finally:
        _close(app)
