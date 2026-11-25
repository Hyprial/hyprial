"""D2 tests: same-uid re-login preserves bound ``owner`` while updating ``username``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hyprial.identity import (
    AuthProfile,
    CommitPermit,
    IDENTITY_RESIDUE_FIELDS,
    IdentityError,
    IdentitySnapshot,
    LEGACY_BUSINESS_DATABASES,
    LEGACY_BUSINESS_TEXT_FILES,
    LEGACY_DEVICE_STATE_FILES,
    LEGACY_USER_KEYS_DIR,
    read_home_binding,
    read_settings_username,
    remove_logout_artifacts,
)
from hyprial.identity.impl.onboarding.credentials.flow import commit_identity
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

from .support import (
    CLIENT_ID,
    PERMIT_LEDGER_NAME,
    PRIMARY_OWNER,
    PRIMARY_UID,
    RENAMED_OWNER,
    create_home,
    expected_home_account,
    populate_legacy_item,
    read_json,
    run_oidc_against_issuer,
    snapshot_bytes,
    write_settings_json,
)


def _seed_business_tree(home: Path) -> list[Path]:
    seeded: list[Path] = []
    for db in LEGACY_BUSINESS_DATABASES:
        seeded.append(populate_legacy_item(home, f"state/{db}", f"db-{db}".encode()))
    for name in LEGACY_BUSINESS_TEXT_FILES:
        seeded.append(populate_legacy_item(home, f"state/{name}", f"text-{name}".encode()))
    for relpath in LEGACY_DEVICE_STATE_FILES:
        seeded.append(populate_legacy_item(home, relpath, f"dev-{relpath}".encode()))
    seeded.append(
        populate_legacy_item(
            home,
            f"{LEGACY_USER_KEYS_DIR}/{PRIMARY_OWNER}/key.json",
            json.dumps({"owner": PRIMARY_OWNER, "casdoorSub": PRIMARY_UID}).encode(),
        )
    )
    return seeded


def test_same_uid_rename_relogin_preserves_owner_updates_username_and_leaves_stores_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-login with the same issuer+sub under a new username preserves ``owner`` (D2)."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    seeded = _seed_business_tree(home)
    config = FakeOidcConfig(
        pending_polls=0,
        sub=PRIMARY_UID,
        preferred_username=RENAMED_OWNER,
    )
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "username": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
                "homeAccount": {
                    "issuer": f"{server.issuer}/",
                    "userId": PRIMARY_UID,
                    "owner": PRIMARY_OWNER,
                },
            },
        )
        before = snapshot_bytes(seeded)

        state, normalized = run_oidc_against_issuer(
            home,
            server.issuer,
            operation_id="op-same-uid-rename",
            bootstrap=False,
        )

    assert state.state == "committed"
    assert state.owner == PRIMARY_OWNER
    assert state.user_id == PRIMARY_UID
    assert read_settings_username(home) == RENAMED_OWNER
    settings = read_json(home / "settings.json")
    assert settings["owner"] == PRIMARY_OWNER
    assert settings["username"] == RENAMED_OWNER
    assert settings["userId"] == PRIMARY_UID
    assert settings["homeAccount"] == expected_home_account(
        normalized, PRIMARY_UID, PRIMARY_OWNER
    )
    assert snapshot_bytes(seeded) == before


def test_same_uid_rename_after_logout_preserves_home_account_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logout keeps ``homeAccount``; subsequent same-uid login preserves its ``owner``."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    seeded = _seed_business_tree(home)
    config = FakeOidcConfig(
        pending_polls=0,
        sub=PRIMARY_UID,
        preferred_username=RENAMED_OWNER,
    )
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "username": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        populate_legacy_item(
            home, "secrets/login.json", b'{"version":1,"refreshToken":"old"}'
        )

        removed = remove_logout_artifacts(home)
        assert "secrets/login.json" in removed
        after_logout = read_json(home / "settings.json")
        assert "owner" not in after_logout
        assert "userId" not in after_logout
        for field in IDENTITY_RESIDUE_FIELDS:
            assert field not in after_logout
        assert after_logout["homeAccount"] == expected_home_account(
            server.issuer, PRIMARY_UID, PRIMARY_OWNER
        )

        before = snapshot_bytes(seeded)
        state, normalized = run_oidc_against_issuer(
            home,
            server.issuer,
            operation_id="op-post-logout-rename",
            bootstrap=True,
        )

    assert state.state == "committed"
    assert state.owner == PRIMARY_OWNER
    re_logged = read_json(home / "settings.json")
    assert re_logged["owner"] == PRIMARY_OWNER
    assert re_logged["username"] == RENAMED_OWNER
    assert re_logged["homeAccount"] == expected_home_account(
        normalized, PRIMARY_UID, PRIMARY_OWNER
    )
    assert snapshot_bytes(seeded) == before


def test_same_uid_rename_checks_hyprial_owner_against_bound_owner(
    tmp_path: Path,
) -> None:
    """``HYPRIAL_OWNER`` comparison under D2 validates against ``binding.owner``."""

    home = create_home(tmp_path)
    issuer = "https://override.example.test"
    profile = AuthProfile(issuer=issuer, client_id=CLIENT_ID)
    write_settings_json(
        home,
        {
            "owner": PRIMARY_OWNER,
            "username": PRIMARY_OWNER,
            "identityMode": "casdoor",
            "identityIssuer": issuer,
            "userId": PRIMARY_UID,
            "homeAccount": expected_home_account(issuer, PRIMARY_UID, PRIMARY_OWNER),
        },
    )
    ledger = home / PERMIT_LEDGER_NAME
    permit = CommitPermit(
        operation_id="op-override-bad",
        nonce="nonce-bad",
        home=home,
        issuer=profile.issuer,
        client_id=profile.client_id,
        expected_identity=IdentitySnapshot(PRIMARY_OWNER, "casdoor", issuer, PRIMARY_UID),
        expires_at=int(os.times().elapsed) + 2_000_000_000,
        bootstrap=False,
        home_binding=read_home_binding(home),
        legacy_business_data=True,
    )
    with pytest.raises(IdentityError) as conflict:
        commit_identity(
            permit=permit,
            profile=profile,
            home=home,
            owner=RENAMED_OWNER,
            subject=PRIMARY_UID,
            refresh_token="rt-new",
            environ={"HYPRIAL_OWNER": RENAMED_OWNER},
            now_epoch=1,
        )
    assert conflict.value.code == "OWNER_OVERRIDE_CONFLICT"
    assert not ledger.exists()

    receipt = commit_identity(
        permit=permit,
        profile=profile,
        home=home,
        owner=RENAMED_OWNER,
        subject=PRIMARY_UID,
        refresh_token="rt-new",
        environ={"HYPRIAL_OWNER": PRIMARY_OWNER},
        now_epoch=1,
    )
    assert receipt == {"credentialCommitted": True, "settingsCommitted": True}
    assert read_json(home / "settings.json")["username"] == RENAMED_OWNER
    assert read_json(home / "settings.json")["owner"] == PRIMARY_OWNER


def test_logged_in_pre_l3_home_relogin_writes_home_account_from_live_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logged-in home without ``homeAccount`` binds on its first same-uid relogin (KP-121)."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    seeded = _seed_business_tree(home)
    config = FakeOidcConfig(
        pending_polls=0,
        sub=PRIMARY_UID,
        preferred_username=PRIMARY_OWNER,
    )
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
            },
        )
        before = snapshot_bytes(seeded)

        state, normalized = run_oidc_against_issuer(
            home,
            server.issuer,
            operation_id="op-pre-l3-relogin",
            bootstrap=False,
        )

    assert state.state == "committed"
    assert state.owner == PRIMARY_OWNER
    settings = read_json(home / "settings.json")
    assert settings["owner"] == PRIMARY_OWNER
    assert settings["homeAccount"] == expected_home_account(
        normalized, PRIMARY_UID, PRIMARY_OWNER
    )
    assert snapshot_bytes(seeded) == before
