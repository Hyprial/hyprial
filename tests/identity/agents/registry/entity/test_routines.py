"""agent_entity: routines scenarios."""

from __future__ import annotations
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.dispatch.matrix import meets_tier
from hyprial.daemon.impl.harnesses.capabilities import declare as _declare
from hyprial.identity.impl.agents.runtime.capabilities import runtime_capabilities
from hyprial.kernel import tier_for_model

from .support import (
    URI,
    _close_test_applications as _close_test_applications,
    _scheduled_routine,
    application,
    enable_routine_coordinator,
    enable_routines,
    register_interactive,
)


def test_new_agent_gets_one_default_routine_and_reuse_does_not_duplicate(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)

    created = app.handle(
        "agent.create",
        {
            "name": "foo",
            "capabilities": {"interactive": True},
        },
    )
    first = app.handle("routine.list", {"actor": "user:op"})["routines"]
    reused = app.handle("agent.create", {"name": "foo", "existing": "reuse"})
    second = app.handle("routine.list", {"actor": "user:op"})["routines"]

    assert created["created"] is True
    assert reused["created"] is False
    assert len(first) == len(second) == 1
    assert first[0]["actor"] == URI
    assert first[0]["actorOwnership"] == "borrowed"
    assert first[0]["mode"] == "scheduled"
    assert first[0]["role"] == "dispatch"
    assert first[0]["nextDueMs"] > 0
    listed = app.handle("agent.list", {})
    assert listed["agentsWithoutRoutine"] == []

    assert app._routine_service is not None
    app._routine_service.close()
    app._routine_service = None
    restarted = application(tmp_path)
    enable_routines(restarted, tmp_path)
    assert len(
        restarted.handle("routine.list", {"actor": "user:op"})["routines"]
    ) == 1


def test_pac_lifecycle_agent_creation_seam_gets_no_default_routine(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)

    worker = app.agents.create("wf-example-worker")

    assert worker.actor == "wf-example-worker"
    assert app.handle("routine.list", {"actor": "user:op"})["routines"] == []


def test_host_invited_resident_gets_default_routine(tmp_path: Path) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)

    created = app.handle(
        "agent.host-invite",
        {"name": "visitor", "owner": "remote-owner"},
    )
    routines = app.handle("routine.list", {"actor": "user:op"})["routines"]

    assert created["created"] is True
    assert len(routines) == 1
    assert routines[0]["actor"] == "agent:remote-owner:node-1:visitor"


def test_agent_create_rolls_back_when_default_routine_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.biz.impl.routine.execution.service import RoutineServiceError

    app = application(tmp_path)
    enable_routines(app, tmp_path)

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RoutineServiceError("SYNTHETIC_ROUTINE_FAILURE", "synthetic failure")

    assert app._routine_service is not None
    monkeypatch.setattr(app._routine_service.service, "add", fail)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.create", {"name": "foo"})

    assert failure.value.code == "AGENT_DEFAULT_ROUTINE_FAILED"
    assert "SYNTHETIC_ROUTINE_FAILURE" in str(failure.value)
    assert app.agents.get("foo") is None


def test_agent_create_reports_missing_default_when_routines_are_unavailable(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)

    created = app.handle("agent.create", {"name": "foo"})
    listed = app.handle("agent.list", {})

    assert created["created"] is True
    assert created["routineWarning"]["code"] == "AGENT_DEFAULT_ROUTINE_UNAVAILABLE"
    assert listed["agentsWithoutRoutine"] == [URI]


def test_last_routine_cannot_be_removed_but_one_of_two_can(tmp_path: Path) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    default = app.handle("routine.list", {"actor": "user:op"})["routines"][0]["name"]

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("routine.remove", {"name": default, "actor": "user:op"})
    assert failure.value.code == "ROUTINE_LAST_BINDING"
    assert failure.value.data == {"routine": default, "actor": URI}
    assert "hyprial routine add" in str(failure.value)
    assert "hyprial routine set" in str(failure.value)

    app.handle(
        "routine.add",
        {
            "yaml": _scheduled_routine("replacement", URI),
            "actor": "user:op",
        },
    )
    removed = app.handle(
        "routine.remove", {"name": default, "actor": "user:op"}
    )
    assert removed["removed"] is True
    assert [
        item["name"]
        for item in app.handle("routine.list", {"actor": "user:op"})["routines"]
    ] == ["replacement"]


def test_last_routine_rejection_data_survives_typed_coordinator(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    name = app.handle("routine.list", {"actor": "user:op"})["routines"][0]["name"]
    enable_routine_coordinator(app, tmp_path)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "routine.remove",
            {"name": name, "actor": "user:op", "operationId": "last-binding-data"},
        )
    assert failure.value.code == "ROUTINE_LAST_BINDING"
    assert failure.value.data == {"routine": name, "actor": URI}
    assert "hyprial routine add" in str(failure.value)


