"""R2 binding-assertion proof acquisition and refresh-token hygiene.

The login-side confirmation also checks the assertion audience.  Reads claims
before signature verification, so it can only compare what the assertion
claims -- but the audience is one of those claims: without it, a proof minted
for another Casdoor application would be accepted as this member's binding
confirmation.  The cases at the end of this module pin that: the array forms
Casdoor signs must pass, every non-binding audience must be refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.shell.impl.login.flow import read_login_credential
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

from .support import _home, _run


def _binding_requests(server, path: str):
    return [
        request
        for request in server.records_for(path)
        if request.form.get("client_id") == [server.config.binding_client_id]
    ]


def test_binding_proof_pending_then_success_is_persisted_for_publication(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_pending_polls=1,
    )

    with fake_oidc(config) as server:
        _run(server, home)

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["identityBinding"] == {
        "user": "alicesmith",
        "larkUnionId": "on_allen",
        "proof": settings["identityBinding"]["proof"],
        "publishedAt": settings["identityBinding"]["publishedAt"],
    }
    assert len(_binding_requests(server, "/api/device-auth")) == 1
    assert len(_binding_requests(server, "/api/token")) == 2
    assert all(
        "client_secret" not in request.form
        for request in _binding_requests(server, "/api/token")
    )


def test_binding_proof_restarts_once_after_expired_token(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_expired_attempts=1,
        binding_pending_polls=0,
    )

    with fake_oidc(config) as server:
        _run(server, home)

    assert len(_binding_requests(server, "/api/device-auth")) == 2
    assert len(_binding_requests(server, "/api/token")) == 2


def test_binding_proof_failure_after_second_expiry_does_not_block_login(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_expired_attempts=2,
        binding_pending_polls=0,
    )

    with fake_oidc(config) as server:
        result = _run(
            server,
            home,
            emit=lambda kind, data: events.append((kind, data)),
        )

    assert result.owner == "alicesmith"
    assert read_login_credential(hyprial_home=home).refresh_token
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "identityBinding" not in settings
    failures = [data for kind, data in events if kind == "binding-proof-failed"]
    assert len(failures) == 1
    assert failures[0]["code"] == "BINDING_PROOF_EXPIRED"
    assert "will retry" in failures[0]["message"]
    assert len(_binding_requests(server, "/api/device-auth")) == 2
    assert len(_binding_requests(server, "/api/token")) == 2


def test_failed_binding_retry_preserves_previous_published_row(tmp_path: Path) -> None:
    home = _home(tmp_path)
    first = FakeOidcConfig(
        lark_union_id="on_allen",
    )
    with fake_oidc(first) as server:
        _run(server, home)
    before = json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "identityBinding"
    ]

    retry = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_expired_attempts=2,
        binding_pending_polls=0,
    )
    with fake_oidc(retry) as server:
        _run(server, home)

    after = json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "identityBinding"
    ]
    assert after == before


def test_binding_refresh_token_is_discarded_before_any_state_or_log_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = _home(tmp_path)
    marker = "binding-refresh-unique-marker-7c3a"
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_pending_polls=0,
        binding_refresh_token=marker,
    )

    with fake_oidc(config) as server:
        _run(server, home)

    files = [path.read_bytes() for path in home.rglob("*") if path.is_file()]
    assert all(marker.encode() not in payload for payload in files)
    assert marker not in caplog.text
    row = json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "identityBinding"
    ]
    assert marker not in json.dumps(row)
    assert set(row) == {"user", "larkUnionId", "proof", "publishedAt"}


def test_binding_confirmation_from_another_account_is_not_stored(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_name="another-user",
    )

    with fake_oidc(config) as server:
        result = _run(
            server,
            home,
            emit=lambda kind, data: events.append((kind, data)),
        )

    assert result.owner == "alicesmith"
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "identityBinding" not in settings
    failures = [data for kind, data in events if kind == "binding-proof-failed"]
    assert len(failures) == 1
    assert "binding confirmation was approved by another account" in failures[0][
        "message"
    ]
    assert "will retry" in failures[0]["message"]


def test_binding_confirmation_from_another_casdoor_owner_is_not_stored(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_owner="another-owner",
    )

    with fake_oidc(config) as server:
        result = _run(
            server,
            home,
            emit=lambda kind, data: events.append((kind, data)),
        )

    assert result.owner == "alicesmith"
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "identityBinding" not in settings
    failures = [data for kind, data in events if kind == "binding-proof-failed"]
    assert [failure["code"] for failure in failures] == [
        "BINDING_ACCOUNT_MISMATCH"
    ]
    assert "different Casdoor organization" in failures[0]["message"]


def test_binding_confirmation_with_mismatched_union_id_is_not_stored(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []
    config = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_union_id="on_another_person",
    )

    with fake_oidc(config) as server:
        _run(server, home, emit=lambda kind, data: events.append((kind, data)))

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "identityBinding" not in settings
    failures = [data for kind, data in events if kind == "binding-proof-failed"]
    assert [failure["code"] for failure in failures] == [
        "BINDING_ACCOUNT_MISMATCH"
    ]


def test_mismatched_confirmation_preserves_previous_published_binding(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(lark_union_id="on_allen")) as server:
        _run(server, home)
    before = json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "identityBinding"
    ]

    mismatch = FakeOidcConfig(
        lark_union_id="on_allen",
        binding_name="another-user",
    )
    with fake_oidc(mismatch) as server:
        _run(server, home)

    after = json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "identityBinding"
    ]
    assert after == before


_OTHER_CLIENT = "another-client-id"


def _proof(claims: dict[str, object]) -> str:
    from hyprial.identity.impl.org_directory import binding_assertion_claims_unverified

    def _seg(value: dict[str, object]) -> str:
        import base64

        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    proof = f"{_seg({'alg': 'RS256'})}.{_seg(claims)}.signature"
    assert binding_assertion_claims_unverified(proof) == claims
    return proof


@pytest.mark.parametrize(
    "audience",
    [
        "1be2763a29154030fbcb",
        ["1be2763a29154030fbcb"],
        ["1be2763a29154030fbcb", _OTHER_CLIENT],
    ],
)
def test_confirmation_accepts_audiences_for_the_binding_application(
    audience,
) -> None:
    from hyprial.shell.impl.login.binding import validate_binding_confirmation

    proof = _proof(
        {
            "aud": audience,
            "azp": "1be2763a29154030fbcb",
            "owner": "hyprial",
            "name": "alicesmith",
            "oauth_Lark_unionId": "on_allen",
        }
    )

    validate_binding_confirmation(
        proof, expected_user="alicesmith", expected_union_id="on_allen"
    )


@pytest.mark.parametrize(
    "audience",
    [
        _OTHER_CLIENT,
        [_OTHER_CLIENT],
        [],
        None,
    ],
)
def test_confirmation_refuses_audiences_for_another_application(audience) -> None:
    from hyprial.shell.impl.login.binding import validate_binding_confirmation
    from hyprial.shell.impl.login.oidc import LoginError

    claims: dict[str, object] = {
        "owner": "hyprial",
        "name": "alicesmith",
        "oauth_Lark_unionId": "on_allen",
    }
    if audience is not None:
        claims["aud"] = audience
    proof = _proof(claims)

    with pytest.raises(LoginError) as caught:
        validate_binding_confirmation(
            proof, expected_user="alicesmith", expected_union_id="on_allen"
        )

    assert caught.value.code == "BINDING_ACCOUNT_MISMATCH"
