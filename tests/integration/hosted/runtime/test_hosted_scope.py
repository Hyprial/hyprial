"""Decision A: authorization has one implemented entry, no setup backdoor."""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_only_receive_chain_calls_hosting_authority() -> None:
    calls = []
    commands = []
    reserved = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        relative = str(path.relative_to(ROOT / "src"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "create_transfer_hosted":
                    calls.append(relative)
                if isinstance(node.func, ast.Name) and node.func.id == "CreateTransferHostedAgentCommand":
                    commands.append(relative)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "squire-container" in node.value:
                reserved.append((relative, node.value))
    assert sorted(calls) == ["hyprial/daemon/impl/application/actors/transfer.py", "hyprial/identity/impl/agents/actor/_generation_dispatch.py"]
    assert commands == ["hyprial/daemon/impl/composition/views.py"]
    # Runtime enum and SQLite CHECK only, no squire setup implementation.
    assert len(reserved) == 2
    assert all(path == "hyprial/identity/impl/agents/registry/_base.py" for path, _ in reserved)
    assert {value for _, value in reserved if "ALTER TABLE" not in value} == {"squire-container"}


@pytest.fixture
def runner(monkeypatch):
    directory = ROOT / "contract" / "e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("hosted_e019_runner", directory / "run-hosted-transfer.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_e2e_environment_has_two_homes_and_no_ambient_identity(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "must-not-inherit")
    monkeypatch.setenv("HARNESS_SOCKET_PATH", "/must-not-use.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    source = runner.isolated_environment(tmp_path / "s", "s-node", "s-owner")
    target = runner.isolated_environment(tmp_path / "t", "t-node", "t-owner")
    for key in ("HYPRIAL_HOME", "UV_TOOL_DIR", "HARNESS_STATE_DIR", "HOME"):
        assert source[key] != target[key]
        assert Path(source[key]).is_relative_to(tmp_path)
        assert Path(target[key]).is_relative_to(tmp_path)
    for env in (source, target):
        assert "HARNESS_SOCKET_PATH" not in env
        assert "HYPRIAL_WORKER_ACTOR" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["HYPRIAL_PEER_DISCOVERY"] == "0"


def test_e2e_cleanup_reaps_only_owned_pid(runner):
    import subprocess

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    report = {}
    try:
        runner.stop_owned_process(process, report)
        assert process.poll() is not None
        assert report["cleanup"] == [{
            "pid": process.pid, "reaped": True, "forcedKill": False,
            "exitCode": process.returncode,
        }]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
