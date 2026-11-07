"""Migration deletes only verified direct grant files, never native credentials."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import pytest

from hyprial.identity import AgentSecretStore, SecretResolutionError, SecretSource
from hyprial.identity.impl.agents.secret_store import legacy

from .test_grant_resolution import _legacy, _matches, _write, custody as custody


def test_migration_preserves_other_fields_and_native_subtree(custody) -> None:
    registry, resolver = custody
    value = _legacy(registry)
    home = Path(registry.home_receipt("worker").path)
    native = home / "secrets" / "native" / "pi"
    native.mkdir(parents=True, mode=0o700)
    credential = native / "auth.json"
    credential.write_text("{}", encoding="utf-8")
    credential.chmod(0o600)
    ungranted = home / "secrets" / "ungranted"
    ungranted.write_text("{}", encoding="utf-8")
    ungranted.chmod(0o600)
    source = home / "secrets" / "cloudflare-routes"
    source.write_text(json.dumps({"CLOUDFLARE_API_TOKEN": value, "otherKey": value}), encoding="utf-8")
    assert _matches(resolver, value)
    assert not source.exists()
    assert credential.read_text(encoding="utf-8") == "{}"
    store = AgentSecretStore(home)
    assert ungranted.read_text(encoding="utf-8") == "{}"
    assert not store.list(service="ungranted")
    assert {row.field for row in store.list(service="cloudflare-routes")} == {
        "CLOUDFLARE_API_TOKEN", "otherKey", "VALUE",
    }
    assert len([event for event in store.history() if event.action == "migrate"]) == 3


def test_raw_legacy_value_migrates_to_value_field(custody) -> None:
    registry, resolver = custody
    home = Path(registry.home_receipt("worker").path)
    directory = home / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    value = secrets.token_hex(24)
    source = directory / "cloudflare-routes"
    source.write_text(value, encoding="utf-8")
    source.chmod(0o600)
    registry.grant_secret(
        "worker", grant_id="routes", source=SecretSource.AGENT_PRIVATE,
        entry_id="cloudflare-routes", field_name=None,
        environment_names=("CLOUDFLARE_API_TOKEN",), revision=2,
    )
    assert _matches(resolver, value)
    assert not directory.exists()
    assert AgentSecretStore(home).get("cloudflare-routes", "VALUE") is not None


def test_migration_retries_after_commit_before_unlink(custody, monkeypatch) -> None:
    registry, resolver = custody
    value = _legacy(registry)
    real_unlink = os.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == "cloudflare-routes":
            raise OSError("injected interruption")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(legacy.os, "unlink", fail_unlink)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    home = Path(registry.home_receipt("worker").path)
    assert (home / "secrets" / "cloudflare-routes").exists()
    store = AgentSecretStore(home)
    assert store.get("cloudflare-routes", "CLOUDFLARE_API_TOKEN") is not None
    monkeypatch.setattr(legacy.os, "unlink", real_unlink)
    assert _matches(resolver, value)
    assert not (home / "secrets").exists()
    assert len([event for event in store.history() if event.action == "migrate"]) == 2


def test_conflicting_sqlite_value_is_not_overwritten_or_legacy_deleted(custody) -> None:
    registry, resolver = custody
    _legacy(registry)
    home = Path(registry.home_receipt("worker").path)
    store = AgentSecretStore(home)
    _write(store)
    before = store.get("cloudflare-routes", "CLOUDFLARE_API_TOKEN").fingerprint
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    assert store.get("cloudflare-routes", "CLOUDFLARE_API_TOKEN").fingerprint == before
    assert (home / "secrets" / "cloudflare-routes").exists()


def test_remove_during_interrupted_migration_cannot_resurrect_value(custody, monkeypatch) -> None:
    registry, resolver = custody
    _legacy(registry)
    real_unlink = os.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == "cloudflare-routes":
            raise OSError("injected interruption")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(legacy.os, "unlink", fail_unlink)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    home = Path(registry.home_receipt("worker").path)
    store = AgentSecretStore(home)
    store.remove(service="cloudflare-routes", field=None, actor="user:owner", via="cli", at_ms=3)
    monkeypatch.setattr(legacy.os, "unlink", real_unlink)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    assert store.get("cloudflare-routes", "CLOUDFLARE_API_TOKEN") is None


@pytest.mark.parametrize("damage", ["mode", "symlink", "invalid", "directory-link"])
def test_unsafe_legacy_source_is_preserved_and_not_imported(custody, damage) -> None:
    registry, resolver = custody
    _legacy(registry)
    home = Path(registry.home_receipt("worker").path)
    source = home / "secrets" / "cloudflare-routes"
    if damage == "mode":
        source.chmod(0o644)
    elif damage == "symlink":
        target = home / "unrelated"
        source.rename(target)
        source.symlink_to(target)
    elif damage == "directory-link":
        target = home / "unrelated"
        source.parent.rename(target)
        source.parent.symlink_to(target, target_is_directory=True)
    else:
        source.write_text("not json", encoding="utf-8")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    assert source.exists()
    assert AgentSecretStore(home).get("cloudflare-routes", "CLOUDFLARE_API_TOKEN") is None


def test_failed_verification_keeps_source_and_rolls_back_import(custody, monkeypatch) -> None:
    registry, resolver = custody
    _legacy(registry)
    monkeypatch.setattr(legacy, "_matches", lambda *_: False)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    home = Path(registry.home_receipt("worker").path)
    assert (home / "secrets" / "cloudflare-routes").exists()
    assert not AgentSecretStore(home).list(service="cloudflare-routes")


def test_migration_refuses_wrong_owner_without_importing(custody, monkeypatch) -> None:
    registry, resolver = custody
    _legacy(registry)
    check = legacy.private_metadata_violation

    def wrong_owner(path, metadata, *, mode):
        return "unsafe-owner" if path.name == "cloudflare-routes" else check(path, metadata, mode=mode)

    monkeypatch.setattr(legacy, "private_metadata_violation", wrong_owner)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "routes")
    home = Path(registry.home_receipt("worker").path)
    assert (home / "secrets" / "cloudflare-routes").exists()
    assert not AgentSecretStore(home).list(service="cloudflare-routes")
