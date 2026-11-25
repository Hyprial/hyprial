"""Tests for ``read_home_binding``, stat-only legacy detection, and reader isolation."""

from __future__ import annotations

import builtins
import inspect
import os
import sqlite3
from pathlib import Path

import pytest

from hyprial.identity import (
    HOME_ACCOUNT_MIGRATION_GUIDE,
    IDENTITY_RESIDUE_FIELDS,
    LEGACY_BUSINESS_DATABASES,
    LEGACY_BUSINESS_TEXT_FILES,
    LEGACY_DEVICE_STATE_FILES,
    LEGACY_UNBOUND_REASON,
    LEGACY_USER_KEYS_DIR,
    LOGIN_SETTINGS_FIELDS,
    HomeBinding,
    IdentityRecordError,
    home_account_conflict_message,
    legacy_business_data_present,
    node_owner_or_none,
    read_home_binding,
    resolve_node_owner,
    resolve_node_user_id,
)
from hyprial.identity.impl.onboarding import identity_record

from .support import (
    OTHER_ISSUER,
    OTHER_UID,
    PRIMARY_ISSUER,
    PRIMARY_OWNER,
    PRIMARY_UID,
    create_home,
    populate_legacy_item,
    write_settings_json,
)

AUXILIARY_SUFFIXES = ("", "-wal", "-shm")


def test_read_home_binding_prefers_home_account_and_normalizes_issuer(
    tmp_path: Path,
) -> None:
    """``settings.homeAccount`` outranks active login fields and strips trailing slash."""

    home = create_home(tmp_path)
    assert read_home_binding(home) is None

    write_settings_json(
        home,
        {
            "owner": "ignored-owner",
            "userId": OTHER_UID,
            "identityIssuer": OTHER_ISSUER,
            "homeAccount": {
                "issuer": f"{PRIMARY_ISSUER}/",
                "userId": PRIMARY_UID,
                "owner": PRIMARY_OWNER,
            },
        },
    )
    assert read_home_binding(home) == HomeBinding(
        issuer=PRIMARY_ISSUER,
        user_id=PRIMARY_UID,
        owner=PRIMARY_OWNER,
    )


def test_read_home_binding_falls_back_to_active_login_fields_when_unbound(
    tmp_path: Path,
) -> None:
    """Pre-L3 logged-in homes expose their active login binding until rewritten."""

    home = create_home(tmp_path)
    write_settings_json(
        home,
        {
            "owner": PRIMARY_OWNER,
            "identityMode": "casdoor",
            "identityIssuer": f"{PRIMARY_ISSUER}/",
            "userId": PRIMARY_UID,
        },
    )
    assert read_home_binding(home) == HomeBinding(
        issuer=PRIMARY_ISSUER,
        user_id=PRIMARY_UID,
        owner=PRIMARY_OWNER,
    )


@pytest.mark.parametrize(
    "bad_home_account",
    [
        "not-an-object",
        {"issuer": "", "userId": PRIMARY_UID, "owner": PRIMARY_OWNER},
        {"issuer": PRIMARY_ISSUER, "userId": " ", "owner": PRIMARY_OWNER},
        {"issuer": PRIMARY_ISSUER, "userId": PRIMARY_UID, "owner": ""},
        {"issuer": PRIMARY_ISSUER, "userId": PRIMARY_UID, "owner": "bad:owner"},
        {
            "issuer": PRIMARY_ISSUER,
            "userId": PRIMARY_UID,
            "owner": "u.ou_" + "a" * 32,
        },
    ],
)
def test_read_home_binding_refuses_corrupt_home_account_shapes(
    tmp_path: Path, bad_home_account: object
) -> None:
    """Malformed ``homeAccount`` entries raise ``IdentityRecordError`` fail-closed."""

    home = create_home(tmp_path)
    write_settings_json(home, {"homeAccount": bad_home_account})
    with pytest.raises(IdentityRecordError) as raised:
        read_home_binding(home)
    assert raised.value.code == "IDENTITY_METADATA_INVALID"


