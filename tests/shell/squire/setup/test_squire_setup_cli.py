from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common import support as _cli_support



def run_setup(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        # squire setup's owner is resolved, not flagged (U5): the identity
        # comes from HYPRIAL_OWNER / settings.json like every other consumer.
        "HYPRIAL_OWNER": "gregpaws",
    }
    environment.pop("HYPRIAL_NODE_ID", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hyprial.cli",
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
            *arguments,
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_setup_creates_seven_step_squire_plan_and_is_byte_idempotent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"
    channel = "channel:gregpaws:mac-studio:squire"

    first = run_setup(home, "--channel", channel)

    assert first.returncode == 0, first.stderr
    result = payload(first)
    assert result["ok"] is True
    assert result["setup"] == "squire"
    assert result["schemaVersion"] == 1
    assert result["complete"] is False
    assert [step["id"] for step in result["steps"]] == [
        "create-squire",
        "create-app",
        "visibility",
        "owner-open-id",
        "owner-access",
        "claude-plugin",
        "service",
    ]
    assert result["steps"][0]["status"] == "complete"
    assert result["steps"][1]["status"] == "deferred"
    assert result["steps"][1]["code"] == "APP_CREATION_DEFERRED"
    binding = result["steps"][3]
    assert binding["status"] == "actionRequired"
    assert isinstance(binding["bindingCode"], str) and binding["bindingCode"]

    from hyprial.daemon.impl.desired_state import DesiredStateStore

    users_path = home / "state" / "users.json"
    desired = DesiredStateStore(
        home / "state" / "desired-state.json"
    ).load().to_json()
    assert desired["providers"] == [
        {
            "provider": "pi",
            "name": "squire",
            "headless": True,
            "args": [
                "--provider",
                "deepseek",
                "--model",
                "deepseek-flash",
                "--skill",
                str(home / "agents" / "squire" / "config" / "shared" / "skills" / "squire"),
                "--approve",
            ],
            "ownership": "managed",
            "nickname": "Squire",
            "cwd": str(home / "agents" / "squire" / "workspace"),
            "sessionRef": "squire:owner-key-1:machine-key-1",
        }
    ]
    profile = json.loads(users_path.read_text(encoding="utf-8"))["users"][0]
    assert profile == {
        "owner": "gregpaws",
        "ownerKey": "owner-key-1",
        "loginName": "gregpaws",
        "agents": ["agent:gregpaws:mac-studio:squire"],
        "squireChannel": channel,
        "preferredReceiver": {
            "machine": "mac-studio",
            "machineKey": "machine-key-1",
        },
        "notificationRules": [],
    }
    store = DesiredStateStore(home / "state" / "desired-state.json")
    before = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm"))
        and path.name != "lifecycle-operations.sqlite3"
    }

    second = run_setup(home, "--channel", channel)

    assert second.returncode == 0, second.stderr
    second_result = payload(second)
    assert second_result["changed"] == []
    assert second_result["steps"][3]["bindingCode"] == binding["bindingCode"]
    assert {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm"))
        and path.name != "lifecycle-operations.sqlite3"
    } == before
    # Document-level idempotence for the excluded live database.
    assert store.load().to_json() == desired


