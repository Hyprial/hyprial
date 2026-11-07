"""The public composition boundary B/C register against.

These tests are deliberately written the way a future move/link family will
use it: a family registrar receives ``(app, runtime)``, asks the runtime for
one generic request, and returns ``CliResult``/``CliStream`` to the one output
path.  No command module, no entry import, no second output channel.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from hyprial.shell.impl.cli.output import CliResult, run_command
from hyprial.shell.impl.cli.runtime import CliRuntime
from hyprial.shell.impl.cli.commands.common import support as _cli_support

ROOT = Path(__file__).resolve().parents[4]
CLI_RUNTIME = ROOT / "src" / "hyprial" / "shell" / "impl" / "cli" / "runtime"


def test_a_bc_family_registers_and_uses_only_the_public_runtime() -> None:
    app = typer.Typer(add_completion=False)
    requests: list[tuple[str, dict[str, Any] | None]] = []

    def request(
        method: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        requests.append((method, params))
        return {"links": []}

    def execute(operation, *, json_output: bool, **_kwargs: Any) -> None:
        run_command(operation, json_output=json_output, require_home=None)

    runtime = CliRuntime(
        app=app,
        request_resolver=lambda: request,
        execute_resolver=lambda: execute,
    )

    def register_links(target: typer.Typer, current: CliRuntime) -> None:
        @target.command("links")
        def links(json_output: bool = typer.Option(False, "--json")) -> None:
            current.execute(
                lambda: CliResult(current.request("link.list", {})),
                json_output=json_output,
            )

    links_app = typer.Typer()
    runtime.add_registrar(register_links, parent=links_app)
    runtime.add_family(links_app, name="probe")

    result = CliRunner().invoke(app, ["probe", "links", "--json"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True, "links": []}
    assert requests == [("link.list", {})]


def test_registering_a_family_preserves_the_existing_root_identity() -> None:
    from hyprial.shell.impl.cli.commands.common.services import get_services
    from hyprial.shell.impl.cli.runtime import entry
    from hyprial.shell.impl.cli.runtime.registration import root_app

    assert entry.runtime.app is entry.app is root_app
    assert get_services().runtime is entry.runtime


def test_runtime_request_reads_the_current_injected_port(monkeypatch) -> None:
    from hyprial.shell.impl.cli.runtime import entry

    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: {"port": "first"})
    assert entry.runtime.request("ping") == {"port": "first"}

    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: {"port": "second"})
    assert entry.runtime.request("ping") == {"port": "second"}


def test_runtime_execute_reads_the_current_injected_output_boundary(monkeypatch) -> None:
    from hyprial.shell.impl.cli.runtime import entry

    calls: list[tuple[str, bool]] = []

    def first(operation, *, json_output: bool, **_kwargs: Any) -> None:
        calls.append((operation(), json_output))

    monkeypatch.setattr(_cli_support, "_execute", first)
    entry.runtime.execute(lambda: "first-result", json_output=True)
    assert calls == [("first-result", True)]


def test_legacy_service_snapshot_does_not_grow() -> None:
    """New B/C code uses CliRuntime; the old flat snapshot is frozen."""

    from dataclasses import fields

    from hyprial.shell.impl.cli.commands.common.services import CliServices

    assert len(fields(CliServices)) == 54


def test_composition_module_does_not_import_the_entry_or_a_command_family() -> None:
    """The public boundary must be constructible without the process entry."""

    source = (CLI_RUNTIME / "composition.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {
        module
        for module in imported
        if "runtime.entry" in module or ".commands" in module
    }
    assert "typer" in imported


def test_registration_module_does_not_import_the_process_entry() -> None:
    source = (CLI_RUNTIME / "registration.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {module for module in imported if "runtime.entry" in module}


def test_request_without_params_keeps_the_single_argument_injection_shape() -> None:
    """Existing fake request ports take only ``method``; do not add a None arg."""

    app = typer.Typer()
    calls: list[str] = []

    def request(method: str) -> dict[str, Any]:
        calls.append(method)
        return {"ok": True}

    runtime = CliRuntime(
        app=app,
        request_resolver=lambda: request,
        execute_resolver=lambda operation, **_kwargs: operation(),
    )

    assert runtime.request("ping") == {"ok": True}
    assert calls == ["ping"]


def test_execute_omits_default_optional_kwargs() -> None:
    """The output boundary keeps the old call shape when defaults apply."""

    app = typer.Typer()
    calls: list[tuple[Any, bool]] = []

    def execute(operation, *, json_output: bool) -> None:
        calls.append((operation(), json_output))

    runtime = CliRuntime(
        app=app,
        request_resolver=lambda: lambda *_args, **_kwargs: None,
        execute_resolver=lambda: execute,
    )

    runtime.execute(lambda: "value", json_output=True)
    assert calls == [("value", True)]


def test_process_composition_rejects_a_second_assembly(monkeypatch) -> None:
    from hyprial.shell.impl.cli.runtime import entry
    from hyprial.shell.impl.cli.runtime import registration

    monkeypatch.setattr(registration, "_ASSEMBLED", True)
    with pytest.raises(RuntimeError, match="already been composed"):
        registration.build_app(entry.runtime)


def test_process_composition_rejects_an_independent_app(monkeypatch) -> None:
    from hyprial.shell.impl.cli.runtime import registration

    monkeypatch.setattr(registration, "_ASSEMBLED", False)
    independent = CliRuntime(
        app=typer.Typer(),
        request_resolver=lambda: lambda *_args, **_kwargs: None,
        execute_resolver=lambda operation, **_kwargs: operation(),
    )
    with pytest.raises(ValueError, match="process root app"):
        registration.build_app(independent)
