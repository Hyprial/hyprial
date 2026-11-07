"""agent_entity: lifecycle scenarios."""

from __future__ import annotations
import threading
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.shell.impl.daemon_wiring.routine_coordinator import RoutineCoordinatorAdapter

from hyprial.kernel import HarnessLaunchSpec

from .support import (
    URI,
    _close_test_applications as _close_test_applications,
    _scheduled_routine,
    application,
    enable_routine_coordinator,
    enable_routines,
    start_headless,
)














def test_host_invite_reports_missing_default_when_routines_are_unavailable(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    created = app.handle(
        "agent.host-invite", {"name": "visitor", "owner": "remote-owner"}
    )
    assert created["routineWarning"] == {
        "code": "AGENT_DEFAULT_ROUTINE_UNAVAILABLE",
        "message": (
            "agent was created without a routine because the routine service "
            "is unavailable; repair the service, then bind one "
            "with `hyprial routine add`"
        ),
    }


def test_default_routine_renderer_preserves_name_substitution():
    from hyprial.biz import load_routine_text
    from hyprial.shell.impl.daemon_wiring.routines import routine_template_renderer

    text = routine_template_renderer(
        "agent-home-setup", owner=URI, escalate_to="user:op",
        name="resident-home-setup",
    )
    spec = load_routine_text(text)

    assert spec.name == "resident-home-setup"
    assert spec.actor == URI


def test_default_routine_preserves_stored_legacy_agent_owner_spelling(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    captured: dict[str, object] = {}

    class RoutineService:
        def list(self) -> dict[str, list[object]]:
            return {"routines": []}

        def add(self, *, yaml_text: str, owner: str, enabled: bool) -> None:
            captured.update(yaml_text=yaml_text, owner=owner, enabled=enabled)

        def close(self) -> None:
            return None

    def render(_template: str, **values: str) -> str:
        captured.update(values)
        return "rendered"

    app._routine_service = RoutineService()  # type: ignore[assignment]
    app._routine_template_renderer = render
    app._routine_text_loader = lambda _text: object()
    app._routine_admit = lambda _spec: None
    agent = SimpleNamespace(
        uri="agent:stored-owner:node-1:worker",
        owner="legacy:owner",
        entity_token="entity-1",
    )

    assert app._register_default_agent_routine(agent) is None
    assert captured["escalate_to"] == "user:legacy:owner"
    assert captured["owner"] == "user:legacy:owner"


def test_default_routine_renderer_rejects_unknown_template_with_name_substitution():
    from hyprial.shell.impl.daemon_wiring.routines import routine_template_renderer

    with pytest.raises(ValueError, match="unknown routine template: protocol-negative"):
        routine_template_renderer(
            "protocol-negative", owner=URI, escalate_to="user:op",
            name="resident-home-setup",
        )


def test_default_routine_timeout_keeps_committed_agent(tmp_path: Path) -> None:
    from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinatorTimeout

    class PendingCoordinator:
        def begin(self, _command):  # type: ignore[no-untyped-def]
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, timeout: float):
            del timeout
            raise RoutineCoordinatorTimeout(operation_id)

        def close(self, timeout: float = 5.0) -> bool:
            return True

    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app._routine_coordinator = RoutineCoordinatorAdapter(PendingCoordinator())  # type: ignore[arg-type]
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "foo"})
    assert failure.value.code == "AGENT_DEFAULT_ROUTINE_PENDING"
    assert app.agents.get("foo") is not None