def _owner_open_id_command(
    home: Path,
    channel: str,
    open_id: str,
    *,
    binding_code: str | None,
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
    return [*command, "--json"]



def test_setup_rejects_open_id_without_channel_before_writing_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"

    rejected = run_setup(home, "--owner-open-id", "ou_owner123")

    assert rejected.returncode == 1
    result = payload(rejected)
    assert result["ok"] is False
    assert result["code"] == "INVALID_ARGUMENT"
    assert "--channel" in result["error"]
    assert not (home / "state" / "lifecycle-operations.sqlite3").exists()
    assert not (home / "state" / "users.json").exists()


def test_setup_supports_isolated_single_step_reruns(tmp_path: Path) -> None:
    home = tmp_path / "hyprial-home"

    app_only = run_setup(home, "--step", "create-app")

    assert app_only.returncode == 0, app_only.stderr
    app_result = payload(app_only)
    assert [step["id"] for step in app_result["steps"]] == ["create-app"]
    assert app_result["steps"][0]["status"] == "deferred"
    assert app_result["changed"] == []
    assert not (home / "state" / "lifecycle-operations.sqlite3").exists()
    assert not (home / "state" / "users.json").exists()

    create_only = run_setup(home, "--step", "create-squire")

    assert create_only.returncode == 0, create_only.stderr
    create_result = payload(create_only)
    assert [step["id"] for step in create_result["steps"]] == ["create-squire"]
    assert create_result["steps"][0]["status"] == "complete"
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    assert DesiredStateStore(
        home / "state" / "desired-state.json"
    ).load().harnesses
    assert (home / "state" / "users.json").is_file()


def test_setup_rejects_unknown_single_step_without_writing_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"

    rejected = run_setup(home, "--step", "invent-app")

    assert rejected.returncode == 1
    result = payload(rejected)
    assert result["code"] == "INVALID_ARGUMENT"
    assert "--step must be one of" in result["error"]
    assert not (home / "state" / "lifecycle-operations.sqlite3").exists()


def test_wrong_channel_binding_code_rejects_without_partial_profile_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    home = tmp_path / "hyprial-home"
    original_channel = "channel:gregpaws:mac-studio:squire"
    wrong_channel = "channel:gregpaws:other-machine:squire"
    initial = payload(run_setup(home, "--channel", original_channel))
    code = initial["steps"][3]["bindingCode"]
    users_path = home / "state" / "users.json"
    before = users_path.read_bytes()

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
            return {"bindings": []}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "gregpaws")
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)

    rejected = CliRunner().invoke(
        cli.app,
        _owner_open_id_command(
            home, wrong_channel, "ou_owner123", binding_code=code
        ),
    )

    assert rejected.exit_code == 1
    result = json.loads(rejected.stdout)
    assert result["code"] == "INVALID_ARGUMENT"
    assert "invalid or expired" in result["error"]
    assert users_path.read_bytes() == before
    assert calls == [
        ("identity.users.list", {"kind": "member"}),
        ("identity.bindings.list", {}),
    ]


def test_setup_state_files_are_private_and_channel_change_stays_unbound_locally(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"
    first_channel = "channel:gregpaws:mac-studio:squire"
    second_channel = "channel:gregpaws:mac-studio:squire-2"
    initial = run_setup(home, "--channel", first_channel)
    assert initial.returncode == 0, initial.stderr

    changed = run_setup(home, "--channel", second_channel)

    assert changed.returncode == 0, changed.stderr
    result = payload(changed)
    assert "users.squireChannel" in result["changed"]
    profile = json.loads((home / "state" / "users.json").read_text(encoding="utf-8"))[
        "users"
    ][0]
    assert profile["squireChannel"] == second_channel
    assert "ownerOpenId" not in profile
    for path in (
        home / "state" / "lifecycle-operations.sqlite3",
        home / "state" / "users.json",
        home / "state" / "squire-setup.json",
    ):
        assert path.stat().st_mode & 0o777 == 0o600


def run_setup_derived(home: Path, *arguments: str, node_id: str | None = None):
    """The no-flags first run: owner from identity, the four lookup keys derived."""

    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_OWNER": "gregpaws",
    }
    environment.pop("HYPRIAL_NODE_ID", None)
    if node_id is not None:
        environment["HYPRIAL_NODE_ID"] = node_id
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hyprial.cli",
            "squire",
            "setup",
            "--home",
            str(home / "squire"),
            *arguments,
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_without_flags_derives_identity_from_owner_and_hostname(
    tmp_path: Path,
) -> None:
    """Acceptance (Allen, 2026-09-18): fresh home, no HYPRIAL_NODE_ID, no
    other flags — setup exits 0 and the actor's machine segment is the
    hostname; owner_key/machine_key are slugs and login_name is the platform
    login."""

    import pwd

    from hyprial.identity import identity_slug

    home = tmp_path / "hyprial-home"
    hostname = socket.gethostname()

    completed = run_setup_derived(home)

    assert completed.returncode == 0, completed.stderr
    result = payload(completed)
    assert result["ok"] is True
    assert result["squire"]["actor"] == f"agent:gregpaws:{hostname}:squire"
    assert result["squire"]["sessionRef"] == f"squire:gregpaws:{identity_slug(hostname)}"
    profile = json.loads((home / "state" / "users.json").read_text(encoding="utf-8"))[
        "users"
    ][0]
    assert profile["owner"] == "gregpaws"
    assert profile["ownerKey"] == "gregpaws"  # slug of the resolved owner
    assert profile["loginName"] == pwd.getpwuid(os.getuid()).pw_name  # platform login
    assert profile["preferredReceiver"]["machine"] == hostname
    assert profile["preferredReceiver"]["machineKey"] == identity_slug(hostname)


