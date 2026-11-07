"""Agent-private grants select the same authority as set/rotate/remove."""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity import (
    AgentRegistry,
    AgentSecretStore,
    SecretResolutionError,
    SecretResolver,
    SecretSource,
)


@pytest.fixture
def custody(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner", machine="node", hyprial_home=home,
    )
    registry.create("worker")
    registry.grant_secret(
        "worker", grant_id="routes", source=SecretSource.AGENT_PRIVATE,
        entry_id="cloudflare-routes", field_name="CLOUDFLARE_API_TOKEN",
        environment_names=("CLOUDFLARE_API_TOKEN",), revision=1,
    )
    try:
        yield registry, SecretResolver(home, registry)
    finally:
        registry.close()


def _write(store: AgentSecretStore, *, rotate: bool = False) -> str:
    value = secrets.token_hex(24)
    method = store.rotate if rotate else store.set
    method(
        service="cloudflare-routes", field="CLOUDFLARE_API_TOKEN", value=value,
        actor="user:owner", via="cli", at_ms=2 if rotate else 1,
    )
    return value


def _legacy(registry: AgentRegistry) -> str:
    value = secrets.token_hex(24)
    path = Path(registry.home_receipt("worker").path) / "secrets" / "cloudflare-routes"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"CLOUDFLARE_API_TOKEN": value}), encoding="utf-8")
    path.chmod(0o600)
    return value


def _matches(resolver: SecretResolver, expected: str) -> bool:
    # Return only a boolean so pytest's assertion diagnostics cannot print values.
    matches = hmac.compare_digest(resolver.resolve("worker", "routes").value, expected)
    if not matches:
        pytest.fail("resolved secret does not match", pytrace=False)
    return matches


def test_set_grant_resolve_and_rotate_remove(custody) -> None:
    registry, resolver = custody
    store = AgentSecretStore(Path(registry.home_receipt("worker").path))
    first = _write(store)
    assert _matches(resolver, first)
    second = _write(store, rotate=True)
    assert _matches(resolver, second)
    store.remove(
        service="cloudflare-routes", field=None, actor="user:owner", via="cli", at_ms=3,
    )
    with pytest.raises(SecretResolutionError) as error:
        resolver.resolve("worker", "routes")
    assert error.value.category == "missing"
    assert "cloudflare-routes" in str(error.value)
    assert (
        "hyprial agent secret set --agent worker --service cloudflare-routes "
        "--field CLOUDFLARE_API_TOKEN"
    ) in str(error.value)
    assert all(value not in str(error.value) for value in (first, second))


def test_legacy_file_is_migrated_and_directory_removed(custody) -> None:
    registry, resolver = custody
    value = _legacy(registry)
    assert _matches(resolver, value)
    home = Path(registry.home_receipt("worker").path)
    assert (home / "secrets.sqlite").is_file()
    assert not (home / "secrets").exists()
    assert _matches(resolver, value)


def test_store_authority_prevents_legacy_resurrection_after_remove(custody) -> None:
    registry, resolver = custody
    value = _legacy(registry)
    assert _matches(resolver, value)
    store = AgentSecretStore(Path(registry.home_receipt("worker").path))
    value = _write(store, rotate=True)
    assert _matches(resolver, value)
    store.remove(
        service="cloudflare-routes", field=None, actor="user:owner", via="cli", at_ms=3,
    )
    with pytest.raises(SecretResolutionError, match="secret resolution missing"):
        resolver.resolve("worker", "routes")


def test_unrelated_store_service_does_not_disable_migration(custody) -> None:
    registry, resolver = custody
    value = _legacy(registry)
    store = AgentSecretStore(Path(registry.home_receipt("worker").path))
    store.set(
        service="unrelated", field="OTHER_TOKEN", value=secrets.token_hex(24),
        actor="user:owner", via="cli", at_ms=1,
    )
    assert _matches(resolver, value)


@pytest.mark.parametrize("fence", ["agent", "receipt"])
def test_wrong_incarnation_refused_before_store_read(custody, monkeypatch, fence) -> None:
    registry, resolver = custody
    _legacy(registry)
    home = Path(registry.home_receipt("worker").path)
    store = AgentSecretStore(home)
    _write(store)
    if fence == "agent":
        original = registry.secret_grant("worker", "routes")
        monkeypatch.setattr(
            registry, "secret_grant", lambda *_: replace(original, entity_token="stale"),
        )
    else:
        original = registry.home_receipt("worker")
        monkeypatch.setattr(
            registry, "home_receipt", lambda *_: replace(original, entity_token="stale"),
        )
    with pytest.raises(SecretResolutionError) as error:
        resolver.resolve("worker", "routes")
    assert error.value.category == "grant-stale"
    assert (home / "secrets" / "cloudflare-routes").exists()
    assert not any(event.action == "migrate" for event in store.history())


@pytest.mark.parametrize("damage", ["mode", "symlink", "corrupt", "key"])
def test_unsafe_store_never_falls_back_to_legacy(custody, damage) -> None:
    registry, resolver = custody
    _legacy(registry)
    store = AgentSecretStore(Path(registry.home_receipt("worker").path))
    _write(store)
    if damage == "mode":
        store.path.chmod(0o644)
    elif damage == "symlink":
        target = store.path.with_suffix(".parked")
        store.path.rename(target)
        store.path.symlink_to(target)
    elif damage == "key":
        store.key_path.unlink()
    else:
        store.path.write_bytes(b"not a database")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")


def test_genuinely_missing_legacy_entry_has_guided_error(custody) -> None:
    _registry, resolver = custody
    with pytest.raises(SecretResolutionError) as error:
        resolver.resolve("worker", "routes")
    assert error.value.category == "missing"
    assert "hyprial agent secret set --agent worker" in str(error.value)
    assert "--field CLOUDFLARE_API_TOKEN" in str(error.value)


def test_new_agent_and_sqlite_operations_do_not_create_secrets_directory(custody) -> None:
    registry, resolver = custody
    home = Path(registry.home_receipt("worker").path)
    assert not (home / "secrets").exists()
    store = AgentSecretStore(home)
    value = _write(store)
    assert _matches(resolver, value)
    assert not (home / "secrets").exists()


def test_sqlite_resolution_is_read_only_even_with_native_directory(custody, monkeypatch) -> None:
    registry, resolver = custody
    home = Path(registry.home_receipt("worker").path)
    store = AgentSecretStore(home)
    value = _write(store)
    (home / "secrets").mkdir(mode=0o700)
    (home / "secrets" / "native").mkdir(mode=0o700)

    def reject_initialization(_self):
        raise AssertionError("normal resolution must not initialize or migrate the database")

    monkeypatch.setattr(AgentSecretStore, "_initialize", reject_initialization)
    assert _matches(resolver, value)
