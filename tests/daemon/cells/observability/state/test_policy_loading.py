"""Configuration is read once, validated, and never execution policy."""
import json
import pytest
from hyprial.daemon.impl.cells.observability.policy import RunPolicies


def test_policy_configuration_loads_per_agent_and_rejects_bad_config(tmp_path):
    assert RunPolicies.load(tmp_path).for_actor("agent:o:n:a").turn_long_after_ms == 1_800_000
    path = tmp_path / "cell-observation.json"
    path.write_text(json.dumps({"revision": 2, "defaults": {"permissionLongAfterMs": None},
                               "agents": {"agent:o:n:a": {"turnLongAfterMs": 123}}}))
    policies = RunPolicies.load(tmp_path)
    assert policies.for_actor("agent:o:n:a").turn_long_after_ms == 123
    assert policies.for_actor("agent:o:n:b").permission_long_after_ms is None
    assert policies.for_actor("agent:o:n:a").revision == 2
    path.write_text('{"agents":{"agent:invalid":{}}}')
    with pytest.raises(ValueError):
        RunPolicies.load(tmp_path)
    path.write_text('{"defaults":{"turnLongAfterMs":true}}')
    with pytest.raises(ValueError):
        RunPolicies.load(tmp_path)
    path.write_text('{"defaults":{"restart":true}}')
    with pytest.raises(ValueError):
        RunPolicies.load(tmp_path)
