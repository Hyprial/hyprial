"""Every identity/effect input participates in home operation replay binding."""

from dataclasses import replace

import pytest

from hyprial.identity.impl.agents.actor._base import (
    _ConfirmLandingHomeCommand, _MaterialiseLandingHomeCommand, _PrepareRuntimeCommand,
)
from hyprial.identity.impl.agents.actor._home_calls import command_fingerprint
from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


@pytest.mark.parametrize("field,value", [
    ("actor", "other"), ("expected_entity_token", "new"), ("harness", "codex"),
    ("cwd", "/other"), ("containerized", True),
    ("tool_profile", replace(TEST_TOOL_PROFILE, profile_id="other-profile")),
])
def test_runtime_fingerprint_covers_each_execution_input(field, value):
    command = _PrepareRuntimeCommand(
        "op", "worker", "pi", "/work", TEST_TOOL_PROFILE, False, "original",
    )
    assert command_fingerprint(command) != command_fingerprint(replace(command, **{field: value}))


@pytest.mark.parametrize("field,value", [
    ("path", "changed.txt"), ("size", 2), ("sha256", "b" * 64), ("mode", 0o644),
])
def test_payload_fingerprint_binds_content_metadata(field, value):
    original = HomePayloadFile("a.txt", 1, "a" * 64, 0o600)
    command = _MaterialiseLandingHomeCommand("op", "worker", "token", "/stage", (original,))
    changed = replace(command, expected_files=(replace(original, **{field: value}),))
    assert command_fingerprint(command) != command_fingerprint(changed)


def test_operation_phase_and_stage_are_bound_but_correlation_is_separate():
    materialise = _MaterialiseLandingHomeCommand("op", "worker", "token", "/stage", ())
    confirm = _ConfirmLandingHomeCommand("op", "worker", "token")
    assert command_fingerprint(materialise) != command_fingerprint(confirm)
    assert command_fingerprint(materialise) != command_fingerprint(replace(materialise, staged_root="/other"))
    assert command_fingerprint(materialise) == command_fingerprint(replace(materialise, correlation_id="op:new-generation"))