def test_routine_set_keeps_name_and_binding_and_is_atomic_on_failure(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    before = app.handle("routine.list", {"actor": "user:op"})["routines"][0]
    name = before["name"]

    changed = app.handle(
        "routine.set",
        {
            "name": name,
            "yaml": _scheduled_routine(
                name, URI, interval="30m", task="new scheduled task"
            ),
            "actor": "user:op",
        },
    )
    after = app.handle("routine.status", {"name": name, "actor": "user:op"})
    assert changed["name"] == name
    assert after["actor"] == URI
    assert after["actorOwnership"] == "borrowed"
    from hyprial.biz.impl.routine.storage.store import RoutineStore
    import yaml

    store = RoutineStore(tmp_path / "routines.sqlite3")
    try:
        snapshot = store.snapshot(name)
        assert snapshot is not None
        document = yaml.safe_load(snapshot.routine.yaml_text)
    finally:
        store.close()
    assert document["schedule"] == {"interval": "30m"}
    assert document["task"] == "new scheduled task"

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "routine.set",
            {
                "name": name,
                "yaml": _scheduled_routine(name, URI, interval="1s", task="bad"),
                "actor": "user:op",
            },
        )
    assert failure.value.code == "ROUTINE_SCHEMA_ERROR"
    assert app.handle("routine.status", {"name": name, "actor": "user:op"}) == after


def test_agent_create_for_start_refuses_before_a_tui_is_spawned(
    tmp_path: Path,
) -> None:
    """`hyprial agent start` calls this first, so the refusal costs nothing to recover.

    The error arrives before any Claude process exists -- and it is an
    AGENT_ALREADY_RUNNING, not an AGENT_EXISTS: the name is fine, the agent is
    simply busy.
    """

    app = application(tmp_path)
    register_interactive(app, URI, session_ref="claude-session-7")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "agent.create",
            {
                "name": "foo",
                "existing": "reuse",
                "harness": "pi",
                "runtime": "headless",
            },
        )

    assert failure.value.code == "AGENT_ALREADY_RUNNING"


def test_agent_create_for_start_returns_the_handover_once_nothing_is_running(
    tmp_path: Path,
) -> None:
    """After the session ends, the same agent moves and A9 comes with it."""

    app = application(tmp_path)
    register_interactive(app, URI, session_ref="claude-session-7")
    app.handle("session.unregister", {"actor": URI, "sessionRef": "claude-session-7"})

    result = app.handle(
        "agent.create",
        {"name": "foo", "existing": "reuse", "harness": "pi", "runtime": "headless"},
    )

    assert result["created"] is False
    assert result["harnessHandover"]["previousHarness"] == "claude"
    assert result["harnessHandover"]["previousSessionId"] == "claude-session-7"





@pytest.mark.parametrize("model,tier", [
    ("gpt-5.6-luna", "fast"), ("gpt-5.6-sol", "strong"), ("k3", "strong"),
    ("fable", "super"), ("gpt-6-astra", "super"),
    ("unknown-model", None), (None, None),
])
def test_capmx_highest_tier_and_unknown(model, tier):
    assert tier_for_model(model) == tier
    assert meets_tier("super", "strong")
    assert not meets_tier("fast", "strong")
    assert not meets_tier(None, "fast")


def test_capmx_headless_capabilities_and_json_list(tmp_path):
    app = application(tmp_path)
    app.handle("lifecycle.start", {
        "provider": "pi", "name": "foo", "headless": True, "cwd": "/work",
        "args": ["--provider", "kimi-coding", "--model", "k3"],
    })
    caps = app.agents.require("foo").capabilities
    assert caps == {
        **caps, "interactive": False, "harness": "pi", "provider": "kimi-coding",
        "model": "k3", "tier": "strong", "proactive_send": True, "tool_injection": True,
    }
    assert isinstance(caps["updated_at_ms"], int)
    assert app.handle("agent.list", {})["agents"][0]["capabilities"] == caps


