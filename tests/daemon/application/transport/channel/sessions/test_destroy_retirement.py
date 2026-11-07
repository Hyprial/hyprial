from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError

from ..support import (
    CANONICAL,
    IPC_WAIT_SECONDS,
    ROUTE_PROGRESS_SECONDS,
    FakeInbox,
    application,
    pending_message,
    register,
)
from ..support import _close_application_domains as _close_application_domains


def _assert_superseded(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DaemonRequestError) as failure:
        call()
    assert failure.value.code == "SESSION_SUPERSEDED"
    assert "destroyed" in str(failure.value)


def _register(app, session_ref: str) -> object:  # type: ignore[no-untyped-def]
    return app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": session_ref,
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


def test_destroyed_session_is_terminal_for_fenced_calls_and_register(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    register(app, "session-a")

    destroyed = app.handle("agent.destroy", {"name": "alice"})

    assert destroyed["destroyed"] is True
    assert app.agents.get("alice") is None
    assert app._agent_session_domains.session.read_session(CANONICAL) is None
    _assert_superseded(
        lambda: app.handle(
            "message.pending.list",
            {"actor": "alice", "sessionRef": "session-a"},
        )
    )
    _assert_superseded(lambda: _register(app, "session-a"))
    assert app.agents.get("alice") is None
    assert app._agent_session_domains.session.read_session(CANONICAL) is None


def test_destroyed_session_remains_terminal_after_store_reopen(tmp_path: Path) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))
    register(first, "session-a")
    first.handle("agent.destroy", {"name": "alice"})

    restarted = application(tmp_path, FakeInbox(pending_message()))

    _assert_superseded(
        lambda: restarted.handle(
            "message.pending.list",
            {"actor": "alice", "sessionRef": "session-a"},
        )
    )
    _assert_superseded(lambda: _register(restarted, "session-a"))
    assert restarted.agents.get("alice") is None
    assert restarted._agent_session_domains.session.read_session(CANONICAL) is None


def test_plain_recreate_does_not_release_refs_but_exact_release_releases_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    register(app, "session-a")
    monkeypatch.setattr(app, "_worker_session_ref", lambda actor: "session-b")
    old_token = app.agents.require("alice").entity_token

    app.handle("agent.destroy", {"name": "alice"})
    app.handle("agent.create", {"name": "alice"})
    recreated_token = app.agents.require("alice").entity_token

    assert recreated_token != old_token
    _assert_superseded(lambda: _register(app, "session-a"))
    registry = app._agent_session_domains._registry
    assert registry.release_retired_session_ref(CANONICAL, "session-a") is True
    assert _register(app, "session-a") is not None
    _assert_superseded(lambda: _register(app, "session-b"))


def test_destroy_retires_managed_worker_session_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app.handle("agent.create", {"name": "alice"})
    monkeypatch.setattr(app, "_worker_session_ref", lambda actor: "worker-ref")

    app.handle("agent.destroy", {"name": "alice"})

    assert app._agent_session_domains._registry.is_session_ref_retired(
        CANONICAL, "worker-ref"
    ) is True
    _assert_superseded(lambda: _register(app, "worker-ref"))
    assert app.agents.get("alice") is None


