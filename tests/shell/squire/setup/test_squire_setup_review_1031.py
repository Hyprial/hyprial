from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support


def _owner_open_id_command(
    home: Path,
    channel: str,
    open_id: str,
    *,
    binding_code: str | None,
    json_output: bool = True,
) -> list[str]:
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
        open_id,
    ]
    if binding_code is not None:
        command.extend(("--binding-code", binding_code))
    if json_output:
        command.append("--json")
    return command


def _prepare_binding(home: Path, channel: str) -> str:
    from hyprial.daemon.impl.squire.setup import SetupStateStore

    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return SetupStateStore(home / "state" / "squire-setup.json").ensure_binding(
        "owner-key-1", channel
    ).code


def _configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    daemon_request: Any,
) -> None:

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)


def _member_rows() -> dict[str, list[dict[str, str]]]:
    return {
        "users": [
            {
                "userKey": "gregpaws-key",
                "kind": "member",
                "owner": "gregpaws",
            }
        ]
    }


def _snapshot_without_spent_challenge(home: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
        and path.name != "squire-setup.json"
        and not path.name.endswith(("-wal", "-shm"))
    }


def test_setup_hints_other_adapter_account_only_rows_without_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli
    from tests.shell.squire.setup.test_squire_setup_cli import run_setup

    home = tmp_path / "hyprial-home"
    old_channel = "channel:gregpaws:mac-studio:old-squire"
    new_channel = "channel:gregpaws:mac-studio:new-squire"
    initial = run_setup(home, "--channel", old_channel)
    assert initial.returncode == 0, initial.stderr
    code = _prepare_binding(home, new_channel)
    calls: list[tuple[str, dict[str, Any]]] = []
    bindings: list[dict[str, Any]] = [
        {
            "user": "gregpaws-key",
            "source": "local-override",
            "outboundOnly": True,
            "accounts": [
                {
                    "adapter": "old-squire",
                    "openId": "ou_old_account",
                    "source": "override",
                }
            ],
        },
        {
            "user": "gregpaws-key",
            "source": "local-override",
            "unionId": "on_owner",
            "outboundOnly": False,
            "accounts": [
                {
                    "adapter": "old-squire",
                    "openId": "ou_union_account",
                    "source": "observed",
                }
            ],
        },
    ]

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return _member_rows()
        if method == "identity.bindings.list":
            return {"bindings": list(bindings)}
        if method == "identity.override.set":
            row = {
                "user": params["user"],
                "source": "local-override",
                "outboundOnly": True,
                "accounts": [
                    {"adapter": params["adapter"], "openId": params["openId"]}
                ],
            }
            bindings.append(row)
            return row
        if method == "identity.override.clear":
            raise AssertionError("squire setup must never clear identity overrides")
        raise AssertionError(f"unexpected method {method}")

    _configure_cli(monkeypatch, home, daemon_request)

    configured = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(
            home, new_channel, "ou_new_account", binding_code=code
        ),
    )

    assert configured.exit_code == 0, configured.stderr or configured.stdout
    result = json.loads(configured.stdout)
    assert result["staleOutboundAccounts"] == [
        {
            "adapter": "old-squire",
            "openId": "ou_old_account",
            "suggestedCommand": (
                "hyprial user unbind --adapter old-squire --open-id ou_old_account"
            ),
        }
    ]
    assert "ou_union_account" not in configured.stdout
    assert not [call for call in calls if call[0] == "identity.override.clear"]

    repeated = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(
            home,
            new_channel,
            "ou_new_account",
            binding_code=None,
            json_output=False,
        ),
    )

    assert repeated.exit_code == 0, repeated.stderr or repeated.stdout
    assert (
        "previous outbound-only account old-squire ou_old_account is still bound; "
        "remove it with `hyprial user unbind --adapter old-squire --open-id "
        "ou_old_account` if it is no longer valid"
    ) in repeated.stdout
    assert "ou_union_account" not in repeated.stdout
    assert len([call for call in calls if call[0] == "identity.override.set"]) == 1
    assert not [call for call in calls if call[0] == "identity.override.clear"]


def test_override_failure_after_consume_leaves_scaffold_profile_and_channel_unwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    code = _prepare_binding(home, channel)
    before = _snapshot_without_spent_challenge(home)
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return _member_rows()
        if method == "identity.bindings.list":
            return {"bindings": []}
        if method == "identity.override.set":
            raise _cli_support.CliError(
                ipc_errors.IDENTITY_CONFLICT,
                "squire ou_owner belongs to a different user",
            )
        raise AssertionError(f"unexpected method {method}")

    _configure_cli(monkeypatch, home, daemon_request)

    rejected = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, channel, "ou_owner", binding_code=code),
    )

    assert rejected.exit_code == 1
    result = json.loads(rejected.stdout)
    assert result["code"] == ipc_errors.IDENTITY_CONFLICT
    assert "binding code is now used" in result["error"]
    assert "rerun step 3 for a new one" in result["error"]
    assert [method for method, _params in calls] == [
        "identity.users.list",
        "identity.bindings.list",
        "identity.override.set",
    ]
    assert _snapshot_without_spent_challenge(home) == before
    assert not (home / "state" / "users.json").exists()
    assert not (home / "state" / "desired-state.json").exists()
    assert not (home / "squire").exists()


def test_setup_failure_after_override_reruns_without_code_or_second_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon import SquireSetup
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"
    code = _prepare_binding(home, channel)
    calls: list[tuple[str, dict[str, Any]]] = []
    bindings: list[dict[str, Any]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "identity.users.list":
            return _member_rows()
        if method == "identity.bindings.list":
            return {"bindings": list(bindings)}
        if method == "identity.override.set":
            row = {
                "user": params["user"],
                "source": "local-override",
                "outboundOnly": True,
                "accounts": [
                    {"adapter": params["adapter"], "openId": params["openId"]}
                ],
            }
            bindings.append(row)
            return row
        raise AssertionError(f"unexpected method {method}")

    original_run = SquireSetup.run
    attempts = 0

    def fail_once(self: SquireSetup, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("forced setup.run failure after the durable binding")
        return original_run(self, *args, **kwargs)

    _configure_cli(monkeypatch, home, daemon_request)
    monkeypatch.setattr(SquireSetup, "run", fail_once)

    failed = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, channel, "ou_owner", binding_code=code),
    )
    rerun = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(home, channel, "ou_owner", binding_code=None),
    )

    assert failed.exit_code == 1
    assert "forced setup.run failure" in json.loads(failed.stdout)["error"]
    assert rerun.exit_code == 0, rerun.stderr or rerun.stdout
    assert attempts == 2
    assert len([call for call in calls if call[0] == "identity.override.set"]) == 1
    assert [method for method, _params in calls] == [
        "identity.users.list",
        "identity.bindings.list",
        "identity.override.set",
        "identity.users.list",
        "identity.bindings.list",
    ]
