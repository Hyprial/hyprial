"""Tests for ``hyprial adapter add`` — registering a new external (Lark) adapter.

Runs the CLI as a subprocess so it exercises the real argv/stdout/stderr
surface (the secret must never appear on argv or in output). Every test uses an
isolated ``HYPRIAL_HOME``/``HARNESS_STATE_DIR`` under ``tmp_path`` and strips
ambient ``HYPRIAL_ZENOH_LISTEN`` so nothing can touch a real ``~/.hyprial`` or the bus.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]

SECRET = "app-secret-must-never-be-printed-9f8c"


def _env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
    }
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    return environment


def run_add(
    home: Path,
    *arguments: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", "add", *arguments],
        cwd=ROOT,
        env=_env(home),
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _add_feishu(home: Path, *extra: str, stdin: str | None = SECRET + "\n") -> Any:
    return run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "dm=oc_native_1",
        "--json",
        *extra,
        stdin=stdin,
    )


def test_add_writes_channels_entry_and_0600_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _add_feishu(home)
    assert result.returncode == 0, result.stderr
    body = payload(result)
    assert body["ok"] is True
    assert body["adapter"] == {
        "provider": "lark",
        "name": "feishu",
        "appId": "cli_app_123",
        "credentialRef": "lark-feishu",
        "routes": [{"name": "dm", "type": "direct", "nativeId": "oc_native_1"}],
        "defaultRoute": "dm",
    }

    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert channels == {
        "version": 1,
        "gateways": [
            {
                "provider": "lark",
                "name": "feishu",
                "appId": "cli_app_123",
                "credentialRef": "lark-feishu",
                "routes": [
                    {"name": "dm", "type": "direct", "nativeId": "oc_native_1"}
                ],
                "defaultRoute": "dm",
            }
        ],
    }

    secret_path = home / "secrets" / "lark-feishu.json"
    assert json.loads(secret_path.read_text(encoding="utf-8")) == {"appSecret": SECRET}
    # The security-critical assertion: the credential file is owner-only.
    assert secret_path.stat().st_mode & 0o777 == 0o600
    # And it is not world- or group-readable.
    assert secret_path.stat().st_mode & 0o077 == 0
    assert (home / "secrets").stat().st_mode & 0o777 == 0o700


def test_channels_bytes_match_shared_atomic_writer(tmp_path: Path) -> None:
    """Freshly-added config must be byte-identical to the migration writer."""

    from hyprial.kernel import (
        ChannelConfiguration,
        ChannelRouteConfig,
        LarkGatewayConfig,
        atomic_json_write,
    )

    home = tmp_path / "home"
    assert _add_feishu(home).returncode == 0

    expected_config = ChannelConfiguration(
        gateways=(
            LarkGatewayConfig(
                name="feishu",
                app_id="cli_app_123",
                credential_ref="lark-feishu",
                routes=(
                    ChannelRouteConfig(
                        name="dm", type="direct", native_id="oc_native_1"
                    ),
                ),
                default_route="dm",
            ),
        )
    )
    expected_path = tmp_path / "expected-channels.json"
    atomic_json_write(expected_path, expected_config.to_json())
    assert (home / "channels.json").read_bytes() == expected_path.read_bytes()


def test_round_trips_through_daemon_config_loader(tmp_path: Path) -> None:
    """The daemon's own loader must accept what we wrote (adapter start finds it)."""

    from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
    from hyprial.daemon.impl.adapters.lark.worker.process import LarkWorkerLauncher
    from hyprial.kernel import PersistentConfigStore

    home = tmp_path / "home"
    assert _add_feishu(home).returncode == 0

    store = PersistentConfigStore(home, home)
    configuration = store.load()
    gateway = next(
        item for item in configuration.channels.gateways if item.name == "feishu"
    )
    assert gateway.app_id == "cli_app_123"
    assert gateway.credential_ref == "lark-feishu"
    assert store.lark_app_secret(gateway.credential_ref) == SECRET

    # The supervisor built at daemon boot backs `adapter start/list`.
    launcher = LarkWorkerLauncher(
        hyprial_home=home,
        state_dir=home,
        socket_path=home / "daemon.sock",
        config_store=store,
    )
    supervisor = AdapterRuntime(launcher=launcher, channels=configuration.channels)
    assert "feishu" in supervisor.gateway_names


