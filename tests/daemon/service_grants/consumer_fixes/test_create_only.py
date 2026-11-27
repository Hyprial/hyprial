"""The granted Key is written into a user-provider entry only when none is there.

Decision (service grant S5 fix, 2026-11-27, critic 1): "does an entry with
this name exist?" and "write the entry" were two steps, and a user entry that
appeared between them was merged over and replaced. Identity now answers the
question and the write under one lock: ``create_user_provider`` creates the
entry file only when it does not exist and reports ``entry-exists`` otherwise,
whatever is at the path. It never merges and never replaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hyprial.identity as identity
from hyprial.identity import SecretResolutionError, SecretResolver, delete_user_provider

NEW = "SYNTHETIC-GRANTED-KEY-0123456789ABCDEF"
OWNED = "SYNTHETIC-USER-OWNED"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    return root


def entry_path(home: Path, entry_id: str) -> Path:
    return home / "secrets" / "providers" / f"{entry_id}.json"


def test_create_writes_a_private_entry_and_reads_back_through_identity(home: Path) -> None:
    path = identity.create_user_provider(home, "grant.first", {"KEY": NEW})

    assert path == entry_path(home, "grant.first")
    assert identity.read_user_provider_value(home, "grant.first", "KEY") == NEW
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_bytes()) == {"KEY": NEW}


def test_create_refuses_an_entry_that_already_exists_and_leaves_it_alone(home: Path) -> None:
    SecretResolver(home, None).write_user_provider("grant.taken", {"KEY": OWNED})  # type: ignore[arg-type]
    before = entry_path(home, "grant.taken").read_bytes()

    with pytest.raises(SecretResolutionError) as caught:
        identity.create_user_provider(home, "grant.taken", {"KEY": NEW})

    assert caught.value.category == "exists"
    assert entry_path(home, "grant.taken").read_bytes() == before
    assert identity.read_user_provider_value(home, "grant.taken", "KEY") == OWNED


def test_create_never_merges_a_field_into_an_existing_entry(home: Path) -> None:
    """The ordinary writer merges; create-only must not add or change anything."""

    SecretResolver(home, None).write_user_provider("grant.taken", {"KEY": OWNED})  # type: ignore[arg-type]

    with pytest.raises(SecretResolutionError):
        identity.create_user_provider(home, "grant.taken", {"OTHER": "SYNTHETIC-ADDED"})

    assert json.loads(entry_path(home, "grant.taken").read_bytes()) == {"KEY": OWNED}


def test_create_refuses_anything_at_the_entry_path_without_touching_it(home: Path) -> None:
    """A symlink is "something is there": refused, and its target is never written."""

    target = home.parent / "outside.json"
    target.write_text(json.dumps({"KEY": "SYNTHETIC-OUTSIDE"}), encoding="utf-8")
    (home / "secrets" / "providers").mkdir(parents=True, mode=0o700)
    (home / "secrets" / "providers" / "grant.link.json").symlink_to(target)

    with pytest.raises(SecretResolutionError):
        identity.create_user_provider(home, "grant.link", {"KEY": NEW})

    assert json.loads(target.read_text(encoding="utf-8")) == {"KEY": "SYNTHETIC-OUTSIDE"}
    assert NEW not in target.read_text(encoding="utf-8")


@pytest.mark.skipif(__import__("os").name == "nt", reason="directory at the entry path needs no privilege anywhere")
def test_create_refuses_a_directory_at_the_entry_path(home: Path) -> None:
    (home / "secrets" / "providers" / "grant.dir.json").mkdir(parents=True, mode=0o700)

    with pytest.raises(SecretResolutionError):
        identity.create_user_provider(home, "grant.dir", {"KEY": NEW})

    assert (home / "secrets" / "providers" / "grant.dir.json").is_dir()


def test_create_and_delete_round_trip_as_one_owner_side_operation(home: Path) -> None:
    identity.create_user_provider(home, "grant.first", {"KEY": NEW})

    assert delete_user_provider(home, "grant.first") is True

    identity.create_user_provider(home, "grant.first", {"KEY": NEW})
    assert identity.read_user_provider_value(home, "grant.first", "KEY") == NEW


def test_create_refuses_an_invalid_entry_id_before_touching_disk(home: Path) -> None:
    for bad in ("", "../kept", "/kept", "a/b", ".hidden", "x" * 129, "kept\x00"):
        with pytest.raises(ValueError):
            identity.create_user_provider(home, bad, {"KEY": NEW})

    assert list((home / "secrets" / "providers").glob("*")) == []


def test_create_refuses_values_the_ordinary_writer_refuses(home: Path) -> None:
    with pytest.raises(TypeError):
        identity.create_user_provider(home, "grant.first", {"KEY": 1})  # type: ignore[dict-item]
    with pytest.raises(SecretResolutionError):
        identity.create_user_provider(home, "grant.second", {"not a field name": NEW})

    assert list((home / "secrets" / "providers").glob("*.json")) == []


def test_the_ordinary_writer_still_replaces_an_existing_entry(home: Path) -> None:
    """Existing callers are unchanged: write_user_provider still replaces whatever entry is there."""

    resolver = SecretResolver(home, None)  # type: ignore[arg-type]
    resolver.write_user_provider("gateway", {"API_KEY": OWNED})
    resolver.write_user_provider("gateway", {"OTHER": "SYNTHETIC-OTHER"})

    assert json.loads(entry_path(home, "gateway").read_bytes()) == {"OTHER": "SYNTHETIC-OTHER"}


def test_failure_is_typed_and_names_no_path_or_value(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("private-error-sentinel " + str(home))

    monkeypatch.setattr("os.link", refuse)
    monkeypatch.setattr("os.replace", refuse)

    with pytest.raises(SecretResolutionError) as caught:
        identity.create_user_provider(home, "grant.first", {"KEY": NEW})

    assert str(home) not in str(caught.value)
    assert "private-error-sentinel" not in str(caught.value)
    assert NEW not in str(caught.value)
    assert caught.value.category == "unavailable"


def test_the_public_faces_export_the_create_only_operation() -> None:
    assert "create_user_provider" in identity.__all__
    assert callable(identity.create_user_provider)