def test_agent_list_reports_shared_credential_divergence(tmp_path: Path) -> None:
    app = application(tmp_path)
    personality = tmp_path / "personality"
    native = personality / "native" / "codex"
    native.mkdir(parents=True)
    (native / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n', encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "shared-auth"
    target.write_text("SYNTHETIC_SHARED_AUTH_NOT_A_SECRET\n", encoding="utf-8")
    target.chmod(0o600)
    app.agents.create(
        "shared-worker",
        cwd=str(work),
        config={
            "sources": [{"path": str(personality), "required": True}],
            "discovery": "explicit-only",
            "sharedCredentials": {"codex": str(target)},
        },
    )
    receipt = app._agent_registry.ensure_home("shared-worker")
    credential = Path(receipt.path) / "secrets" / "native" / "codex" / "auth.json"
    credential.parent.mkdir(parents=True, mode=0o700)
    credential.parent.parent.chmod(0o700)
    credential.parent.chmod(0o700)
    credential.symlink_to(target)

    ready = app.handle("agent.list", {})["agents"][0]["sharedCredentials"][0]
    assert ready["status"] == "ready"
    assert ready["authMode"] == "native-shared-link"

    credential.unlink()
    credential.write_text("SYNTHETIC_DIVERGED_COPY\n", encoding="utf-8")
    credential.chmod(0o600)
    diverged = app.handle("agent.list", {})["agents"][0]["sharedCredentials"][0]
    assert diverged["status"] == "error"
    assert diverged["code"] == "SHARED_CREDENTIAL_DIVERGED"


@pytest.mark.parametrize("tmux", [False, True], ids=["mcp-interactive", "tmux"])
def test_capmx_interactive_registration_refreshes_capabilities(tmp_path, tmux):
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    assert app.agents.require("foo").capabilities == {}  # old rows remain unknown
    params = {
        "actor": URI, "sessionRef": "capmx-session", "cwd": "/work",
        "command": ["claude", "--model", "fable"],
        "source": "claude-channel", "runtime": "claude_interactive",
        **({"tmuxSession": "capmx-tmux"} if tmux else {}),
    }
    app.handle("session.register", params)
    caps = app.agents.require("foo").capabilities
    assert caps["interactive"] is True
    assert caps["tier"] == "super"
    assert caps["harness"] == "claude"
    assert caps["tool_injection"] is True
    app.handle("session.register", {**params, "command": ["claude", "--model", "sonnet"]})
    updated = app.agents.require("foo").capabilities
    assert updated["tier"] == "fast"
    assert updated["updated_at_ms"] >= caps["updated_at_ms"]


def test_capmx_codex_interactive_sends_and_injects_natively():
    # WP5-B (rulings 57/65/77): interactive codex mounts the harness-bridge
    # MCP under on-request approvals, so proactive send and tool injection
    # are native rather than unsupported.
    caps = runtime_capabilities(
        "codex", interactive=True, model="gpt-5.6-sol", declare=_declare
    )
    assert caps["proactive_send"] is True
    assert caps["tool_injection"] is True


def test_capmx_unknown_session_harness_preserves_unknown_support():
    caps = runtime_capabilities("protocol-negative", interactive=True, declare=_declare)
    assert caps["interactive"] is True
    assert caps["harness"] == "protocol-negative"
    assert caps["tier"] is None
    assert caps["proactive_send"] is None
    assert caps["tool_injection"] is None


def test_capmx_application_supplies_native_capability_declarer(tmp_path, monkeypatch):
    from hyprial.daemon.impl.harnesses import capabilities as capability_provider

    native_declare = capability_provider.declare
    calls = []

    def record_declare(harness, *, headless):
        calls.append((harness, headless))
        return native_declare(harness, headless=headless)

    monkeypatch.setattr(capability_provider, "declare", record_declare)
    app = application(tmp_path)
    agent = app._ensure_agent(
        URI, harness="pi", interactive=False,
        args=("--provider", "kimi-coding", "--model", "k3"),
    )

    assert calls == [("pi", True)]
    assert agent is not None
    assert agent.capabilities["provider"] == "kimi-coding"
    assert agent.capabilities["model"] == "k3"
    assert agent.capabilities["tier"] == "strong"
    assert agent.capabilities["proactive_send"] is True
    assert agent.capabilities["tool_injection"] is True


def test_capmx_application_declaration_failure_does_not_create_identity(tmp_path, monkeypatch):
    from hyprial.daemon.impl.harnesses import capabilities as capability_provider

    error = RuntimeError("synthetic capability declaration failure")

    def fail_declare(harness, *, headless):
        raise error

    monkeypatch.setattr(capability_provider, "declare", fail_declare)
    app = application(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic capability declaration failure") as failure:
        app._ensure_agent(URI, harness="pi", interactive=False)

    assert failure.value is error
    assert app.agents.get("foo") is None


def test_capmx_capabilities_are_json_not_stringified(tmp_path):
    app = application(tmp_path)
    caps = {"interactive": False, "tier": None, "updated_at_ms": 12,
            "nested": {"tools": ["send", True]}}
    app.handle("agent.create", {"name": "foo", "capabilities": caps})
    assert app.agents.require("foo").capabilities == caps
    from hyprial.identity.impl.agents import normalize_capabilities, AgentError
    for invalid in ({"bad": object()}, {"bad": float("nan")}, {1: "bad"}):
        with pytest.raises(AgentError):
            normalize_capabilities(invalid)
