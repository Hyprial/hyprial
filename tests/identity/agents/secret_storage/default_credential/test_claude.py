"""Default references create explicit grants, never consumer value copies."""
from pathlib import Path
import hmac
import secrets

import pytest

from hyprial.identity import AgentRegistry, AgentSecretStore, SecretResolver, SecretResolutionError

FIELD = "CLAUDE_CODE_OAUTH_TOKEN"


@pytest.fixture
def custody(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="owner", machine="host", hyprial_home=home)
    registry.create("custodian")
    store = AgentSecretStore(Path(registry.home_receipt("custodian").path))
    value = secrets.token_hex(24)
    store.set(service="claude", field=FIELD, value=value, actor="user:owner", via="cli", at_ms=1)
    try:
        yield registry, store, SecretResolver(home, registry), value
    finally:
        registry.close()


def test_default_creates_shared_grant_without_copy(custody):
    registry, _, resolver, value = custody
    reference = registry.set_default_claude_credential("custodian", "claude")
    assert reference["ownerEntityToken"] == registry.require("custodian").entity_token
    worker = registry.create("wf-review", preferred_harness="claude")
    grant, = registry.secret_inventory(worker.actor)
    assert grant.source.value == "agent-shared"
    assert grant.owner_actor == "custodian"
    assert grant.environment_names == (FIELD,)
    assert hmac.compare_digest(resolver.resolve(worker.actor, grant.grant_id).value, value)
    home = Path(registry.home_receipt(worker.actor).path)
    assert not (home / "secrets.sqlite").exists()
    assert not (home / "secrets").exists()
    registry.create("pi-worker", preferred_harness="pi")
    assert registry.secret_inventory("pi-worker") == ()
    transferred = registry.create_transfer_hosted("transferred", pinned_owner="owner", preferred_harness="claude")
    assert registry.secret_inventory(transferred.actor) == ()


def test_default_does_not_modify_existing_agents_or_regrant(custody):
    registry, _, _, _ = custody
    registry.create("existing", preferred_harness="claude")
    registry.set_default_claude_credential("custodian", "claude")
    assert registry.secret_inventory("existing") == ()
    registry.create("new", preferred_harness="claude")
    assert registry.revoke_secret_grant("new", "default-claude")
    registry.set_default_claude_credential("custodian", "claude")
    assert registry.secret_inventory("new") == ()
    registry.clear_default_claude_credential()
    registry.create("later", preferred_harness="claude")
    assert registry.secret_inventory("later") == ()


def test_rotation_and_owner_fence(custody):
    registry, store, resolver, _ = custody
    registry.set_default_claude_credential("custodian", "claude")
    registry.create("worker", preferred_harness="claude")
    second = secrets.token_hex(24)
    store.rotate(service="claude", field=FIELD, value=second, actor="user:owner", via="cli", at_ms=2)
    assert hmac.compare_digest(resolver.resolve("worker", "default-claude").value, second)
    registry.rotate_secret_authority("custodian")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "default-claude")


def test_default_requires_existing_oauth_field(custody):
    registry, _, _, _ = custody
    with pytest.raises((ValueError, SecretResolutionError)):
        registry.set_default_claude_credential("custodian", "absent")
    assert registry.default_claude_credential() is None


def test_default_and_revocation_survive_registry_reopen(custody):
    registry, _, resolver, _ = custody
    registry.set_default_claude_credential("custodian", "claude")
    registry.create("worker", preferred_harness="claude")
    registry.revoke_secret_grant("worker", "default-claude")
    reopened = AgentRegistry(registry.database, owner="owner", machine="host",
                             hyprial_home=resolver.hyprial_home)
    try:
        assert reopened.default_claude_credential()["ownerActor"] == "custodian"
        assert reopened.secret_inventory("worker") == ()
        reopened.create("later", preferred_harness="claude")
        assert len(reopened.secret_inventory("later")) == 1
    finally:
        reopened.close()


def test_consumer_rotation_and_source_removal_refuse_resolution(custody):
    registry, store, resolver, _ = custody
    registry.set_default_claude_credential("custodian", "claude")
    registry.create("worker", preferred_harness="claude")
    registry.rotate_secret_authority("worker")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "default-claude")
    registry.create("other", preferred_harness="claude")
    store.remove(service="claude", field=FIELD, actor="user:owner", via="cli", at_ms=2)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("other", "default-claude")
