"""TC-005: runtime preparation checks the intended incarnation before effects."""

import threading

import pytest

from hyprial.identity.impl.agents.registry import AgentEntityConflict
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


@pytest.mark.parametrize("configured,harness", [(True, "pi"), (False, "pi"), (True, "dsh")])
@pytest.mark.parametrize("expected", ["old-incarnation", ""])
def test_stale_runtime_request_rejected_before_filesystem_or_legacy(
    home_owner, monkeypatch: pytest.MonkeyPatch, configured: bool, harness: str, expected: str,
) -> None:
    registry, actor, original = home_owner(configured=configured)
    effects = []
    lane = actor._home_effects
    assert lane is not None
    submit = lane.submit_reserved

    def record(effect):
        effects.append(effect)
        return submit(effect)

    monkeypatch.setattr(lane, "submit_reserved", record)
    legacy = []
    with pytest.raises(AgentEntityConflict) as error:
        actor.prepare_runtime_context(
            "worker", harness, original.cwd, TEST_TOOL_PROFILE,
            expected_entity_token=expected,
            legacy_reporter=lambda *args: legacy.append(args),
        )
    assert error.value.code == "AGENT_VERSION_CONFLICT"
    assert effects == [], "stale incarnation must not reach the filesystem lane"
    assert legacy == [], "legacy selection must not hide a stale identity request"
    assert registry.require("worker") == original


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("fenced", [True, False])
def test_current_or_legacy_unfenced_runtime_still_works(home_owner, configured, fenced):
    _registry, actor, original = home_owner(configured=configured)
    kwargs = {"expected_entity_token": original.entity_token} if fenced else {}
    result = actor.prepare_runtime_context(
        "worker", "pi", original.cwd, TEST_TOOL_PROFILE, **kwargs,
    )
    if configured:
        assert result is not None and result.launch_token
        assert result.entity_token == original.entity_token
    else:
        assert result is None


def test_token_comparison_reads_authority_inside_identity_owner(home_owner, monkeypatch):
    registry, actor, original = home_owner(configured=True)
    observed_threads = []
    require = registry.require

    def record(name):
        observed_threads.append(threading.get_ident())
        return require(name)

    monkeypatch.setattr(registry, "require", record)
    caller = threading.get_ident()
    with pytest.raises(AgentEntityConflict):
        actor.prepare_runtime_context(
            "worker", "pi", original.cwd, TEST_TOOL_PROFILE,
            expected_entity_token="old-incarnation",
        )
    assert observed_threads, "identity authority must be consulted"
    assert caller not in observed_threads, "caller-side prechecks cannot replace owner admission"
