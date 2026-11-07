"""Never cache a filesystem result for a command that did not begin."""

import pytest

from hyprial.identity import AgentHomeAccessError, HomeOperationUnknown
from hyprial.identity.impl.agents.actor._home_calls import HomeCallRegistry
from hyprial.kernel import AdmissionResult


@pytest.mark.parametrize("mode", ["overloaded", "closed", "undelivered"])
def test_unstarted_command_releases_id_and_allows_explicit_resubmission(home_owner, monkeypatch, mode):
    _registry, actor, agent = home_owner(configured=False)
    tell = actor._runtime.tell
    attempts = []

    def reject_once(handle, command):
        attempts.append(command.correlation_id)
        if len(attempts) == 1:
            if mode == "undelivered":
                actor._undelivered(command, "owner retired before dispatch")
                return AdmissionResult.ACCEPTED
            return AdmissionResult(mode)
        return tell(handle, command)

    monkeypatch.setattr(actor._runtime, "tell", reject_once)
    kwargs = dict(expected_entity_token=agent.entity_token, operation_id="confirm:reject")
    with pytest.raises((AgentHomeAccessError, RuntimeError)):
        actor.confirm_landing_home(agent.actor, **kwargs)
    assert actor._home_calls.pending_count == 0
    with pytest.raises(HomeOperationUnknown):
        actor.wait_home_access("confirm:reject", timeout=0)
    # Explicit submission after the caller has handled the rejection; no auto retry.
    home = actor.confirm_landing_home(agent.actor, **kwargs)
    assert attempts == ["confirm:reject", "confirm:reject"]
    assert actor.wait_home_access("confirm:reject", timeout=0) == home


def test_abandon_unstarted_wakes_attached_waiters_without_retaining_result():
    calls = HomeCallRegistry()
    first, submit = calls.reserve("op", "payload")
    follower, submit_again = calls.reserve("op", "payload")
    assert submit and not submit_again and follower is first
    error = AgentHomeAccessError("not admitted")
    calls.abandon_unstarted("op", error)
    assert first.event.is_set() and follower.error is error
    assert calls.pending_count == 0
    with pytest.raises(HomeOperationUnknown):
        calls.lookup("op")
    calls.abandon_unstarted("op", error)  # Duplicate/unknown rejection is harmless.
    replacement, new = calls.reserve("op", "payload")
    assert new and replacement is not first and not replacement.event.is_set()
    calls.settle("op", "completed", None)
    calls.abandon_unstarted("op", error)
    assert calls.lookup("op").value == "completed"
