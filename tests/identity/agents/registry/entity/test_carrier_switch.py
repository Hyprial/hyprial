"""Carrier liveness, reap and interactive-to-headless switch contracts."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.identity.impl.agents import (
    Agent,
    AgentLiveness,
    AgentRegistry,
    local_actors,
    set_default_registry,
    verify_fetch_claim,
)
from hyprial.kernel import HarnessLaunchSpec, PortAdmission

from .support import (
    NODE,
    OWNER,
    URI,
    _close_test_applications as _close_test_applications,
    application,
    register_interactive,
    start_headless,
)


def test_a_live_binding_is_reported_and_a_dead_one_is_not(tmp_path: Path) -> None:
    """The refusal reads live_binding; a dead connector must never block."""

    liveness = AgentLiveness(node_id=NODE, clock=lambda: 0.0)
    assert liveness.live_binding(URI) is None

    liveness.bind(URI, harness="claude", runtime="interactive", session_id="s1")
    liveness.touch(URI)
    live = liveness.live_binding(URI)
    assert live is not None
    assert live.harness == "claude"

    # Interactive with a heartbeat past its TTL is offline; a process fence
    # is only a veto, not a positive signal.
    liveness = AgentLiveness(node_id=NODE, clock=lambda: 0.0)
    liveness.bind(URI, harness="claude", runtime="interactive", session_id="s1")
    assert liveness.verdict(URI) is False
    assert liveness.live_binding(URI) is None


def test_an_interactive_binding_whose_process_is_dead_is_not_live() -> None:
    """A process-dead interactive carrier is offline even while its heartbeat
    is still fresh; process existence is a veto, never positive evidence."""

    liveness = AgentLiveness(
        node_id=NODE,
        clock=lambda: 0.0,
        interactive_process_alive=lambda _actor: False,
    )
    liveness.bind(URI, harness="pi", runtime="interactive", session_id="s1")
    liveness.touch(URI)

    assert liveness.verdict(URI) is False
    assert liveness.live_binding(URI) is None


def test_a_dead_interactive_carrier_does_not_block_start(
    tmp_path: Path,
) -> None:
    """A registered interactive session whose process is gone is not online,
    and its stale binding must not refuse the headless start (card 706)."""

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "dead-session",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 999999,
            "processIdentity": "0",
            "channelConfirmed": True,
        },
    )

    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "offline"
    started = start_headless(app, "pi", "foo")
    assert started["actor"] == URI
    # The new headless worker displaces the dead interactive carrier: it is
    # not merely marked offline, its stale session record is reaped too.
    assert app.handle("ps", {})["interactiveSessions"] == []


def test_replace_interactive_switches_the_same_actor_to_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The formal switch path stops the interactive connector first, then
    starts the same actor headless; it never kills the CLI parent.
    """

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    state = {"value": "alive"}
    kills: list[tuple[int, int]] = []
    old_status = ownership._owner_process_status
    old_kill = ownership.os.kill
    try:
        ownership._owner_process_status = lambda *_a, **_k: _Status(state["value"])  # type: ignore[assignment]
        def record_kill(pid, sig):
            kills.append((pid, sig))
            state["value"] = "pid-missing"

        ownership.os.kill = record_kill  # type: ignore[assignment]
        monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
        app._harnesses.wait_ready = lambda *_args, **_kwargs: True  # type: ignore[attr-defined]
        app._harnesses.session_refs = lambda: {("pi", "foo"): "resume-sid"}  # type: ignore[attr-defined]
        started = app.handle(
        "lifecycle.start",
            {
                "provider": "pi",
                "name": "foo",
                "headless": True,
                "cwd": "/work",
                "sessionRef": "resume-sid",
                "replaceInteractive": True,
            },
        )
    finally:
        ownership._owner_process_status = old_status
        ownership.os.kill = old_kill

    assert kills == [(424242, 15)]
    assert started["actor"] == URI
    assert started["sessionRef"] == "resume-sid"
    assert app._agent_liveness.binding(URI).runtime == "headless"  # type: ignore[union-attr]
    assert app.handle("ps", {})["interactiveSessions"] == []


