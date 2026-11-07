"""login: profile scenarios."""

from __future__ import annotations
from pathlib import Path
import pytest
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402
from hyprial.shell.impl.login.flow import (
    LoginError,
)
from hyprial.daemon.impl.configuration.identity import resolve_node_owner

from .support import (
    _home,
    _profile_for,
    _run,
    _snapshot,
)


# -- T1: discovery issuer must equal the profile issuer byte for byte ----------


@pytest.mark.parametrize(
    "bad_issuer",
    [
        "https://auth.acme.io/",  # trailing slash — one byte off
        "https://other.acme.io",  # different host
        "https://auth.acme.io:8443",  # different port
    ],
)
def test_t1_discovery_issuer_mismatch_terminates_with_zero_writes(
    tmp_path: Path, bad_issuer: str
) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(discovery_issuer_override=bad_issuer)
    with fake_oidc(config) as server:
        profile = _profile_for(server)
        with pytest.raises(LoginError) as caught:
            _run(server, home, profile=profile)
    assert caught.value.code == "ISSUER_MISMATCH"
    assert caught.value.data["profileIssuer"] == profile.issuer
    # "terminates, writes nothing" — the whole home is untouched.
    assert _snapshot(home) == {}


def test_t1_matching_issuer_passes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        result = _run(server, home)
    assert result.owner == "alicesmith"
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"


# -- T2: the three endpoints must share the issuer's origin ---------------------


def test_t2_userinfo_on_another_origin_terminates(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        userinfo_endpoint_override="https://evil.example/api/userinfo"
    )
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "ENDPOINT_ORIGIN_MISMATCH"
    assert _snapshot(home) == {}


def test_t2_device_endpoint_on_another_origin_terminates(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        device_endpoint_override="https://evil.example/api/device-auth"
    )
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "ENDPOINT_ORIGIN_MISMATCH"
    assert _snapshot(home) == {}


def test_t2_token_endpoint_on_another_port_terminates(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        token_endpoint_override="http://127.0.0.1:1/api/token"  # different port
    )
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "ENDPOINT_ORIGIN_MISMATCH"
    assert _snapshot(home) == {}


def test_t2_origin_comparison_rules() -> None:
    """The origin unit under test: scheme+host+effective port — nothing
    looser; path-insensitivity is correct (endpoints live under paths)."""

    from hyprial.shell.impl.login.oidc import _origin

    assert _origin("https://a.io") == _origin("https://a.io:443")
    assert _origin("https://a.io/x/y") == _origin("https://a.io")
    assert _origin("https://a.io") != _origin("https://a.io:8443")
    assert _origin("http://a.io") != _origin("https://a.io")
    assert _origin("https://a.io") != _origin("https://b.io")


# -- T3: client_id and scope are exactly the profile's --------------------------


def test_t3_device_authorization_sends_profile_client_id_and_exact_scope(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(client_id="profile-public-id")) as server:
        _run(server, home, profile=_profile_for(server, client_id="profile-public-id"))
        device_requests = [
            record for record in server.records if record.path == "/api/device-auth"
        ]
    assert device_requests, "the fake never saw a device authorization request"
    form = device_requests[0].form
    assert form["client_id"] == ["profile-public-id"]
    scope = form["scope"][0]
    assert scope.split() == ["openid", "profile", "offline_access"]
    assert "email" not in scope  # D1: the owner is never taken from email


def test_t3_wrong_client_id_is_refused_with_zero_writes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(outcome="bad_client")
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "TOKEN_ENDPOINT_ERROR"
    assert caught.value.data["error"] == "invalid_client"
    assert _snapshot(home) == {}
