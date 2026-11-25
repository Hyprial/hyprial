"""Tests rejecting different-uid login with legacy data while allowing clean rebind."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity import (
    DIFFERENT_ACCOUNT_REASON,
    HOME_ACCOUNT_MIGRATION_GUIDE,
    LEGACY_BUSINESS_DATABASES,
    LEGACY_BUSINESS_TEXT_FILES,
    LEGACY_DEVICE_STATE_FILES,
    LEGACY_USER_KEYS_DIR,
    home_account_conflict_message,
)
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

from .support import (
    OTHER_ISSUER,
    OTHER_UID,
    PERMIT_LEDGER_NAME,
    PRIMARY_OWNER,
    PRIMARY_UID,
    create_home,
    expected_home_account,
    populate_legacy_item,
    read_json,
    run_oidc_against_issuer,
    run_oidc_login,
    snapshot_bytes,
    write_settings_json,
)

AUXILIARY_SUFFIXES = ("", "-wal", "-shm")


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
        f"{LEGACY_USER_KEYS_DIR}/{PRIMARY_OWNER}/key.json",
    ],
)
def test_different_uid_with_any_legacy_item_refuses_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relpath: str
) -> None:
    """Every counted legacy storage triggers ``HOME_ACCOUNT_CONFLICT`` before permit burn."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    populate_legacy_item(home, relpath)
    config = FakeOidcConfig(pending_polls=0, sub=OTHER_UID, preferred_username="bob")
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        watched = [
            home / "settings.json",
            home / "secrets" / "login.json",
            home / PERMIT_LEDGER_NAME,
        ]
        before = snapshot_bytes(watched)

        state, normalized = run_oidc_against_issuer(
            home,
            server.issuer,
            operation_id="op-diff-uid",
            bootstrap=True,
        )

    assert state.state == "failed"
    assert state.error_code == "HOME_ACCOUNT_CONFLICT"
    assert state.error_message == home_account_conflict_message(DIFFERENT_ACCOUNT_REASON)
    assert state.receipt == {
        "reason": DIFFERENT_ACCOUNT_REASON,
        "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
        "loginUserId": OTHER_UID,
        "issuer": normalized,
    }
    assert PRIMARY_OWNER not in str(state.receipt)
    assert PRIMARY_UID not in str(state.receipt)
    assert snapshot_bytes(watched) == before


def test_different_issuer_same_sub_with_legacy_data_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account equality requires matching both normalized issuer and ``userId``."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    write_settings_json(
        home,
        {
            "version": 1,
            "homeAccount": expected_home_account(
                OTHER_ISSUER, PRIMARY_UID, PRIMARY_OWNER
            ),
        },
    )
    populate_legacy_item(home, "state/agents.sqlite3")
    watched = [
        home / "settings.json",
        home / "secrets" / "login.json",
        home / PERMIT_LEDGER_NAME,
    ]
    before = snapshot_bytes(watched)

    state, normalized = run_oidc_login(
        home,
        sub=PRIMARY_UID,
        username=PRIMARY_OWNER,
        operation_id="op-diff-issuer",
        bootstrap=True,
    )

    assert state.state == "failed"
    assert state.error_code == "HOME_ACCOUNT_CONFLICT"
    assert state.receipt["reason"] == DIFFERENT_ACCOUNT_REASON
    assert state.receipt["issuer"] == normalized
    assert snapshot_bytes(watched) == before


def test_different_uid_without_any_legacy_data_rewrites_home_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-serving bound home with zero legacy files admits the new account cleanly."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    write_settings_json(
        home,
        {
            "version": 1,
            "homeAccount": expected_home_account(
                OTHER_ISSUER, PRIMARY_UID, PRIMARY_OWNER
            ),
        },
    )

    state, normalized = run_oidc_login(
        home,
        sub=OTHER_UID,
        username="bob",
        operation_id="op-clean-rebind",
        bootstrap=True,
    )

    assert state.state == "committed"
    assert state.owner == "bob"
    settings = read_json(home / "settings.json")
    assert settings["owner"] == "bob"
    assert settings["username"] == "bob"
    assert settings["userId"] == OTHER_UID
    assert settings["homeAccount"] == expected_home_account(normalized, OTHER_UID, "bob")
    assert (home / "secrets" / "login.json").is_file()


def test_logged_in_different_uid_without_legacy_data_still_refuses_owner_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-logged-in home refuses another uid by owner even with zero counted files.

    The clean rebind above only applies once the old account has logged out;
    while ``settings.owner`` is present the owner gate wins and nothing is written.
    """

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    first, _ = run_oidc_login(
        home,
        sub=PRIMARY_UID,
        username=PRIMARY_OWNER,
        operation_id="op-first-account",
        bootstrap=True,
    )
    assert first.state == "committed"
    watched = [
        home / "settings.json",
        home / "secrets" / "login.json",
        home / PERMIT_LEDGER_NAME,
    ]
    before = snapshot_bytes(watched)

    state, _ = run_oidc_login(
        home,
        sub=OTHER_UID,
        username="bob",
        operation_id="op-second-account",
        bootstrap=False,
    )

    assert state.state == "failed"
    assert state.error_code == "OWNER_MISMATCH"
    assert state.receipt.get("currentOwner") == PRIMARY_OWNER
    assert snapshot_bytes(watched) == before
