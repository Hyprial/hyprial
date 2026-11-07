"""Permanent inventory gate for the big-bang unreachable-code retirement."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

import hyprial.daemon.impl.mcp as mcp
import hyprial.daemon.impl.transport as transport
from hyprial import daemon
from hyprial.daemon.impl import runtime
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


ROOT = Path(__file__).resolve().parents[3]
PRODUCT_ROOTS = (ROOT / "src", ROOT / "contract")

RETIRED_MODULES = frozenset(
    {
        "hyprial.daemon.impl.doctor",
        "hyprial.daemon.impl.zenohd",
        "hyprial.transport.storage",
        "hyprial.mcp.http",
    }
)
RETIRED_DEFINITIONS = frozenset(
    {
        "AsyncHookDriver",
        "ClaudeAgentSdkStreamingDriver",
        "McpHttpConfig",
        "StorageRuntime",
        "ZenohBundle",
        "ZenohBundleError",
        "ZenohdStorageConfig",
        "ZenohdStorageRuntime",
        "ZenohdSupervisor",
        "_SessionStorage",
        "build_http_app",
        "load_or_create_token",
        "select_wake_driver",
        "serve_http",
    }
)


def _module_name(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_import(current: str, path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = (
        current.split(".")
        if path.name == "__init__.py"
        else current.split(".")[:-1]
    )
    if node.level > 1:
        package = package[: -(node.level - 1)]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _retired_spec(module: str):
    try:
        return importlib.util.find_spec(module)
    except ModuleNotFoundError as error:
        if error.name and module.startswith(error.name + "."):
            return None  # The exact retired module's parent itself is absent.
        raise


def test_retired_spec_does_not_hide_an_unrelated_missing_dependency(monkeypatch) -> None:
    def unavailable(_module):
        raise ModuleNotFoundError("missing dependency", name="unrelated_dependency")

    monkeypatch.setattr(importlib.util, "find_spec", unavailable)
    with pytest.raises(ModuleNotFoundError, match="missing dependency"):
        _retired_spec("hyprial.mcp.http")


def test_retired_modules_and_self_only_tests_are_physically_absent() -> None:
    for module in RETIRED_MODULES:
        assert _retired_spec(module) is None, module

    for name in (
        "test_daemon_doctor.py",
        "test_daemon_zenohd.py",
        "test_mcp_http.py",
    ):
        assert not (ROOT / "tests" / name).exists(), name


def _scan_retired_surface(
    roots: tuple[Path, ...],
) -> tuple[list[tuple[Path, str, str, str | None]], list[tuple[Path, str]]]:
    retired_imports: list[tuple[Path, str, str, str | None]] = []
    definitions: list[tuple[Path, str]] = []
    for source_root in roots:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            current = _module_name(path, source_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in RETIRED_MODULES:
                            retired_imports.append(
                                (path, alias.name, "*", alias.asname)
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = _resolved_import(current, path, node)
                    for alias in node.names:
                        if module in RETIRED_MODULES or (
                            (module == "hyprial" or module.startswith("hyprial."))
                            and alias.name in RETIRED_DEFINITIONS
                        ):
                            retired_imports.append(
                                (path, module, alias.name, alias.asname)
                            )
                elif isinstance(
                    node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                ):
                    if node.name in RETIRED_DEFINITIONS:
                        definitions.append((path, node.name))
    return retired_imports, definitions


def test_retired_modules_and_definitions_cannot_regenerate() -> None:
    retired_imports, definitions = _scan_retired_surface(PRODUCT_ROOTS)
    assert retired_imports == []
    assert definitions == []

    product_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in PRODUCT_ROOTS
        for path in sorted(root.rglob("*.py"))
    )
    assert "zenohd_restarted" not in product_text
    assert "httpx2" not in product_text


def test_retirement_gate_catches_from_import_alias_provenance(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from hyprial.mcp import McpHttpConfig as LegacyHttp\n",
        encoding="utf-8",
    )

    retired_imports, definitions = _scan_retired_surface((tmp_path,))

    assert retired_imports == [
        (probe, "hyprial.mcp", "McpHttpConfig", "LegacyHttp")
    ]
    assert definitions == []


def test_public_imports_and_cli_expose_only_live_replacements() -> None:
    assert daemon.DaemonApplication
    assert transport.ZenohTransport
    assert mcp.UnixDaemonConnection
    assert mcp.create_mcp_server
    assert mcp.serve_channel_stdio
    assert mcp.serve_worker_stdio

    for owner, names in (
        (
            daemon,
            {
                "Doctor",
                "DoctorAction",
                "DoctorCheck",
                "DoctorReport",
                "ZenohBundle",
                "ZenohdSupervisor",
            },
        ),
        (
            transport,
            {"StorageRuntime", "ZenohdStorageConfig", "ZenohdStorageRuntime"},
        ),
        (
            mcp,
            {
                "AsyncHookDriver",
                "ClaudeAgentSdkStreamingDriver",
                "McpHttpConfig",
                "build_http_app",
                "load_or_create_token",
                "select_wake_driver",
                "serve_http",
            },
        ),
    ):
        assert not (names & set(owner.__all__))
        assert all(not hasattr(owner, name) for name in names)

    # R11: ``mcp`` stays out of the CLI tree and all help inventories. The
    # installed entry separately recognizes only the previous release's exact
    # machine argv until the cross-version removal condition is met.
    groups = {group.name for group in _cli_root_app.registered_groups}
    assert "mcp" not in groups

    from hyprial.daemon.impl.mcp import entry as mcp_entry

    assert callable(mcp_entry.main)
    # The one deleted command is not a subcommand of the private entry either.
    with pytest.raises(SystemExit):
        mcp_entry.main(["claude-turn-ended", "--signal-dir", "/tmp"])
    signature = inspect.signature(mcp.create_mcp_server)
    assert signature.parameters["actor"].default is inspect.Parameter.empty
    assert signature.parameters["session_ref"].default is inspect.Parameter.empty


def test_daemon_runtime_has_no_placeholder_storage_supervision_axis() -> None:
    signature = inspect.signature(runtime.DaemonEventBridge)
    assert "storage" not in signature.parameters
    assert "zenohd_restarted" not in runtime.ReconcileSummary.__dataclass_fields__
    assert not hasattr(transport, "StorageRuntime")


def test_live_mcp_contract_passes_on_the_fixed_identity_server() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "contract" / "mcp-tools" / "run.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "mcp-tools: PASS 32/32"
