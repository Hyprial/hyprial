"""login: persistence scenarios."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.identity import (
    resolve_node_owner,
    write_settings_owner,
)
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.shell.impl.login.flow import (
    CREDENTIAL_VERSION,
    LoginError,
    credential_path,
    read_login_credential,
    refresh_access_token,
)
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402

from .support import (
    _home,
    _profile_for,
    _run,
    _snapshot,
)

# -- T6 / immutable ids P1: username and OIDC subject are persisted ---------------


def test_t6_three_fields_separate_owner_takes_username(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        preferred_username="alicesmith",
        sub="ou_9876543210abcdef9876543210abcdef",
        userinfo_email=True,
        email="aaron@not-the-owner.example",
    )
    with fake_oidc(config) as server:
        result = _run(server, home)
    assert result.owner == "alicesmith"
    assert result.subject == "ou_9876543210abcdef9876543210abcdef"
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["owner"] == "alicesmith"
    assert settings["username"] == "alicesmith"
    assert settings["userId"] == "ou_9876543210abcdef9876543210abcdef"


def test_login_projects_only_the_lark_union_id_claim(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(lark_union_id="on_allen")
    with fake_oidc(config) as server:
        _run(server, home)
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["identityClaims"] == {
        "properties": {"oauth_Lark_unionId": "on_allen"}
    }
    credential = json.loads(
        (home / "secrets" / "login.json").read_text(encoding="utf-8")
    )
    assert "identityClaims" not in credential


def test_login_projects_the_union_id_from_the_access_jwt(tmp_path: Path) -> None:
    home = _home(tmp_path)
    token = (
        "e30."
        "eyJwcm9wZXJ0aWVzIjp7Im9hdXRoX0xhcmtfdW5pb25JZCI6Im9uX2p3dCJ9fQ."
        "fixture-signature"
    )
    with fake_oidc(FakeOidcConfig(access_token=token)) as server:
        _run(server, home)
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["identityClaims"] == {
        "properties": {"oauth_Lark_unionId": "on_jwt"}
    }


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("ou_" + "7d18a56e2fa34b31" * 2, id="feishu-open-id"),
        pytest.param("6f8b1d21-33a8-4688-a32e-b1b23e87d5d9", id="uuid"),
    ],
)
def test_user_id_shapes_persist_verbatim_and_relogin_is_idempotent(
    tmp_path: Path, subject: str
) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(sub=subject)
    with fake_oidc(config) as server:
        _run(server, home)
        first = (home / "settings.json").read_bytes()
        _run(server, home)
    assert (home / "settings.json").read_bytes() == first
    settings = json.loads(first)
    assert settings["userId"] == subject
    assert settings["username"] == "alicesmith"


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("", id="empty"),
        pytest.param("bad:subject", id="colon"),
        pytest.param("bad subject", id="space"),
        pytest.param("bad\nsubject", id="whitespace-control"),
        pytest.param("bad\x00subject", id="non-whitespace-control"),
    ],
)
def test_invalid_user_id_fails_before_any_home_write(
    tmp_path: Path, subject: str
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(sub=subject)) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code in {"USER_ID_MISSING", "USER_ID_INVALID"}
    assert _snapshot(home) == {}


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("on_" + "b" * 32, id="union-id"),
        pytest.param(
            "6F8B1D21-33A8-4688-A32E-B1B23E87D5D9", id="uppercase-uuid"
        ),
        pytest.param("b" * 32, id="unprefixed-hex"),
    ],
)
def test_unrecognized_user_id_shape_fails_before_any_home_write(
    tmp_path: Path, subject: str
) -> None:
    home = _home(tmp_path)
    # If the subject-shape check disappears, this same string is a legal
    # owner name and the login writes it, proving the P3 collision directly.
    config = FakeOidcConfig(preferred_username=subject, sub=subject)
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "USER_ID_INVALID"
    assert "identity provider returned an id format" in str(caught.value)
    assert "administrator" in str(caught.value)
    assert "predicate" in str(caught.value)
    assert _snapshot(home) == {}


@pytest.mark.parametrize(
    "owner",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        # Case-insensitive reservation (APFS): uppercase variants are refused too.
        "U.OU_" + "B" * 32,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_login_rejects_exact_identity_id_shapes_without_writes(
    tmp_path: Path, owner: str
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(preferred_username=owner)) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "OWNER_INVALID"
    assert _snapshot(home) == {}


@pytest.mark.parametrize("owner", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_login_accepts_id_prefix_lookalikes(tmp_path: Path, owner: str) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(preferred_username=owner)) as server:
        _run(server, home)
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == owner


def test_t6_missing_preferred_username_terminates_with_zero_writes(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(preferred_username=None, userinfo_email=True)
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "USERNAME_MISSING"
    assert _snapshot(home) == {}


def test_t6_scope_without_profile_yields_no_username(tmp_path: Path) -> None:
    """Casdoor fills preferred_username only under a profile-containing
    scope (object/user.go:1246-1249): a client that dropped 'profile' from
    the scope gets no username and login must refuse, not guess."""

    home = _home(tmp_path)
    with fake_oidc() as server:
        import hyprial.shell.impl.login.oidc as login_oidc_module

        original = login_oidc_module.LOGIN_SCOPE
        login_oidc_module.LOGIN_SCOPE = "openid offline_access"
        try:
            with pytest.raises(LoginError) as caught:
                _run(server, home)
        finally:
            login_oidc_module.LOGIN_SCOPE = original
    assert caught.value.code == "USERNAME_MISSING"


# -- T7: HYPRIAL_OWNER conflict --------------------------------------------------------


def test_t7_override_conflict_refuses_zero_writes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, env={"HYPRIAL_OWNER": "someone-else"})
    assert caught.value.code == "OWNER_OVERRIDE_CONFLICT"
    assert caught.value.data == {"override": "someone-else", "loginOwner": "alicesmith"}
    assert _snapshot(home) == {}


def test_t7_override_same_value_still_writes_settings(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home, env={"HYPRIAL_OWNER": "alicesmith"})
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"


# -- T8: a different existing owner is detected and refused ------------------------


def test_t8_existing_owner_mismatch_refuses_zero_writes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_owner("old-owner", environ={"HYPRIAL_HOME": str(home)})
    before = _snapshot(home)
    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "OWNER_MISMATCH"
    assert caught.value.data["flag"] == "--switch-account"
    assert "--switch-account" in str(caught.value)
    assert "old-owner" in str(caught.value) and "alicesmith" in str(caught.value)
    assert _snapshot(home) == before


def test_t8_switch_account_with_daemon_stopped_switches(tmp_path: Path) -> None:
    'U5 replaced the U2 ``SWITCH_NOT_IMPLEMENTED`` stub: with the daemon\n    stopped (no live heartbeat) the switch writes — credential first, owner\n    second.  The full gate matrix lives in ``tests/shell/login/test_login_u5.py``.'

    home = _home(tmp_path)
    write_settings_owner("old-owner", environ={"HYPRIAL_HOME": str(home)})
    with fake_oidc() as server:
        result = _run(server, home, switch_account=True, assume_yes=True)
    assert result.switched is True
    assert result.previous_owner == "old-owner"
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"


def test_t8_same_owner_relogin_is_allowed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_owner("alicesmith", environ={"HYPRIAL_HOME": str(home)})
    with fake_oidc() as server:
        result = _run(server, home)
    assert result.owner == "alicesmith"


# -- T9: credential first, owner second; idempotent re-login ------------------------


def test_t9_owner_write_failure_leaves_credential_and_errors(tmp_path: Path) -> None:
    home = _home(tmp_path)

    def failing_write_owner(*_args: object, **_kwargs: object) -> Path:
        raise ValueError("disk exploded")

    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, write_owner=failing_write_owner)
    assert caught.value.code == "OWNER_WRITE_FAILED"
    # One-way commit (design §2.2 step 8): the credential stays written…
    credential = read_login_credential({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    assert credential.issuer == server.issuer
    assert credential.refresh_token == "fake-refresh-token-1"
    # …settings was not touched…
    assert not (home / "settings.json").exists()
    # …and a re-run of the same login completes the commit.
    with fake_oidc() as again:
        _run(again, home)
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"


def test_t9_same_value_second_run_leaves_settings_bytes_identical(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    write_settings_owner("alicesmith", environ={"HYPRIAL_HOME": str(home)})
    settings = home / "settings.json"
    settings.write_text(
        json.dumps({"updateTrack": "dev", "owner": "alicesmith"}, indent=2) + "\n",
        encoding="utf-8",
    )
    with fake_oidc() as server:
        _run(server, home)
        upgraded = settings.read_bytes()
        _run(server, home)
    assert settings.read_bytes() == upgraded
    assert json.loads(upgraded)["userId"] == server.config.sub


def test_t9_credential_write_restores_original_bytes_on_failure(
    tmp_path: Path,
) -> None:
    """The adapter_registration pattern for the credential file itself: a
    failed atomic write restores the prior bytes instead of half-writing."""

    import hyprial.shell.impl.login.flow as login_module
    from hyprial.kernel import atomic_json_write as real_write

    home = _home(tmp_path)
    target = credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    target.parent.mkdir(parents=True, mode=0o700)
    original = (
        b'{"issuer": "https://old.example", "obtainedAt": "t", '
        b'"refreshToken": "keep", "version": 1}\n'
    )
    target.write_bytes(original)

    def exploding_write(path: Path, value: object) -> None:
        if path.name == "login.json":
            raise OSError("no space left on device")
        real_write(path, value)

    with fake_oidc() as server:
        real = login_module.atomic_json_write
        login_module.atomic_json_write = exploding_write
        try:
            with pytest.raises(OSError):
                _run(server, home)
        finally:
            login_module.atomic_json_write = real
    assert target.read_bytes() == original
    assert not (home / "settings.json").exists()


def test_t9_credential_file_shape_and_mode(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)
    target = credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, f"credential must be 0600; got {oct(mode)}"
    record = json.loads(target.read_text(encoding="utf-8"))
    assert set(record) == {
        "version", "issuer", "refreshToken", "obtainedAt", "identityGeneration",
    }
    assert record["identityGeneration"] == 1  # identity-commit counter, no secret
    assert record["version"] == CREDENTIAL_VERSION
    assert record["refreshToken"] == "fake-refresh-token-1"
    # ⚠️ the access token never reaches disk (T10's persistence half)
    assert "fake-access-token-1" not in target.read_text(encoding="utf-8")


def test_t9_settings_file_is_atomic_mode_0600(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)

    settings = home / "settings.json"
    assert settings.stat().st_mode & 0o777 == 0o600
    assert json.loads(settings.read_text(encoding="utf-8"))["owner"] == "alicesmith"


# -- T10: no secret ever reaches stdout, stderr, logs, or the wrong file ------------


def test_t10_no_secrets_in_emitted_events_or_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []

    def emit(kind: str, data: dict) -> None:
        events.append((kind, data))

    config = FakeOidcConfig()
    with fake_oidc(config) as server:
        _run(server, home, emit=emit)

    secret_values = [
        config.device_code,
        config.access_token,
        config.refresh_token,
        config.rotated_refresh_token,
        config.id_token,
    ]
    for kind, data in events:
        rendered = json.dumps(data)
        for secret in secret_values:
            assert secret not in rendered, (kind, secret)
    assert "--switch-account" not in json.dumps(dict(events))
    # The whole home (settings + credential + any state/logs) only ever
    # contains the refresh token, in login.json, nowhere else.
    for path in home.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            for secret in secret_values:
                if secret == config.refresh_token and path.name == "login.json":
                    continue
                assert secret not in content, (path, secret)


def test_t10_log_redaction_still_scrubs_login_shaped_payloads() -> None:
    """login itself never logs; the shared redaction boundary must still
    eat token-shaped values if any component ever logs near one."""

    from hyprial.kernel.impl.log import redact

    config = FakeOidcConfig()
    scrubbed = redact(
        {
            "event": "login",
            "access_token": config.access_token,
            "refreshToken": config.refresh_token,
            "url": f"https://x.example/callback?code={config.device_code}",
        }
    )
    rendered = json.dumps(scrubbed)
    for secret in (config.access_token, config.refresh_token, config.device_code):
        assert secret not in rendered
    assert scrubbed["access_token"] == "[REDACTED]"
    assert scrubbed["refreshToken"] == "[REDACTED]"


# -- T12: refresh — rotation replaces atomically; failure keeps the file ----------------


def test_t12_refresh_rotation_replaces_credential_atomically(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)
        first = read_login_credential({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
        access = refresh_access_token(
            _profile_for(server),
            environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
            hyprial_home=home,
            now=lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC),
        )
        second = read_login_credential({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    assert access == "fake-access-token-1"
    assert first.refresh_token == "fake-refresh-token-1"
    assert second.refresh_token == "fake-refresh-token-2"  # rotated
    assert second.obtained_at == "2026-09-05T12:00:00+00:00"
    mode = (
        credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home).stat().st_mode & 0o777
    )
    assert mode == 0o600


def test_t12_rotation_replace_failure_leaves_file_intact(tmp_path: Path) -> None:
    """Atomicity: if the replace step dies, the credential still holds the
    OLD refresh token — a naive in-place write would have clobbered it."""

    import os as _os

    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)
        target = credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
        before = target.read_bytes()
        real_replace = _os.replace

        def failing_replace(*_args: object, **_kwargs: object) -> None:
            raise OSError("replace failed")

        _os.replace = failing_replace
        try:
            with pytest.raises(OSError):
                refresh_access_token(
                    _profile_for(server),
                    environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                    hyprial_home=home,
                )
        finally:
            _os.replace = real_replace
        assert target.read_bytes() == before
        # No temp files left behind.
        assert sorted(p.name for p in target.parent.iterdir()) == ["login.json"]


def test_t12_refresh_failure_reports_rerun_and_keeps_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(refresh_error=True)) as server:
        _run(server, home)
        target = credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
        before = target.read_bytes()
        with pytest.raises(LoginError) as caught:
            refresh_access_token(
                _profile_for(server),
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    assert caught.value.code == "REFRESH_FAILED"
    assert "re-run" in str(caught.value)
    assert target.read_bytes() == before


def test_t12_refresh_without_credential_is_loud(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            refresh_access_token(
                _profile_for(server),
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    assert caught.value.code == "NOT_LOGGED_IN"
    assert "hyprial login" in str(caught.value)


def test_t12_credential_from_another_issuer_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)
        other = NetworkProfile(
            org="other",
            issuer="https://auth.other.example",
            client_id="other-id",
        )
        with pytest.raises(LoginError) as caught:
            refresh_access_token(
                other,
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    assert caught.value.code == "CREDENTIAL_ISSUER_MISMATCH"


def test_t12_refresh_interleaved_with_a_login_never_rolls_the_generation_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login commits generation N+1 while a refresh (read at N) is on the
    wire: the rotated token is discarded, not written over the new identity."""

    from hyprial.shell.impl.login import flow as login_flow

    home = _home(tmp_path)
    path = credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    with fake_oidc() as server:
        _run(server, home)
        first = read_login_credential({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
        real_request = login_flow._request_json

        def request_while_a_login_commits(*args: object, **kwargs: object):
            answer = real_request(*args, **kwargs)
            data = kwargs.get("data")
            if isinstance(data, dict) and data.get("grant_type") == "refresh_token":
                record = json.loads(path.read_text(encoding="utf-8"))
                record["refreshToken"] = "the-newer-login"
                record["identityGeneration"] = first.identity_generation + 1
                path.write_text(json.dumps(record), encoding="utf-8")
            return answer

        monkeypatch.setattr(login_flow, "_request_json", request_while_a_login_commits)
        with pytest.raises(LoginError) as raised:
            refresh_access_token(
                _profile_for(server),
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    assert raised.value.code == "CREDENTIAL_WRITE_CONFLICT"
    after = read_login_credential({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    assert after.refresh_token == "the-newer-login"
    assert after.identity_generation == first.identity_generation + 1
