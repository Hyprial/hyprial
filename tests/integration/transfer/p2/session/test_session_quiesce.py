"""S2: session-shaped source quiesce (kill -> verify -> deregister) + rollback.

Single machine, no real tmux: the effect's ports are scripted, the journal is
the real P2 SQLite journal (the same ``quiesce`` step the headless effect
uses), and the removal adapter is either faked or a real desired-state store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.p2.actor import AdmissionActor
from hyprial.daemon.impl.transfer.p2.source import session as api


class FakeTmux:
    def __init__(self, *, alive_after_kill: bool = False, running: bool = False) -> None:
        self.alive_after_kill = alive_after_kill
        self.running = running
        self.killed: list[str] = []
        self.checked: list[str] = []

    def kill_session(self, name: str) -> None:
        self.killed.append(name)

    def has_session(self, name: str) -> bool:
        self.checked.append(name)
        return self.alive_after_kill or self.running


class FakeSessions:
    def __init__(self, *, removed: bool = True) -> None:
        self.removed = removed
        self.calls: list[tuple[str, str]] = []

    def remove(self, actor: str, session_ref: str) -> bool:
        self.calls.append((actor, session_ref))
        return self.removed


def _request(actor: str, token: str) -> api.SessionQuiesceRequest:
    return api.SessionQuiesceRequest(
        "move",
        actor,
        token,
        "session-1",
        process_pid=4321,
        process_identity="proc:99",
        tmux_session="hyprial-actor",
    )


def _effect(journal, tmux, sessions, **kwargs) -> api.SessionQuiesceEffect:
    return api.SessionQuiesceEffect(
        journal,
        tmux,
        sessions,
        clock_ms=kwargs.pop("clock_ms", lambda: 4242),
        process_alive=kwargs.pop("process_alive", lambda pid, identity: False),
        **kwargs,
    )


def test_quiesce_kills_verifies_then_deregisters(ledger, binding):
    store, _now, _db = ledger
    store.prepare("move", binding)
    owner = AdmissionActor(store)
    tmux, sessions = FakeTmux(), FakeSessions()
    effect = _effect(owner, tmux, sessions)
    request = _request(binding.source_agent_id, binding.source_entity_token)
    try:
        result = effect(request)

        assert result.state == "quiesced"
        assert result.kill_moment_ms == 4242
        assert result.in_flight_turn == api.IN_FLIGHT_TURN_NOT_PRESERVED
        # The receipt records the kill moment and the interrupted-turn verdict.
        assert result.receipt == "kill=4242;turn=not-preserved"
        # Best-effort kill happened, and existence was re-read (never trusted).
        assert tmux.killed == ["hyprial-actor"]
        assert tmux.checked == ["hyprial-actor"]
        assert sessions.calls == [(binding.source_agent_id, "session-1")]
        state = store.journal.get("move")
        assert state.phase == "quiesced"
        assert state.receipt == "kill=4242;turn=not-preserved"
    finally:
        assert owner.close(timeout=5)


def test_a_tmux_that_survives_the_kill_is_unknown(ledger, binding):
    store, _now, _db = ledger
    store.prepare("move", binding)
    owner = AdmissionActor(store)
    tmux, sessions = FakeTmux(alive_after_kill=True), FakeSessions()
    effect = _effect(owner, tmux, sessions)
    request = _request(binding.source_agent_id, binding.source_entity_token)
    try:
        result = effect(request)

        assert result.state == "unknown"
        assert result.reason == "tmux-still-present"
        assert result.kill_moment_ms == 4242
        # The intent stays pending and the session is NOT removed.
        assert store.journal.get("move").phase == "quiesce-pending"
        assert sessions.calls == []
    finally:
        assert owner.close(timeout=5)


def test_a_surviving_recorded_process_is_unknown(ledger, binding):
    store, _now, _db = ledger
    store.prepare("move", binding)
    owner = AdmissionActor(store)
    tmux, sessions = FakeTmux(), FakeSessions()
    effect = _effect(owner, tmux, sessions, process_alive=lambda pid, identity: True)
    request = _request(binding.source_agent_id, binding.source_entity_token)
    try:
        result = effect(request)

        assert result.state == "unknown"
        assert result.reason == "process-still-alive"
        assert store.journal.get("move").phase == "quiesce-pending"
        assert sessions.calls == []
    finally:
        assert owner.close(timeout=5)


def test_an_unconfirmed_removal_is_unknown(ledger, binding):
    store, _now, _db = ledger
    store.prepare("move", binding)
    owner = AdmissionActor(store)
    tmux, sessions = FakeTmux(), FakeSessions(removed=False)
    effect = _effect(owner, tmux, sessions)
    request = _request(binding.source_agent_id, binding.source_entity_token)
    try:
        result = effect(request)

        assert result.state == "unknown"
        assert result.reason == "removal-not-confirmed"
        assert store.journal.get("move").phase == "quiesce-pending"
    finally:
        assert owner.close(timeout=5)


def test_a_replayed_intent_is_read_from_the_journal_not_reexecuted(ledger, binding):
    store, _now, _db = ledger
    store.prepare("move", binding)
    owner = AdmissionActor(store)
    tmux, sessions = FakeTmux(), FakeSessions()
    effect = _effect(owner, tmux, sessions)
    request = _request(binding.source_agent_id, binding.source_entity_token)
    try:
        first = effect(request)
        second = effect(request)

        assert first.state == "quiesced"
        assert second.state == "quiesced"
        assert second.reason == "journal-replay"
        assert second.receipt == first.receipt
        # The second call re-read the durable row; no second kill/removal.
        assert tmux.killed == ["hyprial-actor"]
        assert sessions.calls == [(binding.source_agent_id, "session-1")]
    finally:
        assert owner.close(timeout=5)


class _HangingJournal:
    def submit(self, command):
        class _Future:
            def result(self, timeout):
                raise TimeoutError("probe still owned")

        return _Future()


def test_a_hung_probe_is_unknown_not_a_successful_quiesce():
    """TTL is only a fallback upper bound; it never grants "quiesced"."""

    effect = _effect(_HangingJournal(), FakeTmux(), FakeSessions())
    result = effect(_request("alice", "entity-1"))

    assert result.state == "unknown"
    assert result.reason == "TimeoutError"
    assert result.receipt is None
    # The design's wording for the record-lag interval inside the TTL.
    assert api.LAGGED_RECORDING_WINDOW_NOTE == "记账滞后窗口"


def test_real_removal_adapter_never_removes_a_different_ref(tmp_path: Path):
    from hyprial.daemon.impl.desired_state import DesiredStateStore, InteractiveSession

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.register_interactive(
        InteractiveSession(
            actor="alice",
            cwd="/work",
            command=("claude",),
            source="claude-channel",
            session_ref="session-1",
        )
    )
    removal = api.DesiredStateSessionRemoval(store)

    assert removal.remove("alice", "session-other") is False
    assert [item.session_ref for item in store.load().interactive_sessions] == [
        "session-1"
    ]
    assert removal.remove("alice", "session-1") is True
    assert store.load().interactive_sessions == ()


# ---------------------------------------------------------------------------
# rollback: same session_ref + --resume, and the post-S7 dual-active window
# ---------------------------------------------------------------------------


def test_resume_argv_pins_the_same_ref():
    assert api.resume_argv("claude", "session-1", ("claude", "--model", "x")) == (
        "claude",
        "--model",
        "x",
        "--resume",
        "session-1",
    )
    assert api.resume_argv("pi", "session-1", ("pi", "--resume", "old")) == (
        "pi",
        "--resume",
        "session-1",
    )


def test_an_unsupported_tui_harness_refuses_the_restart():
    with pytest.raises(api.UnsupportedSessionHarness):
        api.resume_argv("codex", "session-1", ("codex",))


def test_restart_refuses_the_post_s7_dual_active_window():
    request = api.SessionRollbackRequest(
        "move",
        "alice",
        "claude",
        "session-1",
        Path("/work"),
        {"A": "b"},
        ("claude",),
        "hyprial-alice",
        target_started=True,
    )
    started: list[object] = []

    with pytest.raises(api.DualActiveRiskWindow):
        api.restart_session(
            request,
            tmux=FakeTmux(),
            tmux_bin="/usr/bin/tmux",
            launch=lambda *args, **kwargs: started.append((args, kwargs)),
        )

    assert started == []


def test_restart_relaunches_the_same_tmux_and_ref():
    request = api.SessionRollbackRequest(
        "move",
        "alice",
        "claude",
        "session-1",
        Path("/work"),
        {"A": "b"},
        ("claude",),
        "hyprial-alice",
    )
    seen: dict[str, object] = {}

    def launch(tmux_bin, name, argv, *, cwd, env):
        seen.update(tmux_bin=tmux_bin, name=name, argv=argv, cwd=cwd, env=env)

    result = api.restart_session(
        request,
        tmux=FakeTmux(),
        tmux_bin="/usr/bin/tmux",
        launch=launch,
    )

    assert result == "hyprial-alice"
    assert seen == {
        "tmux_bin": "/usr/bin/tmux",
        "name": "hyprial-alice",
        "argv": ("claude", "--resume", "session-1"),
        "cwd": Path("/work"),
        "env": {"A": "b"},
    }
