"""Deleting one user-provider entry: the public identity operation.

Decision (service grant S5, 2026-11-26): the recipient side of a service
grant writes the opened Key as a user-provider entry and must remove it
again when the grant is withdrawn. Identity exposes removal as one public
function next to ``read_user_provider_value``: it deletes exactly one entry
file, is idempotent, and refuses anything that is not a plain private file
(symlink, directory, public ancestor) without touching it.
"""

import json
import os
from pathlib import Path

import pytest

import hyprial.identity as identity
from hyprial.identity import AgentRegistry, SecretResolutionError, SecretResolver, delete_user_provider


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    return root


def _resolver(home: Path) -> SecretResolver:
    registry = AgentRegistry(
        home.parent / "state" / "agents.sqlite3", owner="owner", machine="node", hyprial_home=home
    )
    try:
        return SecretResolver(home, registry)
    finally:
        registry.close()


def test_delete_removes_only_the_named_entry_and_is_idempotent(home: Path) -> None:
    resolver = _resolver(home)
    resolver.write_user_provider("grant.first", {"KEY": "synthetic-first"})
    resolver.write_user_provider("kept", {"KEY": "synthetic-kept"})

    assert delete_user_provider(home, "grant.first") is True

    assert not (home / "secrets" / "providers" / "grant.first.json").exists()
    assert identity.read_user_provider_value(home, "kept", "KEY") == "synthetic-kept"
    assert delete_user_provider(home, "grant.first") is False


def test_delete_refuses_an_invalid_entry_id_before_touching_disk(home: Path) -> None:
    resolver = _resolver(home)
    resolver.write_user_provider("kept", {"KEY": "synthetic-kept"})

    for bad in ("", "../kept", "/kept", "a/b", ".hidden", "x" * 129, "kept\x00"):
        with pytest.raises(ValueError):
            delete_user_provider(home, bad)

    assert (home / "secrets" / "providers" / "kept.json").exists()
    assert delete_user_provider(home, "kept.json") is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privilege on Windows")
def test_delete_refuses_a_symlink_and_leaves_its_target(home: Path) -> None:
    resolver = _resolver(home)
    resolver.write_user_provider("kept", {"KEY": "synthetic-kept"})
    outside = home.parent / "outside.json"
    outside.write_text(json.dumps({"KEY": "synthetic-outside"}), encoding="utf-8")
    (home / "secrets" / "providers" / "grant.link.json").symlink_to(outside)

    with pytest.raises(SecretResolutionError) as caught:
        delete_user_provider(home, "grant.link")

    assert caught.value.category == "unsafe-type"
    assert "synthetic-outside" not in str(caught.value)
    assert outside.exists()
    assert (home / "secrets" / "providers" / "grant.link.json").is_symlink()
    assert identity.read_user_provider_value(home, "kept", "KEY") == "synthetic-kept"


def test_delete_refuses_a_public_ancestors_entry(home: Path) -> None:
    resolver = _resolver(home)
    resolver.write_user_provider("grant.exposed", {"KEY": "synthetic-exposed"})
    providers = home / "secrets" / "providers"
    providers.chmod(0o755)
    try:
        with pytest.raises(SecretResolutionError):
            delete_user_provider(home, "grant.exposed")
        assert (providers / "grant.exposed.json").exists()
    finally:
        providers.chmod(0o700)


def test_delete_failure_is_typed_and_names_no_path(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _resolver(home)
    resolver.write_user_provider("grant.first", {"KEY": "synthetic-first"})

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("private-error-sentinel " + str(home))

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(SecretResolutionError) as caught:
        delete_user_provider(home, "grant.first")

    assert str(home) not in str(caught.value)
    assert "private-error-sentinel" not in str(caught.value)


def test_facade_exports_the_delete_operation() -> None:
    assert "delete_user_provider" in identity.__all__
    assert callable(identity.delete_user_provider)