def test_clean_home_without_legacy_files_reports_false_even_with_empty_user_keys_dir(
    tmp_path: Path,
) -> None:
    """Identity config/credentials and empty directories do not count as legacy data."""

    home = create_home(tmp_path)
    write_settings_json(home, {"version": 1})
    populate_legacy_item(home, "secrets/login.json", b"{}")
    (home / LEGACY_USER_KEYS_DIR).mkdir(parents=True, exist_ok=True)
    assert legacy_business_data_present(home) is False


@pytest.mark.parametrize(
    "relpath",
    [
        *(
            f"state/{db}{suffix}"
            for db in LEGACY_BUSINESS_DATABASES
            for suffix in AUXILIARY_SUFFIXES
        ),
        *(f"state/{filename}" for filename in LEGACY_BUSINESS_TEXT_FILES),
        *LEGACY_DEVICE_STATE_FILES,
        f"{LEGACY_USER_KEYS_DIR}/alice/key.json",
    ],
)
def test_legacy_business_data_present_detects_each_counted_item_via_stat_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relpath: str
) -> None:
    """Every counted artifact triggers ``True`` without opening files or SQLite."""

    home = create_home(tmp_path)
    populate_legacy_item(home, relpath)

    def forbid_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy detector must never open files or SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbid_open)
    monkeypatch.setattr(builtins, "open", forbid_open)
    monkeypatch.setattr(Path, "open", forbid_open)
    assert legacy_business_data_present(home) is True


def test_legacy_detector_fails_closed_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PermissionError or OSError on lstat/scandir must fail closed as ``True``."""

    home = create_home(tmp_path)

    def raise_permission(_self: Path) -> object:
        raise PermissionError("stat blocked")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", raise_permission)
        assert legacy_business_data_present(home) is True

    (home / LEGACY_USER_KEYS_DIR).mkdir(parents=True, exist_ok=True)

    def raise_scandir(_path: Path) -> object:
        raise OSError("scandir blocked")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", raise_scandir)
        assert legacy_business_data_present(home) is True


def test_home_account_is_excluded_from_logout_and_node_identity_readers(
    tmp_path: Path,
) -> None:
    """``homeAccount`` never leaks into node owner/uid resolution or logout/residue sets."""

    assert "homeAccount" not in LOGIN_SETTINGS_FIELDS
    assert "homeAccount" not in IDENTITY_RESIDUE_FIELDS

    for reader in (resolve_node_owner, node_owner_or_none, resolve_node_user_id):
        assert "homeAccount" not in inspect.getsource(reader)
    assert "homeAccount" not in inspect.getsource(identity_record._settings_owner)
    assert "homeAccount" not in inspect.getsource(identity_record._settings_user_id)
    assert "homeAccount" not in inspect.getsource(identity_record._strict_snapshot)

    home = create_home(tmp_path)
    write_settings_json(
        home,
        {
            "homeAccount": {
                "issuer": PRIMARY_ISSUER,
                "userId": PRIMARY_UID,
                "owner": PRIMARY_OWNER,
            }
        },
    )
    env = {"HYPRIAL_HOME": str(home)}
    assert node_owner_or_none(env, hyprial_home=home) is None
    assert resolve_node_user_id(env, hyprial_home=home) is None
    with pytest.raises(ValueError, match="user identity is not set"):
        resolve_node_owner(env, hyprial_home=home)


def test_migration_guide_constant_and_messages_pin_contract() -> None:
    """Migration guide path constant and Chinese prompt match L3 contract."""

    assert HOME_ACCOUNT_MIGRATION_GUIDE == "docs/guides/migration-account-binding.md"
    message = home_account_conflict_message(LEGACY_UNBOUND_REASON)
    assert "没有账号绑定记录" in message
    assert HOME_ACCOUNT_MIGRATION_GUIDE in message
