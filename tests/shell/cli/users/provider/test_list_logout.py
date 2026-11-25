"""``hyprial user provider list|logout``: exact IPC params and logout confirmation.

Contract §6: logout without --yes lists the affected agents on stderr and asks;
non-interactive and --json callers must pass --yes.
"""

from __future__ import annotations

import json

import pytest

from .conftest import KEY_ID, FakeDaemon, invoke, key_view

OTHER_KEY = "pk.aaaaaaaaaaaaaaaaaaaaaaaaaa"
GRANTS = [
    {"agent": "00000000-0000-0000-0000-000000000001", "agentId": "a.bbbbbbbbbbbbbbbbbbbbbbbbbb",
     "grantId": "openai", "provider": "openai", "state": "active"},
    {"agent": "00000000-0000-0000-0000-000000000002", "agentId": None,
     "grantId": "openai", "provider": "openai", "state": "active"},
]


def _listing(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.list"] = {
        "keys": [key_view(grants=GRANTS, lastUsedAt=5), key_view(keyId=OTHER_KEY, grants=[])],
        "invalid": [{"keyId": "pk.cccccccccccccccccccccccccc", "code": "PROVIDER_KEY_SIGNATURE_INVALID"}],
    }


def _logout_answer(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.logout"] = {
        "keyId": KEY_ID, "state": "deleted", "alreadyDeleted": False,
        "revokedGrants": [{key: grant[key] for key in ("agent", "agentId", "grantId", "provider")}
                          for grant in GRANTS],
    }


@pytest.mark.parametrize(("extra", "params"), [
    ([], {}),
    (["--provider", "openai"], {"provider": "openai"}),
    (["--all"], {"includeDeleted": True}),
    (["--provider", "deepseek", "--all"], {"provider": "deepseek", "includeDeleted": True}),
])
def test_list_sends_exact_params(daemon: FakeDaemon, extra: list[str], params: dict) -> None:
    _listing(daemon)

    result = invoke(["user", "provider", "list", *extra, "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.list", params)]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [key["keyId"] for key in payload["keys"]] == [KEY_ID, OTHER_KEY]


def test_list_human_view_names_keys_and_invalid_records(daemon: FakeDaemon) -> None:
    _listing(daemon)

    result = invoke(["user", "provider", "list"])

    assert result.exit_code == 0, result.output
    assert KEY_ID in result.stdout and OTHER_KEY in result.stdout
    assert "pk.cccccccccccccccccccccccccc" in result.stdout
    assert "PROVIDER_KEY_SIGNATURE_INVALID" in result.stdout


def test_list_human_view_says_when_empty(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.list"] = {"keys": [], "invalid": []}

    result = invoke(["user", "provider", "list"])

    assert result.exit_code == 0, result.output
    assert "no keys" in result.stdout


def test_logout_with_yes_sends_only_the_key_id(daemon: FakeDaemon) -> None:
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", KEY_ID, "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.logout", {"keyId": KEY_ID})]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["state"] == "deleted"
    assert len(payload["revokedGrants"]) == 2


def test_logout_under_json_without_yes_is_refused_before_ipc(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)

    result = invoke(["user", "provider", "logout", KEY_ID, "--json"])

    assert result.exit_code == 1
    assert daemon.calls == []
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["code"] == "CONFIRMATION_REQUIRED"


def test_logout_non_interactive_without_yes_is_refused_before_ipc(daemon: FakeDaemon) -> None:
    result = invoke(["user", "provider", "logout", KEY_ID])

    assert result.exit_code == 1
    assert daemon.calls == []
    assert "--yes" in result.stderr


def test_logout_interactive_lists_affected_agents_then_logs_out(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    _listing(daemon)
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="y\n")

    assert result.exit_code == 0, result.output
    assert daemon.calls == [
        ("user.provider.list", {"includeDeleted": True}),
        ("user.provider.logout", {"keyId": KEY_ID}),
    ]
    assert "a.bbbbbbbbbbbbbbbbbbbbbbbbbb" in result.stderr
    assert "00000000-0000-0000-0000-000000000002" in result.stderr
    assert KEY_ID in result.stdout


def test_logout_interactive_decline_never_logs_out(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    _listing(daemon)

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="n\n")

    assert result.exit_code == 1
    assert daemon.methods() == ["user.provider.list"]
    assert "--yes" in result.stderr


def test_logout_preview_lists_only_active_grants(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    revoked = {"agent": "00000000-0000-0000-0000-000000000003", "agentId": "a.dddddddddddddddddddddddddd",
               "grantId": "openai", "provider": "openai", "state": "revoked"}
    daemon.answers["user.provider.list"] = {
        "keys": [key_view(grants=[GRANTS[0], revoked])], "invalid": [],
    }
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="y\n")

    assert result.exit_code == 0, result.output
    assert "a.bbbbbbbbbbbbbbbbbbbbbbbbbb" in result.stderr
    assert "a.dddddddddddddddddddddddddd" not in result.stderr
    assert "revoked)" not in result.stderr


def test_logout_preview_with_only_revoked_grants_revokes_none(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    revoked = [{**grant, "state": "revoked"} for grant in GRANTS]
    daemon.answers["user.provider.list"] = {"keys": [key_view(grants=revoked)], "invalid": []}
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="y\n")

    assert result.exit_code == 0, result.output
    assert "revokes no agent grant" in result.stderr
    assert "a.bbbbbbbbbbbbbbbbbbbbbbbbbb" not in result.stderr


def test_logout_preview_names_a_missing_key(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    missing = "pk.eeeeeeeeeeeeeeeeeeeeeeeeee"
    _listing(daemon)

    result = invoke(["user", "provider", "logout", missing], stdin="n\n")

    assert result.exit_code == 1
    assert f"key {missing} is not listed (PROVIDER_KEY_NOT_FOUND); affected agents unknown" in result.stderr
    assert "revokes no agent grant" not in result.stderr
    assert daemon.methods() == ["user.provider.list"]


def test_logout_preview_names_an_invalid_key(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    invalid = "pk.cccccccccccccccccccccccccc"
    _listing(daemon)
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", invalid], stdin="y\n")

    assert result.exit_code == 0, result.output
    assert (f"key {invalid} is not listed (PROVIDER_KEY_SIGNATURE_INVALID); affected agents unknown"
            in result.stderr)
    assert "revokes no agent grant" not in result.stderr
    assert daemon.methods() == ["user.provider.list", "user.provider.logout"]


def test_logout_preview_prefers_the_invalid_code_for_a_listed_key(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    daemon.answers["user.provider.list"] = {
        "keys": [key_view(grants=GRANTS)],
        "invalid": [{"keyId": KEY_ID, "code": "PROVIDER_KEY_SIGNATURE_INVALID"}],
    }

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="n\n")

    assert result.exit_code == 1
    assert (f"key {KEY_ID} is not listed (PROVIDER_KEY_SIGNATURE_INVALID); affected agents unknown"
            in result.stderr)


def test_logout_interactive_preview_for_a_key_without_grants(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)
    _listing(daemon)
    _logout_answer(daemon)

    result = invoke(["user", "provider", "logout", OTHER_KEY], stdin="y\n")

    assert result.exit_code == 0, result.output
    assert "no agent" in result.stderr
    assert daemon.methods() == ["user.provider.list", "user.provider.logout"]
