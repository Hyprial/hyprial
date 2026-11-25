"""The user agent inside a daemon application: the upgrade path and the refusals.

The application is the real ``DaemonApplication`` of the agent entity tests
(fake harness supervisor, inline lifecycle).  No OrgFS is composed: the
ensure reads and writes nothing replicated.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.agent_templates import template_cell_params
from hyprial.daemon.impl.bootstrap.names import require_agent_name_available
from hyprial.daemon.impl.cells.runtime.agents import HARNESS_ROLE, agent_cell_spec
from hyprial.daemon.impl.user_agent import (
    CREATED,
    PRESENT,
    USER_AGENT_NAME,
    local_user_agent,
    local_user_agent_address,
    run_user_agent_ensure,
    schedule_user_agent_ensure,
)
from hyprial.identity import agent_id_for_entity_token
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
    enable_routines,
)


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert run_user_agent_ensure(app) == CREATED
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    return app, agent


def test_an_existing_owner_gets_the_user_agent_at_the_next_start(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    assert run_user_agent_ensure(app) == PRESENT
    assert local_user_agent_address(app.agents) == agent.uri
    assert agent.uri == f"agent:{OWNER}:{NODE}:{agent.agent_id}"
    assert getattr(app, "_orgfs_runtime", None) is None


def test_no_harness_spec_no_process_and_no_default_routine(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    assert app.desired_state.load().harnesses == ()
    assert app._harnesses.running == {}
    assert app.handle("routine.list", {"actor": "user:op"})["routines"] == []
    listed = app.handle("agent.list", {"includeUserAgent": True})
    rows = {row["actor"]: row for row in listed["agents"]}
    assert rows[USER_AGENT_NAME]["capabilities"] == {"template": "user"}
    assert agent.uri not in listed["agentsWithoutRoutine"]


def test_the_agent_cell_has_no_member_and_its_role_is_user(tmp_path: Path) -> None:
    _app, agent = _ensured(tmp_path)

    spec = agent_cell_spec(agent)
    assert spec.kind == "agent"
    assert spec.members == ()
    assert HARNESS_ROLE not in {member.role for member in spec.members}
    assert dict(template_cell_params(agent)) == {
        "agent.role": "user",
        "agent.template": "user",
    }


def test_a_scheduled_ensure_runs_off_the_caller_thread(tmp_path: Path) -> None:
    app = application(tmp_path)
    done = threading.Event()
    original = app.agents.create

    def create(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    app.agents.create = create  # type: ignore[method-assign]
    schedule_user_agent_ensure(app, "test")

    assert done.wait(10.0)
    state = app._user_agent_ensure_state
    thread = state["thread"]
    if thread is not None:
        thread.join(10.0)
    assert local_user_agent(app.agents.list(), OWNER) is not None


@pytest.mark.parametrize(
    "method, params",
    [
        ("agent.create", {"name": USER_AGENT_NAME}),
        ("agent.create", {"name": "plain", "capabilities": {"template": "user"}}),
    ],
)
def test_public_creation_cannot_take_the_name_or_the_marker(
    tmp_path: Path, method: str, params: dict[str, object]
) -> None:
    app = application(tmp_path)

    with pytest.raises(DaemonRequestError) as raised:
        app.handle(method, params)
    assert raised.value.code == "INVALID_ARGUMENT"
    assert "user agent" in str(raised.value)
    assert app.agents.list() == ()


def test_start_and_attach_share_the_name_gate(tmp_path: Path) -> None:
    app = application(tmp_path)

    with pytest.raises(DaemonRequestError) as raised:
        require_agent_name_available(app, USER_AGENT_NAME)
    assert raised.value.code == "INVALID_ARGUMENT"
    assert "user agent" in str(raised.value)


def _start_attempts(uri: str) -> list[tuple[str, dict[str, object]]]:
    attempts: list[tuple[str, dict[str, object]]] = []
    for name in (USER_AGENT_NAME, uri):
        attempts += [
            ("agent.start.preflight", {"name": name, "harness": "codex"}),
            ("agent.create", {"name": name, "existing": "reuse", "harness": "codex"}),
            ("agent.start", {"name": name, "harness": "codex"}),
            ("lifecycle.start", {"provider": "codex", "name": name, "headless": True,
                                 "cwd": "/work"}),
            ("agent.attach", {"name": name}),
        ]
    return attempts


def test_no_entrance_binds_a_harness_to_the_user_agent(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    for method, params in _start_attempts(agent.uri):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(method, params)
        assert raised.value.code in {
            "INVALID_ARGUMENT", "INVALID_AGENT_NAME", "AGENT_EXISTS", "USER_AGENT_IMMUTABLE",
        }, (
            method, params, raised.value.code, str(raised.value),
        )

    assert app.desired_state.load().harnesses == ()
    assert app._harnesses.running == {}
    current = app.agents.get(USER_AGENT_NAME)
    assert (current.preferred_harness, current.last_harness) == (None, None)
    assert dict(current.capabilities) == {"template": "user"}


def test_the_user_agent_cannot_be_renamed_and_its_name_cannot_be_taken(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})

    for params, code in (
        ({"name": USER_AGENT_NAME, "displayName": "me"}, "USER_AGENT_IMMUTABLE"),
        ({"name": agent.uri, "displayName": "me"}, "USER_AGENT_IMMUTABLE"),
        ({"name": "plain", "displayName": USER_AGENT_NAME}, "INVALID_ARGUMENT"),
    ):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle("agent.rename", params)
        assert raised.value.code == code
        assert "user agent" in str(raised.value)
    assert "user agent" in str(raised.value)
    assert app.agents.get(USER_AGENT_NAME).display == USER_AGENT_NAME


def test_the_user_agent_cannot_be_destroyed(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    with pytest.raises(DaemonRequestError) as raised:
        app.handle("agent.destroy.preview", {"name": USER_AGENT_NAME})
    assert raised.value.code == "USER_AGENT_IMMUTABLE"
    assert "user agent" in str(raised.value)
    assert app.agents.get(agent.uri) is not None


def test_the_restore_gate_schedules_the_ensure_after_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    events: list[str] = []
    for name in (
        "_restore_adapters",
        "_restore_harnesses",
        "_restore_provider_auth",
        "_start_maintenance_scheduler",
        "_start_held_reminder",
    ):
        monkeypatch.setattr(app, name, lambda: None)
    log = app._log

    def record(level, component, event, **fields):  # type: ignore[no-untyped-def]
        events.append(event)
        return log(level, component, event, **fields)

    monkeypatch.setattr(app, "_log", record)

    app._restore_then_open_gate(lambda operation, _phase: operation())

    thread = app._user_agent_ensure_state["thread"]
    if thread is not None:
        thread.join(10.0)
    assert events.index("daemon.ready") < events.index("user_agent.created")
    assert local_user_agent(app.agents.list(), OWNER) is not None


def test_agent_create_cannot_choose_the_entity_token(tmp_path: Path) -> None:
    app = application(tmp_path)
    chosen = "0123456789abcdef0123456789abcdef"

    try:
        app.handle("agent.create", {"name": "plain", "entityToken": chosen})
    except DaemonRequestError as error:
        assert error.code == "INVALID_ARGUMENT"
        assert app.agents.get("plain") is None
        return
    created = app.agents.get("plain")
    assert created is not None and created.agent_id != agent_id_for_entity_token(chosen)


def _admin_attempts(target: str) -> list[tuple[str, dict[str, object]]]:
    return [
        ("agent.start", {"name": target, "harness": "codex"}),
        ("agent.stop", {"name": target}),
        ("agent.rename", {"name": target, "displayName": "me"}),
        ("agent.destroy.preview", {"name": target}),
        ("agent.attach", {"name": target}),
        ("agent.detach", {"name": target}),
        ("agent.harness-args.set", {"name": target, "harness": "codex", "args": ["--x"],
                                    "expectedHarnessArgs": {}}),
        ("agent.harness-args.clear", {"name": target, "harness": "codex",
                                      "expectedHarnessArgs": {}}),
        ("agent.provider.login", {"actor": target, "provider": "anthropic"}),
        ("agent.provider.logout", {"actor": target}),
        ("agent.secret.grant", {"actor": target, "grantId": "g-1", "source": "agent-private",
                                "entryId": "e-1", "fieldName": "f",
                                "environmentNames": ["X"], "revision": 1}),
    ]


@pytest.mark.parametrize("by", ["name", "uri"])
def test_every_admin_method_refuses_the_user_agent_with_one_code(tmp_path: Path, by: str) -> None:
    app, agent = _ensured(tmp_path)
    target = USER_AGENT_NAME if by == "name" else agent.uri

    for method, params in _admin_attempts(target):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(method, params)
        assert raised.value.code == "USER_AGENT_IMMUTABLE", (method, raised.value.code, str(raised.value))
        assert "user agent" in str(raised.value), (method, str(raised.value))

    assert app.desired_state.load().harnesses == ()
    current = app.agents.get(USER_AGENT_NAME)
    assert current is not None and current.display == USER_AGENT_NAME
    assert dict(current.capabilities) == {"template": "user"}
