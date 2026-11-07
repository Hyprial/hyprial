"""Waiting for accepted home I/O must not create a second filesystem effect."""

from dataclasses import replace
from threading import Event

import pytest

from hyprial.identity.impl.agents.actor._base import (
    AgentHomeAccessPending,
    _PrepareRuntimeCommand,
)
from hyprial.identity.impl.agents.actor._home_calls import (
    HomeOperationConflict,
    HomeOperationUnknown,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_wait_and_identical_pending_replay_never_resubmit_io(home_owner, monkeypatch):
    entered, release = Event(), Event()
    effects = []

    def hold(context):
        effects.append(context)
        entered.set()
        assert release.wait(5)

    _registry, actor, agent = home_owner(configured=True, preparer=hold)
    submissions = []
    tell = actor._runtime.tell

    def record(handle, command):
        if isinstance(command, _PrepareRuntimeCommand):
            submissions.append(command)
        return tell(handle, command)

    monkeypatch.setattr(actor._runtime, "tell", record)
    command = _PrepareRuntimeCommand(
        "runtime:0", "worker", "pi", agent.cwd, TEST_TOOL_PROFILE,
        False, agent.entity_token,
    )
    try:
        with pytest.raises(AgentHomeAccessPending):
            actor._call_home_access(command, timeout=0)
        assert entered.wait(3)
        with pytest.raises(AgentHomeAccessPending):
            actor.wait_home_access("runtime:0", timeout=0)
        with pytest.raises(AgentHomeAccessPending):
            actor._call_home_access(command, timeout=0)
        with pytest.raises(HomeOperationConflict):
            actor._call_home_access(replace(command, cwd="/different-workspace"), timeout=0)
        assert len(effects) == 1
        assert len(submissions) == 1, "replays must not enqueue another owner command"
        release.set()
        result = actor.wait_home_access("runtime:0", timeout=3)
        assert result.entity_token == agent.entity_token and result.launch_token
        replay = actor._call_home_access(command, timeout=0)
        assert replay is result
        assert len(effects) == 1
        assert len(submissions) == 1
    finally:
        release.set()


def test_wait_unknown_does_not_admit_work(home_owner, monkeypatch):
    _registry, actor, _agent = home_owner(configured=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("a status query must not enqueue an actor command")

    with monkeypatch.context() as patch:
        patch.setattr(actor._runtime, "tell", forbidden)
        with pytest.raises(HomeOperationUnknown):
            actor.wait_home_access("never-submitted", timeout=0)


def test_wait_legacy_returns_public_none_instead_of_internal_marker(home_owner):
    _registry, actor, agent = home_owner(configured=False)
    actor._call_home_access(_PrepareRuntimeCommand(
        "legacy:0", "worker", "pi", agent.cwd, TEST_TOOL_PROFILE,
        False, agent.entity_token,
    ))
    assert actor.wait_home_access("legacy:0", timeout=0) is None
