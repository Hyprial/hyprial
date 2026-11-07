"""``squire setup --owner-open-id`` through the daemon identity resolver.

Split from test_squire_setup_cli.py (review 1027/1031/1037 coverage) to keep
both files under the structural line limit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from .test_squire_setup_cli import _owner_open_id_command, payload, run_setup
from hyprial.shell.impl.cli.commands.common import support as _cli_support

def test_setup_resolves_member_key_consumes_code_once_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    initial = payload(run_setup(home, "--channel", channel))
    code = initial["steps"][3]["bindingCode"]
    calls: list[tuple[str, dict[str, Any]]] = []
    bindings: list[dict[str, Any]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {
                "users": [
                    {
                        "userKey": "gregpaws-key",
                        "kind": "member",
                        "owner": "gregpaws",
                    }
                ]
            }
        if method == "identity.bindings.list":
            return {"bindings": list(bindings)}
        if method == "identity.override.set":
            bindings.append(
                {
                    "user": params["user"],
                    "source": "local-override",
                    "outboundOnly": True,
                    "accounts": [
                        {
                            "adapter": params["adapter"],
                            "openId": params["openId"],
                            "source": "override",
                        }
                    ],
                }
            )
            return bindings[-1]
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    command = _owner_open_id_command(
        home, channel, "ou_owner123", binding_code=code
    )

    bound = CliRunner().invoke(cli.app, command)

    assert bound.exit_code == 0, bound.stderr
    result = json.loads(bound.stdout)
    assert result["steps"] == [
        {
            "id": "owner-open-id",
            "status": "complete",
            "detail": "The owner account is bound through the daemon identity resolver.",
        }
    ]
    users_path = home / "state" / "users.json"
    profile = json.loads(users_path.read_text(encoding="utf-8"))["users"][0]
    assert "ownerOpenId" not in profile
    assert calls == [
        ("identity.users.list", {"kind": "member"}),
        ("identity.bindings.list", {}),
        (
            "identity.override.set",
            {
                "adapter": "squire",
                "openId": "ou_owner123",
                "user": "gregpaws-key",
                "confirmedBy": "gregpaws",
            },
        )
    ]
    before = users_path.read_bytes()

    repeated = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(
            home, channel, "ou_owner123", binding_code=None
        ),
    )

    assert repeated.exit_code == 0, repeated.stderr
    assert json.loads(repeated.stdout)["changed"] == []
    assert users_path.read_bytes() == before
    assert calls[3:] == [
        ("identity.users.list", {"kind": "member"}),
        ("identity.bindings.list", {}),
    ]


def test_setup_refuses_reusing_consumed_code_for_a_different_open_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    initial = payload(run_setup(home, "--channel", channel))
    code = initial["steps"][3]["bindingCode"]
    calls: list[tuple[str, dict[str, Any]]] = []
    bindings: list[dict[str, Any]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {
                "users": [
                    {"userKey": "gregpaws-key", "kind": "member", "owner": "gregpaws"}
                ]
            }
        if method == "identity.bindings.list":
            return {"bindings": list(bindings)}
        if method == "identity.override.set":
            row = {
                "user": params["user"],
                "source": "local-override",
                "accounts": [
                    {"adapter": params["adapter"], "openId": params["openId"]}
                ],
            }
            bindings.append(row)
            return row
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    first = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, channel, "ou_first", binding_code=code),
    )
    reused = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, channel, "ou_second", binding_code=code),
    )

    assert first.exit_code == 0, first.stderr
    assert reused.exit_code == 1
    assert "invalid or expired" in json.loads(reused.stdout)["error"]
    assert [call for call in calls if call[0] == "identity.override.set"] == [
        (
            "identity.override.set",
            {
                "adapter": "squire",
                "openId": "ou_first",
                "user": "gregpaws-key",
                "confirmedBy": "gregpaws",
            },
        )
    ]


def test_another_users_override_on_same_adapter_and_open_id_never_skips_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 1027 M3 boundary (hq): the consume may be skipped only when the
    # existing override matches adapter, openId AND the resolved user.
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    run_setup(home, "--channel", channel)
    calls: list[tuple[str, dict[str, Any]]] = []
    bindings = [
        {
            "user": "someone-else-key",
            "source": "local-override",
            "accounts": [{"adapter": "squire", "openId": "ou_first"}],
        }
    ]

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {
                "users": [
                    {"userKey": "gregpaws-key", "kind": "member", "owner": "gregpaws"}
                ]
            }
        if method == "identity.bindings.list":
            return {"bindings": list(bindings)}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    result = CliRunner().invoke(
        cli.app, _owner_open_id_command(home, channel, "ou_first", binding_code=None)
    )

    assert result.exit_code == 1
    assert "--binding-code is required" in json.loads(result.stdout)["error"]
    # This invocation supplied no binding code; only that path guarantees that
    # squire setup never calls identity.override.set.
    assert not [call for call in calls if call[0] == "identity.override.set"]


def test_setup_without_owner_member_fails_before_every_local_or_daemon_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.squire.setup import SetupStateStore
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    home.mkdir(mode=0o700)
    state = home / "state"
    channel = "channel:gregpaws:mac-studio:squire"
    code = SetupStateStore(state / "squire-setup.json").ensure_binding(
        "owner-key-1", channel
    ).code
    before = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {"users": []}
        if method == "identity.bindings.list":
            return {"bindings": []}
        if method == "identity.override.set":
            return {"user": params["user"], "source": "local-override"}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    rejected = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(
            home,
            channel,
            "ou_owner123",
            binding_code=code,
        ),
    )

    assert rejected.exit_code == 1
    result = json.loads(rejected.stdout)
    assert result["ok"] is False
    assert (
        "run `hyprial user add --kind member --owner gregpaws "
        "--confirmed-by <you>` first"
    ) in result["error"]
    assert calls == [("identity.users.list", {"kind": "member"})]
    assert {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    } == before
    assert not (state / "users.json").exists()
    assert not (state / "desired-state.json").exists()
    assert not (home / "squire").exists()


def test_setup_channel_change_reports_previous_account_without_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.squire.setup import SetupStateStore
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    old_channel = "channel:gregpaws:mac-studio:old-squire"
    new_channel = "channel:gregpaws:mac-studio:new-squire"
    initial = run_setup(home, "--channel", old_channel)
    assert initial.returncode == 0, initial.stderr
    code = SetupStateStore(home / "state" / "squire-setup.json").ensure_binding(
        "owner-key-1", new_channel
    ).code
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {
                "users": [
                    {"userKey": "gregpaws-key", "kind": "member", "owner": "gregpaws"}
                ]
            }
        if method == "identity.bindings.list":
            return {
                "bindings": [
                    {
                        "user": "gregpaws-key",
                        "source": "local-override",
                        "outboundOnly": True,
                        "accounts": [
                            {
                                "adapter": "old-squire",
                                "openId": "ou_old",
                                "source": "override",
                            }
                        ],
                    }
                ]
            }
        if method == "identity.override.set":
            return {"user": params["user"], "source": "local-override"}
        if method == "identity.override.clear":
            raise AssertionError("squire setup must never clear identity overrides")
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    changed = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, new_channel, "ou_new", binding_code=code),
    )

    assert changed.exit_code == 0, changed.stderr
    assert json.loads(changed.stdout)["staleOutboundAccounts"] == [
        {
            "adapter": "old-squire",
            "openId": "ou_old",
            "suggestedCommand": (
                "hyprial user unbind --adapter old-squire --open-id ou_old"
            ),
        }
    ]
    assert calls[-1][0] == "identity.override.set"
    assert not [call for call in calls if call[0] == "identity.override.clear"]
    profile = json.loads((home / "state" / "users.json").read_text(encoding="utf-8"))[
        "users"
    ][0]
    assert profile["squireChannel"] == new_channel


def _channel_change_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bindings: list[dict[str, Any]],
    override_set: Any = None,
) -> tuple[Path, str, str, list[tuple[str, dict[str, Any]]]]:
    from hyprial.daemon.impl.squire.setup import SetupStateStore

    home = tmp_path / "hyprial-home"
    old_channel = "channel:gregpaws:mac-studio:old-squire"
    new_channel = "channel:gregpaws:mac-studio:new-squire"
    assert run_setup(home, "--channel", old_channel).returncode == 0
    code = SetupStateStore(home / "state" / "squire-setup.json").ensure_binding(
        "owner-key-1", new_channel
    ).code
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return {
                "users": [
                    {"userKey": "gregpaws-key", "kind": "member", "owner": "gregpaws"}
                ]
            }
        if method == "identity.bindings.list":
            return {"bindings": bindings}
        if method == "identity.override.set":
            if override_set is not None:
                return override_set(params)
            return {"user": params["user"], "source": "local-override"}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    return home, new_channel, code, calls


def test_stale_hint_never_lists_an_account_that_backs_a_union_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 1037: the daemon's account-only row also lists accounts that sit
    # on the owner's union-keyed override; unbinding them would delete it.
    from hyprial.shell.impl.cli.runtime import entry as cli

    bindings = [
        {
            "user": "gregpaws-key",
            "source": "local-override",
            "unionId": "on_owner",
            "accounts": [
                {"adapter": "main", "openId": "ou_owner", "source": "observed"}
            ],
        },
        {
            "user": "gregpaws-key",
            "source": "local-override",
            "unionId": None,
            "outboundOnly": True,
            "accounts": [
                {"adapter": "main", "openId": "ou_owner", "source": "override"},
                {"adapter": "old-squire", "openId": "ou_old", "source": "override"},
            ],
        },
    ]
    home, new_channel, code, _calls = _channel_change_fixture(
        tmp_path, monkeypatch, bindings
    )

    changed = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, new_channel, "ou_new", binding_code=code),
    )

    assert changed.exit_code == 0, changed.stderr
    stale = json.loads(changed.stdout)["staleOutboundAccounts"]
    assert [entry["adapter"] for entry in stale] == ["old-squire"]
    assert "main" not in changed.stdout.replace('"main"', "")


def test_transient_daemon_failure_after_consume_says_the_code_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel import ipc_errors
    from hyprial.shell.impl.cli.runtime import entry as cli

    def unavailable(_params: dict[str, Any]) -> dict[str, Any]:
        raise ipc_errors.DaemonUnavailableError(
            ipc_errors.DaemonUnavailableError.CODE, "daemon unavailable"
        )

    home, new_channel, code, _calls = _channel_change_fixture(
        tmp_path, monkeypatch, [], override_set=unavailable
    )

    result = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, new_channel, "ou_new", binding_code=code),
    )

    assert result.exit_code == 1
    assert "the binding code is now used" in json.loads(result.stdout)["error"]


def test_setup_owner_open_id_renders_override_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    initial = payload(run_setup(home, "--channel", channel))
    code = initial["steps"][3]["bindingCode"]

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "identity.users.list":
            return {
                "users": [
                    {"userKey": "gregpaws-key", "kind": "member", "owner": "gregpaws"}
                ]
            }
        if method == "identity.bindings.list":
            return {"bindings": []}
        if method == "identity.override.set":
            raise _cli_support.CliError(
                ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN,
                "identity override forbidden",
            )
        raise AssertionError(f"unexpected method {method} {params}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    command = [
        "squire",
        "setup",
        "--owner-key",
        "owner-key-1",
        "--login-name",
        "gregpaws",
        "--machine",
        "mac-studio",
        "--machine-key",
        "machine-key-1",
        "--home",
        str(home / "squire"),
        "--step",
        "owner-open-id",
        "--channel",
        channel,
        "--owner-open-id",
        "ou_owner123",
        "--binding-code",
        code,
        "--json",
    ]

    rejected = CliRunner().invoke(cli.app, command)

    assert rejected.exit_code == 1
    result = json.loads(rejected.stdout)
    assert result["ok"] is False
    assert result["code"] == "IDENTITY_OVERRIDE_FORBIDDEN"
    # The failed write still spent its authorization; use a fresh challenge to
    # exercise the human renderer rather than accidentally asserting reuse.
    from hyprial.daemon.impl.squire.setup import SetupStateStore

    fresh_code = SetupStateStore(home / "state" / "squire-setup.json").ensure_binding(
        "owner-key-1", channel
    ).code
    human_command = [item for item in command if item != "--json"]
    human_command[human_command.index("--binding-code") + 1] = fresh_code
    human = CliRunner().invoke(cli.app, human_command)
    assert human.exit_code == 1
    assert "permission denied" in human.stderr.lower()