def test_destroy_retires_superseded_ref_and_cannot_hijack_recreated_agent(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _register(app, "session-a")
    _register(app, "session-b")

    app.handle("agent.destroy", {"name": "alice"})
    app.handle("agent.create", {"name": "alice"})
    _register(app, "session-c")

    _assert_superseded(lambda: _register(app, "session-a"))
    current = app._agent_session_domains.session.read_session(CANONICAL)
    assert current is not None
    assert current.session_ref == "session-c"


def test_destroy_retires_superseded_ref_without_recreating_agent(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _register(app, "session-a")
    _register(app, "session-b")

    app.handle("agent.destroy", {"name": "alice"})

    _assert_superseded(lambda: _register(app, "session-a"))
    assert app.agents.get("alice") is None
    assert app._agent_session_domains.session.read_session(CANONICAL) is None


def test_destroy_retires_ref_that_was_unregistered_first(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _register(app, "session-a")
    app.handle(
        "session.unregister", {"actor": "alice", "sessionRef": "session-a"}
    )

    app.handle("agent.destroy", {"name": "alice"})

    _assert_superseded(lambda: _register(app, "session-a"))
    assert app.agents.get("alice") is None


@pytest.mark.parametrize("method", ("session.refresh", "session.heartbeat"))
def test_retired_ref_is_superseded_at_session_maintenance_entry_points(
    method: str,
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    _register(app, "session-a")
    app.handle("agent.destroy", {"name": "alice"})

    _assert_superseded(
        lambda: app.handle(
            method, {"actor": "alice", "sessionRef": "session-a"}
        )
    )


def test_failed_destroy_rolls_back_only_its_retired_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    agent = app.agents.create("alice")
    app.agents.retire_session_refs(
        agent.uri,
        agent.entity_token,
        ("previously-retired",),
        reason="earlier-destroy",
        destroy_attempt="earlier-attempt",
    )
    _register(app, "session-a")

    def fail_after_retirement(_agent) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
        raise RuntimeError("injected destroy failure")

    monkeypatch.setattr(app, "_remove_agent_routines", fail_after_retirement)

    with pytest.raises(RuntimeError, match="injected destroy failure"):
        app.handle("agent.destroy", {"name": "alice"})

    assert app.agents.get("alice") is not None
    assert app.agents.is_session_ref_retired(CANONICAL, "session-a") is False
    assert app.agents.is_session_ref_retired(CANONICAL, "previously-retired") is True
    _register(app, "session-a")


def test_startup_seed_skips_a_retired_persisted_session(tmp_path: Path) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))
    _register(first, "session-a")
    agent = first.agents.require("alice")
    first.agents.retire_session_refs(
        agent.uri,
        agent.entity_token,
        ("session-a",),
        reason="test-retirement",
        destroy_attempt="seed-test-attempt",
    )

    restarted = application(tmp_path, FakeInbox(pending_message()))
    persisted = restarted.desired_state.load().interactive_sessions[0]
    assert restarted.agents.is_session_ref_retired(CANONICAL, "session-a") is True
    assert (
        restarted.agents.is_session_ref_retired(
            persisted.actor, persisted.session_ref
        )
        is True
    )
    restarted._seed_agent_bindings()

    assert restarted._agent_liveness.binding(CANONICAL) is None


def test_register_and_destroy_are_serialized_across_retirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app.handle("agent.create", {"name": "alice"})
    app._ensure_session_route_coordinator()
    register_entered = threading.Event()
    release_register = threading.Event()
    original_call_session = app._call_session

    def pause_register(command):  # type: ignore[no-untyped-def]
        if command.__class__.__name__ == "RegisterSessionCommand":
            register_entered.set()
            assert release_register.wait(IPC_WAIT_SECONDS)
        return original_call_session(command)

    monkeypatch.setattr(app, "_call_session", pause_register)
    register_errors: list[BaseException] = []
    destroy_errors: list[BaseException] = []

    def run_register() -> None:
        try:
            _register(app, "session-race")
        except BaseException as error:  # noqa: BLE001 - thread result crosses test edge
            register_errors.append(error)

    def run_destroy() -> None:
        try:
            app.handle("agent.destroy", {"name": "alice"})
        except BaseException as error:  # noqa: BLE001 - thread result crosses test edge
            destroy_errors.append(error)

    register_thread = threading.Thread(target=run_register, daemon=True)
    register_thread.start()
    assert register_entered.wait(ROUTE_PROGRESS_SECONDS)
    destroy_thread = threading.Thread(target=run_destroy, daemon=True)
    destroy_thread.start()
    release_register.set()
    register_thread.join(timeout=IPC_WAIT_SECONDS)
    destroy_thread.join(timeout=IPC_WAIT_SECONDS)

    assert not register_thread.is_alive()
    assert not destroy_thread.is_alive()
    assert register_errors == []
    assert destroy_errors == []
    _assert_superseded(lambda: _register(app, "session-race"))
    assert app.agents.get("alice") is None


def test_destroy_between_register_precheck_and_ensure_agent_creates_nothing(
    tmp_path: Path,
) -> None:
    # Review 862: a whole destroy can land after a registration's first
    # retired check and before _ensure_agent. Simulate that interleaving by
    # letting the first check pass, as it did before the destroy ran.
    app = application(tmp_path, FakeInbox(pending_message()))
    register(app, "session-a")
    app.handle("agent.destroy", {"name": "alice"})
    real_check = app._reject_retired_session_ref
    calls = {"count": 0}

    def precheck_ran_before_destroy(actor: str, session_ref: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            return
        real_check(actor, session_ref)

    app._reject_retired_session_ref = precheck_ran_before_destroy  # type: ignore[method-assign]

    _assert_superseded(lambda: _register(app, "session-a"))
    assert calls["count"] >= 2
    assert app.agents.get("alice") is None
    assert app._agent_session_domains.session.read_session(CANONICAL) is None
