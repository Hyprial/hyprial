"""Diagnostic cache fencing; the existing PID/birth classifier owns verdicts."""
from types import SimpleNamespace

from hyprial.daemon.impl.mcp.channel import ownership
from hyprial.daemon.impl.session_actor.process_observation import SessionProcessObservations


def source(monkeypatch, verdict="alive"):
    session = SimpleNamespace(actor="a", session_ref="s", process_pid=123, process_identity="birth")
    state = {"entity": "e", "revision": 1, "generation": 1}
    identity = SimpleNamespace(entity_token=lambda _: state["entity"],
                               binding_key=lambda _: (state["revision"], "claude", "interactive", "s"))
    samples = SessionProcessObservations(identity, lambda _: session, lambda: state["generation"])
    monkeypatch.setattr(ownership, "_owner_process_status", lambda *_: SimpleNamespace(value=verdict))
    return samples, session, state


def test_unknown_identity_is_not_exit_and_no_read_probes(monkeypatch):
    samples, session, _ = source(monkeypatch, "unknown")
    assert not samples.read(session).supported
    samples.sample(session)
    def forbidden(*_):
        raise AssertionError("diagnostic read called process probe")
    monkeypatch.setattr(ownership, "_owner_process_status", forbidden)
    assert samples.read(session).supported and not samples.read(session).exit_verified


def test_verified_birth_mismatch_is_sticky_only_for_same_fence(monkeypatch):
    samples, session, state = source(monkeypatch, "identity-mismatch")
    samples.sample(session)
    assert samples.read(session).exit_verified
    monkeypatch.setattr(ownership, "_owner_process_status", lambda *_: SimpleNamespace(value="unknown"))
    samples.sample(session)
    assert samples.read(session).exit_verified
    state["generation"] += 1
    assert not samples.read(session).supported
    state["generation"] -= 1
    state["entity"] = "replacement"
    assert not samples.read(session).supported


def test_late_exit_sample_cannot_cross_binding_change(monkeypatch):
    samples, session, state = source(monkeypatch)
    def probe(*_):
        state["revision"] += 1
        return SimpleNamespace(value="pid-missing")
    monkeypatch.setattr(ownership, "_owner_process_status", probe)
    assert samples.sample(session).value == "pid-missing"  # Existing caller verdict unchanged.
    assert not samples.read(session).supported


def test_failing_observation_fence_does_not_change_sampler_result(monkeypatch):
    samples, session, _ = source(monkeypatch, "pid-missing")
    def unavailable(*_):
        raise RuntimeError("projection unavailable")
    samples._read_session = unavailable
    assert samples.sample(session).value == "pid-missing"
    assert not samples.read(session).supported
