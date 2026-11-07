"""Behavior and import boundaries of the extracted transfer command family."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli
from tests.support.transfer.cli_graph import transfer_command_graph
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.transfer.move as _cli_move

BASELINE = Path(__file__).resolve().parents[4] / 'tests/fixtures/transfer_cli/command-graph-declarations-epoch.json'


def test_transfer_command_graph_matches_frozen_baseline():
    assert transfer_command_graph(cli) == json.loads(BASELINE.read_text())


def test_transfer_callbacks_live_only_on_the_registered_tree():
    root = get_command(_cli_root_app)
    for row in json.loads(BASELINE.read_text()):
        command = root
        for part in row["route"].split():
            command = command.commands[part]
        assert inspect.unwrap(command.callback).__name__ == row["callback"]
        assert not hasattr(cli, row["callback"])


def test_transfer_callbacks_read_current_cli_dependencies(tmp_path, monkeypatch):
    from hyprial import daemon as runtime_state

    calls = []
    monkeypatch.setattr(_cli_support, "_execute", lambda operation, **kwargs: calls.append((operation(), kwargs)))
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path / "selected-state")
    monkeypatch.setattr(runtime_state, "downgrade_state", lambda state, output: (state, output))
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "transfer", "runtime", "downgrade-state",
            "--output", str(tmp_path / "output"), "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [((tmp_path / "selected-state", tmp_path / "output"), {"json_output": True})]


def test_hidden_transfer_precheck_keeps_provider_wire_key(monkeypatch):
    calls = []
    # Intentionally accepts no keyword arguments: the move must not invent any.
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params: calls.append((method, params)) or {"ok": True})
    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "precheck", "--harness", "codex", "--name", "worker", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [("transfer.precheck", {"provider": "codex", "name": "worker"})]
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("payload,timeout", [({}, 150.0), ({"strictTimeoutSeconds": 7}, 67.0)])
def test_hidden_transfer_receive_keeps_timeout_margin(monkeypatch, payload, timeout):
    calls = []
    def daemon(method, params, **kwargs):
        calls.append((method, params, kwargs))
        return {"ok": True}
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "receive", "--json"], input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    assert calls == [("transfer.receive", payload, {"timeout": timeout})]
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"\xff"])
def test_hidden_transfer_receive_refuses_invalid_stdin_without_ipc(monkeypatch, payload):
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: pytest.fail("invalid stdin reached IPC"))
    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "receive", "--json"], input=payload)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_hidden_transfer_filesystem_commands_keep_paths_and_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path / "state")
    runner = CliRunner()
    args = ["agent", "transfer", "cred-stage", "--name", "worker", "--harness", "codex", "--json"]
    staged = runner.invoke(_cli_root_app, args)
    assert staged.exit_code == 0, staged.output
    path = Path(json.loads(staged.stdout)["path"])
    assert path.is_relative_to(tmp_path / "state")
    file = path / "synthetic.txt"
    file.write_text("fixture")
    file.chmod(0o755)
    finalized = runner.invoke(_cli_root_app, [*args, "--finalize"])
    assert finalized.exit_code == 0, finalized.output
    assert json.loads(finalized.stdout)["files"] == ["synthetic.txt"]
    if os.name != "nt":
        assert file.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o700
    result = runner.invoke(_cli_root_app, ["agent", "transfer", "container-home", "--name", "worker", "--harness", "codex", "--json"])
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["home"]).is_relative_to(tmp_path / "state")
    home = tmp_path / "target-home"
    args = ["agent", "transfer", "session-path", "--harness", "codex", "--cwd", str(tmp_path), "--ref", "synthetic", "--filename", "rollout.jsonl", "--home", str(home), "--json"]
    refused = runner.invoke(_cli_root_app, args)
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["code"] == "INVALID_ARGUMENT"
    accepted = runner.invoke(_cli_root_app, [*args, "--sessions-rel", "2026/rollout.jsonl"])
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["path"] == str(home / ".codex/sessions/2026/rollout.jsonl")
    assert (home / ".codex/sessions/2026").is_dir()


def test_runtime_probe_does_not_resolve_or_require_initialized_home(tmp_path, monkeypatch):
    from hyprial import daemon as smolvm

    def forbidden():
        pytest.fail("probe touched an initialized-home dependency")
    for name in ("_state_dir", "require_initialized_hyprial_home", "configured_hyprial_home", "default_hyprial_home"):
        monkeypatch.setattr(_cli_get_services(), name, forbidden)
    calls = []
    monkeypatch.setattr(smolvm, "probe_smolvm", lambda **kwargs: calls.append(kwargs) or {"ok": True})
    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "runtime", "probe", "--smolvm", str(tmp_path / "tool"), "--rootfs", str(tmp_path / "rootfs"), "--resize2fs", str(tmp_path / "resize"), "--output", str(tmp_path / "output"), "--json"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0]["output"] == tmp_path / "output"


@pytest.mark.parametrize("order", ["module-first", "cli-first"])
def test_transfer_module_import_orders_do_not_load_daemon_or_duplicate_commands(tmp_path, order):
    imports = (
        "import sys\nimport hyprial.shell.impl.cli.commands.transfer\nassert 'hyprial.shell.impl.cli.runtime.entry' not in sys.modules\nfrom hyprial.shell.impl.cli.runtime import entry as cli\n"
        if order == "module-first" else
        "from hyprial.shell.impl.cli.runtime import entry as cli\nimport hyprial.shell.impl.cli.commands.transfer\n"
    )
    script = imports + '''
import sys
from typer.main import get_command
assert not any(name == "hyprial.daemon" or name.startswith("hyprial.daemon.") for name in sys.modules)
assert not any(name == "hyprial.transfer" or name.startswith("hyprial.transfer.") for name in sys.modules)
root = get_command(cli.app)
assert "host" in root.commands
assert "runtime" in root.commands["agent"].commands["transfer"].commands
names = [item.name or item.callback.__name__.replace("_", "-") for item in cli.app.registered_commands]
assert len(names) == len(set(names))
'''
    home = tmp_path / "absent-home"
    env = {**os.environ, "HYPRIAL_HOME": str(home), "HARNESS_STATE_DIR": str(home / "state"), "HYPRIAL_NETWORK_ISOLATED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not home.exists()


def test_dependency_parser_alias_keeps_error_identity_and_late_binding(tmp_path, monkeypatch):
    assert _cli_move.parse_depends(["binary=!ripgrep", "file=config/a"]) == [
        {"kind": "binary", "present": False, "note": "ripgrep"},
        {"kind": "file", "present": True, "path": "config/a"},
    ]
    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_move.parse_depends(["invalid"])
    assert caught.value.code == "depends_invalid"

    class PatchedError(Exception):
        pass
    with monkeypatch.context() as errors:
        errors.setattr(_cli_move, "CliError", PatchedError)
        with pytest.raises(PatchedError):
            _cli_move.parse_depends(["invalid"])

    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("synthetic")
    calls = []
    declarations = [{"kind": "binary", "present": False, "note": "caller injected"}]
    monkeypatch.setattr(_cli_move, "parse_depends", lambda values: calls.append(values) or declarations)
    result = CliRunner().invoke(_cli_root_app, [
        "agent", "transfer", "bundle-export", "--from", str(source),
        "--to", str(tmp_path / "bundle"), "--owner", "alice",
        "--actor", "worker", "--depends", "binary=!original",
        "--source-epoch", "synthetic-source-generation", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [["binary=!original"]]
    assert json.loads(result.stdout)["dependencies"] == declarations
    assert "synthetic-source-generation" not in result.stdout
    validated = CliRunner().invoke(_cli_root_app, [
        "agent", "transfer", "bundle-validate", str(tmp_path / "bundle"),
        "--expect-epoch", "synthetic-source-generation", "--json",
    ])
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.stdout)["source_epoch"] == json.loads(result.stdout)["source_epoch"]
