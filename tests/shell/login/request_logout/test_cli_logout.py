"""``hyprial logout`` is one daemon ``auth.logout``, rendered.

Decision: L1 (owner 2026-11-21).  The CLI only presents: it ensures the daemon,
asks for confirmation, sends exactly one write request
(``auth.logout{operationKey, confirm: true, expectedEpoch: <ping.epoch>}``) and
renders the receipt.  It takes no identity lock, proves no process exit, stops
no daemon and removes no file.  The request client and the launch primitive are
replaced on the explicit service bindings only; no daemon or home outside the
per-test one is involved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.services import get_services

LABELS = [
    "secrets/login.json",
    "settings.identityIssuer",
    "settings.identityMode",
    "settings.owner",
    "settings.userId",
    "settings.username",
]


def _receipt(state: str = "completed", removed: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": "op-logout-0001",
        "receiptId": "receipt-logout-0001",
        "state": state,
        "loggedOut": state == "completed",
        "removed": list(LABELS if removed is None else removed),
        "issuerRevocation": "not-attempted",
        "nextAction": "login" if state == "completed" else "inspect",
    }


@dataclass
class World:
    home: Path
    phase: str | None
    receipt: Any = field(default_factory=_receipt)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    launches: list[dict[str, Any]] = field(default_factory=list)

    def writes(self) -> list[tuple[str, Any]]:
        return [call for call in self.calls if call[0] != "ping"]

    def invoke(self, *arguments: str):
        return CliRunner().invoke(app, ["logout", *arguments])

    def payload(self, result) -> dict[str, Any]:
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1, (result.exit_code, result.stdout)
        return json.loads(lines[0])


@pytest.fixture
def make_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def build(*, phase: str | None = "serving", home_exists: bool = True) -> World:
        home, state = tmp_path / "home", tmp_path / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        if home_exists:
            home.mkdir(mode=0o700)
            (home / "settings.json").write_text(
                json.dumps({"owner": "alice", "identityMode": "casdoor"}), encoding="utf-8"
            )
        monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
        monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
        monkeypatch.setenv("HYPRIAL_HOME", str(home))
        monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
        world = World(home=home, phase=phase)
        services = get_services()

        def request(method: str, params: Any = None, **_options: Any) -> Any:
            world.calls.append((method, params))
            if method == "ping":
                if world.phase is None:
                    raise ipc_errors.DaemonUnavailableError("daemon socket is not reachable")
                return {
                    "running": True,
                    "pid": 4242,
                    "epoch": f"epoch-{world.phase}",
                    "phase": world.phase,
                    "owner": "alice" if world.phase in {"serving", "reconciled"} else None,
                }
            if method == "auth.logout":
                if isinstance(world.receipt, BaseException):
                    raise world.receipt
                return world.receipt
            raise RequestPortError("METHOD_NOT_FOUND", f"unscripted method {method}")

        def launch(**kwargs: Any) -> Any:
            world.launches.append(kwargs)
            world.phase = "serving"
            return SimpleNamespace(running=True, already_running=False)

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("logout must not lock, stop a daemon or remove files in the CLI")

        from hyprial import identity

        monkeypatch.setattr(support, "daemon_request", request)
        monkeypatch.setattr(services, "_launch_daemon_process", launch)
        monkeypatch.setattr(identity.IdentityTransactionLock, "acquire", forbidden)
        monkeypatch.setattr(identity, "remove_logout_artifacts", forbidden)
        return world

    return build


def test_logout_sends_one_auth_logout_fenced_by_the_ping_epoch(make_world):
    world = make_world(phase="serving")

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in world.writes()] == ["auth.logout"]
    params = world.writes()[0][1]
    assert params["confirm"] is True
    assert params["expectedEpoch"] == "epoch-serving"
    assert isinstance(params["operationKey"], str) and params["operationKey"]
    payload = world.payload(result)
    assert payload["ok"] is True
    assert payload["status"] == "logged-out"
    assert payload["home"] == str(world.home)
    assert payload["removed"] == LABELS
    assert payload["operationId"] == "op-logout-0001"
    assert payload["daemon"] == {"stopRequested": True}
    assert world.launches == []
    assert json.loads((world.home / "settings.json").read_text())["owner"] == "alice"


def test_a_reconciled_serving_daemon_that_removed_the_login_requests_its_stop(make_world):
    world = make_world(phase="reconciled")

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 0, result.output
    payload = world.payload(result)
    assert payload["status"] == "logged-out"
    assert payload["daemon"] == {"stopRequested": True}
    assert world.writes()[0][1]["expectedEpoch"] == "epoch-reconciled"


def test_logout_starts_the_daemon_when_none_answers(make_world):
    world = make_world(phase=None)

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 0, result.output
    assert len(world.launches) == 1
    assert [method for method, _ in world.writes()] == ["auth.logout"]
    assert world.writes()[0][1]["expectedEpoch"] == "epoch-serving"


def test_a_bootstrap_logout_with_nothing_to_remove_reports_not_logged_in(make_world):
    world = make_world(phase="bootstrap")
    world.receipt = _receipt(removed=[])

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 0, result.output
    payload = world.payload(result)
    assert payload["status"] == "not-logged-in"
    assert payload["removed"] == []
    assert payload["daemon"] == {"stopRequested": False}
    assert world.writes()[0][1]["expectedEpoch"] == "epoch-bootstrap"


def test_a_missing_home_is_not_logged_in_and_starts_nothing(make_world):
    world = make_world(phase=None, home_exists=False)

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 0, result.output
    assert world.payload(result) == {
        "ok": True,
        "status": "not-logged-in",
        "home": str(world.home),
        "removed": [],
    }
    assert world.calls == []
    assert world.launches == []
    assert not world.home.exists()


def test_json_without_yes_refuses_before_any_write(make_world):
    world = make_world(phase="serving")

    result = world.invoke("--json")

    assert result.exit_code == 1
    assert world.payload(result)["code"] == "CONFIRMATION_REQUIRED"
    assert world.writes() == []


def test_a_failed_receipt_exits_one_and_names_the_operation(make_world):
    world = make_world(phase="serving")
    world.receipt = _receipt(state="failed", removed=["settings.owner"])

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 1
    payload = world.payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "LOGOUT_FAILED"
    assert payload["data"]["operationId"] == "op-logout-0001"
    assert payload["data"]["removed"] == ["settings.owner"]
    assert payload["data"]["daemon"] == {"stopRequested": True}


def test_a_daemon_refusal_is_surfaced_as_its_code(make_world):
    world = make_world(phase="serving")
    world.receipt = RequestPortError(ipc_errors.STALE_DAEMON_GENERATION, "stale")

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 1
    assert world.payload(result)["code"] == ipc_errors.STALE_DAEMON_GENERATION
    assert [method for method, _ in world.writes()] == ["auth.logout"]


def test_a_lost_reply_is_an_unknown_outcome(make_world):
    world = make_world(phase="serving")
    world.receipt = ipc_errors.DaemonDisconnectedError("peer closed")

    result = world.invoke("--yes", "--json")

    assert result.exit_code == 1
    payload = world.payload(result)
    assert payload["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert [method for method, _ in world.writes()] == ["auth.logout"]
