"""CLI-to-daemon identity contract through the real application dispatcher."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark import Identity, LarkStateStore
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import UserStore, identity_slug
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime._owned_applications import close_application
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


OWNER = "Bob.smith@example.com"
OWNER_KEY = identity_slug(OWNER)
SQUIRE_PROFILE_KEY = "operator-selected-owner-key"
NODE = "identity-cli-node"


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


def _invoke_json(*arguments: str) -> tuple[dict[str, Any], Any]:
    result = CliRunner().invoke(_cli_root_app, [*arguments, "--json"])
    assert result.exit_code == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    return payload, result


@pytest.fixture
def real_identity_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DaemonApplication, list[tuple[str, dict[str, Any]]], Path]:
    home = tmp_path / "home"
    state = home / "state"
    home.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityIssuer": "https://auth.hyprial.com",
                "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "on_owner"}
                },
            }
        ),
        encoding="utf-8",
    )
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
    users = UserStore(state / "users.sqlite3", hyprial_home=home)
    try:
        owner = users.add_user(
            kind="member", owner=OWNER, confirmed_by="operator"
        )
        assert owner.user_key == OWNER_KEY
    finally:
        users.close()
    observed = LarkStateStore(state / "adapters.sqlite3", adapter="lark:main")
    try:
        observed.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_owner",
                union_id="on_owner",
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:fixture",
            ),
            now_ms=101,
        )
    finally:
        observed.close()

    calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch(
        method: str,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        payload = dict(params or {})
        calls.append((method, payload))
        if method == "management.squire.ensure":
            raise ipc_errors.DaemonUnavailableError(
                "the integration fixture uses the offline management port"
            )
        return app.handle(method, payload, **kwargs)

    monkeypatch.setattr(_cli_support, "_daemon_request", dispatch)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield app, calls, tmp_path
    finally:
        close_application(app)


def test_every_cli_user_command_reaches_real_identity_handlers(
    real_identity_cli: tuple[
        DaemonApplication, list[tuple[str, dict[str, Any]]], Path
    ],
) -> None:
    _app, calls, tmp_path = real_identity_cli

    listed, _ = _invoke_json("user", "list", "--source", "casdoor-login")
    assert OWNER_KEY != OWNER
    assert listed["rows"][0]["user"] == OWNER_KEY
    assert listed["rows"][0]["unionId"] == "on_owner"
    assert calls[-1] == (
        "identity.bindings.list",
        {"source": "casdoor-login"},
    )

    added, _ = _invoke_json(
        "user",
        "add",
        "--kind",
        "guest",
        "--display-name",
        "Fixture Guest",
        "--confirmed-by",
        "operator",
    )
    guest = added["user"]["userKey"]
    assert added["user"]["kind"] == "guest"
    assert calls[-1] == (
        "identity.users.add",
        {
            "kind": "guest",
            "displayName": "Fixture Guest",
            "confirmedBy": "operator",
        },
    )

    shown, _ = _invoke_json("user", "show", OWNER_KEY)
    assert shown["userKey"] == OWNER_KEY
    assert shown["kind"] == "member"
    assert calls[-1] == ("identity.users.list", {"user": OWNER_KEY})

    bound_union, _ = _invoke_json(
        "user",
        "bind",
        guest,
        "--union-id",
        "on_guest",
        "--confirmed-by",
        "operator",
    )
    assert bound_union["user"] == guest
    assert bound_union["unionId"] == "on_guest"
    assert calls[-1] == (
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_guest",
            "user": guest,
            "confirmedBy": "operator",
        },
    )
    by_union, _ = _invoke_json("user", "whois", "--union-id", "on_guest")
    assert by_union["user"] == guest
    assert by_union["kind"] == "guest"
    assert by_union["verified"] is True
    assert calls[-1] == (
        "identity.resolve",
        {"platform": "lark", "unionId": "on_guest"},
    )

    bound_account, _ = _invoke_json(
        "user",
        "bind",
        OWNER_KEY,
        "--adapter",
        "main",
        "--open-id",
        "ou_owner",
        "--confirmed-by",
        "operator",
    )
    assert bound_account["user"] == OWNER_KEY
    assert bound_account["unionId"] == "on_owner"
    assert bound_account["accounts"][0]["adapter"] == "main"
    assert calls[-1] == (
        "identity.override.set",
        {
            "adapter": "main",
            "openId": "ou_owner",
            "user": OWNER_KEY,
            "confirmedBy": "operator",
        },
    )
    by_account, _ = _invoke_json("user", "whois", "main", "ou_owner")
    assert by_account["user"] == OWNER_KEY
    assert by_account["source"] == "casdoor-login"
    assert by_account["verified"] is True
    assert by_account["accounts"][0]["adapter"] == "main"
    assert calls[-1] == (
        "identity.resolve",
        {"adapter": "main", "openId": "ou_owner"},
    )

    local, _ = _invoke_json("user", "list", "--source", "local-override")
    assert {row["unionId"] for row in local["rows"]} == {
        "on_guest",
        "on_owner",
    }

    audited, _ = _invoke_json("user", "bindings", "audit")
    assert len(audited["rows"]) == 1
    assert audited["rows"][0]["user"] == OWNER_KEY
    assert audited["rows"][0]["audit"] == "confirmed elsewhere"
    assert calls[-3:] == [
        ("identity.bindings.list", {"source": "legacy-user-bind"}),
        ("identity.bindings.list", {"source": "legacy-identities"}),
        ("identity.bindings.list", {}),
    ]

    cleared_account, _ = _invoke_json(
        "user", "unbind", "--adapter", "main", "--open-id", "ou_owner"
    )
    assert cleared_account["cleared"] is True
    assert calls[-1] == (
        "identity.override.clear",
        {"adapter": "main", "openId": "ou_owner"},
    )

    cleared_union, _ = _invoke_json(
        "user", "unbind", "--union-id", "on_guest"
    )
    assert cleared_union["cleared"] is True
    assert calls[-1] == (
        "identity.override.clear",
        {"platform": "lark", "unionId": "on_guest"},
    )

    channel = f"channel:{OWNER}:{NODE}:squire"
    squire_home = tmp_path / "squire"
    setup, _ = _invoke_json(
        "squire",
        "setup",
        "--owner-key",
        SQUIRE_PROFILE_KEY,
        "--login-name",
        "lin",
        "--machine",
        NODE,
        "--machine-key",
        NODE,
        "--home",
        str(squire_home),
        "--channel",
        channel,
    )
    binding_code = next(
        step["bindingCode"]
        for step in setup["steps"]
        if step["id"] == "owner-open-id"
    )
    configured, _ = _invoke_json(
        "squire",
        "setup",
        "--owner-key",
        SQUIRE_PROFILE_KEY,
        "--login-name",
        "lin",
        "--machine",
        NODE,
        "--machine-key",
        NODE,
        "--home",
        str(squire_home),
        "--step",
        "owner-open-id",
        "--channel",
        channel,
        "--owner-open-id",
        "ou_squire",
        "--binding-code",
        binding_code,
    )
    assert configured["steps"] == [
        {
            "id": "owner-open-id",
            "status": "complete",
            "detail": "The owner account is bound through the daemon identity resolver.",
        }
    ]
    assert calls[-3:] == [
        ("identity.users.list", {"kind": "member"}),
        ("identity.bindings.list", {}),
        (
            "identity.override.set",
            {
                "adapter": "squire",
                "openId": "ou_squire",
                "user": OWNER_KEY,
                "confirmedBy": OWNER,
            },
        ),
    ]


def test_account_only_rows_are_visible_but_never_verified(
    real_identity_cli: tuple[
        DaemonApplication, list[tuple[str, dict[str, Any]]], Path
    ],
) -> None:
    _app, calls, _tmp_path = real_identity_cli
    runner = CliRunner()

    bound = runner.invoke(
        _cli_root_app,
        [
            "user",
            "bind",
            OWNER_KEY,
            "--adapter",
            "outbound",
            "--open-id",
            "ou_outbound",
            "--confirmed-by",
            "operator",
        ],
    )
    listed = runner.invoke(_cli_root_app, ["user", "list"])
    resolved = runner.invoke(
        _cli_root_app, ["user", "whois", "outbound", "ou_outbound"]
    )

    assert bound.exit_code == 0, bound.stderr
    assert "account-only (outbound only)" in bound.stdout
    assert calls[-3:] == [
        (
            "identity.override.set",
            {
                "adapter": "outbound",
                "openId": "ou_outbound",
                "user": OWNER_KEY,
                "confirmedBy": "operator",
            },
        ),
        ("identity.bindings.list", {}),
        (
            "identity.resolve",
            {"adapter": "outbound", "openId": "ou_outbound"},
        ),
    ]

    drift: list[str] = []
    if "account-only (outbound only)" not in listed.stdout:
        drift.append("identity.bindings.list omitted the account-only row")
    if resolved.exit_code != 0:
        drift.append("identity.resolve returned IDENTITY_UNBOUND for the account-only row")
    if "verified: true" in resolved.stdout.lower():
        drift.append("identity.resolve marked the account-only row verified")
    if "account-only (outbound only)" not in resolved.stdout:
        drift.append("user whois could not render the account-only row")
    assert drift == []


def test_union_and_account_only_overrides_cross_cli_identity_surfaces(
    real_identity_cli: tuple[
        DaemonApplication, list[tuple[str, dict[str, Any]]], Path
    ],
) -> None:
    """A union override and an account-only row in one store (#1215 regression)."""

    app, _calls, tmp_path = real_identity_cli
    state = tmp_path / "home" / "state"
    channel = f"channel:{OWNER}:{NODE}:squire"
    squire_home = tmp_path / "squire-mixed-overrides"
    from hyprial.daemon.impl.squire.setup import SetupStateStore

    binding_code = SetupStateStore(state / "squire-setup.json").ensure_binding(
        SQUIRE_PROFILE_KEY, channel
    ).code
    app.handle(
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_local_override",
            "user": OWNER_KEY,
            "confirmedBy": "operator",
        },
    )
    app.handle(
        "identity.override.set",
        {
            "adapter": "outbound",
            "openId": "ou_account_only",
            "user": OWNER_KEY,
            "confirmedBy": "operator",
        },
    )

    commands = [
        ["user", "list"],
        ["user", "bindings", "audit"],
        [
            "squire",
            "setup",
            "--owner-key",
            SQUIRE_PROFILE_KEY,
            "--login-name",
            "lin",
            "--machine",
            NODE,
            "--machine-key",
            NODE,
            "--home",
            str(squire_home),
            "--step",
            "owner-open-id",
            "--channel",
            channel,
            "--owner-open-id",
            "ou_squire_mixed",
            "--binding-code",
            binding_code,
        ],
    ]
    results = []
    for command in commands:
        results.append(CliRunner().invoke(_cli_root_app, [*command, "--json"]))

    # The CLI boundary envelopes handler exceptions, so call bindings.list
    # directly too: before #1215 it raised TypeError sorting None against str.
    app.handle("identity.bindings.list", {})
    for result in results:
        assert result.exit_code == 0, result.stderr or result.stdout