def test_replace_interactive_requires_a_resume_ref(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    register_interactive(app, URI)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "lifecycle.start",
            {
                "provider": "pi",
                "name": "foo",
                "headless": True,
                "cwd": "/work",
                "replaceInteractive": True,
            },
        )
    assert raised.value.code == "INVALID_ARGUMENT"


def test_process_alive_does_not_override_a_stale_heartbeat() -> None:
    """706's process probe is a veto, not a positive liveness signal (D1)."""

    clock = {"now": 100.0}
    liveness = AgentLiveness(
        node_id=NODE,
        clock=lambda: clock["now"],
        interactive_process_alive=lambda _actor: True,
    )
    liveness.bind(URI, harness="pi", runtime="interactive", session_id="s1")
    liveness.touch(URI)
    clock["now"] += 1000.0

    assert liveness.verdict(URI) is False


def test_displacement_reaps_the_dead_interactive_session_directly(
    tmp_path: Path,
) -> None:
    """Displacement must clear the session row even before any ps read."""

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "dead-session",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 999999,
            "processIdentity": "0",
            "channelConfirmed": True,
        },
    )
    start_headless(app, "pi", "foo")
    assert app._agent_session_domains.session.read_sessions() == ()


def test_switch_failure_leaves_no_headless_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel import DaemonRequestError

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    state = {"value": "alive"}
    monkeypatch.setattr(
        ownership, "_owner_process_status", lambda *_a, **_k: _Status(state["value"])
    )

    def record_kill(_pid, _sig):
        state["value"] = "pid-missing"

    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)

    def fail(*_args, **_kwargs):
        app.desired_state.upsert_harness(
            HarnessLaunchSpec("pi", "foo", True, session_ref="resume-sid")
        )
        raise DaemonRequestError("STRICT_RESUME_FAILED", "headless failed")

    removed: list[tuple[str, str]] = []
    real_remove = app.desired_state.remove_harness

    def record_remove(harness, name):
        removed.append((harness, name))
        return real_remove(harness, name)

    monkeypatch.setattr(app.desired_state, "remove_harness", record_remove)
    monkeypatch.setattr(app, "_run_lifecycle_operation", fail)
    with pytest.raises(DaemonRequestError):
        app.handle(
            "lifecycle.start",
            {
                "provider": "pi",
                "name": "foo",
                "headless": True,
                "cwd": "/work",
                "sessionRef": "resume-sid",
                "replaceInteractive": True,
            },
        )
    binding = app._agent_liveness.binding(URI)
    assert binding is None
    assert ("pi", "foo") in removed


def test_switch_refusal_preserves_existing_headless_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    register_interactive(app, URI, session_ref="interactive-sid")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("pi", "foo", True, session_ref="old-headless")
    )
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "lifecycle.start",
            {
                "provider": "pi",
                "name": "foo",
                "headless": True,
                "cwd": "/work",
                "sessionRef": "resume-sid",
                "replaceInteractive": True,
            },
        )
    assert raised.value.code == "INTERACTIVE_CARRIER_STOP_FAILED"
    specs = app.desired_state.load().harnesses
    assert [(spec.harness, spec.name, spec.session_ref) for spec in specs] == [
        ("pi", "foo", "old-headless")
    ]
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


def test_custody_refusal_happens_before_the_carrier_is_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    monkeypatch.setattr(
        app,
        "_hydrate_transfer_source",
        lambda *_args, **_kwargs: SimpleNamespace(blocks_start=True, state="held"),
    )
    class _Status:
        value = "alive"

    kills: list[tuple[int, int]] = []
    old_status = ownership._owner_process_status
    old_kill = ownership.os.kill
    ownership._owner_process_status = lambda *_a, **_k: _Status()  # type: ignore[assignment]
    ownership.os.kill = lambda pid, sig: kills.append((pid, sig))  # type: ignore[assignment]
    try:
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(
                "lifecycle.start",
                {
                    "provider": "pi",
                    "name": "foo",
                    "headless": True,
                    "cwd": "/work",
                    "sessionRef": "resume-sid",
                    "replaceInteractive": True,
                },
            )
    finally:
        ownership._owner_process_status = old_status
        ownership.os.kill = old_kill
    assert raised.value.code == "TRANSFER_STARTS_HELD"
    assert kills == []
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


