"""The identity-owned logout artifact primitive (A2).

The primitive owns exactly two artifacts and must be truthful about every
failure mode: missing files, symlinks, a directory where the credential file
belongs, unreadable settings, denied permissions, and partial removal.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from hyprial.identity import (
    IDENTITY_RESIDUE_FIELDS,
    LOGIN_ARTIFACT_LABELS,
    LOGIN_SETTINGS_FIELDS,
    LogoutArtifacts,
    LogoutCredentialUnreadable,
    LogoutError,
    LogoutPartialRemoval,
    LogoutPermissionDenied,
    LogoutSettingsUnreadable,
    inspect_logout_artifacts,
    remove_logout_artifacts,
)

_ISSUER = "https://issuer.example.test"
_ALL_FIELDS = {
    "identityIssuer": _ISSUER,
    "identityMode": "casdoor",
    "owner": "alice",
    "userId": "subject-1",
    "username": "alice",
}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _seed_login(
    home: Path, *, extra: dict[str, object] | None = None
) -> dict[str, object]:
    settings: dict[str, object] = {**_ALL_FIELDS, "unrelated": {"keep": True}}
    settings.update(extra or {})
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (home / "secrets").mkdir(mode=0o700, exist_ok=True)
    (home / "secrets" / "login.json").write_text(
        json.dumps({"version": 1, "issuer": _ISSUER, "refreshToken": "token-abc"}),
        encoding="utf-8",
    )
    return settings


def _seed_foreign_material(home: Path) -> tuple[Path, Path]:
    agent_credential = home / "agents" / "worker" / "config" / "auth.json"
    agent_credential.parent.mkdir(parents=True)
    agent_credential.write_bytes(b"agent-secret\n")
    device_key = home / "secrets" / "tailcat" / "device-key.json"
    device_key.parent.mkdir(parents=True, exist_ok=True)
    device_key.write_bytes(b"device-secret\n")
    return agent_credential, device_key


def test_labels_are_a_bounded_allowlist() -> None:
    assert LOGIN_ARTIFACT_LABELS == (
        "secrets/login.json",
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
        "settings.username",
    )
    assert len(LOGIN_ARTIFACT_LABELS) == 6
    assert set(LOGIN_SETTINGS_FIELDS) - {"owner"} == set(IDENTITY_RESIDUE_FIELDS)


def test_removes_only_login_owned_artifacts(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    agent_credential, device_key = _seed_foreign_material(home)

    assert inspect_logout_artifacts(home) == LogoutArtifacts(
        credential_present=True,
        casdoor_identity_present=True,
        settings_fields=LOGIN_SETTINGS_FIELDS,
    )
    removed = remove_logout_artifacts(home)

    assert removed == LOGIN_ARTIFACT_LABELS
    assert not (home / "secrets" / "login.json").exists()
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "unrelated": {"keep": True}
    }
    assert stat.S_IMODE((home / "settings.json").stat().st_mode) == 0o600
    assert agent_credential.read_bytes() == b"agent-secret\n"
    assert device_key.read_bytes() == b"device-secret\n"

    # Idempotent: the second call writes nothing and reports nothing removed.
    before = (home / "settings.json").read_bytes()
    assert inspect_logout_artifacts(home).present is False
    assert remove_logout_artifacts(home) == ()
    assert (home / "settings.json").read_bytes() == before


def test_empty_home_creates_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)

    assert inspect_logout_artifacts(home) == LogoutArtifacts(
        credential_present=False, casdoor_identity_present=False, settings_fields=()
    )
    assert inspect_logout_artifacts(home).labels == ()
    assert remove_logout_artifacts(home) == ()
    assert sorted(path.name for path in home.iterdir()) == []


def test_preview_labels_equal_removal_for_a_partial_field_set(tmp_path: Path) -> None:
    """The preview must not over-report fields that are not present."""

    home = _home(tmp_path)
    _seed_login(home)
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    for field in ("identityIssuer", "username"):
        settings.pop(field)
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    artifacts = inspect_logout_artifacts(home)

    assert artifacts.settings_fields == (
        "identityMode",
        "owner",
        "userId",
    )
    assert artifacts.labels == (
        "secrets/login.json",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
    )
    # The preview is exactly what removal reports, and it is canonical.
    assert artifacts.labels == remove_logout_artifacts(home)
    assert set(artifacts.labels) <= set(LOGIN_ARTIFACT_LABELS)


def test_preview_hides_settings_fields_of_a_non_casdoor_home(tmp_path: Path) -> None:
    """Leftover identity keys are only login-owned under a Casdoor identity."""

    home = _home(tmp_path)
    _seed_login(home)
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    settings["identityMode"] = "local-usage"
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    artifacts = inspect_logout_artifacts(home)

    assert artifacts.settings_fields == ()
    assert artifacts.casdoor_identity_present is False
    assert artifacts.labels == ("secrets/login.json",)
    assert remove_logout_artifacts(home) == ("secrets/login.json",)
    remaining = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert remaining["owner"] == "alice"
    assert remaining["identityMode"] == "local-usage"


def test_missing_settings_still_removes_the_credential(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets").mkdir()
    (home / "secrets" / "login.json").write_text("{}", encoding="utf-8")

    assert remove_logout_artifacts(home) == ("secrets/login.json",)
    assert not (home / "secrets" / "login.json").exists()


def test_credential_bytes_are_never_parsed(tmp_path: Path) -> None:
    """Garbage at the credential path is removed, not decoded."""

    home = _home(tmp_path)
    (home / "secrets").mkdir()
    (home / "secrets" / "login.json").write_bytes(b"\x00not-json-refresh-token")

    assert remove_logout_artifacts(home) == ("secrets/login.json",)
    assert not (home / "secrets" / "login.json").exists()


def test_non_casdoor_identity_keeps_every_settings_field(tmp_path: Path) -> None:
    home = _home(tmp_path)
    settings = {
        "owner": "local-user",
        "identityMode": "local-usage",
        "identityIssuer": None,
    }
    _seed_login(home)
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    before = (home / "settings.json").read_bytes()

    assert remove_logout_artifacts(home) == ("secrets/login.json",)
    assert (home / "settings.json").read_bytes() == before


def test_only_present_settings_fields_are_reported(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_login(home, extra={"owner": "alice"})
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    for field in ("userId", "username"):
        settings.pop(field)
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    removed = remove_logout_artifacts(home)

    assert removed == (
        "secrets/login.json",
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
    )
    assert set(removed) <= set(LOGIN_ARTIFACT_LABELS)


def test_inspect_labels_preview_without_writing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    snapshot = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }

    labels = inspect_logout_artifacts(home).labels

    assert labels == LOGIN_ARTIFACT_LABELS
    assert {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    } == snapshot


def test_symlink_credential_is_unlinked_and_target_preserved(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text("keep-me", encoding="utf-8")
    (home / "secrets" / "login.json").symlink_to(target)

    assert inspect_logout_artifacts(home).credential_present is True
    assert remove_logout_artifacts(home) == ("secrets/login.json",)

    assert not (home / "secrets" / "login.json").exists()
    assert not (home / "secrets" / "login.json").is_symlink()
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_symlink_to_directory_is_unlinked_without_recursing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets").mkdir()
    target = tmp_path / "real-dir"
    target.mkdir()
    (target / "keep.txt").write_text("keep-me", encoding="utf-8")
    (home / "secrets" / "login.json").symlink_to(target, target_is_directory=True)

    assert remove_logout_artifacts(home) == ("secrets/login.json",)

    assert not (home / "secrets" / "login.json").is_symlink()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep-me"


def test_directory_at_credential_path_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets" / "login.json").mkdir(parents=True)
    (home / "secrets" / "login.json" / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(LogoutCredentialUnreadable) as caught:
        remove_logout_artifacts(home)

    assert caught.value.code == "CREDENTIAL_UNREADABLE"
    assert isinstance(caught.value, LogoutError)
    assert (home / "secrets" / "login.json" / "keep.txt").exists()


def test_directory_at_credential_path_after_settings_reports_partial(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    (home / "secrets" / "login.json").unlink()
    (home / "secrets" / "login.json").mkdir()

    with pytest.raises(LogoutPartialRemoval) as caught:
        remove_logout_artifacts(home)

    error = caught.value
    assert error.code == "PARTIAL_REMOVAL"
    assert error.data["cause"] == "CREDENTIAL_UNREADABLE"
    assert error.removed == LOGIN_ARTIFACT_LABELS[1:]
    assert error.data["removed"] == list(LOGIN_ARTIFACT_LABELS[1:])


@pytest.mark.parametrize("payload", [b"{not json", b"[]", b'"text"'])
def test_malformed_settings_abort_before_any_removal(
    tmp_path: Path, payload: bytes
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    (home / "settings.json").write_bytes(payload)
    credential = home / "secrets" / "login.json"
    before = credential.read_bytes()

    with pytest.raises(LogoutSettingsUnreadable) as caught:
        remove_logout_artifacts(home)
    with pytest.raises(LogoutSettingsUnreadable):
        inspect_logout_artifacts(home)

    assert caught.value.code == "SETTINGS_UNREADABLE"
    assert credential.read_bytes() == before
    assert (home / "settings.json").read_bytes() == payload


def test_unreadable_settings_do_not_leak_credential_bytes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    (home / "settings.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(LogoutSettingsUnreadable) as caught:
        remove_logout_artifacts(home)

    assert "token-abc" not in str(caught.value)
    assert "token-abc" not in json.dumps(caught.value.data)


_NON_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory permission bits"
)


@_NON_ROOT
def test_denied_permission_with_nothing_removed_is_reported(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets").mkdir()
    (home / "secrets" / "login.json").write_text("{}", encoding="utf-8")
    (home / "secrets").chmod(0o500)
    try:
        with pytest.raises(LogoutPermissionDenied) as caught:
            remove_logout_artifacts(home)
    finally:
        (home / "secrets").chmod(0o700)

    assert caught.value.code == "PERMISSION_DENIED"
    assert isinstance(caught.value, LogoutError)
    assert not isinstance(caught.value, LogoutPartialRemoval)
    assert (home / "secrets" / "login.json").exists()


@_NON_ROOT
def test_denied_permission_after_settings_removal_reports_partial(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    (home / "secrets").chmod(0o500)
    try:
        with pytest.raises(LogoutPartialRemoval) as caught:
            remove_logout_artifacts(home)
    finally:
        (home / "secrets").chmod(0o700)

    error = caught.value
    assert error.code == "PARTIAL_REMOVAL"
    assert error.data["cause"] == "PERMISSION_DENIED"
    # The settings rewrite already happened; report it truthfully, not silently.
    assert error.removed == LOGIN_ARTIFACT_LABELS[1:]
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "unrelated": {"keep": True}
    }
    assert (home / "secrets" / "login.json").exists()
