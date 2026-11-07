"""Review probes: migrate a source, not whichever grant resolves first."""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path

import pytest

from hyprial.identity import (
    AgentRegistry, AgentSecretStore, SecretResolutionError, SecretResolver, SecretSource,
)
from hyprial.identity.impl.agents.secret_store import legacy

from .test_grant_resolution import custody as custody


def _source(registry, entry, *, collision=False):
    value = secrets.token_hex(24)
    document = {"API_TOKEN": value}
    if collision:
        document.update(VALUE=secrets.token_hex(24), LEGACY_RAW_1=secrets.token_hex(24))
    text = json.dumps(document, indent=2) + "\n"
    directory = Path(registry.home_receipt("worker").path) / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    source = directory / entry
    source.write_text(text, encoding="utf-8")
    source.chmod(0o600)
    fields = {"raw": None, "raw-second": None, "structured": "API_TOKEN"}
    if collision:
        fields.update(collision="VALUE", collision_second="LEGACY_RAW_1")
    for grant_id, field in fields.items():
        registry.grant_secret(
            "worker", grant_id=grant_id, source=SecretSource.AGENT_PRIVATE,
            entry_id=entry, field_name=field, environment_names=("API_TOKEN",), revision=1,
        )
    expected = {key: text if field is None else document[field] for key, field in fields.items()}
    return source, expected


def _check(resolver, expected, order):
    for grant_id in order:
        matched = hmac.compare_digest(resolver.resolve("worker", grant_id).value, expected[grant_id])
        if not matched:
            pytest.fail("legacy grant value changed", pytrace=False)


@pytest.mark.parametrize("entry", ["legacy-entry", "native"])
@pytest.mark.parametrize("first", ["raw", "structured"])
@pytest.mark.parametrize("collision", [False, True])
def test_all_grants_survive_source_migration(custody, entry, first, collision):
    registry, resolver = custody
    source, expected = _source(registry, entry, collision=collision)
    order = [first, *[key for key in expected if key != first]]
    _check(resolver, expected, order)
    assert not source.parent.exists()
    for _ in range(2):
        _check(resolver, expected, reversed(order))
    for grant_id in expected:
        grant = registry.secret_grant("worker", grant_id)
        assert grant.field_name is not None
        assert grant.revision == (2 if grant_id.startswith("raw") else 1)
    assert registry.secret_grant("worker", "routes").revision == 1
    store = AgentSecretStore(source.parent.parent)
    count = len([event for event in store.history() if event.action == "migrate"])
    assert count == (4 if collision else 2)


def test_resolution_refreshes_a_grant_migrated_by_another_resolver(custody, monkeypatch):
    registry, resolver = custody
    source, expected = _source(registry, "legacy-entry", collision=True)
    original = registry.secret_grant
    triggered = False

    def concurrent_read(actor, grant_id):
        nonlocal triggered
        snapshot = original(actor, grant_id)
        if not triggered and grant_id == "raw":
            triggered = True
            other = SecretResolver(resolver.hyprial_home, registry)
            _check(other, expected, ["structured"])
            assert not source.exists()
        return snapshot

    monkeypatch.setattr(registry, "secret_grant", concurrent_read)
    _check(resolver, expected, ["raw"])
    assert resolver.resolve("worker", "raw").grant.revision == 2


class _Crash(BaseException):
    pass


@pytest.mark.parametrize("stage", ["import", "unlink"])
@pytest.mark.parametrize("first", ["raw", "structured"])
def test_restart_after_import_preserves_every_grant(custody, monkeypatch, stage, first):
    registry, resolver = custody
    source, expected = _source(registry, "legacy-entry", collision=True)
    if stage == "import":
        original = legacy._import

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise _Crash()

        monkeypatch.setattr(legacy, "_import", crash)
    else:
        original = os.unlink

        def crash(path, *args, **kwargs):
            if path == "legacy-entry":
                # All grants must be durably rebound BEFORE the only file is deleted.
                assert all(registry.secret_grant("worker", key).field_name for key in expected)
                raise _Crash()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(legacy.os, "unlink", crash)
    with pytest.raises(_Crash):
        resolver.resolve("worker", first)
    assert source.exists()
    store = AgentSecretStore(source.parent.parent)
    assert len(store.list(service="legacy-entry")) == 4
    monkeypatch.undo()
    # Reopen both authorities, rather than reusing an in-memory migration marker.
    reopened = AgentRegistry(
        registry.database, owner="owner", machine="node", hyprial_home=resolver.hyprial_home,
    )
    try:
        restarted = SecretResolver(resolver.hyprial_home, reopened)
        _check(restarted, expected, reversed(list(expected)))
        _check(restarted, expected, expected)
        assert not source.parent.exists()
        assert len([event for event in store.history() if event.action == "migrate"]) == 4
        assert reopened.secret_grant("worker", "raw").revision == 2
        assert reopened.secret_grant("worker", "raw-second").revision == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("collision", [False, True])
def test_invalid_selector_cannot_gain_raw_text_access(custody, collision):
    registry, resolver = custody
    source, _expected = _source(registry, "legacy-entry", collision=collision)
    registry.grant_secret(
        "worker", grant_id="invalid", source=SecretSource.AGENT_PRIVATE,
        entry_id="legacy-entry", field_name="LEGACY_RAW_2" if collision else "VALUE",
        environment_names=("API_TOKEN",), revision=1,
    )
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "invalid")
    assert source.exists()
    assert not AgentSecretStore(source.parent.parent).list(service="legacy-entry")


def test_native_symlink_is_refused(custody):
    registry, resolver = custody
    source, _expected = _source(registry, "native")
    target = source.with_name("unrelated")
    source.rename(target)
    source.symlink_to(target)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("worker", "raw")
    assert source.is_symlink()
    assert target.is_file()
    assert not AgentSecretStore(source.parent.parent).list(service="native")


def test_native_directory_is_not_a_legacy_entry(custody):
    registry, resolver = custody
    home = Path(registry.home_receipt("worker").path)
    native = home / "secrets" / "native"
    native.mkdir(parents=True, mode=0o700)
    native.parent.chmod(0o700)
    marker = native / "auth.json"
    marker.write_text("{}", encoding="utf-8")
    marker.chmod(0o600)
    store = AgentSecretStore(home)
    value = secrets.token_hex(24)
    store.set(service="native", field="API_TOKEN", value=value, actor="user:owner", via="cli", at_ms=1)
    registry.grant_secret(
        "worker", grant_id="native", source=SecretSource.AGENT_PRIVATE,
        entry_id="native", field_name="API_TOKEN", environment_names=("API_TOKEN",), revision=1,
    )
    _check(resolver, {"native": value}, ["native"])
    assert marker.read_text(encoding="utf-8") == "{}"
    assert not any(event.action == "migrate" for event in store.history())