@pytest.mark.parametrize("recreate", [False, True])
def test_default_routine_timeout_late_failure_cas_compensates_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recreate: bool,
) -> None:
    from hyprial.biz.impl.routine.execution.coordinator import (
        RoutineCoordinator,
        RoutineCoordinatorError,
    )

    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert app._routine_service is not None
    entered = threading.Event()
    release = threading.Event()

    def fail_late(_routine):
        entered.set()
        assert release.wait(3.0)
        raise RoutineCoordinatorError(
            "SYNTHETIC_ROUTINE_FAILURE", "late failure", "unused"
        )

    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=app._routine_service.service,
        ensure_coordinator=fail_late,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=app._compensate_default_agent_incarnation,
    )
    app._routine_coordinator = RoutineCoordinatorAdapter(coordinator)
    settled_wait = coordinator.wait
    monkeypatch.setattr(
        coordinator,
        "wait",
        lambda operation_id, timeout: settled_wait(operation_id, 0.01),
    )
    try:
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("agent.create", {"name": "foo"})
        assert failure.value.code == "AGENT_DEFAULT_ROUTINE_PENDING"
        assert entered.wait(1.0)
        original = app.agents.require("foo")
        if recreate:
            assert app.agents.destroy(
                "foo", expected_entity_token=original.entity_token
            )
            successor = app.agents.create("foo")
            assert successor.entity_token != original.entity_token
        release.set()
        with pytest.raises(RoutineCoordinatorError) as late:
            settled_wait(f"agent-default-routine:{original.entity_token}", 3.0)
        assert late.value.code == "SYNTHETIC_ROUTINE_FAILURE"
        current = app.agents.get("foo")
        assert (current is not None) is recreate
        if recreate:
            assert current is not None
            assert current.entity_token == successor.entity_token
    finally:
        release.set()


