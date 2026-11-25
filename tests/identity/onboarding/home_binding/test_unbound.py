"""Tests for unbound homes with and without legacy business data."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity import (
    HOME_ACCOUNT_MIGRATION_GUIDE,
    LEGACY_UNBOUND_REASON,
    home_account_conflict_message,
)

from .support import (
    PERMIT_LEDGER_NAME,
    PRIMARY_OWNER,
    PRIMARY_UID,
    create_home,
    expected_home_account,
    populate_legacy_item,
    read_json,
    run_oidc_login,
    snapshot_bytes,
    write_settings_json,
)


def test_unbound_home_with_legacy_data_refuses_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy data on an unbound home raises ``HOME_ACCOUNT_CONFLICT`` before permit burn."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    write_settings_json(home, {"version": 1})
    populate_legacy_item(home, "state/cells.sqlite3-wal", b"wal-bytes")
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
        operation_id="op-unbound-legacy",
        bootstrap=True,
    )

    assert state.state == "failed"
    assert state.error_code == "HOME_ACCOUNT_CONFLICT"
    assert state.error_message == home_account_conflict_message(LEGACY_UNBOUND_REASON)
    assert state.receipt == {
        "reason": LEGACY_UNBOUND_REASON,
        "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
        "loginUserId": PRIMARY_UID,
        "issuer": normalized,
    }
    assert snapshot_bytes(watched) == before


def test_unbound_clean_home_first_login_writes_home_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh home without legacy state succeeds and establishes ``settings.homeAccount``."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)

    state, normalized = run_oidc_login(
        home,
        sub=PRIMARY_UID,
        username=PRIMARY_OWNER,
        operation_id="op-fresh-first",
        bootstrap=True,
    )

    assert state.state == "committed"
    assert state.owner == PRIMARY_OWNER
    assert state.issuer == normalized
    assert state.user_id == PRIMARY_UID
    settings = read_json(home / "settings.json")
    assert settings["owner"] == PRIMARY_OWNER
    assert settings["username"] == PRIMARY_OWNER
    assert settings["userId"] == PRIMARY_UID
    assert settings["homeAccount"] == expected_home_account(
        normalized, PRIMARY_UID, PRIMARY_OWNER
    )
    assert (home / "secrets" / "login.json").is_file()
