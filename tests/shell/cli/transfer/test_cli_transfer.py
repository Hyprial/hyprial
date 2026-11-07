"""Transfer CLI boundaries; daemon/SSH are injected, never live targets."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer import orchestrator
from hyprial.daemon.impl.transfer.execution.ssh import SshRunner
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_rehearsal_override_does_not_bypass_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def local_request(method, params):
        calls.append((method, params))
        assert method == "hosts"
        return {"hosts": [{"nodeId": "server", "status": "online"}]}

    def no_ssh(*args, **kwargs):
        pytest.fail("undiscovered node must not reach SSH, even in rehearsal")

    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    monkeypatch.setattr(SshRunner, "run_json", no_ssh)
    result = CliRunner().invoke(_cli_root_app, [
        "agent", "move", "wk", "--to", "user@unknown", "--json",
        "--remote-hyprial", "HYPRIAL_HOME=/tmp/xfer-target /tmp/xfer/bin/hyprial",
    ])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "TRANSFER_TARGET_UNDISCOVERED"
    assert calls == [("hosts", {})]


def _inject_transfer_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, socket: object,
    *, session_ref: str | None = None,
):
    events = []
    actor = "agent:alice:laptop:wk"
    spec = {"provider": "claude", "cwd": str(tmp_path / "work"), "sessionRef": session_ref}
    plan = {"ok": True, "spec": spec, "actor": actor, "owner": "alice", "nodeId": "laptop"}

    def record_print(message, **kwargs):
        events.append(("print", str(message)))
        builtins.print(message, **kwargs)

    def local_request(method, params):
        events.append((method, params))
        if method == "hosts":
            return {"hosts": [{"nodeId": "server", "status": "online"}]}
        if method in {"transfer.plan", "transfer.quiesce"}:
            return plan
        if method == "transfer.complete":
            return {"ok": True}
        pytest.fail(f"unexpected local call {method}")

    def remote_json(self, argv, stdin=None):
        events.append(("remote", argv))
        if argv == ["daemon", "status"]:
            return {"ok": True, "daemon": {
                "nodeId": "server", "owner": "alice", "socket": socket,
            }}
        if argv[:3] == ["agent", "transfer", "precheck"]:
            return {"ok": True, "conflicts": []}
        if argv[:3] == ["agent", "transfer", "receive"]:
            return {"ok": True, "actor": "agent:alice:server:wk", "sessionRef": None}
        pytest.fail(f"unexpected remote command {argv}")

    monkeypatch.setattr(_cli_get_services(), "print", record_print, raising=False)
    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    monkeypatch.setattr(SshRunner, "run_json", remote_json)
    monkeypatch.setattr(SshRunner, "has_binary", lambda _self, name: True)
    monkeypatch.setattr(SshRunner, "login_user", lambda _self: orchestrator._local_login_user())
    monkeypatch.setattr(SshRunner, "sync_tree", lambda _self, *args: events.append(("sync", args)))
    monkeypatch.setattr(SshRunner, "_run", lambda *a, **kw: pytest.fail("no real SSH allowed"))
    return events, actor


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("command", ["hyprial", "HYPRIAL_HOME=/tmp/isolated-target /tmp/build/bin/hyprial"])
def test_confirmation_facts_precede_mutation_and_require_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yes: bool, dry_run: bool,
    json_output: bool, command: str,
) -> None:
    socket = "/srv/nonstandard-state/daemon.sock"  # do NOT infer <home>/state
    events, actor = _inject_transfer_boundary(monkeypatch, tmp_path, socket)
    argv = ["agent", "move", "wk", "--to", "user@server", "--remote-hyprial", command]
    if yes:
        argv.append("--yes")
    if dry_run:
        argv.append("--dry-run")
    if json_output:
        argv.append("--json")
    result = CliRunner().invoke(_cli_root_app, argv)
    assert result.exit_code == (0 if yes else 1), result.output
    facts = {"host": "user@server", "remoteHyprial": command, "daemonSocket": socket, "actor": actor}
    rendered = result.stderr  # facts precede execution on stderr in both modes
    for value in facts.values():
        assert value in rendered
    assert "will run" in rendered and "daemon.socket" in rendered
    if json_output:
        payload = json.loads(result.stdout)  # stdout stays ONE JSON object
        if yes:
            assert payload["remote"] == facts
        else:
            assert payload["code"] == "TRANSFER_CONFIRMATION_REQUIRED"
            assert payload["data"] == facts
    if not yes:
        assert "--yes" in result.output
        assert [value for kind, value in events if kind == "remote"] == [["daemon", "status"]]
    if not yes or dry_run:
        assert not any(kind in {"transfer.quiesce", "sync", "transfer.complete"} for kind, _ in events)
    else:
        quiesce = next(index for index, event in enumerate(events) if event[0] == "transfer.quiesce")
        before = "\n".join(value for kind, value in events[:quiesce] if kind == "print")
        for value in facts.values():
            assert value in before
        assert any(kind == "transfer.complete" for kind, _ in events)


@pytest.mark.parametrize("socket", [None, "", 123, "relative/daemon.sock"])
def test_missing_socket_fact_refuses_even_with_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket: object
) -> None:
    events, _actor = _inject_transfer_boundary(monkeypatch, tmp_path, socket)
    result = CliRunner().invoke(_cli_root_app, ["agent", "move", "wk", "--to", "server", "--yes", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "TRANSFER_PROTOCOL"
    assert "daemon.socket" in payload["error"]
    assert not any(kind == "transfer.quiesce" for kind, _ in events)


def test_cli_reports_missing_session_with_other_preflight_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ref = "6f2e3327-0000-4000-8000-000000000001"  # synthetic UUID
    events, _actor = _inject_transfer_boundary(
        monkeypatch, tmp_path, "/remote/state/daemon.sock", session_ref=ref,
    )
    monkeypatch.setattr(SshRunner, "has_binary", lambda _self, name: name != "uv")
    result = CliRunner().invoke(_cli_root_app, ["agent", "move", "wk", "--to", "server", "--yes", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "TRANSFER_PREREQUISITE"
    missing = payload["data"]["missing"]
    assert len(missing) == 2
    assert any("target server: uv" in entry for entry in missing)
    assert any(ref in entry and "先跑一个 turn" in entry for entry in missing)
    assert not any(kind in {"transfer.quiesce", "sync"} for kind, _ in events)


@pytest.mark.parametrize("target", ["server", "bobadmin@server"])
@pytest.mark.parametrize("cwd", [None, "/srv/explicit-target"])
def test_cli_cross_user_requires_explicit_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, cwd: str | None
) -> None:
    events, _actor = _inject_transfer_boundary(monkeypatch, tmp_path, "/remote/state/daemon.sock")
    monkeypatch.setattr(orchestrator, "_local_login_user", lambda: "amyws")
    probes = []

    def target_user(self):
        probes.append("id -un")
        return "bobadmin"

    monkeypatch.setattr(SshRunner, "login_user", target_user)
    argv = ["agent", "move", "wk", "--to", target, "--yes", "--json"]
    if cwd:
        argv += ["--cwd", cwd]
    result = CliRunner().invoke(_cli_root_app, argv)
    payload = json.loads(result.stdout)
    if cwd is None:
        assert result.exit_code == 1
        assert payload["code"] == "TRANSFER_CWD_REQUIRED"
        assert payload["data"]["sourceUser"] == "amyws"
        assert payload["data"]["targetUser"] == "bobadmin"
        assert "--cwd" in payload["error"]
        assert not any(kind in {"transfer.quiesce", "sync"} for kind, _ in events)
        assert probes == ["id -un"]
    else:
        assert result.exit_code == 0, result.output
        assert payload["cwd"] == cwd
        assert probes == []


@pytest.mark.parametrize("home_mode", ["unset", "empty", "default", "isolated"])
@pytest.mark.parametrize("command", ["hyprial", "HYPRIAL_HOME=/tmp/xfer-target /tmp/xfer/bin/hyprial"])
def test_remote_override_requires_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home_mode: str, command: str
) -> None:
    # Even the default-home cases use a temporary HOME, not the operator's.
    monkeypatch.setenv("HOME", str(tmp_path))
    default = tmp_path / ".hyprial"
    default.mkdir()
    isolated = tmp_path / "rehearsal"
    isolated.mkdir()
    if home_mode == "unset":
        monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    else:
        monkeypatch.setenv("HYPRIAL_HOME", {
            "empty": "", "default": str(default), "isolated": str(isolated),
        }[home_mode])
    calls = []

    def fake_transfer(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(orchestrator, "run_transfer", fake_transfer)
    result = CliRunner().invoke(
        _cli_root_app, ["agent", "move", "wk", "--to", "user@server", "--remote-hyprial", command, "--json"]
    )
    payload = json.loads(result.stdout)
    if command != "hyprial" and home_mode != "isolated":
        assert result.exit_code == 1
        assert payload["code"] == "TRANSFER_REMOTE_OVERRIDE"
        assert "--remote-hyprial" in payload["error"]
        assert "HYPRIAL_HOME" in payload["error"]
        assert calls == []
    else:
        assert result.exit_code == 0, result.output
        assert payload["ok"] is True
        assert len(calls) == 1
        assert calls[0]["remote"]._remote_hyprial[-1] == command.split()[-1]
