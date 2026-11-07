"""Execute generated entry points against their real post-refactor owners."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_process_contracts_have_one_kernel_identity():
    from hyprial import kernel
    from hyprial.daemon.impl import api

    assert api.ProcessLivenessProbeError is kernel.ProcessLivenessProbeError
    assert api.ManagedHarnessProcess is kernel.ManagedHarnessProcess


def _load(relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location("native_entry_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _generated_module(module_name: str, suffix: str) -> str:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin is not None, module_name
    tree = ast.parse(Path(spec.origin).read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        for left, right in zip(node.elts, node.elts[1:]):
            if (isinstance(left, ast.Constant) and left.value == "-m"
                    and isinstance(right, ast.Constant)
                    and isinstance(right.value, str) and right.value.endswith(suffix)):
                found.append(right.value)
    # Several hooks may invoke the same module (claude registers it for both
    # Stop and UserPromptSubmit); the entry point itself must be unique.
    modules = sorted(set(found))
    assert found and len(modules) == 1, found
    return modules[0]


def test_generated_stop_hook_runs_without_cli_or_mcp_imports(tmp_path):
    module = _generated_module(
        "hyprial.shell.impl.cli.commands.agent.runtime.claude", "turn_pulse"
    )
    directory = tmp_path / "pulses"
    code = (
        "import runpy, sys\n"
        "def guard(event, args):\n"
        " if event == 'import' and (args[0].startswith('hyprial.shell') "
        "or args[0].startswith('hyprial.daemon')): raise AssertionError(args[0])\n"
        "sys.addaudithook(guard)\n"
        "sys.argv = [sys.argv[1], '--signal-dir', sys.argv[2]]\n"
        "runpy.run_module(sys.argv[0], run_name='__main__')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, module, str(directory)],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    pulses = list(directory.iterdir())
    assert len(pulses) == 1
    assert pulses[0].stat().st_mode & 0o777 == 0o600


def test_generated_jev_worker_reaches_real_argument_parser():
    module = _generated_module(
        "hyprial.shell.impl.cli.commands.agent.runtime.start", "_python_worker"
    )
    completed = subprocess.run(
        [sys.executable, "-m", module], input="", capture_output=True,
        text=True, timeout=10,
    )
    assert completed.returncode == 2
    assert "--kind" in completed.stderr
    assert "No module named" not in completed.stderr


def test_shutdown_closes_native_direct_inbox_engine(tmp_path, monkeypatch):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    app = DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        node_id="native-inbox-close", hyprial_home=tmp_path / "home",
        **daemon_dependencies(),
    )
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    close = InboxService.close
    calls = []

    def observed_close(self):
        calls.append(self)
        close(self)

    monkeypatch.setattr(InboxService, "close", observed_close)
    app._inbox = inbox
    try:
        app._close()
        assert calls == [inbox]
        assert app._inbox is None
    finally:
        close(inbox)


def _hyprial_bin() -> Path:
    if "HYPRIAL_BIN" in os.environ:
        return Path(os.environ["HYPRIAL_BIN"])
    candidate = shutil.which("hyprial")
    if candidate:
        return Path(candidate)
    raise RuntimeError("HYPRIAL_BIN is not set and hyprial is not on PATH")


def test_upgrade_identity_probe_uses_child_installed_native_provider():
    runner = _load("contract/e2e/run-upgrade-continuity.py")
    from hyprial.daemon import read_process_identity

    assert runner.read_target_process_identity(_hyprial_bin(), os.getpid()) == (
        read_process_identity(os.getpid())
    )


def test_old_only_desired_state_probe_retains_original_namespace():
    runner = _load("contract/e2e/run-upgrade-continuity.py")
    names = {node.module for node in ast.walk(ast.parse(runner._READ_DESIRED_STATE))
             if isinstance(node, ast.ImportFrom)}
    assert "hyprial.daemon.desired_state_sqlite" in names
    assert "hyprial.daemon.state_db" in names


def test_builtin_usage_skill_resolves_packaged_resource():
    from hyprial.shell.impl.plugins import builtin_skill_dirs

    expected = ROOT / "src/hyprial/skills/hyprial-ops"
    assert expected in builtin_skill_dirs()
    assert all((path / "SKILL.md").is_file() for path in builtin_skill_dirs())


def test_native_retry_policy_is_an_empty_immutable_compatibility_token():
    from dataclasses import fields, is_dataclass
    from hyprial.daemon.impl.inbox.service.policy import RetryPolicy

    policy = RetryPolicy()
    assert is_dataclass(policy)
    assert fields(policy) == ()
    assert not hasattr(policy, "__dict__")


def test_real_routine_adapter_preserves_generation_fenced_timer_admission(tmp_path, monkeypatch):
    from hyprial.daemon import RoutineRuntimeDeps
    from hyprial.daemon.impl.application.ports import RoutineServicePort
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.kernel import PortAdmission
    from hyprial.shell.impl.daemon_wiring.routines import routine_runtime_factory

    workflow = GraphWorkflowService(
        state_dir=tmp_path, owner="test", machine="node", sender=lambda notice: True,
        admit=lambda **keywords: None, clock_ms=lambda: 500, start_thread=False,
    )
    runtime = None
    commands = []
    try:
        runtime = routine_runtime_factory(RoutineRuntimeDeps(
            state_dir=tmp_path, hyprial_home=tmp_path / "home", owner="test",
            logger=None, alarm=SimpleNamespace(escalate=lambda **keywords: None),
            deliver_task=lambda **keywords: True, clock_ms=lambda: 500,
            resolve_principal=lambda actor: actor, migrate_address=lambda actor: actor,
            workflow_service=workflow,
        ))
        assert runtime is not None
        adapter = runtime.service
        service = adapter.service
        submit = service.submit

        def observe(command):
            commands.append(command)
            return submit(command)

        monkeypatch.setattr(service, "submit", observe)
        generation = service._runtime.snapshot(service._handle).generation
        assert isinstance(adapter, RoutineServicePort)
        assert adapter.submit_timer(123) is PortAdmission.ACCEPTED
        assert adapter.submit_timer() is PortAdmission.ACCEPTED
        assert [command.observed_at_ms for command in commands] == [123, 500]
        assert [command.generation for command in commands] == [generation, generation]
        assert [command.version for command in commands] == [1, 2]
        adapter.close()
        assert adapter.submit_timer(999) is PortAdmission.CLOSING
        assert len(commands) == 2
    finally:
        if runtime is not None:
            runtime.service.close()
        workflow.close()


def test_upgrade_primer_loads_actual_alert_provider_in_fresh_process():
    code = (
        "import sys\n"
        "from hyprial.shell.impl.cli.commands.socialware.commands import "
        "_prime_post_install_restart_imports\n"
        "_prime_post_install_restart_imports()\n"
        "alert = sys.modules.get('hyprial.daemon.impl.autoupdate.alert')\n"
        "assert alert is not None, 'alert provider absent before tool replacement'\n"
        "assert callable(alert.notify_upgrade_outcome) and callable(alert.run_self_check)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("layout", ["old", "native", "broken-native"])
def test_upgrade_provider_selection_is_version_bound_and_fail_closed(tmp_path, monkeypatch, layout):
    runner = _load("contract/e2e/run-upgrade-continuity.py")
    captured = []
    run = subprocess.run

    def capture(argv, **kwargs):
        captured.append(argv[2])
        return SimpleNamespace(returncode=0, stdout="captured", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", capture)
    runner.read_target_process_identity(_hyprial_bin(), os.getpid())
    assert len(captured) == 1
    package = tmp_path / "hyprial"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "daemon").mkdir()
    native_body = "def read_process_identity(pid): return 'native-' + str(pid)\n"
    (package / "daemon/__init__.py").write_text(native_body if layout == "native" else "")
    if layout != "old":
        (package / "daemon/interface.py").write_text("# native layout marker\n")
    (package / "mcp").mkdir()
    (package / "mcp/__init__.py").write_text("")
    (package / "mcp/channel.py").write_text(
        "def _read_process_identity(pid): return 'old-' + str(pid)\n"
    )
    result = run(
        [sys.executable, "-c", captured[0], "123"], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True, timeout=10,
    )
    if layout == "broken-native":
        assert result.returncode != 0
        assert "cannot import name 'read_process_identity'" in result.stderr
        assert "old-123" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == layout + "-123"


def test_generated_autoupdate_receiver_imports_resolve_native_providers(tmp_path):
    runner = _load("contract/e2e/run-inprocess-autoupdate.py")
    path = tmp_path / "receiver.py"
    runner.write_bus_receiver(path)
    imports = [node for node in ast.parse(path.read_text()).body
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    namespace = {}
    exec(compile(ast.Module(body=imports, type_ignores=[]), "receiver-imports", "exec"), namespace)
    from hyprial.daemon.impl.squire import UserDeliveryResult
    from hyprial.daemon.impl.transport import ZenohTransport

    assert namespace["UserDeliveryResult"] is UserDeliveryResult
    assert namespace["ZenohTransport"] is ZenohTransport
