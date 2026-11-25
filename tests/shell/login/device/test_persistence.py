"""login: persistence scenarios."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.shell.impl.login.flow import (
    LoginError,
    credential_path,
    read_login_credential,
    refresh_access_token,
)
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402

from .support import (
    _home,
    _profile_for,
    _seed_credential,
)


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
        _seed_credential(home, server.issuer)
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
        _seed_credential(home, server.issuer)
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
        _seed_credential(home, server.issuer)
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
        _seed_credential(home, server.issuer)
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
        _seed_credential(home, server.issuer)
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
