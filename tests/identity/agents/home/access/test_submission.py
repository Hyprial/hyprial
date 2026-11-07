"""Public home ports carry stable IDs; recovery never infers from cache misses."""

from pathlib import Path

import pytest

from hyprial.identity.impl.agents.actor._home_calls import (
    HomeCallRegistry, HomeOperationConflict, HomeOperationUnknown,
)
from hyprial.identity.impl.agents.home.provisioner import snapshot_home_payload
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def invoke(actor, agent, phase, operation_id):
    options = {} if operation_id is None else {"operation_id": operation_id}
    if phase == "runtime":
        return actor.prepare_runtime_context(
            agent.actor, "pi", agent.cwd, TEST_TOOL_PROFILE,
            expected_entity_token=agent.entity_token, **options,
        )
    return actor.confirm_landing_home(
        agent.actor, expected_entity_token=agent.entity_token, **options,
    )


@pytest.mark.parametrize("phase", ["runtime", "confirm"])
def test_public_stable_id_replays_once_and_default_calls_get_fresh_ids(home_owner, monkeypatch, phase):
    _registry, actor, agent = home_owner(configured=False)
    submitted = []
    tell = actor._runtime.tell

    def record(handle, command):
        if hasattr(command, "correlation_id"):
            submitted.append(command.correlation_id)
        return tell(handle, command)

    monkeypatch.setattr(actor._runtime, "tell", record)
    first = invoke(actor, agent, phase, "phase:0")
    assert invoke(actor, agent, phase, "phase:0") == first
    assert actor.wait_home_access("phase:0", timeout=0) == first
    assert submitted == ["phase:0"]
    invoke(actor, agent, phase, None)
    invoke(actor, agent, phase, None)
    assert len(submitted) == len(set(submitted)) == 3


def test_materialise_replay_keeps_imported_bytes_and_binds_payload(home_owner, tmp_path):
    _registry, actor, agent = home_owner(configured=False)
    stage = tmp_path / "stage"
    (stage / "state").mkdir(parents=True)
    source = stage / "state" / "test-payload"
    source.write_text("synthetic payload")
    expected = snapshot_home_payload(stage)
    kwargs = dict(expected_entity_token=agent.entity_token, expected_files=expected, operation_id="land:0")
    home = actor.materialise_landing_home(agent.actor, str(stage), **kwargs)
    assert (Path(home) / "state/test-payload").read_text() == "synthetic payload"
    assert not source.exists()  # Materialisation consumed the staging bytes.
    assert actor.materialise_landing_home(agent.actor, str(stage), **kwargs) == home
    assert actor.wait_home_access("land:0", timeout=0) == home
    with pytest.raises(HomeOperationConflict):
        actor.materialise_landing_home(agent.actor, str(stage), **{**kwargs, "expected_files": ()})
    with pytest.raises(HomeOperationConflict):
        actor.confirm_landing_home(agent.actor, expected_entity_token=agent.entity_token, operation_id="land:0")


@pytest.mark.parametrize("phase", ["runtime", "confirm", "materialise"])
@pytest.mark.parametrize("bad_id", ["", "  ", 123])
def test_invalid_id_is_rejected_before_admission(home_owner, monkeypatch, phase, bad_id):
    _registry, actor, agent = home_owner(configured=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid operation ID reached the owner mailbox")

    monkeypatch.setattr(actor._runtime, "tell", forbidden)
    with pytest.raises(ValueError, match="operation_id"):
        if phase == "materialise":
            actor.materialise_landing_home(agent.actor, "/unused", expected_entity_token=agent.entity_token, expected_files=(), operation_id=bad_id)
        else:
            invoke(actor, agent, phase, bad_id)


def test_unknown_after_eviction_is_not_absent_but_explicit_submission_is_new(home_owner, monkeypatch):
    registry, actor, agent = home_owner(configured=False)
    now = [0.0]
    actor._home_calls = HomeCallRegistry(clock=lambda: now[0], retention_seconds=1)
    home = invoke(actor, agent, "confirm", "confirm:0")
    now[0] = 1.0
    with pytest.raises(HomeOperationUnknown):
        actor.wait_home_access("confirm:0", timeout=0)
    assert registry.home_receipt(agent.actor).path == home  # unknown is NOT absent.
    submitted = []
    tell = actor._runtime.tell

    def record(handle, command):
        submitted.append(command)
        return tell(handle, command)

    monkeypatch.setattr(actor._runtime, "tell", record)
    # This tests the submission contract only, not P2's absent/retry decision.
    # The API cannot remember an evicted ID and does not promise it will.
    assert invoke(actor, agent, "confirm", "confirm:0") == home
    assert len(submitted) == 1


def test_submission_detaches_payload_sequence_before_mailbox(home_owner, monkeypatch):
    from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile

    _registry, actor, agent = home_owner(configured=False)
    item = HomePayloadFile("state/item", 1, "a" * 64, 0o600)
    supplied = [item]
    commands = []

    def capture(command):
        supplied.clear()
        commands.append(command)
        return "/test-home"

    monkeypatch.setattr(actor, "_call_home_access", capture)
    actor.materialise_landing_home(agent.actor, "/stage", expected_entity_token=agent.entity_token, expected_files=supplied, operation_id="land:immutable")
    assert commands[0].expected_files == (item,)


def test_recovery_errors_are_available_through_identity_public_surface():
    from hyprial.identity import (
        AgentHomeAccessError, AgentHomeAccessPending,
        HomeOperationConflict as Conflict, HomeOperationUnknown as Unknown,
    )

    assert Conflict is HomeOperationConflict and Unknown is HomeOperationUnknown
    assert issubclass(AgentHomeAccessPending, AgentHomeAccessError)
