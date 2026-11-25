"""The person registry after the IM binding chain is removed (R3, design #2950 8.5).

An IM sender is the person who owns that IM account; hyprial keeps no record of
whose account is whose.  What stays is the name-only registry: ``hyprial user
add|show|list`` over ``identity.users.add|list``, and ``identity.whoami``.  The
resolver, the local override store, the binding publication chain and their
methods and commands are gone, and a home that still holds their files starts,
works, and leaves those files exactly as they were.

The fixture is the real ``DaemonApplication`` behind the real CLI, on a home
seeded with every file the removed code used to read: ``identity.sqlite3``,
``users.sqlite3`` with a ``user_accounts`` row, ``verified-org-bindings.json``,
``identity-binding-suppression.json`` and ``settings.json`` with
``identityClaims`` and ``identityBinding``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.identity import UserStore, identity_slug
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime._owned_applications import close_application
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


OWNER = "Carol.jones@example.com"
OWNER_KEY = identity_slug(OWNER)
NODE = "person-registry-node"

REMOVED_METHODS = (
    ("identity.resolve", {"platform": "lark", "unionId": "on_owner"}),
    (
        "identity.override.set",
        {"platform": "lark", "unionId": "on_x", "user": OWNER_KEY, "confirmedBy": "operator"},
    ),
    ("identity.override.clear", {"platform": "lark", "unionId": "on_x"}),
    ("identity.bindings.list", {}),
    ("identity.bindings.audit", {}),
    ("identity.binding.publish", {"all": True}),
    ("identity.binding.withdraw", {"all": True}),
)

REMOVED_COMMANDS = (
    ("user", "bind", OWNER_KEY, "--union-id", "on_x", "--confirmed-by", "operator"),
    ("user", "unbind", "--union-id", "on_x"),
    ("user", "whois", "--union-id", "on_x"),
    ("user", "bindings", "audit"),
    ("user", "bindings", "publish", "--all"),
    ("user", "bindings", "withdraw", "--all"),
)


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[()]:
        return ()

    def raw_online_actors(self) -> tuple[()]:
        return ()

    def liveness_keeps(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _Inbox:
    pass


def _seed_old_binding_files(home: Path, state: Path) -> dict[Path, str]:
    """Write every file the removed code read; return their digests."""

    state.mkdir(mode=0o700, parents=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityIssuer": "https://auth.hyprial.com",
                "identityClaims": {"properties": {"oauth_Lark_unionId": "on_owner"}},
                "identityBinding": {
                    "user": OWNER,
                    "larkUnionId": "on_owner",
                    "proof": "header.payload.signature",
                },
            }
        ),
        encoding="utf-8",
    )
    overrides = sqlite3.connect(state / "identity.sqlite3")
    try:
        overrides.execute("CREATE TABLE overrides (union_id TEXT, user_key TEXT)")
        overrides.execute("INSERT INTO overrides VALUES ('on_owner', ?)", (OWNER_KEY,))
        overrides.commit()
    finally:
        overrides.close()
    (state / "verified-org-bindings.json").write_text(
        json.dumps({"bindings": [{"user": OWNER, "unionId": "on_owner"}]}),
        encoding="utf-8",
    )
    (state / "identity-binding-suppression.json").write_text(
        json.dumps({"all": True}), encoding="utf-8"
    )
    users = UserStore(state / "users.sqlite3", hyprial_home=home)
    try:
        users.add_user(kind="member", owner=OWNER, confirmed_by="operator")
    finally:
        users.close()
    accounts = sqlite3.connect(state / "users.sqlite3")
    try:
        accounts.execute(
            "CREATE TABLE IF NOT EXISTS user_accounts ("
            " user_key TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'lark',"
            " adapter TEXT NOT NULL, open_id TEXT NOT NULL, union_id TEXT,"
            " confirmed_by TEXT NOT NULL, confirmed_at_ms INTEGER NOT NULL,"
            " source TEXT NOT NULL, UNIQUE (adapter, open_id))"
        )
        accounts.execute(
            "INSERT INTO user_accounts VALUES (?, 'lark', 'lark:main', 'ou_owner',"
            " 'on_owner', 'operator', 1, 'manual:operator')",
            (OWNER_KEY,),
        )
        accounts.commit()
    finally:
        accounts.close()
    return {
        path: _digest(path)
        for path in (
            home / "settings.json",
            state / "identity.sqlite3",
            state / "verified-org-bindings.json",
            state / "identity-binding-suppression.json",
        )
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _account_rows(state: Path) -> list[tuple[Any, ...]]:
    database = sqlite3.connect(state / "users.sqlite3")
    try:
        return database.execute(
            "SELECT user_key, adapter, open_id, union_id FROM user_accounts"
        ).fetchall()
    finally:
        database.close()


@pytest.fixture
def registry_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DaemonApplication, list[tuple[str, dict[str, Any]]], Path, dict[Path, str]]:
    home = tmp_path / "home"
    state = home / "state"
    home.mkdir(mode=0o700)
    digests = _seed_old_binding_files(home, state)
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
        **daemon_dependencies(),
    )
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch(
        method: str, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        payload = dict(params or {})
        calls.append((method, payload))
        return app.handle(method, payload, **kwargs)

    monkeypatch.setattr(_cli_support, "_daemon_request", dispatch)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield app, calls, state, digests
    finally:
        close_application(app)


def _invoke_json(*arguments: str) -> dict[str, Any]:
    result = CliRunner().invoke(_cli_root_app, [*arguments, "--json"])
    assert result.exit_code == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    return payload


@pytest.mark.parametrize(("method", "params"), REMOVED_METHODS, ids=[m for m, _ in REMOVED_METHODS])
def test_removed_identity_methods_are_unknown_to_the_daemon(registry_cli, method, params) -> None:
    app, _calls, _state, _digests = registry_cli

    assert METHOD_REGISTRY.get(method) is None
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(method, params)
    assert raised.value.code == ipc_errors.METHOD_NOT_FOUND


@pytest.mark.parametrize("argv", REMOVED_COMMANDS, ids=[" ".join(a[:3]) for a in REMOVED_COMMANDS])
def test_removed_user_commands_are_usage_errors(registry_cli, argv) -> None:
    _app, calls, _state, _digests = registry_cli

    result = CliRunner().invoke(_cli_root_app, list(argv))

    assert result.exit_code == 2, result.stdout
    assert "No such command" in result.output
    assert calls == []


def test_user_list_has_no_source_filter(registry_cli) -> None:
    _app, calls, _state, _digests = registry_cli

    result = CliRunner().invoke(_cli_root_app, ["user", "list", "--source", "casdoor-login"])

    assert result.exit_code == 2, result.stdout
    assert "No such option" in result.output
    assert calls == []


def test_name_only_registry_works_on_a_home_with_the_old_binding_files(registry_cli) -> None:
    app, calls, state, digests = registry_cli

    added = _invoke_json(
        "user", "add", "--kind", "guest", "--display-name", "Visiting Guest",
        "--confirmed-by", "operator",
    )
    guest = added["user"]["userKey"]
    assert added["user"]["kind"] == "guest"
    assert calls[-1] == (
        "identity.users.add",
        {"kind": "guest", "displayName": "Visiting Guest", "confirmedBy": "operator"},
    )

    shown = _invoke_json("user", "show", OWNER_KEY)
    assert shown["userKey"] == OWNER_KEY
    assert shown["kind"] == "member"
    assert shown["owner"] == OWNER
    assert calls[-1] == ("identity.users.list", {"user": OWNER_KEY})

    listed = _invoke_json("user", "list")
    assert calls[-1] == ("identity.users.list", {})
    assert [row["userKey"] for row in listed["rows"]] == sorted([OWNER_KEY, guest])
    assert {row["kind"] for row in listed["rows"]} == {"member", "guest"}
    for row in listed["rows"]:
        assert set(row) == {
            "userKey", "kind", "owner", "nickname", "realName", "displayName",
            "createdAtMs", "updatedAtMs",
        }

    assert app.handle("identity.users.list", {"kind": "guest"})["users"][0]["userKey"] == guest
    assert METHOD_REGISTRY.get("identity.whoami") is not None

    assert {path: _digest(path) for path in digests} == digests
    assert _account_rows(state) == [(OWNER_KEY, "lark:main", "ou_owner", "on_owner")]


def test_listing_users_on_a_fresh_home_creates_no_store(tmp_path: Path) -> None:
    from types import SimpleNamespace

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    app = SimpleNamespace(state_dir=state, hyprial_home=home, owner=OWNER)

    assert METHOD_REGISTRY.invoke(app, "identity.users.list", {}) == {"users": []}
    assert not (state / "users.sqlite3").exists()