def test_verify_failure_compensates_the_headless_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    state = {"value": "alive"}
    old_status = ownership._owner_process_status
    old_kill = ownership.os.kill
    ownership._owner_process_status = lambda *_a, **_k: _Status(state["value"])  # type: ignore[assignment]
    def record_kill(_pid, _sig):
        state["value"] = "pid-missing"
    ownership.os.kill = record_kill  # type: ignore[assignment]
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    monkeypatch.setattr(app, "_verify_started_resume", lambda *_a, **_k: (_ for _ in ()).throw(DaemonRequestError("STRICT_RESUME_FAILED", "not resumed")))
    removed: list[tuple[str, str]] = []
    real_remove = app.desired_state.remove_harness
    monkeypatch.setattr(app.desired_state, "remove_harness", lambda h, n: (removed.append((h, n)), real_remove(h, n))[1])
    try:
        with pytest.raises(DaemonRequestError):
            app.handle(
                "lifecycle.start",
                {
                    "provider": "pi",
                    "name": "foo",
                    "headless": True,
                    "cwd": "/work",
                    "sessionRef": "resume-sid",
                    "replaceInteractive": True,
                },
            )
    finally:
        ownership._owner_process_status = old_status
        ownership.os.kill = old_kill
    assert ("pi", "foo") in removed
    binding = app._agent_liveness.binding(URI)
    assert binding is None


def test_switch_poll_unknown_is_not_treated_as_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    killed = {"value": False}
    kills: list[tuple[int, int]] = []
    def status(*_a, **_k):
        return _Status("unknown" if killed["value"] else "alive")
    def record_kill(pid, sig):
        kills.append((pid, sig))
        killed["value"] = True
    from hyprial.daemon.impl.application.actors.agents_admin import destroys

    monkeypatch.setattr(ownership, "_owner_process_status", status)
    monkeypatch.setattr(destroys.os, "kill", record_kill)
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    try:
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(
                "lifecycle.start",
                {
                    "provider": "pi",
                    "name": "foo",
                    "headless": True,
                    "cwd": "/work",
                    "sessionRef": "resume-sid",
                    "replaceInteractive": True,
                },
            )
    finally:
        pass
    assert kills == [(424242, 15)]
    assert raised.value.code == "INTERACTIVE_CARRIER_STOP_FAILED"
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


def test_switch_without_a_process_fence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    register_interactive(app, URI, session_ref="interactive-sid")
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "lifecycle.start",
            {
                "provider": "pi",
                "name": "foo",
                "headless": True,
                "cwd": "/work",
                "sessionRef": "resume-sid",
                "replaceInteractive": True,
            },
        )
    assert raised.value.code == "INTERACTIVE_CARRIER_STOP_FAILED"
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


def test_switch_timeout_refuses_and_keeps_the_interactive_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    class _Status:
        value = "alive"

    old_status = ownership._owner_process_status
    old_kill = ownership.os.kill
    ownership._owner_process_status = lambda *_a, **_k: _Status()  # type: ignore[assignment]
    ownership.os.kill = lambda *_a, **_k: None  # type: ignore[assignment]
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    try:
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(
                "lifecycle.start",
                {
                    "provider": "pi",
                    "name": "foo",
                    "headless": True,
                    "cwd": "/work",
                    "sessionRef": "resume-sid",
                    "replaceInteractive": True,
                },
            )
    finally:
        ownership._owner_process_status = old_status
        ownership.os.kill = old_kill
    assert raised.value.code == "INTERACTIVE_CARRIER_STOP_FAILED"
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


def test_interactive_carrier_is_signalled_and_waited() -> None:
    from types import SimpleNamespace

    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    app = application(Path("/tmp"))
    calls: list[tuple[int, int]] = []
    statuses = iter(("alive", "pid-missing"))

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def monkeypatch_status(*_args, **_kwargs):
        return _Status(next(statuses))

    old_status = ownership._owner_process_status
    old_kill = ownership.os.kill
    try:
        ownership._owner_process_status = monkeypatch_status  # type: ignore[assignment]
        ownership.os.kill = lambda pid, sig: calls.append((pid, sig))  # type: ignore[assignment]
        app._terminate_interactive_carrier(
            SimpleNamespace(actor=URI, process_pid=123, process_identity="x")
        )
    finally:
        ownership._owner_process_status = old_status
        ownership.os.kill = old_kill
    assert calls == [(123, 15)]