def test_setup_without_flags_derives_machine_from_configured_node_id(
    tmp_path: Path,
) -> None:
    """Acceptance: with HYPRIAL_NODE_ID exported, the derived machine segment
    equals the node id (not the hostname)."""

    home = tmp_path / "hyprial-home"

    completed = run_setup_derived(home, node_id="mac-studio")

    assert completed.returncode == 0, completed.stderr
    result = payload(completed)
    assert result["squire"]["actor"] == "agent:gregpaws:mac-studio:squire"
    assert result["squire"]["sessionRef"] == "squire:gregpaws:mac-studio"


def run_setup_with_node(home: Path, node_id: str, *arguments: str):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_NODE_ID": node_id,
        # Resolved owner, not a flag (U5): same identity the daemon would
        # resolve for this home.
        "HYPRIAL_OWNER": "gregpaws",
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hyprial.cli",
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
            *arguments,
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def write_channels(home: Path, *names: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": name,
                        "appId": f"cli_{name}",
                        "credentialRef": f"lark-{name}",
                        "routes": [],
                    }
                    for name in names
                ],
            }
        ),
        encoding="utf-8",
    )


def test_setup_rejects_channel_naming_an_unconfigured_adapter(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"
    write_channels(home, "mac-studio-feishu")

    rejected = run_setup(home, "--channel", "channel:gregpaws:mac-studio:squire")

    assert rejected.returncode == 1
    result = payload(rejected)
    assert result["ok"] is False
    assert result["code"] == "INVALID_ARGUMENT"
    assert "squire" in result["error"]
    assert "mac-studio-feishu" in result["error"]
    # validation must happen before any state is written
    assert not (home / "state" / "users.json").exists()
    assert not (home / "state" / "lifecycle-operations.sqlite3").exists()


def test_setup_accepts_channel_naming_a_configured_adapter(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"
    write_channels(home, "mac-studio-feishu")

    accepted = run_setup(
        home, "--channel", "channel:gregpaws:mac-studio:mac-studio-feishu"
    )

    assert accepted.returncode == 0, accepted.stderr
    profile = json.loads((home / "state" / "users.json").read_text(encoding="utf-8"))[
        "users"
    ][0]
    assert profile["squireChannel"] == "channel:gregpaws:mac-studio:mac-studio-feishu"


def test_setup_warns_when_channels_json_is_absent(tmp_path: Path) -> None:
    home = tmp_path / "hyprial-home"

    result = payload(run_setup(home, "--channel", "channel:gregpaws:mac-studio:squire"))

    assert any("channels.json" in warning for warning in result["warnings"])


def test_setup_rejects_machine_mismatch_with_configured_node_id(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"

    rejected = run_setup_with_node(home, "macbook")

    assert rejected.returncode == 1
    result = payload(rejected)
    assert result["ok"] is False
    assert result["code"] == "INVALID_ARGUMENT"
    assert "HYPRIAL_NODE_ID" in result["error"]
    assert "macbook" in result["error"]
    assert not (home / "state" / "users.json").exists()


def test_setup_rejects_explicit_missing_adapter_before_public_seam_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "hyprial-home"

    rejected = run_setup(home, "--adapter", "missing-squire")

    assert rejected.returncode == 1
    result = payload(rejected)
    assert result["code"] == "INVALID_ARGUMENT"
    assert "hyprial adapter add missing-squire --new" in result["error"]
    assert not (home / "squire").exists()
    assert not (home / "state" / "agents.sqlite3").exists()
    assert not (home / "state" / "lifecycle-operations.sqlite3").exists()


def test_setup_accepts_public_agent_configuration_overrides(tmp_path: Path) -> None:
    home = tmp_path / "hyprial-home"

    completed = run_setup(
        home,
        "--provider",
        "openai",
        "--model",
        "gpt-5.6",
        "--preferred-harness",
        "codex",
    )

    assert completed.returncode == 0, completed.stderr
    result = payload(completed)
    assert result["squire"]["provider"] == "openai"
    assert result["squire"]["model"] == "gpt-5.6"
    assert result["squire"]["preferredHarness"] == "codex"
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    desired = DesiredStateStore(home / "state" / "desired-state.json").load().to_json()
    assert desired["providers"][0]["provider"] == "codex"
    assert desired["providers"][0]["args"] == ["--model", "gpt-5.6"]
