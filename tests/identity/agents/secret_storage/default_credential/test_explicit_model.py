"""Explicit model intent prevents default credentials at the identity authority."""

from pathlib import Path

from tests.identity.agents.secret_storage.default_credential.test_claude import custody as custody


def test_explicit_model_identity_never_inherits_a_default(custody):
    registry, _, _, _ = custody
    registry.set_default_claude_credential("custodian", "claude")
    agent = registry.create("explicit", preferred_harness="claude",
                            capabilities={"modelConfigurationRequired": True})
    assert registry.secret_inventory(agent.actor) == ()
    assert not (Path(registry.home_receipt(agent.actor).path) / "secrets").exists()
    registry._grant_default_claude_locked(agent)
    assert registry.secret_inventory(agent.actor) == ()
    legacy = registry.create("legacy", preferred_harness="claude")
    assert len(registry.secret_inventory(legacy.actor)) == 1