def test_ps_is_observational_for_a_dead_interactive_carrier(tmp_path: Path) -> None:
    """Protocol-v4 status reads report projections without running cleanup."""

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "dead-session",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 999999,
            "processIdentity": "0",
            "channelConfirmed": True,
        },
    )

    status = app.handle("ps", {})
    assert [row["sessionRef"] for row in status["interactiveSessions"]] == [
        "dead-session"
    ]


def test_ps_reaps_a_dead_interactive_carrier(tmp_path: Path) -> None:
    """Maintenance retires a session whose PID/birth fence is dead, so a later
    headless worker is not shown beside a ghost interactive carrier."""

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "dead-session",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 999999,
            "processIdentity": "0",
            "channelConfirmed": True,
        },
    )

    # Status reads are projection-only (f4a9e5f16); dead-carrier cleanup is owned
    # by the maintenance tick (messaging/maintenance.py), so run that step here.
    app._clean_dead_interactive_sessions()
    status = app.handle("ps", {})
    assert status["interactiveSessions"] == []


def test_a_managed_start_preserves_the_learned_session_ref(tmp_path: Path) -> None:
    """Lifecycle start replaces the spec, but must not clear a learned
    sessionRef unless the caller explicitly supplies a new one (card 708)."""

    app = application(tmp_path)
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("pi", "foo", True, session_ref="learned-sid")
    )

    start_headless(app, "pi", "foo")

    # Two managed restarts: the process may be gone while the desired spec and
    # its learned ref remain; the next lifecycle.start must carry it forward.
    app._agent_liveness.release(URI)
    start_headless(app, "pi", "foo")

    specs = app.desired_state.load().harnesses
    assert [spec.session_ref for spec in specs if spec.name == "foo"] == [
        "learned-sid"
    ]


# --------------------------------------------------------------------------
# A9 -- the harness handover is announced, never silent
# --------------------------------------------------------------------------


