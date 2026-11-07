"""Codex arg0 alias acceptance must cover the npm shim's vendor binary.

Field report 2026-10-16: an npm-installed codex (``bin/codex.js`` shim) writes
its arg0 aliases to the vendored native binary the shim actually spawns, not
to the shim. The exact-match check rejected every restart after the first
launch. These tests pin the npm layout that must be accepted and the negative
cases that must stay refused.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex import CodexAgentHomeError
from hyprial.daemon.impl.harnesses.codex.native_env import (
    _verify_codex_native_inventory,
)

_ALIASES = ("apply_patch", "applypatch", "codex-execve-wrapper")


def _host_npm_target() -> tuple[str, str, str]:
    """Return ``(platform package, target triple, codex.exe name)`` for this host.

    Mirrors ``PLATFORM_PACKAGE_BY_TARGET`` in the published ``@openai/codex``
    ``bin/codex.js`` shim so the fixture matches what the shim would spawn.
    """

    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "darwin":
        triple = "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
        return f"@openai/codex-darwin-{arch}", triple, "codex"
    if sys.platform.startswith("linux"):
        triple = (
            "aarch64-unknown-linux-musl"
            if arch == "arm64"
            else "x86_64-unknown-linux-musl"
        )
        return f"@openai/codex-linux-{arch}", triple, "codex"
    if sys.platform == "win32":
        triple = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
        return f"@openai/codex-win32-{arch}", triple, "codex.exe"
    pytest.skip(f"unsupported host platform for the npm codex fixture: {sys.platform}")


def _build_npm_layout(root: Path) -> tuple[Path, Path, Path]:
    """Return ``(shim, vendor native binary, platform package root)``."""

    package_name, triple, executable_name = _host_npm_target()
    package_root = root / "node_modules" / "@openai" / "codex"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "bin": {"codex": "bin/codex.js"},
                "optionalDependencies": {package_name: "npm:@openai/codex@0.161.0"},
            }
        )
    )
    shim = package_root / "bin" / "codex.js"
    shim.write_text("// codex npm shim (layout fixture)\n")
    platform_root = root / "node_modules" / Path(*package_name.split("/"))
    platform_root.mkdir(parents=True)
    (platform_root / "package.json").write_text(json.dumps({"name": "@openai/codex"}))
    vendor = platform_root / "vendor" / triple / "bin" / executable_name
    vendor.parent.mkdir(parents=True)
    vendor.write_text("#!/bin/sh\nexit 0\n")
    vendor.chmod(0o755)
    return shim, vendor, platform_root


def _main_vendor_binary(root: Path) -> Path:
    """Create the main package's fallback vendor binary the shim may spawn."""

    _package_name, triple, executable_name = _host_npm_target()
    package_root = root / "node_modules" / "@openai" / "codex"
    vendor = package_root / "vendor" / triple / "bin" / executable_name
    vendor.parent.mkdir(parents=True, exist_ok=True)
    vendor.write_text("#!/bin/sh\nexit 0\n")
    vendor.chmod(0o755)
    return vendor


def _block_package_json_export(platform_root: Path) -> None:
    """Give the platform package an ``exports`` gate that hides package.json."""

    manifest = json.loads((platform_root / "package.json").read_text())
    manifest["exports"] = {".": "./index.js"}
    (platform_root / "package.json").write_text(json.dumps(manifest))


def _native_root(root: Path) -> tuple[Path, Path]:
    native = root / "native"
    arg0 = native / "tmp" / "arg0" / "codex-arg0AbC123"
    arg0.mkdir(parents=True)
    (root / "sessions").mkdir()
    return native, arg0


def _verify(native: Path, root: Path, shim: Path) -> None:
    _verify_codex_native_inventory(
        native,
        expected=frozenset(),
        session_root=root / "sessions",
        codex_executable=shim.resolve(),
    )


def test_npm_shim_vendor_binary_is_accepted(tmp_path: Path) -> None:
    shim, vendor, _platform_root = _build_npm_layout(tmp_path)
    native, arg0 = _native_root(tmp_path)
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(vendor)

    _verify(native, tmp_path, shim)


def test_npm_shim_missing_platform_binary_never_falls_back(tmp_path: Path) -> None:
    # The shim picks one vendor root, then checks it once. When the platform
    # package resolves but its binary is missing, the shim exits instead of
    # trying the main package's vendor directory.
    shim, platform_vendor, _platform_root = _build_npm_layout(tmp_path)
    platform_vendor.unlink()
    main_vendor = _main_vendor_binary(tmp_path)
    native, arg0 = _native_root(tmp_path)
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(main_vendor)

    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _verify(native, tmp_path, shim)


def test_npm_shim_missing_platform_binary_keeps_launched(tmp_path: Path) -> None:
    shim, platform_vendor, _platform_root = _build_npm_layout(tmp_path)
    platform_vendor.unlink()
    _main_vendor_binary(tmp_path)
    native, arg0 = _native_root(tmp_path)
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(shim.resolve())

    _verify(native, tmp_path, shim)


def test_npm_shim_exports_blocked_package_json_uses_main_vendor(
    tmp_path: Path,
) -> None:
    # A package whose ``exports`` gate hides ``./package.json`` makes
    # ``require.resolve`` fail, so the shim falls back to the main vendor.
    shim, _platform_vendor, platform_root = _build_npm_layout(tmp_path)
    _block_package_json_export(platform_root)
    main_vendor = _main_vendor_binary(tmp_path)
    native, arg0 = _native_root(tmp_path)
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(main_vendor)

    _verify(native, tmp_path, shim)


def test_npm_shim_exports_blocked_package_json_refuses_platform_vendor(
    tmp_path: Path,
) -> None:
    shim, platform_vendor, platform_root = _build_npm_layout(tmp_path)
    _block_package_json_export(platform_root)
    _main_vendor_binary(tmp_path)
    native, arg0 = _native_root(tmp_path)
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(platform_vendor)

    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _verify(native, tmp_path, shim)


def test_npm_shim_unresolvable_vendor_stays_refused(tmp_path: Path) -> None:
    shim, vendor, platform_root = _build_npm_layout(tmp_path)
    native, arg0 = _native_root(tmp_path)
    # Keep the vendor binary real, but break the package structure the shim
    # resolves through: an unresolvable shim must not widen acceptance.
    (platform_root / "package.json").unlink()
    for alias in _ALIASES:
        (arg0 / alias).symlink_to(vendor)

    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _verify(native, tmp_path, shim)


@pytest.mark.parametrize(
    "case",
    ["other-file", "relative-link", "dangling-link", "disallowed-name"],
)
def test_npm_shim_unrelated_alias_shapes_stay_refused(
    tmp_path: Path, case: str
) -> None:
    shim, vendor, _platform_root = _build_npm_layout(tmp_path)
    native, arg0 = _native_root(tmp_path)
    alias = arg0 / "apply_patch"
    if case == "other-file":
        other = tmp_path / "elsewhere" / "native-codex"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        alias.symlink_to(other)
    elif case == "relative-link":
        alias.symlink_to(os.path.relpath(vendor, arg0))
    elif case == "dangling-link":
        alias.symlink_to(tmp_path / "missing" / "codex")
    else:
        alias = arg0 / "not-allow-listed"
        alias.symlink_to(vendor)

    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _verify(native, tmp_path, shim)
