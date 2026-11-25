"""First run creates no agent and no messaging configuration.

Decision: retire-first-run-onboarding-20261120.  ``hyprial init`` and
``hyprial login`` create the home, start the daemon and sign the user in; they
issue no ``agent.*``, ``adapter.*`` or ``management.squire.*`` request.  The
retired ``hyprial init onboarding`` group is not registered, so every old
subcommand and step id exits 2 before any daemon contact.

Both cases execute the real command flows; only the lowest external ports are
replaced: the daemon process launch, the CLI's daemon request transport, and
the identity service's network calls.  Every daemon method the flow asks for
is recorded, and the isolated home/state roots are checked afterwards for
persistent agent, adapter, route or pin state: the registry and adapter state
databases, the home-side adapter config (``channels.json`` holds gateways,
routes and their pins) and the agent workspaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the CLI composition)
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services

SERVING = {
    "running": True,
    "pid": 4242,
    "epoch": "first-run",
    "phase": "serving",
    "owner": "first-run-owner",
    "zenoh": {"listen": ["tcp/127.0.0.1:1"], "connect": []},
}

FIRST_RUN_PREFIXES = ("agent.", "adapter.", "management.squire")

MESSAGING_STATE = (
    ("state", "agents.sqlite3"),
    ("state", "adapters.sqlite3"),
    ("home", "channels.json"),
    ("home", "agents"),
    ("home", "adapters"),
)


def _record_and_fail(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every daemon method a command asks for; answer as no daemon."""

    calls: list[str] = []

    def request(method: str, _params: Any = None, **_kwargs: Any) -> Any:
        calls.append(method)
        raise ipc_errors.DaemonUnavailableError("no daemon")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def _forbidden(calls: list[str]) -> list[str]:
    return [
        method for method in calls if method.startswith(FIRST_RUN_PREFIXES)
    ]


def _persistent_messaging_state(home: Path, state_dir: Path) -> list[str]:
    roots = {"home": home, "state": state_dir}
    return sorted(
        f"{root}/{name}"
        for root, name in MESSAGING_STATE
        if (roots[root] / name).exists()
    )


def test_init_creates_no_agent_and_no_messaging_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HYPRIAL_OWNER", "first-run-owner")
    calls = _record_and_fail(monkeypatch)

    launches: list[dict[str, Any]] = []

    def launch(**kwargs: Any) -> DaemonLaunchResult:
        launches.append(kwargs)
        return DaemonLaunchResult.launched(SERVING)

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["ok"] is True
    assert launches, "init must still start the daemon"
    assert calls, "the flow must have asked the daemon at least its readiness probe"
    assert _forbidden(calls) == []
    assert _persistent_messaging_state(home, state_dir) == []


def test_login_creates_no_agent_and_no_messaging_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "first-run-owner"}), encoding="utf-8"
    )
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HYPRIAL_OWNER", "first-run-owner")
    monkeypatch.delenv("HYPRIAL_TAILCAT_BINARY", raising=False)
    profile, _source = _cli_get_services().resolve_profile()

    calls: list[str] = []
    launches: list[dict[str, Any]] = []
    daemon_state = {"running": False}
    serving_status = {
        **SERVING,
        "owner": "first-run-owner",
        "identityMode": "casdoor",
        "identityIssuer": profile.issuer,
        "migration": {"status": "applied"},
    }

    def request(method: str, _params: Any = None, **_kwargs: Any) -> Any:
        calls.append(method)
        if not daemon_state["running"]:
            raise ipc_errors.DaemonUnavailableError("no daemon")
        if method == "ping":
            return dict(serving_status)
        if method == "auth.relogin":
            return {
                "ok": True,
                "operationId": "op-first-run-relogin",
                "intent": "login",
                "state": "pending",
                "verification": {
                    "verificationUri": "https://issuer.example/device?code=fixture",
                    "userCode": "FIXTURE-CODE",
                    "expiresAt": "2026-11-23T10:00:00Z",
                    "intervalSeconds": 5,
                },
                "owner": None,
                "completion": {
                    "stage": "authorizing",
                    "identity": None,
                    "device": None,
                    "failure": None,
                },
            }
        if method == "auth.operation.get":
            return {
                "ok": True,
                "operation": {
                    "operationId": "op-first-run-relogin",
                    "kind": "login",
                    "state": "authenticated",
                    "owner": "first-run-owner",
                    "receiptId": "receipt-first-run-relogin",
                    "verification": None,
                    "completion": {
                        "stage": "serving",
                        "identity": {
                            "mode": "casdoor",
                            "issuer": profile.issuer,
                            "source": "profile",
                        },
                        "device": {
                            "ready": True,
                            "deviceId": "first-run-node",
                            "keyGeneration": 1,
                            "userKey": {
                                "state": "unbound",
                                "code": "USER_KEY_UNBOUND",
                            },
                            "error": None,
                            "retry": None,
                        },
                        "failure": None,
                    },
                },
            }
        raise ipc_errors.DaemonUnavailableError("no daemon")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    def launch(**_kwargs: Any) -> DaemonLaunchResult:
        launches.append(dict(_kwargs))
        daemon_state["running"] = True
        return DaemonLaunchResult.launched(dict(serving_status))

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)

    result = CliRunner().invoke(_cli_root_app, ["login", "--json", "--no-open"])

    assert result.exit_code == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["identity"]["status"] == "authenticated"
    assert launches, "the verified login must start the replacement daemon"
    assert calls, "the flow must have asked the daemon at least its readiness probe"
    assert "auth.relogin" in calls
    assert "auth.operation.get" in calls
    assert _forbidden(calls) == []
    assert _persistent_messaging_state(home, state_dir) == []


def test_the_services_no_longer_carry_an_onboarding_seam() -> None:
    services = _cli_get_services()

    assert not hasattr(services, "_onboarding_snapshot")
    assert not hasattr(services, "_onboarding_apply_actions")


@pytest.mark.parametrize(
    "argv",
    [
        ("init", "onboarding"),
        ("init", "onboarding", "plan", "--json"),
        ("init", "onboarding", "apply", "--auto", "--json"),
        ("init", "onboarding", "apply", "runtime-ready", "--json"),
        ("init", "onboarding", "apply", "default-agent", "--json"),
        ("init", "onboarding", "apply", "worker-agent", "--json"),
        ("init", "onboarding", "apply", "squire", "--json"),
        ("init", "onboarding", "apply", "lark-app", "--json"),
        ("init", "onboarding", "apply", "lark-authorize", "--json"),
        ("init", "onboarding", "apply", "lark-route", "--json"),
        ("init", "onboarding", "apply", "lark-pin", "--json"),
        ("init", "onboarding", "apply", "lark-roundtrip", "--json"),
        ("init", "onboarding", "harnesses", "--json"),
        ("init", "onboarding", "harness-select", "pi", "--json"),
        ("init", "onboarding", "harness-install", "pi", "--consent", "--json"),
    ],
)
def test_retired_onboarding_commands_fail_before_any_daemon_contact(
    monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    calls = _record_and_fail(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, list(argv))

    assert result.exit_code == 2, result.exception
    assert result.stdout == ""
    assert "No such command 'onboarding'" in result.output
    assert calls == []