def test_swapping_harness_announces_the_previous_harness_and_session(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    register_interactive(app, URI, session_ref="claude-session-7")
    # The supported move: stop the current connector, then start the new one.
    app.handle("session.unregister", {"actor": URI, "sessionRef": "claude-session-7"})

    result = start_headless(app, "pi", "foo")

    handover = result["harnessHandover"]
    assert handover["previousHarness"] == "claude"
    assert handover["previousSessionId"] == "claude-session-7"
    assert handover["nextHarness"] == "pi"
    assert handover["contextCarriedOver"] is False
    # The notice must carry both clues a user needs to resume by hand.
    assert "claude" in handover["notice"]
    assert "claude-session-7" in handover["notice"]


def test_no_handover_notice_when_the_harness_did_not_change(tmp_path: Path) -> None:
    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    app.handle("down", {"target": "foo"})

    result = start_headless(app, "pi", "foo")

    assert "harnessHandover" not in result


def test_the_last_session_clue_is_persisted_on_the_agent(tmp_path: Path) -> None:
    app = application(tmp_path)

    register_interactive(app, URI, session_ref="claude-session-7")

    agent = app.agents.require("foo")
    assert agent.last_harness == "claude"
    assert agent.last_session_id == "claude-session-7"
    # It survives a reload from disk, because that is the point of persisting.
    reloaded = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    assert reloaded.require("foo").last_session_id == "claude-session-7"


# --------------------------------------------------------------------------
# §5.2 -- the seam the delivery line consumes
# --------------------------------------------------------------------------


def test_local_actors_lists_every_actor_this_machine_speaks_for(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    registry.create("bar")
    set_default_registry(registry)
    try:
        assert local_actors() == (
            f"agent:{OWNER}:{NODE}:bar",
            URI,
        )
    finally:
        set_default_registry(None)


def test_verify_fetch_claim_rejects_anything_but_a_local_agent(
    tmp_path: Path,
) -> None:
    """Half of G5 is answerable today; the crypto half is not, and says so.

    No Ed25519 key material exists anywhere in the Python daemon, so this
    checks what it can: the claim names an agent this machine owns and carries
    a claim at all. It is deliberately not a signature check.
    """

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    set_default_registry(registry)
    try:
        assert verify_fetch_claim(URI, b"sig", b"nonce") is True
        assert verify_fetch_claim("foo", b"sig", b"nonce") is True
        assert verify_fetch_claim("stranger", b"sig", b"nonce") is False
        assert verify_fetch_claim("agent:her:other-host:foo", b"sig", b"nonce") is False
        assert verify_fetch_claim(URI, b"", b"nonce") is False
        assert verify_fetch_claim(URI, b"sig", b"") is False
    finally:
        set_default_registry(None)


def test_the_seam_answers_honestly_before_a_daemon_installs_a_registry() -> None:
    set_default_registry(None)

    assert local_actors() == ()
    assert verify_fetch_claim(URI, b"sig", b"nonce") is False


# --------------------------------------------------------------------------
# restart
# --------------------------------------------------------------------------


def test_bindings_are_reseeded_so_a_restart_still_refuses(tmp_path: Path) -> None:
    """Bindings are per daemon generation; without reseeding the guard reopens.

    A restarted daemon that has forgotten `foo` is on pi would accept an
    interactive registration beside the still-running pi connector, putting two
    connectors back on one URI.
    """

    first = application(tmp_path)
    start_headless(first, "pi", "foo")

    second = application(tmp_path)
    second._harnesses.running[("pi", "foo")] = True  # type: ignore[union-attr]
    second._seed_agent_bindings()
    assert second._agent_liveness.binding(URI).harness == "pi"  # type: ignore[union-attr]

    with pytest.raises(DaemonRequestError) as failure:
        register_interactive(second, URI)

    assert failure.value.code == "AGENT_ALREADY_RUNNING"


def test_carrier_heartbeat_keeps_targets_live_without_inbox_polling(tmp_path: Path) -> None:
    app = application(tmp_path)
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]
    session_ref = "codex-thread-work"
    register_interactive(app, URI, session_ref=session_ref, source="codex-app-server", runtime="codex_interactive")
    params = {"actor": URI, "sessionRef": session_ref}

    def status() -> str:
        return app.handle("targets", {"kind": "agent"})["targets"][0]["status"]

    assert status() == "online"
    clock["now"] = 20.0
    assert app.handle("identity.whoami", params)["sessionRegistered"] is True
    assert status() == "offline", "identity lookup must not renew a carrier"
    app.handle("session.heartbeat", params)
    assert status() == "online"
    for _ in range(6):
        clock["now"] += 3.0
        app.handle("session.heartbeat", params)
        assert status() == "online"
    clock["now"] += 20.0
    assert status() == "offline"
    register_interactive(app, URI, session_ref="codex-thread-new", source="codex-app-server", runtime="codex_interactive")
    with pytest.raises(DaemonRequestError) as error:
        app.handle("session.heartbeat", params)
    assert error.value.code == "SESSION_SUPERSEDED"


def test_agent_migration_effect_keeps_mailbox_live_and_close_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted filesystem migration holds its lease through effect ACK."""

    import time
    import hyprial.identity.impl.agents.actor._core as actor_module
    from hyprial.identity.impl.agents.actor._core import AgentActor
    from hyprial.identity.impl.agents.migration.actor import MigrationOutcome, MigrationOwnerCall
    from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand
    from hyprial.kernel.impl.contracts.ports import PortCommandRejected

    registry = AgentRegistry(
        tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE,
        hyprial_home=tmp_path / "home",
    )
    registry.create("foo")
    entered = threading.Event()
    release = threading.Event()
    events: list[object] = []
    writer_threads: list[int] = []
    effect_threads: list[int] = []
    original_update = registry.update

    def record_update(*args: object, **kwargs: object) -> Agent:
        writer_threads.append(threading.get_ident())
        return original_update(*args, **kwargs)

    monkeypatch.setattr(registry, "update", record_update)

    def held_effect(effect: object, owner: object) -> MigrationOutcome:
        assert hasattr(effect, "request") and hasattr(effect, "agent")
        assert hasattr(owner, "migration_owner_call")
        effect_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        result = owner.migration_owner_call(MigrationOwnerCall(
            effect.request.operation_id,
            "migration-test-config",
            "update-config",
            effect.agent.actor,
            effect.agent.entity_token,
            effect.agent.config,
        ))
        assert result.failure is None
        return MigrationOutcome.success({"ok": True})

    monkeypatch.setattr(actor_module, "run_migration_effect", held_effect)
    actor = AgentActor(registry, event_sink=events.append)
    settled: list[object] = []
    waiter = threading.Thread(
        target=lambda: settled.append(
            actor.run_migration("agent.migrate.preflight", "foo")
        )
    )
    waiter.start()
    try:
        assert entered.wait(3)
        started = time.monotonic()
        assert actor.read_agent("foo") is not None
        assert time.monotonic() - started < 0.5
        assert actor.submit(UpdateAgentCommand("migration-busy", "foo")) is PortAdmission.ACCEPTED
        deadline = time.monotonic() + 2
        while not any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "migration-busy"
            for event in events
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "migration-busy"
            and event.code == "AGENT_MIGRATION_BUSY"
            for event in events
        )
        assert registry.require("foo").entity_token == actor.read_agent("foo").entity_token
        assert not actor.drain(0.05).complete
    finally:
        release.set()
        waiter.join(5)
        assert not waiter.is_alive()
        assert actor.drain(3).complete
        registry.close()
    assert settled == [{"ok": True}]
    assert writer_threads and all(thread_id != effect_threads[0] for thread_id in writer_threads)


@pytest.mark.parametrize("change", ["replace", "pin", "online"])
def test_gc_destroy_rechecks_selection_after_reservation(tmp_path, monkeypatch, change):
    """A scan result cannot authorize effects on changed ownership/liveness."""
    from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
    from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation

    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    selected = app.agents.require("foo")
    reserve = app._reserve_agent_destroy
    released = []
    release = app._release_agent_destroy_reservation

    def reserve_then_change(actor):
        if change == "pin":
            app.agents.pin("gc-test-adapter", "foo")
        token = reserve(actor)
        if change == "replace":
            app.agents.destroy("foo", expected_entity_token=selected.entity_token)
            app.handle("agent.create", {"name": "foo"})
        return token

    def observe(_self, actor):
        return RuntimeObservation(change == "online")

    def record_release(token):
        released.append(token)
        return release(token)

    monkeypatch.setattr(app, "_reserve_agent_destroy", reserve_then_change)
    monkeypatch.setattr(app, "_release_agent_destroy_reservation", record_release)
    monkeypatch.setattr(DaemonActorRuntime, "observe", observe)
    # No runtime-stop/route/inbox/home effect may happen before rejection.
    monkeypatch.setattr(app, "_stop_agent_runtime", lambda *_a, **_k: pytest.fail("GC stopped a changed agent"))
    result = app._destroy_agent(
        selected.uri, expected_entity_token=selected.entity_token,
        require_unpinned=True, require_offline=True,
    )
    assert result["destroyed"] is False
    current = app.agents.require("foo")
    assert len(released) == 1
    if change == "replace":
        assert current.entity_token != selected.entity_token
    else:
        assert current.entity_token == selected.entity_token


def test_gc_destroy_exact_eligible_incarnation_succeeds(tmp_path):
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    selected = app.agents.require("foo")
    result = app._destroy_agent(selected.uri, expected_entity_token=selected.entity_token,
                                require_unpinned=True, require_offline=True)
    assert result["destroyed"] is True
    assert app.agents.get("foo") is None


def test_destroy_reservation_rejects_late_adapter_pin_until_release(tmp_path):
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    selected = app.agents.require("foo")
    reservation = app._reserve_agent_destroy(selected.actor)
    try:
        with pytest.raises(Exception, match="destroy is already reserved"):
            app.agents.pin("gc-late-pin", "foo")
        assert app.agents.require("foo").pinned_adapters == ()
    finally:
        app._release_agent_destroy_reservation(reservation.reservation_token)
    app.agents.pin("gc-late-pin", "foo")
    assert "gc-late-pin" in app.agents.require("foo").pinned_adapters

