"""Login projects no external-account claim into settings (tier 2, T2-A).

Allen, 2026-11-19: an adapter is bound to an agent, not to the person, and an
IM sender is simply whoever owns that IM account.  So login no longer copies
the owner's IM union id into ``settings.json`` ``identityClaims``: there is no
reader that judges "this IM account is the owner" any more.  A key left by an
earlier version is neither read nor cleaned up (the migration guide says how
to remove it by hand), and it is not an identity residue field.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import hyprial.identity as identity_face
from hyprial.identity import interface
from hyprial.identity.impl.onboarding import identity_record
from hyprial.identity.impl.onboarding.identity_record import (
    IDENTITY_RESIDUE_FIELDS,
    write_settings_identity,
    write_settings_owner,
)
from hyprial.identity.impl.onboarding.logout import LOGIN_SETTINGS_FIELDS

STALE = {"properties": {"oauth_Lark_unionId": "on_allen"}}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def test_no_projection_is_exported_or_threaded_through_the_writers() -> None:
    assert "external_account_claims" not in interface.__all__
    assert not hasattr(identity_face, "external_account_claims")
    assert not hasattr(identity_face, "EXTERNAL_ACCOUNT_CLAIM_KEYS")
    for writer in (write_settings_identity, write_settings_owner):
        assert "external_claims" not in inspect.signature(writer).parameters
    assert "identityClaims" not in inspect.getsource(identity_record)


def test_identity_commit_writes_no_claim(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "alicesmith",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="ou_1",
        username="alicesmith",
        hyprial_home=home,
    )
    record = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "identityClaims" not in record
    assert record["owner"] == "alicesmith"


def test_a_stale_claim_is_neither_read_nor_cleaned(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text(
        json.dumps({"version": 3, "identityClaims": STALE}), encoding="utf-8"
    )
    write_settings_owner("alicesmith", user_id="ou_1", hyprial_home=home)
    record = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert record["identityClaims"] == STALE
    assert record["owner"] == "alicesmith"


def test_the_claim_is_not_an_identity_residue_or_a_logout_field() -> None:
    assert "identityClaims" not in IDENTITY_RESIDUE_FIELDS
    assert "identityClaims" not in LOGIN_SETTINGS_FIELDS
