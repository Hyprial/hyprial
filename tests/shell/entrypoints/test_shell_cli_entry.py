"""The thin installed entry and native Shell patch authority are explicit."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common import support as _cli_support


ROOT = Path(__file__).resolve().parents[3]


def test_installed_root_is_only_a_public_shell_main_delegate():
    source = (ROOT / "src/hyprial/cli.py").read_text()
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert len(source.splitlines()) <= 12
    assert [(node.module, [(alias.name, alias.asname) for alias in node.names]) for node in imports] == [
        ("hyprial.shell", [("main", "main")])
    ]
    assert "sys.modules" not in source


def test_installed_and_public_main_are_the_native_shell_object():
    from hyprial import cli, shell
    from hyprial.shell.impl.cli.runtime import entry

    assert cli.main is shell.main is entry.main
    assert cli.__all__ == ["main"]
    assert not hasattr(cli, "_daemon_request")


def test_command_service_request_port_reads_the_live_runtime_provider(monkeypatch):
    from hyprial.shell.impl.cli.commands.common.services import get_services

    def first(*arguments, **keywords):
        return "first"

    def second(*arguments, **keywords):
        return "second"

    monkeypatch.setattr(_cli_support, "_daemon_request", first)
    assert get_services().runtime.request_resolver() is first
    assert get_services().runtime.request("ping") == "first"
    monkeypatch.setattr(_cli_support, "_daemon_request", second)
    assert get_services().runtime.request_resolver() is second
    assert get_services().runtime.request("ping") == "second"


def test_whole_registered_command_inventory_stays_on_native_app():
    from hyprial.shell.impl.cli.inventory import registered_top_level
    from hyprial.shell.impl.cli.runtime import entry

    names = {command.name for command in registered_top_level(entry.app)}
    # `dispatch` was retired as a top-level command (expected-commands
    # 2026-10-06: removed with no replacement; PAC runs as `workflow`), so
    # the frozen expectation no longer lists it.
    assert {
        "daemon",
        "init",
        "adapter",
        "routine",
        "workflow",
        "user",
        "org",
        "gui",
        "message",
        "agent",
        "help",
        "version",
    } <= names


def test_service_group_and_daemon_status_have_distinct_root_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.shell.impl.cli.commands.daemon import commands as daemon
    from hyprial.shell.impl.cli.runtime import entry

    class Services:
        class CliError(RuntimeError):
            pass

        def __init__(self) -> None:
            self.requests: list[tuple[str, float]] = []
            self.runtime = SimpleNamespace(
                request=self._daemon_request,
                execute=self._execute,
            )

        def _daemon_request(self, method: str, *, restore_wait: float):
            self.requests.append((method, restore_wait))
            return {
                "daemon": {
                    "running": True,
                    "pid": 42,
                    "phase": "serving",
                },
                "restoring": False,
                "restorePending": False,
                "connectors": [],
            }

        def _execute(self, operation, *, json_output: bool, **_kwargs) -> None:
            from hyprial.shell.impl.cli.output import run_command

            run_command(operation, json_output=json_output, require_home=None)

    services = Services()
    monkeypatch.setattr(daemon, "get_services", lambda: services)

    status = CliRunner().invoke(entry.app, ["daemon", "status", "--json"])
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout) == {
        "ok": True,
        "daemon": {"running": True, "pid": 42, "phase": "serving"},
        "restoring": False,
        "restorePending": False,
    }
    assert services.requests == [("ps", 0.0)]

    service = CliRunner().invoke(entry.app, ["org", "service"])
    assert service.exit_code == 2, service.stderr
    assert all(name in service.output for name in ("connect", "disconnect", "guide", "list"))
    assert "installed" not in service.output
    assert "mode" not in service.output

    retired = CliRunner().invoke(entry.app, ["service", "--json"])
    assert retired.exit_code == 2
    assert "daemon" not in retired.stdout


def test_transfer_commands_moved_under_agent_and_stay_hidden():
    from typer.main import get_command

    from hyprial.shell.impl.cli.runtime import entry

    root = get_command(entry.app)
    assert "transfer" not in root.commands
    assert "transfer-runtime" not in root.commands
    agent = root.commands["agent"]
    assert agent.commands["move"].hidden is not True
    transfer = agent.commands["transfer"]
    assert transfer.hidden is True
    assert {
        "bundle-export",
        "bundle-validate",
        "bundle-receive",
        "bundle-land",
        "bundle-complete",
        "credential-seal",
        "credential-open",
        "trust-source-add",
        "trust-source-list",
        "trust-source-retire",
        "precheck",
        "receive",
        "cred-stage",
        "container-home",
        "session-path",
        "recipient-key",
        "runtime",
    } <= set(transfer.commands)
