"""tui_tmux: channel projection scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from pathlib import Path
from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from .support import (
    _FakeInbox,
    _StubPresence,
    _legacy_runtime_context as _legacy_runtime_context,
)


def test_interactive_session_tmux_session_round_trips(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    session = InteractiveSession(
        actor="agent:op:node-1:alice",
        cwd="/work/alice",
        command=("claude",),
        source="claude-channel",
        session_ref="session-1",
        tmux_session="hyprial-agent-op-node-1-alice",
    )
    store.save(DesiredState(interactive_sessions=(session,)))

    reloaded = store.load().interactive_sessions[0]
    assert reloaded.tmux_session == "hyprial-agent-op-node-1-alice"
    assert reloaded.to_json()["tmuxSession"] == "hyprial-agent-op-node-1-alice"


def test_daemon_register_persists_and_ps_surfaces_tmux_session(
    tmp_path: Path,
) -> None:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = _FakeInbox()  # type: ignore[assignment]

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]

    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
            "tmuxSession": "hyprial-agent-op-node-1-alice",
        },
    )

    persisted = app.desired_state.load().interactive_sessions[0]
    assert persisted.tmux_session == "hyprial-agent-op-node-1-alice"
    status = app.handle("ps", {})
    (entry,) = status["interactiveSessions"]
    assert entry["tmuxSession"] == "hyprial-agent-op-node-1-alice"