def test_duplicate_name_without_force_errors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _add_feishu(home).returncode == 0

    clash = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_other",
        "--route",
        "dm=oc_other",
        "--json",
        stdin="different-secret\n",
    )
    assert clash.returncode != 0
    body = payload(clash)
    assert body["ok"] is False
    assert body["code"] == "ADAPTER_EXISTS"
    # The rejection message must not carry either secret.
    assert SECRET not in clash.stderr
    assert "different-secret" not in clash.stderr
    assert "different-secret" not in clash.stdout

    # The original entry and credential are untouched.
    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert channels["gateways"][0]["appId"] == "cli_app_123"
    secret = json.loads(
        (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8")
    )
    assert secret == {"appSecret": SECRET}


def test_force_overwrites_existing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _add_feishu(home).returncode == 0

    replaced = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_replacement",
        "--route",
        "dm=oc_replacement",
        "--force",
        "--json",
        stdin="replacement-secret\n",
    )
    assert replaced.returncode == 0, replaced.stderr
    body = payload(replaced)
    assert body["overwritten"] is True

    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert len(channels["gateways"]) == 1
    assert channels["gateways"][0]["appId"] == "cli_replacement"
    secret = json.loads(
        (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8")
    )
    assert secret == {"appSecret": "replacement-secret"}


def test_secret_never_appears_in_output_or_channels(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _add_feishu(home)
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    # The secret lives only in the 0600 credential file, never in channels.json.
    assert SECRET not in (home / "channels.json").read_text(encoding="utf-8")


def test_app_secret_flag_is_rejected_and_absent_from_help(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # There is deliberately no --app-secret flag: passing a literal secret on
    # argv (visible in `ps`) must be impossible.
    rejected = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "dm=oc_native_1",
        "--app-secret",
        "leaked-on-argv",
        "--json",
        stdin=SECRET + "\n",
    )
    assert rejected.returncode != 0
    # Nothing was written from the rejected invocation.
    assert not (home / "channels.json").exists()

    help_result = subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", "add", "--help"],
        cwd=ROOT,
        env=_env(home),
        text=True,
        capture_output=True,
        check=False,
    )
    assert "--app-secret" not in help_result.stdout
    assert "--secret-file" in help_result.stdout


def test_help_honors_no_color_and_dumb_terminal_in_ci(tmp_path: Path) -> None:
    for terminal_env in (
        {"NO_COLOR": "", "TERM": "xterm-256color"},
        {"TERM": "dumb"},
    ):
        environment = _env(tmp_path / terminal_env["TERM"].replace("-", "_"))
        environment.update({"GITHUB_ACTIONS": "true", **terminal_env})
        result = subprocess.run(
            [sys.executable, "-m", "hyprial.cli", "adapter", "add", "--help"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "\x1b[" not in result.stdout
        assert "--secret-file" in result.stdout


def test_secret_from_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")

    result = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "dm=oc_native_1",
        "--secret-file",
        str(secret_file),
        "--json",
        stdin="",
    )
    assert result.returncode == 0, result.stderr
    secret = json.loads(
        (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8")
    )
    assert secret == {"appSecret": SECRET}


def test_default_route_auto_set_for_single_route(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _add_feishu(home).returncode == 0
    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert channels["gateways"][0]["defaultRoute"] == "dm"


def test_multiple_routes_leave_default_unset_unless_specified(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "dm=oc_dm",
        "--route",
        "team=oc_team",
        "--json",
        stdin=SECRET + "\n",
    )
    assert result.returncode == 0, result.stderr
    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert "defaultRoute" not in channels["gateways"][0]

    home_two = tmp_path / "home2"
    with_default = run_add(
        home_two,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "dm=oc_dm",
        "--route",
        "team=oc_team",
        "--default-route",
        "team",
        "--json",
        stdin=SECRET + "\n",
    )
    assert with_default.returncode == 0, with_default.stderr
    channels_two = json.loads(
        (home_two / "channels.json").read_text(encoding="utf-8")
    )
    assert channels_two["gateways"][0]["defaultRoute"] == "team"


def test_bad_name_rejected_no_files_written(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for bad in ("../escape", "has/slash", "Has:Colon", "UPPER"):
        result = run_add(
            home,
            bad,
            "--app-id",
            "cli_app_123",
            "--route",
            "dm=oc_native_1",
            "--json",
            stdin=SECRET + "\n",
        )
        assert result.returncode != 0, bad
        assert payload(result)["code"] == "INVALID_ARGUMENT"
    # No stray secret files escaped the secrets directory either.
    assert not (home / "channels.json").exists()
    assert not list(tmp_path.glob("**/lark-*.json"))


def test_channels_write_failure_removes_orphan_new_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A failed add of a brand-new adapter must not leave an orphan secret."""

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl.bootstrap.adapter_registration import RouteInput, add_lark_gateway

    home = tmp_path / "home"
    real_writer = adapter_registration.atomic_json_write

    def flaky(path: Path, value: object) -> None:
        if path.name == "channels.json":
            raise OSError("simulated channels.json write failure")
        real_writer(path, value)

    monkeypatch.setattr(adapter_registration, "atomic_json_write", flaky)

    with pytest.raises(OSError):
        add_lark_gateway(
            hyprial_home=home,
            name="feishu",
            app_id="cli_app_123",
            app_secret=SECRET,
            routes=[RouteInput(name="dm", native_id="oc_native_1")],
        )
    assert not (home / "secrets" / "lark-feishu.json").exists()
    assert not (home / "channels.json").exists()


def test_force_write_failure_preserves_original_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A failed --force overwrite must leave the prior working credential intact."""

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl.bootstrap.adapter_registration import RouteInput, add_lark_gateway

    home = tmp_path / "home"
    # A first, successful registration establishes the working credential.
    add_lark_gateway(
        hyprial_home=home,
        name="feishu",
        app_id="cli_original",
        app_secret="original-secret",
        routes=[RouteInput(name="dm", native_id="oc_original")],
    )

    real_writer = adapter_registration.atomic_json_write

    def flaky(path: Path, value: object) -> None:
        if path.name == "channels.json":
            raise OSError("simulated channels.json write failure")
        real_writer(path, value)

    monkeypatch.setattr(adapter_registration, "atomic_json_write", flaky)

    with pytest.raises(OSError):
        add_lark_gateway(
            hyprial_home=home,
            name="feishu",
            app_id="cli_replacement",
            app_secret="replacement-secret",
            routes=[RouteInput(name="dm", native_id="oc_replacement")],
            force=True,
        )

    # The credential and channel entry are exactly as before: a no-op.
    secret = json.loads(
        (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8")
    )
    assert secret == {"appSecret": "original-secret"}
    assert (home / "secrets" / "lark-feishu.json").stat().st_mode & 0o777 == 0o600
    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert channels["gateways"][0]["appId"] == "cli_original"


def test_missing_route_errors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--json",
        stdin=SECRET + "\n",
    )
    assert result.returncode != 0
    assert payload(result)["code"] == "INVALID_ARGUMENT"


def test_bad_route_format_errors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_add(
        home,
        "feishu",
        "--app-id",
        "cli_app_123",
        "--route",
        "no-equals-sign",
        "--json",
        stdin=SECRET + "\n",
    )
    assert result.returncode != 0
    assert payload(result)["code"] == "INVALID_ARGUMENT"


# 2026-09-26 allen-channel: `adapter add --secret-file` was given onboard's own
# secrets/lark-<name>.json ({"appSecret": ...}) and stored its whole JSON text
# as the secret (53 chars), which surfaced only later as Lark 10014.  A JSON
# object is now read for its appSecret; any other JSON shape is refused.


def test_an_onboard_shaped_secret_file_stores_the_bare_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    secret_file = tmp_path / "lark-feishu.json"
    secret_file.write_text(json.dumps({"appSecret": SECRET}, indent=2) + "\n", encoding="utf-8")

    result = run_add(
        home, "feishu", "--app-id", "cli_app_123", "--route", "dm=oc_native_1",
        "--secret-file", str(secret_file), "--json", stdin="",
    )
    assert result.returncode == 0, result.stderr
    stored = json.loads((home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8"))
    assert stored == {"appSecret": SECRET}


def test_an_onboard_shaped_secret_on_stdin_stores_the_bare_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_add(
        home, "feishu", "--app-id", "cli_app_123", "--route", "dm=oc_native_1",
        "--json", stdin=json.dumps({"appSecret": SECRET}) + "\n",
    )
    assert result.returncode == 0, result.stderr
    stored = json.loads((home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8"))
    assert stored == {"appSecret": SECRET}


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"secret": SECRET}),  # an object without appSecret
        json.dumps({"appSecret": ""}),  # an empty appSecret
        json.dumps([SECRET]),  # JSON, not an object
        '{"appSecret": "' + SECRET,  # starts like JSON, does not parse
    ],
)
def test_any_other_json_shaped_secret_is_refused_and_nothing_is_written(
    tmp_path: Path, text: str
) -> None:
    home = tmp_path / "home"
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(text + "\n", encoding="utf-8")

    result = run_add(
        home, "feishu", "--app-id", "cli_app_123", "--route", "dm=oc_native_1",
        "--secret-file", str(secret_file), "--json", stdin="",
    )
    assert result.returncode != 0
    assert payload(result)["code"] == "SECRET_FORMAT_INVALID"
    assert SECRET not in result.stdout + result.stderr
    assert not (home / "secrets" / "lark-feishu.json").exists()