def test_default_routine_compensation_joins_accepted_destroy_after_home_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-shaped RED: destroy commits revoke before slow home cleanup.

    A fresh-correlation retry sees no Agent row and cannot join the original
    late completion.  The corrected path must retain one exact destroy
    correlation until the real Agent owner settles cleanup.
    """

    from hyprial.identity.impl.agents.home.effects import CleanupHome
    from hyprial.biz.impl.routine.execution.coordinator import (
        RoutineCoordinator,
        RoutineCoordinatorError,
    )

    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert app._routine_service is not None
    provisioner = app._agent_registry._home
    assert provisioner is not None
    setup_entered = threading.Event()
    release_setup = threading.Event()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    original_cleanup = provisioner.cleanup

    def fail_late(_routine):
        setup_entered.set()
        assert release_setup.wait(10.0)
        raise RoutineCoordinatorError(
            "SYNTHETIC_ROUTINE_FAILURE", "late setup failure", "unused"
        )

    def slow_cleanup(receipt, *, expected_token):  # type: ignore[no-untyped-def]
        cleanup_entered.set()
        assert release_cleanup.wait(15.0)
        return original_cleanup(receipt, expected_token=expected_token)

    monkeypatch.setattr(provisioner, "cleanup", slow_cleanup)
    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=app._routine_service.service,
        ensure_coordinator=fail_late,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=app._compensate_default_agent_incarnation,
    )
    app._routine_coordinator = RoutineCoordinatorAdapter(coordinator)
    settled_wait = coordinator.wait
    monkeypatch.setattr(
        coordinator,
        "wait",
        lambda operation_id, timeout: settled_wait(operation_id, 0.01),
    )
    try:
        with pytest.raises(DaemonRequestError) as pending:
            app.handle("agent.create", {"name": "foo"})
        assert pending.value.code == "AGENT_DEFAULT_ROUTINE_PENDING"
        original = app.agents.require("foo")
        assert setup_entered.wait(1.0)
        release_setup.set()
        assert cleanup_entered.wait(3.0)

        # Agent identity revocation is already committed before CleanupHome.
        assert app._agent_registry.get("foo") is None
        cleanup = app._agent_registry.prepare_cleanup_revoked_home("foo")
        assert cleanup is not None
        assert isinstance(cleanup.plan, CleanupHome)
        assert cleanup.plan.receipt.entity_token == original.entity_token
        assert cleanup.plan.receipt.status == "revoked"

        # Cross the real AgentDirectoryFacade wait budget, then let the exact
        # accepted home effect finish.  A fresh UUID retry cannot observe it.
        assert not release_cleanup.wait(5.2)
        release_cleanup.set()
        operation_id = f"agent-default-routine:{original.entity_token}"
        with pytest.raises(RoutineCoordinatorError) as settled:
            settled_wait(operation_id, 3.0)
        assert settled.value.code == "SYNTHETIC_ROUTINE_FAILURE"
    finally:
        release_setup.set()
        release_cleanup.set()


def test_exact_agent_destroy_settlement_recovers_revoked_home_after_restart(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.composition import DomainCommandError

    first = application(tmp_path)
    original = first.agents.create("foo")
    changed, reservation = first._agent_registry.prepare_destroy_record("foo")
    assert changed is True and reservation is not None
    assert first._agent_registry.get("foo") is None
    assert first._agent_registry.prepare_cleanup_revoked_home("foo") is not None
    first._close()
    first._close_home_lease()

    restarted = application(tmp_path)
    settled = restarted.agents.settle_destroy(
        "agent-default-compensation:restart",
        "foo",
        expected_entity_token=original.entity_token,
    )
    assert settled.disposition == "destroyed"
    assert restarted._agent_registry.get("foo") is None
    assert restarted._agent_registry.prepare_cleanup_revoked_home("foo") is None

    replay = restarted.agents.settle_destroy(
        "agent-default-compensation:restart",
        "foo",
        expected_entity_token=original.entity_token,
    )
    assert replay.disposition == "already-cleaned"

    with pytest.raises(DomainCommandError) as unknown:
        restarted.agents.settle_destroy(
            "agent-default-compensation:unknown",
            "never-existed",
            expected_entity_token="missing-entity",
        )
    assert unknown.value.code == "AGENT_DESTROY_SETTLEMENT_UNKNOWN"
    assert (
        restarted._agent_session_domains._agent_events._settlement_reservations
        == set()
    )


def test_exact_agent_destroy_settlement_preserves_same_name_successor(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    original = app.agents.create("foo")
    assert app.agents.destroy(
        "foo", expected_entity_token=original.entity_token
    )
    successor = app.agents.create("foo")
    assert successor.entity_token != original.entity_token

    settled = app.agents.settle_destroy(
        "agent-default-compensation:stale",
        "foo",
        expected_entity_token=original.entity_token,
    )
    assert settled.disposition == "stale-incarnation"
    assert app.agents.require("foo").entity_token == successor.entity_token


@pytest.mark.parametrize(
    "admission",
    [PortAdmission.OVERLOADED, PortAdmission.CLOSING],
)
def test_exact_agent_destroy_settlement_retires_pin_on_refused_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admission: PortAdmission,
) -> None:
    from hyprial.daemon.impl.composition import DomainCommandError

    app = application(tmp_path)
    agent = app.agents.create("foo")
    events = app._agent_session_domains._agent_events
    monkeypatch.setattr(
        app._agent_session_domains.agent,
        "submit",
        lambda _command: admission,
    )

    with pytest.raises(DomainCommandError) as refused:
        app.agents.settle_destroy(
            f"agent-default-compensation:{admission.value}",
            "foo",
            expected_entity_token=agent.entity_token,
        )
    assert refused.value.code == f"PORT_{admission.value.upper()}"
    assert events._settlement_reservations == set()


def test_late_default_failure_cannot_delete_recreated_agent(tmp_path: Path) -> None:
    from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinatorError

    app = application(tmp_path)
    enable_routines(app, tmp_path)

    class RecreatingCoordinator:
        def begin(self, _command):  # type: ignore[no-untyped-def]
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, timeout: float):
            del timeout
            original = app.agents.require("foo")
            assert app.agents.destroy(
                "foo", expected_entity_token=original.entity_token
            )
            successor = app.agents.create("foo")
            assert successor.entity_token != original.entity_token
            raise RoutineCoordinatorError(
                "SYNTHETIC_ROUTINE_FAILURE", "late failure", operation_id
            )

        def close(self, timeout: float = 5.0) -> bool:
            return True

    app._routine_coordinator = RoutineCoordinatorAdapter(RecreatingCoordinator())  # type: ignore[arg-type]
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "foo"})
    assert failure.value.code == "AGENT_DEFAULT_ROUTINE_FAILED"
    assert app.agents.get("foo") is not None


def test_concurrent_last_binding_removals_leave_one(tmp_path: Path) -> None:
    from hyprial.biz.impl.routine.execution.service import RoutineServiceError

    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    app.handle(
        "routine.add",
        {"yaml": _scheduled_routine("replacement", URI), "actor": "user:op"},
    )
    assert app._routine_service is not None
    names = [
        item["name"]
        for item in app.handle("routine.list", {"actor": "user:op"})["routines"]
    ]
    outcomes: list[str] = []

    def remove(name: str) -> None:
        try:
            app._routine_service.service.remove(name=name, enforce_last=True)
            outcomes.append("removed")
        except RoutineServiceError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=remove, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["ROUTINE_LAST_BINDING", "removed"]
    assert len(app._routine_service.list()["routines"]) == 1


def test_agent_destroy_settles_bound_routine_before_entity_delete(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    enable_routine_coordinator(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    result = app.handle("agent.destroy", {"name": "foo"})
    assert result["destroyed"] is True
    assert len(result["removedRoutines"]) == 1
    assert app.agents.get("foo") is None
    assert app._routine_service is not None
    assert app._routine_service.list()["routines"] == []


def test_agent_destroy_without_bound_routine_reports_empty_removals(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    result = app.handle("agent.destroy", {"name": "foo"})
    assert result["destroyed"] is True
    assert result["removedRoutines"] == []


# --------------------------------------------------------------------------
# Stored harnessArgs reach every launch path (pi P2 launch-trust defect A)
# --------------------------------------------------------------------------
#
# 2026-10-16 production: a resident pi agent whose record already carried
# ``harnessArgs: {"pi": ["--approve"]}`` still failed ``hyprial start`` with
# "requires explicit --approve trust", because lifecycle.start built its
# HarnessLaunchSpec from the request params alone and never consulted the
# agent record.  The same gap would have hit daemon restore and routine wake.


def _stored_harness_args(app, name: str, args: list[str]) -> None:  # type: ignore[no-untyped-def]
    from hyprial.identity import normalize_harness_args

    app.agents.create(
        name,
        harness_args=normalize_harness_args({"pi": args}),
        preferred_harness="pi",
    )


def _desired_args(app, name: str) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    return next(
        spec.args for spec in app.desired_state.load().harnesses if spec.name == name
    )


def test_explicit_start_merges_the_agents_stored_harness_args(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    _stored_harness_args(app, "foo", ["--approve"])

    started = start_headless(app, "pi", "foo")

    assert started["ok"] is True
    assert _desired_args(app, "foo") == ("--approve",)


def test_explicit_start_args_win_and_stored_args_are_deduplicated(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    _stored_harness_args(app, "foo", ["--approve", "--model", "stored"])

    app.handle(
        "lifecycle.start",
        {
            "provider": "pi",
            "name": "foo",
            "headless": True,
            "cwd": "/work",
            "args": ["--model", "explicit"],
        },
    )

    assert _desired_args(app, "foo") == ("--model", "explicit", "--approve")


def test_routine_wake_up_merges_the_agents_stored_harness_args(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    _stored_harness_args(app, "foo", ["--approve"])

    launched = app._ensure_routine_coordinator(
        {
            "name": "resident-routine",
            "produces": URI,
            "registrationId": "reg-1",
            "launch": {"harness": "pi"},
        },
        recovering=False,
    )

    assert launched is not None
    assert launched["actor"] == URI
    assert _desired_args(app, "foo") == ("--approve",)


def test_restore_merges_the_agents_stored_harness_args(tmp_path: Path) -> None:
    app = application(tmp_path)
    _stored_harness_args(app, "foo", ["--approve"])

    declared = app._classify_restore_specs(
        (HarnessLaunchSpec("pi", "foo", True, cwd="/work"),)
    )

    assert tuple(spec.args for spec in declared) == (("--approve",),)


def test_explicit_codex_short_option_still_wins(tmp_path: Path) -> None:
    """A stored Codex ``-a on-request`` must not survive ``-a never``."""

    app = application(tmp_path)
    app.agents.create(
        "foo",
        harness_args={"codex": ("-a", "on-request")},
        preferred_harness="codex",
    )

    app.handle(
        "lifecycle.start",
        {
            "provider": "codex",
            "name": "foo",
            "headless": True,
            "cwd": "/work",
            "args": ["-a", "never"],
        },
    )

    spec = next(
        row for row in app.desired_state.load().harnesses if row.name == "foo"
    )
    assert spec.args == ("-a", "never")


def test_merge_replaces_a_short_option_group() -> None:
    from hyprial.kernel import merge_harness_launch_args

    assert merge_harness_launch_args(
        ("-a", "on-request"), ("-a", "never")
    ) == ("-a", "never")


def test_merge_keeps_options_that_share_a_value() -> None:
    from hyprial.kernel import merge_harness_launch_args

    merged = merge_harness_launch_args(
        ("--provider", "deepseek"), ("--model", "deepseek")
    )
    assert merged == ("--model", "deepseek", "--provider", "deepseek")


def test_merge_canonicalizes_codex_short_and_long_aliases() -> None:
    from hyprial.kernel import merge_harness_launch_args

    merged = merge_harness_launch_args(
        ("--ask-for-approval", "on-request"), ("-a", "never"), harness="codex"
    )
    assert merged == ("-a", "never")


@pytest.mark.parametrize("old,new", [((), ("--permission-mode", "auto")), (("--permission-mode", "auto"), ())])
@pytest.mark.parametrize("refresh", [False, True])
def test_start_keeps_out_of_band_stored_args(
    tmp_path: Path, old: tuple[str, ...], new: tuple[str, ...], refresh: bool,
) -> None:
    app = application(tmp_path)
    app.agents.create("foo", harness_args={"claude": old}, preferred_harness="claude")
    with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
        database.execute("UPDATE agents SET harness_args = ? WHERE actor = ?", (json.dumps({"claude": list(new)}), "foo"))
    if refresh:
        app.agents.create("unrelated-refresh")
    app.handle(
        "agent.create",
        {"name": "foo", "existing": "reuse", "harness": "claude", "runtime": "headless", "cwd": "/work"},
    )
    start_headless(app, "claude", "foo")
    assert _desired_args(app, "foo") == new


@pytest.mark.parametrize("args", [("--permission-mode", "invalid-probe"), ("--permission-mode=invalid-probe",), ("--permission-mode",)])
def test_invalid_stored_permission_mode_refuses_start(tmp_path: Path, args: tuple[str, ...]) -> None:
    app = application(tmp_path)
    app.agents.create("foo", preferred_harness="claude")
    with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
        database.execute("UPDATE agents SET harness_args = ? WHERE actor = 'foo'", (json.dumps({"claude": args}),))
    app.handle("agent.create", {"name": "foo", "existing": "reuse", "harness": "claude", "runtime": "headless", "cwd": "/work"})
    with pytest.raises(DaemonRequestError, match="invalid Claude --permission-mode") as failure:
        start_headless(app, "claude", "foo")
    assert failure.value.code == "INVALID_ARGUMENT"
    assert not app.desired_state.load().harnesses


def test_runtime_save_does_not_overwrite_concurrent_stored_args(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.agents.create("foo", preferred_harness="claude")
    for mode in ("auto", "default", "plan", "acceptEdits", "auto"):
        with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
            database.execute("UPDATE agents SET harness_args = ? WHERE actor = 'foo'", (json.dumps({"claude": ["--permission-mode", mode]}),))
        app.agents.record_session("foo", harness="claude", session_id="concurrent-session")
        start_headless(app, "claude", "foo")
        assert _desired_args(app, "foo") == ("--permission-mode", mode)
        app.handle("down", {"target": "foo"})


def test_refused_resume_restores_current_stored_args(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.agents.create("foo", preferred_harness="claude")
    with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
        database.execute("UPDATE agents SET harness_args = ? WHERE actor = 'foo'", (json.dumps({"claude": ["--permission-mode", "auto"]}),))
    created = app.handle("agent.create", {
        "name": "foo", "existing": "reuse", "harness": "claude",
        "runtime": "headless", "cwd": "/work", "includeResumeSnapshot": True,
    })
    app.handle("agent.resume.restore", {
        "name": "foo", "expected": created["resumeSnapshot"],
        "replacement": created["resumePreviousSnapshot"],
    })
    start_headless(app, "claude", "foo")
    assert _desired_args(app, "foo") == ("--permission-mode", "auto")


def test_resume_preflight_returns_current_stored_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = application(tmp_path)
    app.agents.create("foo", preferred_harness="claude")
    with closing(sqlite3.connect(tmp_path / "agents.sqlite3")) as database, database:
        database.execute("UPDATE agents SET harness_args = ? WHERE actor = 'foo'", (json.dumps({"claude": ["--permission-mode", "auto"]}),))
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    result = app.handle("lifecycle.resume.preflight", {
        "provider": "claude", "name": "foo", "headless": True, "sessionRef": "isolated-session",
    })
    assert result["agentSnapshot"]["harnessArgs"] == {"claude": ["--permission-mode", "auto"]}
