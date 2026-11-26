"""The Pi SDK package root is found beside the exact command, in both npm layouts.

POSIX npm links ``<prefix>/bin/pi`` to the package's CLI, so the package is
an ancestor of the resolved command.  Windows npm writes ``pi.cmd``/``pi``
shims into the prefix directory and keeps the package in the sibling
``node_modules`` (KP-134 symptom A); that sibling is trusted only when the
command's text names the SDK path, as npm's cmd/ps1/sh shims (reproduced
here) do.  The shim layouts are built with plain files; the symlink layout
creates a real symlink and is skipped, with the reason, on a machine whose
account cannot create one.  No test has a platform branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import (
    PiStartPreconditionError,
    find_pi_package_root,
)

_PACKAGE = "@earendil-works/pi-coding-agent"


def _package(root: Path, name: str = _PACKAGE) -> Path:
    (root / "dist").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    cli = root / "dist" / "cli.js"
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    cli.chmod(0o755)
    return root


def _cmd_shim(package: str) -> str:
    target = "%dp0%\\node_modules\\" + package.replace("/", "\\") + "\\dist\\bundle\\cli.js"
    return (
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
        "SETLOCAL\r\nCALL :find_dp0\r\n\r\nIF EXIST \"%dp0%\\node.exe\" (\r\n"
        "  SET \"_prog=%dp0%\\node.exe\"\r\n) ELSE (\r\n  SET \"_prog=node\"\r\n"
        "  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n"
        "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & \"%_prog%\"  "
        f"\"{target}\" %*\r\n"
    )


def _sh_shim(package: str) -> str:
    target = f"$basedir/node_modules/{package}/dist/bundle/cli.js"
    return (
        "#!/bin/sh\n"
        "basedir=$(dirname \"$(echo \"$0\" | sed -e 's,\\\\,/,g')\")\n\n"
        "case `uname` in\n    *CYGWIN*|*MINGW*|*MSYS*)\n"
        "        if command -v cygpath > /dev/null 2>&1; then\n"
        "            basedir=`cygpath -w \"$basedir\"`\n        fi\n    ;;\nesac\n\n"
        "if [ -x \"$basedir/node\" ]; then\n"
        f"  exec \"$basedir/node\"  \"{target}\" \"$@\"\n"
        f"else \n  exec node  \"{target}\" \"$@\"\nfi\n"
    )


def _ps1_shim(package: str) -> str:
    target = f"$basedir/node_modules/{package}/dist/bundle/cli.js"
    return (
        "#!/usr/bin/env pwsh\n"
        "$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n\n"
        "$exe=\"\"\nif ($PSVersionTable.PSVersion -lt \"6.0\" -or $IsWindows) {\n"
        "  $exe=\".exe\"\n}\n$ret=0\nif (Test-Path \"$basedir/node$exe\") {\n"
        f"  & \"$basedir/node$exe\"  \"{target}\" $args\n"
        "  $ret=$LASTEXITCODE\n} else {\n"
        f"  & \"node$exe\"  \"{target}\" $args\n"
        "  $ret=$LASTEXITCODE\n}\nexit $ret\n"
    )


_SHIMS = {"pi": _sh_shim, "pi.cmd": _cmd_shim, "pi.ps1": _ps1_shim}


def _shim(directory: Path, name: str, package: str = _PACKAGE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / name
    shim.write_bytes(_SHIMS[name](package).encode("utf-8"))
    shim.chmod(0o755)
    return shim


def _windows_prefix(
    tmp_path: Path, name: str = _PACKAGE, shim_package: str = _PACKAGE
) -> tuple[Path, Path]:
    prefix = tmp_path / "AppData" / "Roaming" / "npm"
    root = _package(prefix / "node_modules" / "@earendil-works" / "pi-coding-agent", name)
    for shim in _SHIMS:
        _shim(prefix, shim, shim_package)
    return prefix, root


def test_a_symlinked_bin_resolves_to_the_package_ancestor(tmp_path: Path) -> None:
    """The POSIX npm layout: ``bin/pi`` is a symlink into the package's ``dist/cli.js``.

    The command names the link itself, so after ``resolve`` the ancestor
    walk starts from the file the link points to, one level inside the SDK.
    An SDK of the same name nested above the inner package shows the walk
    stops at the nearest ancestor.  A bare command looked up on ``PATH`` is
    covered by the shim tests below.  Creating a symlink is refused for an
    account without the privilege; that is a property of the machine, so
    the case is skipped there with this reason instead of failing.
    """

    prefix = tmp_path / "prefix"
    outer = _package(prefix / "lib" / "node_modules" / _PACKAGE / "nested")
    root = _package(outer / "node_modules" / _PACKAGE)
    bin_dir = prefix / "bin"
    bin_dir.mkdir()
    link = bin_dir / "pi"
    try:
        link.symlink_to(root / "dist" / "cli.js")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this machine does not let the test create a symlink: {error}")

    found = find_pi_package_root((str(link),), {})

    assert found == root.resolve()
    assert found != outer.resolve()


@pytest.mark.parametrize("shim", sorted(_SHIMS))
def test_each_npm_shim_resolves_to_the_sibling_node_modules(
    tmp_path: Path, shim: str
) -> None:
    prefix, root = _windows_prefix(tmp_path)

    found = find_pi_package_root((str(prefix / shim),), {})

    assert found == root.resolve()


@pytest.mark.parametrize("shim", sorted(_SHIMS))
def test_a_shim_for_another_package_beside_the_sdk_is_rejected(
    tmp_path: Path, shim: str
) -> None:
    prefix, _root = _windows_prefix(tmp_path, shim_package="@other/pi-fork")

    with pytest.raises(PiStartPreconditionError, match="Pi SDK package root not found"):
        find_pi_package_root((str(prefix / shim),), {})


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("node.exe", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\xff\xff" + bytes(range(256)) * 8),
        ("npx.cmd", b"@ECHO off\r\nnode \"%~dp0\\node_modules\\npm\\bin\\npx-cli.js\" %*\r\n"),
        ("wrapper.cmd", b"@ECHO off\r\nnode %~dp0\\tools\\run.js %*\r\n"),
    ],
)
def test_an_unrelated_executable_beside_the_sdk_is_rejected(
    tmp_path: Path, name: str, body: bytes
) -> None:
    prefix, _root = _windows_prefix(tmp_path)
    command = prefix / name
    command.write_bytes(body)
    command.chmod(0o755)

    with pytest.raises(PiStartPreconditionError, match="Pi SDK package root not found"):
        find_pi_package_root((str(command),), {})


def test_the_shim_marker_beyond_the_read_limit_is_not_seen(tmp_path: Path) -> None:
    prefix, _root = _windows_prefix(tmp_path)
    shim = prefix / "pi.cmd"
    shim.write_bytes(b"REM " + b"x" * (64 * 1024) + b"\r\n" + _cmd_shim(_PACKAGE).encode("utf-8"))

    with pytest.raises(PiStartPreconditionError, match="Pi SDK package root not found"):
        find_pi_package_root((str(shim),), {})


def test_windows_shim_found_on_path_resolves_to_the_sibling_node_modules(
    tmp_path: Path,
) -> None:
    prefix, root = _windows_prefix(tmp_path)

    found = find_pi_package_root(("pi",), {"PATH": str(prefix)})

    assert found == root.resolve()


def test_a_sibling_package_with_another_name_is_not_the_pi_sdk(tmp_path: Path) -> None:
    prefix, _root = _windows_prefix(tmp_path, name="not-pi")

    with pytest.raises(PiStartPreconditionError, match="Pi SDK package root not found"):
        find_pi_package_root((str(prefix / "pi.cmd"),), {})


def test_node_modules_outside_the_command_directory_is_not_searched(
    tmp_path: Path,
) -> None:
    _package(tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent")
    shim = _shim(tmp_path / "elsewhere", "pi.cmd")

    with pytest.raises(PiStartPreconditionError, match="Pi SDK package root not found"):
        find_pi_package_root((str(shim),), {})
