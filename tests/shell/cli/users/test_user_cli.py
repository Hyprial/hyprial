"""CLI contract for ``hyprial user``: the name-only person registry.

R3 of the user-to-IM binding removal: ``add``, ``show`` and ``list`` read and
write ``identity.users.add|list``; ``bind``, ``unbind``, ``whois``, the
``bindings`` group and ``list --source`` are gone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class FakeIdentityDaemon:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, str] = {}
        self.filter_user_list = True
        self.users = [
            {
                "userKey": "owner",
                "kind": "member",
                "owner": "owner@example.com",
                "nickname": "Owner",
                "realName": None,
                "displayName": None,
                "createdAtMs": 1,
                "updatedAtMs": 1,
            },
            {
                "userKey": "guest-1",
                "kind": "guest",
                "owner": None,
                "nickname": None,
                "realName": None,
                "displayName": "Guest",
                "createdAtMs": 2,
                "updatedAtMs": 2,
            },
        ]

    def __call__(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        **_kwargs: Any,
    ) -> Any:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method in self.failures:
            code = self.failures[method]
            raise _cli_get_services().CliError(code, code.lower().replace("_", " "))
        if method == "identity.users.add":
            return {"user": {"userKey": "guest-new", **payload}}
        if method == "identity.users.list":
            key = payload.get("user")
            return {
                "users": [
                    row
                    for row in self.users
                    if not self.filter_user_list or key is None or row["userKey"] == key
                ]
            }
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def identity_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeIdentityDaemon:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "owner@example.com"}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    daemon = FakeIdentityDaemon()
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    return daemon


def _run_json(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(_cli_root_app, ["user", *args, "--json"])
    assert result.stdout, result.stderr
    return result.exit_code, json.loads(result.stdout)


def test_list_calls_users_list_and_returns_people_as_rows(
    identity_cli: FakeIdentityDaemon,
) -> None:
    code, result = _run_json("list")

    assert code == 0
    assert result == {"ok": True, "rows": identity_cli.users}
    assert identity_cli.calls == [("identity.users.list", {})]


def test_list_human_columns_name_people_not_accounts(
    identity_cli: FakeIdentityDaemon,
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "list"])

    assert result.exit_code == 0, result.stderr
    header = result.stdout.splitlines()[0].split()
    assert header == ["userKey", "kind", "owner", "displayName", "nickname", "realName"]
    assert "owner@example.com" in result.stdout
    assert "guest-1" in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        ("bind", "owner", "--union-id", "on_owner", "--confirmed-by", "allen"),
        ("unbind", "--union-id", "on_owner"),
        ("whois", "cli-developer", "ou_owner"),
        ("bindings", "audit"),
        ("bindings", "publish", "--all"),
        ("bindings", "withdraw", "--org", "group-a"),
    ],
)
def test_binding_commands_no_longer_exist(
    identity_cli: FakeIdentityDaemon, command: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", *command])

    assert result.exit_code == 2
    assert "No such command" in result.output
    assert identity_cli.calls == []


def test_list_source_filter_no_longer_exists(identity_cli: FakeIdentityDaemon) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "list", "--source", "local-override"])

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert identity_cli.calls == []


def test_add_and_show_use_users_ipc(identity_cli: FakeIdentityDaemon) -> None:
    code, added = _run_json(
        "add", "--kind", "guest", "--display-name", "Visitor", "--confirmed-by", "allen",
    )
    assert code == 0
    assert added["user"]["userKey"] == "guest-new"
    assert identity_cli.calls[-1] == (
        "identity.users.add",
        {"kind": "guest", "displayName": "Visitor", "confirmedBy": "allen"},
    )
    human = CliRunner().invoke(
        _cli_root_app,
        ["user", "add", "--kind", "guest", "--display-name", "Visitor", "--confirmed-by", "allen"],
    )
    assert human.exit_code == 0, human.stderr
    assert "added guest guest-new" in human.stdout

    code, shown = _run_json("show", "owner")
    assert code == 0
    assert shown["userKey"] == "owner"
    assert identity_cli.calls[-1] == ("identity.users.list", {"user": "owner"})


def test_show_selects_the_requested_user_when_daemon_returns_the_full_list(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.filter_user_list = False

    code, shown = _run_json("show", "owner")

    assert code == 0
    assert shown["userKey"] == "owner"
    assert identity_cli.calls[-1] == ("identity.users.list", {"user": "owner"})


def test_show_of_an_unknown_user_is_user_not_found(identity_cli: FakeIdentityDaemon) -> None:
    code, result = _run_json("show", "nobody")

    assert code == 1
    assert result["code"] == "USER_NOT_FOUND"


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (("list",), "identity.users.list"),
        (("show", "owner"), "identity.users.list"),
        (
            ("add", "--kind", "guest", "--display-name", "V", "--confirmed-by", "allen"),
            "identity.users.add",
        ),
    ],
)
def test_source_unavailable_exits_nonzero_with_the_frozen_code(
    identity_cli: FakeIdentityDaemon, command: tuple[str, ...], method: str
) -> None:
    identity_cli.failures[method] = "IDENTITY_SOURCE_UNAVAILABLE"

    exit_code, result = _run_json(*command)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["code"] == "IDENTITY_SOURCE_UNAVAILABLE"


def test_user_list_help_names_the_cli_json_rows_envelope(
    identity_cli: FakeIdentityDaemon,
) -> None:
    result = CliRunner().invoke(_cli_root_app, ["user", "list", "--help"])

    assert result.exit_code == 0, result.stderr
    assert '{"rows": [...]}' in result.stdout
    assert identity_cli.calls == []


def test_identity_override_forbidden_is_a_clear_permission_error(
    identity_cli: FakeIdentityDaemon,
) -> None:
    identity_cli.failures["identity.users.add"] = "IDENTITY_OVERRIDE_FORBIDDEN"
    command = ("add", "--kind", "guest", "--display-name", "V", "--confirmed-by", "allen")

    exit_code, result = _run_json(*command)

    assert exit_code == 1
    assert result == {
        "ok": False,
        "code": "IDENTITY_OVERRIDE_FORBIDDEN",
        "error": "identity override forbidden",
    }
    human = CliRunner().invoke(_cli_root_app, ["user", *command])
    assert human.exit_code == 1
    assert "permission denied" in human.stderr.lower()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--kind", "member", "--confirmed-by", "allen"), "--owner"),
        (("--kind", "guest", "--confirmed-by", "allen"), "--display-name"),
    ],
)
def test_add_requires_the_kinds_key_anchor_and_never_sends_a_key(
    identity_cli: FakeIdentityDaemon, arguments: tuple[str, ...], message: str
) -> None:
    exit_code, result = _run_json("add", *arguments)
    assert exit_code == 1
    assert result["code"] == "INVALID_ARGUMENT"
    assert message in result["error"]
    assert not [call for call in identity_cli.calls if call[0] == "identity.users.add"]
