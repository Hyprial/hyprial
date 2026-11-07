"""Atomic Agent plus Session commit for guarded interactive resume."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError

from ..support import (
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
)


def _create_agent(app) -> None:  # type: ignore[no-untyped-def]
    app.handle(
        "agent.create",
        {
            "name": "alice",
            "cwd": "/work/original",
            "harness": "codex",
            "runtime": "interactive",
            "provider": "openai",
            "model": "gpt-6-astra",
        },
    )
    app.agents.pin("adapter-a", "alice")


def _guarded_params() -> dict[str, object]:
    return {
        "actor": "alice",
        "sessionRef": "thread-resumed",
        "cwd": "/work/moved",
        "command": ["codex", "--provider", "openai", "--model", "gpt-5.6-luna"],
        "source": "codex-app-server",
        "runtime": "codex_interactive",
    }


def test_preflight_advertises_guarded_resume_without_writing(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before = app.agents.require("alice")
    result = app.handle(
        "agent.start.preflight",
        {"name": "alice", "harness": "codex", "runtime": "interactive"},
    )
    assert result["resumeCommitGuardVersion"] == 1
    assert app.agents.require("alice") == before


def test_guarded_session_commit_is_atomic_and_echoes_receipt(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    result = app.handle("session.register_guarded", _guarded_params())
    assert result["liveIncumbentGuardApplied"] is True
    agent = app.agents.require("alice")
    assert agent.cwd == "/work/moved"
    assert agent.provider == "openai"
    assert agent.model == "gpt-5.6-luna"
    assert agent.pinned_adapters == ("adapter-a",)
    session = app.desired_state.load().interactive_sessions[0]
    assert session.session_ref == "thread-resumed"
    assert session.cwd == "/work/moved"


def test_guarded_session_commit_requires_existing_agent(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())
    assert raised.value.code == "AGENT_NOT_FOUND"
    assert app.agents.get("alice") is None
    assert app.desired_state.load().interactive_sessions == ()


def test_guarded_session_rejection_restores_complete_agent_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before = app.agents.require("alice")
    desired_before = app.desired_state.load()

    def reject(_command):  # type: ignore[no-untyped-def]
        raise DaemonRequestError("SESSION_COMMIT_REJECTED", "injected rejection")

    monkeypatch.setattr(app, "_call_session", reject)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())
    assert raised.value.code == "SESSION_COMMIT_REJECTED", str(raised.value)
    assert app.agents.require("alice") == before
    assert app.desired_state.load() == desired_before


def test_guarded_post_commit_lost_completion_reports_committed_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lost receipt cannot turn a durable Session commit into failure."""

    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    real_call = app._call_session

    def commit_then_lose_completion(command):  # type: ignore[no-untyped-def]
        real_call(command)
        raise DaemonRequestError(
            "DOMAIN_COMMAND_TIMEOUT", "injected lost completion after durable commit"
        )

    monkeypatch.setattr(app, "_call_session", commit_then_lose_completion)
    result = app.handle("session.register_guarded", _guarded_params())

    assert result["registered"] is True
    assert result["sessionRef"] == "thread-resumed"
    assert result["liveIncumbentGuardApplied"] is True
    agent = app.agents.require("alice")
    assert agent.cwd == "/work/moved"
    assert agent.model == "gpt-5.6-luna"
    assert app.desired_state.load().interactive_sessions[0].session_ref == "thread-resumed"


def test_guarded_lost_completion_without_commit_is_unknown_without_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before_agent = deepcopy(app.agents.require("alice"))
    before_desired = deepcopy(app.desired_state.load())

    def lose_without_commit(_command):  # type: ignore[no-untyped-def]
        raise DaemonRequestError(
            "DOMAIN_COMMAND_TIMEOUT", "injected lost completion without commit"
        )

    monkeypatch.setattr(app, "_call_session", lose_without_commit)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())

    assert raised.value.code == "RESUME_OUTCOME_UNKNOWN"
    assert app.agents.require("alice") != before_agent
    assert app.desired_state.load() == before_desired


