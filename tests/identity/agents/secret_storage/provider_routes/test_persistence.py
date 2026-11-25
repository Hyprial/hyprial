"""Grant metadata survives restart and configured writes are serialized."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

from hyprial.identity import AgentRegistry, SecretResolutionError, SecretSource
from .test_records import configure, grant, store as store


def test_route_required_survives_database_reopen(store):
    registry, resolver = store
    configure(resolver, value="synthetic-key")
    grant(registry)
    registry.close()
    reopened = AgentRegistry(resolver.hyprial_home.parent / "state" / "agents.sqlite3",
                             owner="owner", machine="node", hyprial_home=resolver.hyprial_home)
    try:
        assert reopened.secret_grant("worker", "model").route_required is True
    finally:
        reopened.close()


def test_old_database_upgrade_is_idempotent_and_preserves_grant(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = tmp_path / "state" / "agents.sqlite3"
    registry = AgentRegistry(path, owner="owner", machine="node", hyprial_home=home)
    registry.create("worker")
    grant(registry)
    registry.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE agent_secret_grants DROP COLUMN route_required")
    for _ in range(2):
        registry = AgentRegistry(path, owner="owner", machine="node", hyprial_home=home)
        try:
            row = registry.secret_grant("worker", "model")
            assert row.route_required is False
            assert row.revision == 1
        finally:
            registry.close()


def test_concurrent_rotations_preserve_unrelated_fields_and_routes(store):
    _registry, resolver = store
    path = configure(resolver, value="synthetic-key")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: resolver.write_user_provider(
            "gateway", {f"FIELD_{index}": "synthetic-value"},
        ), range(8)))
    record = json.loads(path.read_bytes())
    assert set(record["values"]) == {"TOKEN", *(f"FIELD_{index}" for index in range(8))}
    assert record["routes"]["TOKEN"]["endpoint"] == "https://model.example/v1"
    assert len(tuple(path.parent.glob("*.json"))) == 1


def test_concurrent_create_only_configurations_have_one_winner(store):
    _registry, resolver = store

    def save(index):
        value = f"synthetic-{index}"
        try:
            configure(resolver, value=value, create_only=True)
        except SecretResolutionError as error:
            assert error.category == "exists"
            return None
        return value

    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = [value for value in pool.map(save, range(8)) if value is not None]
    assert len(winners) == 1
    path = resolver.hyprial_home / "secrets/providers/gateway.json"
    record = json.loads(path.read_bytes())
    assert record["values"]["TOKEN"] == winners[0]
    assert record["routes"]["TOKEN"]["endpoint"] == "https://model.example/v1"


@pytest.mark.parametrize("parent", ["secrets", "secrets/providers"])
@pytest.mark.parametrize("kind", ["private", "public", "symlink", "file"])
def test_concurrent_parent_creation_requires_a_private_real_directory(store, monkeypatch, parent, kind):
    _registry, resolver = store
    target = resolver.hyprial_home / parent
    outside = resolver.hyprial_home.parent / "outside"
    outside.mkdir(mode=0o700)
    original = Path.mkdir
    injected = False

    def race(path, *args, **kwargs):
        nonlocal injected
        if path != target or injected:
            return original(path, *args, **kwargs)
        injected = True
        if kind == "symlink":
            path.symlink_to(outside, target_is_directory=True)
        elif kind == "file":
            path.write_text("competitor")
        else:
            original(path, mode=0o700)
            path.chmod(0o700 if kind == "private" else 0o755)
        raise FileExistsError(path)

    monkeypatch.setattr(Path, "mkdir", race)
    if kind == "private":
        saved = configure(resolver, value="synthetic-key", create_only=True)
        assert json.loads(saved.read_bytes())["values"]["TOKEN"] == "synthetic-key"
    else:
        with pytest.raises(SecretResolutionError) as failure:
            configure(resolver, value="synthetic-key", create_only=True)
        assert failure.value.category == ("unsafe-mode" if kind == "public" else "unsafe-path")
        assert not (target / "gateway.json").exists()
        if kind == "public":
            assert target.stat().st_mode & 0o777 == 0o755
    assert not tuple(outside.iterdir())


def test_private_grant_cannot_claim_user_route(store):
    from dataclasses import replace

    registry, _resolver = store
    row = grant(registry)
    with pytest.raises(ValueError, match="only user-provider"):
        replace(row, source=SecretSource.AGENT_PRIVATE, route_required=True)


@pytest.mark.parametrize("endpoint", [
    "https://user:password@example.com/v1", "https://example.com/v1?key=secret",
    "http://example.com/v1", "https://example.com/\nkey", "https://example.com/#secret",
])
def test_endpoint_rejects_credential_components_and_insecure_remote(store, endpoint):
    _registry, resolver = store
    with pytest.raises(ValueError):
        resolver.configure_model_service("bad", field_name="TOKEN", value="synthetic",
                                         vendor_id="deepseek", endpoint=endpoint,
                                         protocol="openai-completions")
    assert not (resolver.hyprial_home / "secrets/providers/bad.json").exists()
