"""CasdoorAccountClient against a fake Casdoor on 127.0.0.1 (real HTTP)."""

from __future__ import annotations

import pytest

from hyprial.shell.impl.invites.casdoor import (
    USER_AGENT,
    CasdoorAccountClient,
    InvitePickupError,
)
from tests.shell.invites.fakes import FakeCasdoor


def test_get_account_returns_the_data_object_and_sends_the_bearer() -> None:
    with FakeCasdoor() as server:
        client = CasdoorAccountClient(server.url, "token-1")
        account = client.get_account()

    assert account["owner"] == "test-org"
    assert account["name"] == "alice"
    assert server.authorization_headers == ["Bearer token-1"]


def test_every_call_sends_the_hyprial_user_agent() -> None:
    # Cloudflare in front of auth.hyprial.com answers urllib's default
    # "Python-urllib/x" agent with 403 / error code 1010.
    with FakeCasdoor() as server:
        CasdoorAccountClient(server.url, "token-1").get_account()

    assert server.user_agents == [USER_AGENT]
    assert USER_AGENT.startswith("hyprial-cli/")


def test_get_invites_defaults_to_empty_when_the_property_is_absent() -> None:
    with FakeCasdoor() as server:
        client = CasdoorAccountClient(server.url, "token-1")
        assert client.get_invites() == []


def test_get_invites_parses_the_property_json_string() -> None:
    with FakeCasdoor() as server:
        server.set_invites_property(
            [{"payload": "p-1", "acceptedAt": "2026-10-03T00:00:00+00:00"}]
        )
        client = CasdoorAccountClient(server.url, "token-1")
        items = client.get_invites()

    assert items == [{"payload": "p-1", "acceptedAt": "2026-10-03T00:00:00+00:00"}]


def test_get_invites_rejects_a_broken_property_json() -> None:
    with FakeCasdoor() as server:
        server.set_invites_property("{not json")
        client = CasdoorAccountClient(server.url, "token-1")
        with pytest.raises(InvitePickupError) as raised:
            client.get_invites()

    assert raised.value.code == "INVITES_MALFORMED"


def test_the_client_is_read_only() -> None:
    # The landing page is the property's only writer (2026-10-04).
    assert not hasattr(CasdoorAccountClient, "set_invites")


def test_http_failures_raise_a_typed_error_without_the_token() -> None:
    with FakeCasdoor() as server:
        server.account_status = 403
        client = CasdoorAccountClient(server.url, "token-secret")
        with pytest.raises(InvitePickupError) as raised:
            client.get_account()

    assert raised.value.code == "CASDOOR_REQUEST_FAILED"
    assert "token-secret" not in str(raised.value)