def test_guarded_timeout_empty_readback_cannot_precede_late_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty projection cannot disprove a still-settling admitted command."""

    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before_agent = deepcopy(app.agents.require("alice"))
    before_desired = deepcopy(app.desired_state.load())
    real_call = app._call_session
    submitted: list[object] = []

    def timeout_with_completion_still_pending(command: object) -> None:
        submitted.append(command)
        raise DaemonRequestError(
            "DOMAIN_COMMAND_TIMEOUT", "injected timeout before delayed durable commit"
        )

    monkeypatch.setattr(app, "_call_session", timeout_with_completion_still_pending)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())

    assert raised.value.code == "RESUME_OUTCOME_UNKNOWN"
    assert "hyprial daemon status" in str(raised.value)
    assert app.agents.require("alice") != before_agent
    assert app.desired_state.load() == before_desired
    assert len(submitted) == 1

    real_call(submitted[0])

    assert app.desired_state.load() != before_desired


def test_guarded_lost_completion_readback_failure_is_unknown_and_does_not_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)

    def lose_completion(_command):  # type: ignore[no-untyped-def]
        raise DaemonRequestError(
            "DOMAIN_COMMAND_TIMEOUT", "injected uncertain Session outcome"
        )

    def unreadable(_actor: str):
        raise OSError("injected projection read failure")

    monkeypatch.setattr(app, "_call_session", lose_completion)
    monkeypatch.setattr(app._agent_session_domains.session, "read_session", unreadable)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())

    assert raised.value.code == "RESUME_OUTCOME_UNKNOWN"
    assert "Session may or may not be committed" in str(raised.value)
    attempted = app.agents.require("alice")
    assert attempted.cwd == "/work/moved"
    assert attempted.model == "gpt-5.6-luna"


def test_guarded_restore_failure_is_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)

    def reject(_command):  # type: ignore[no-untyped-def]
        raise DaemonRequestError("SESSION_COMMIT_REJECTED", "injected rejection")

    monkeypatch.setattr(app, "_call_session", reject)
    monkeypatch.setattr(app.agents, "compare_and_swap", lambda *_args: False, raising=False)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())
    assert raised.value.code == "CODEX_RESUME_AGENT_RESTORE_FAILED"
    assert app.desired_state.load().interactive_sessions == ()


def test_guarded_restore_cas_never_overwrites_later_legitimate_agent_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)

    def reject_after_later_commit(_command):  # type: ignore[no-untyped-def]
        current = app.agents.require("alice")
        app.agents.save(
            replace(
                current,
                cwd="/work/later-legitimate",
                model="gpt-6-luna",
                capabilities={**current.capabilities, "winner": "later"},
            )
        )
        raise DaemonRequestError("SESSION_COMMIT_REJECTED", "injected rejection")

    monkeypatch.setattr(app, "_call_session", reject_after_later_commit)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("session.register_guarded", _guarded_params())
    assert raised.value.code == "CODEX_RESUME_AGENT_RESTORE_FAILED"
    agent = app.agents.require("alice")
    assert agent.cwd == "/work/later-legitimate"
    assert agent.model == "gpt-6-luna"
    assert agent.capabilities["winner"] == "later"
    assert agent.pinned_adapters == ("adapter-a",)
    assert app.desired_state.load().interactive_sessions == ()


def test_headless_resume_restore_cas_restores_complete_agent_snapshot(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before = deepcopy(app.agents.require("alice"))
    result = app.handle(
        "agent.create",
        {
            "name": "alice",
            "existing": "reuse",
            "harness": "codex",
            "runtime": "headless",
            "cwd": "/work/attempted",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "includeResumeSnapshot": True,
        },
    )
    assert result["resumePreviousSnapshot"] == before.to_json()

    app.handle(
        "agent.resume.restore",
        {
            "name": "alice",
            "expected": result["resumeSnapshot"],
            "replacement": result["resumePreviousSnapshot"],
        },
    )

    assert app.agents.require("alice") == before


def test_headless_resume_restore_cas_never_overwrites_later_agent_write(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    before = deepcopy(app.agents.require("alice"))
    result = app.handle(
        "agent.create",
        {
            "name": "alice",
            "existing": "reuse",
            "harness": "codex",
            "runtime": "headless",
            "cwd": "/work/attempted",
            "includeResumeSnapshot": True,
        },
    )
    attempted = app.agents.require("alice")
    later = app.agents.save(replace(attempted, cwd="/work/later", model="gpt-6-luna"))

    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "agent.resume.restore",
            {
                "name": "alice",
                "expected": result["resumeSnapshot"],
                "replacement": before.to_json(),
            },
        )

    assert raised.value.code == "RESUME_AGENT_RESTORE_FAILED"
    assert app.agents.require("alice") == later


def test_headless_restore_preserves_write_between_preflight_and_create(
    tmp_path: Path,
) -> None:
    """A write after preflight is not owned by the resume attempt."""

    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    preflight_snapshot = deepcopy(app.agents.require("alice"))

    concurrent = app.agents.save(
        replace(
            preflight_snapshot,
            harness_args={"codex": ("--legitimate-concurrent-write",)},
        )
    )

    create_result = app.handle(
        "agent.create",
        {
            "name": "alice",
            "existing": "reuse",
            "harness": "codex",
            "runtime": "headless",
            "cwd": "/work/attempted",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "includeResumeSnapshot": True,
        },
    )
    attempted = app.agents.require("alice")
    assert attempted.harness_args == concurrent.harness_args

    app.handle(
        "agent.resume.restore",
        {
            "name": "alice",
            "expected": create_result["resumeSnapshot"],
            "replacement": preflight_snapshot.to_json(),
        },
    )

    assert app.agents.require("alice") == concurrent


def test_plain_register_keeps_existing_supersede_contract(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    first = app.handle("session.register", _guarded_params())
    second_params = {**_guarded_params(), "sessionRef": "thread-next"}
    second = app.handle("session.register", second_params)
    assert first["registered"] is True
    assert second["registered"] is True
    assert "liveIncumbentGuardApplied" not in second
    assert app.desired_state.load().interactive_sessions[0].session_ref == "thread-next"


def test_concurrent_guarded_commits_admit_exactly_one(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    def commit(session_ref: str) -> None:
        barrier.wait()
        try:
            app.handle(
                "session.register_guarded",
                {**_guarded_params(), "sessionRef": session_ref, "cwd": f"/{session_ref}"},
            )
        except DaemonRequestError as error:
            with lock:
                failures.append((session_ref, error.code))
        else:
            with lock:
                successes.append(session_ref)

    workers = [
        threading.Thread(target=commit, args=(session_ref,), daemon=True)
        for session_ref in ("resume-a", "resume-b")
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5.0)
        assert not worker.is_alive()

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][1] == "AGENT_ALREADY_RUNNING"
    winner = successes[0]
    assert app.desired_state.load().interactive_sessions[0].session_ref == winner
    assert app.agents.require("alice").cwd == f"/{winner}"


@pytest.mark.parametrize("guarded_first", [True, False])
def test_guarded_and_plain_register_share_the_same_actor_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guarded_first: bool
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _create_agent(app)
    first_ref = "guarded" if guarded_first else "plain"
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()
    real_call_session = app._call_session

    def controlled(command):  # type: ignore[no-untyped-def]
        if command.session_ref == first_ref:
            first_inside.set()
            assert release_first.wait(5.0)
        else:
            second_inside.set()
        return real_call_session(command)

    monkeypatch.setattr(app, "_call_session", controlled)
    failures: list[BaseException] = []

    def register(method: str, session_ref: str) -> None:
        try:
            app.handle(
                method,
                {**_guarded_params(), "sessionRef": session_ref, "cwd": f"/{session_ref}"},
            )
        except BaseException as error:  # noqa: BLE001 - thread boundary
            failures.append(error)

    first_method = "session.register_guarded" if guarded_first else "session.register"
    second_method = "session.register" if guarded_first else "session.register_guarded"
    first = threading.Thread(target=register, args=(first_method, first_ref), daemon=True)
    second_ref = "plain" if guarded_first else "guarded"
    second = threading.Thread(target=register, args=(second_method, second_ref), daemon=True)
    first.start()
    assert first_inside.wait(5.0)
    second.start()
    assert not second_inside.wait(0.2)
    release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)
    assert not first.is_alive()
    assert not second.is_alive()
    if guarded_first:
        assert second_inside.is_set()
        assert failures == []
        assert app.desired_state.load().interactive_sessions[0].session_ref == "plain"
    else:
        assert not second_inside.is_set()
        assert len(failures) == 1
        assert isinstance(failures[0], DaemonRequestError)
        assert failures[0].code == "AGENT_ALREADY_RUNNING"
        assert app.desired_state.load().interactive_sessions[0].session_ref == "plain"
