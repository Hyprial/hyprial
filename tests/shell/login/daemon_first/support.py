"""A scripted daemon for the CLI's daemon-run first login.

Decision: daemon-first-z5-cli-tests-20261119.  The request client and the launch
primitive are replaced on the explicit service bindings only; the identity
service is never contacted and no daemon, service unit or home outside the
per-test one is involved.  ``run_login`` and ``_run_login_cli_flow`` are replaced
by recorders that fail the test: a first login must not reach the in-process
engine.  The answers below are validated against the daemon's published schemas
(``test_contract.py``), so the scripted daemon cannot drift from the contract.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.services import get_services

OWNER = "alicesmith"
OPERATION_ID = "op-first-login-0001"
URI = "https://id.example.test/device?user_code=ABCD-EFGH"
CODE = "ABCD-EFGH"

BOOTSTRAP = {"running": True, "pid": 4242, "epoch": "epoch-boot", "phase": "bootstrap", "owner": None}
SERVING = {"running": True, "pid": 4242, "epoch": "epoch-serving", "phase": "serving", "owner": OWNER}

DEVICE_READY = {
    "ready": True,
    "deviceId": "scripted-node",
    "keyGeneration": 1,
    "userKey": {"state": "unbound", "code": "USER_KEY_UNBOUND"},
    "error": None,
    "retry": None,
}
DEVICE_NOT_READY = {
    "ready": False,
    "deviceId": None,
    "keyGeneration": None,
    "userKey": {"state": "unavailable", "code": "USER_KEY_ABSENT"},
    "error": {"code": "DEVICE_ENSURE_FAILED", "message": "the device could not be registered during this login"},
    "retry": "serving-start",
}
IDENTITY = {"mode": "casdoor", "issuer": "https://id.example.test", "source": "profile.json"}


def begin_result() -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": OPERATION_ID,
        "intent": "login",
        "state": "pending",
        "verification": {
            "verificationUri": URI,
            "userCode": CODE,
            "expiresAt": "2026-11-19T10:00:00Z",
            "intervalSeconds": 5,
        },
        "owner": None,
        "completion": {"stage": "authorizing", "identity": None, "device": None, "failure": None},
    }


def answer(*, state: str = "authenticated", stage: str = "serving", device: dict | None = DEVICE_READY,
           failure: dict | None = None, owner: str | None = OWNER) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": {
            "operationId": OPERATION_ID,
            "kind": "login",
            "state": state,
            "owner": owner,
            "receiptId": "receipt-first-login-0001",
            "verification": None,
            "completion": {
                "stage": stage,
                "identity": dict(IDENTITY) if state == "authenticated" else None,
                "device": None if device is None else dict(device),
                "failure": failure,
            },
        },
    }


@dataclass
class Call:
    method: str
    params: dict[str, Any] | None
    options: dict[str, Any]


@dataclass
class World:
    home: Path
    state: Path
    phase: str | None = None
    calls: list[Call] = field(default_factory=list)
    launches: list[dict[str, Any]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    pickups: list[Path] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)
    owned: list[dict[str, Any]] = field(default_factory=list)
    owned_allowed: bool = False
    already_running: bool = False

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def auth(self) -> list[Call]:
        return [call for call in self.calls if call.method.startswith("auth.")]

    def login(self, *arguments: str, json_output: bool = True):
        argv = ["login", *arguments] + (["--json"] if json_output else [])
        return CliRunner().invoke(app, argv)

    def payload(self, result) -> dict[str, Any]:
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1, (result.exit_code, result.stdout, result.stderr)
        return json.loads(lines[0])


def _write_home(home: Path, cell: str) -> None:
    if cell == "missing":
        return
    home.mkdir(mode=0o700, exist_ok=True)
    settings: Any = {"version": 1}
    if cell == "owner":
        settings = {"version": 1, "owner": "old-owner", "identityIssuer": "https://id.example.test"}
    text = "{not json" if cell == "malformed" else json.dumps(settings)
    (home / "settings.json").write_text(text, encoding="utf-8")


@pytest.fixture
def make_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def build(*, cell: str = "ownerless", phase: str | None = None, env_owner: bool = False) -> World:
        home, state = tmp_path / "home", tmp_path / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
        monkeypatch.setenv("HYPRIAL_HOME", str(home))
        monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
        monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
        if env_owner:
            monkeypatch.setenv("HYPRIAL_OWNER", "env-owner")
        _write_home(home, cell)
        world = World(home=home, state=state, phase=phase)
        install(world, monkeypatch)
        return world

    return build


def install(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    services = get_services()
    profile = NetworkProfile(org="acme", issuer="https://id.example.test", client_id="scripted-client")

    def ping() -> dict[str, Any]:
        if world.phase is None:
            raise ipc_errors.DaemonUnavailableError("daemon socket is not reachable")
        return dict(BOOTSTRAP if world.phase == "bootstrap" else SERVING)

    def request(method: str, params: Any = None, **options: Any) -> Any:
        world.calls.append(Call(method, params, options))
        scripted = world.answers.get(method)
        if scripted is not None:
            value = scripted.pop(0) if isinstance(scripted, list) else scripted
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                value = value()
            if value is not None:
                return value
        if method == "ping":
            return ping()
        if method in {"auth.login.begin", "auth.relogin"}:
            return begin_result()
        if method == "auth.operation.get":
            world.phase = "serving"
            return answer()
        if method == "auth.operation.cancel":
            return {"ok": True, "operationId": OPERATION_ID, "state": "cancelled", "cancelled": True, "reason": "cancelled"}
        if method == "org.list":
            return {"orgs": [{"id": "org-0001"}]}
        raise RequestPortError("METHOD_NOT_FOUND", f"unscripted method {method}")

    def launch(**kwargs: Any) -> Any:
        world.launches.append(kwargs)
        world.phase = "serving" if os.environ.get("HYPRIAL_OWNER") else "bootstrap"
        return SimpleNamespace(running=True, already_running=world.already_running)

    monkeypatch.setattr(support, "daemon_request", request)
    monkeypatch.setattr(services, "_launch_daemon_process", launch)
    monkeypatch.setattr(services, "resolve_profile", lambda *a, **k: (profile, "profile.json"))
    monkeypatch.setattr(services, "_initialize_org_context", lambda: None)
    import hyprial.shell.impl.cli.commands.login.aftercare as cli_aftercare
    import hyprial.shell.impl.login.flow as login_flow

    monkeypatch.setattr(
        login_flow, "open_verification_uri", lambda url: world.opened.append(url) or True, raising=False
    )

    def pickup(home: Path, *, json_output: bool, services: Any = None) -> dict[str, Any]:
        del services
        world.pickups.append(home)
        return {"status": "not-attempted"}

    monkeypatch.setattr(cli_aftercare, "_pickup_org_invites_after_login", pickup)
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.login.commands._squire_setup_warning", lambda: None, raising=False
    )
