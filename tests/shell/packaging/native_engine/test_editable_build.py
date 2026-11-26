"""The editable native build runs in Python and names the module from interpreter facts.

KP-135 item 1: on Windows the hook executed scripts/build/build_native_engine.sh
and failed with WinError 193.  These tests pin that the hook never runs the
script, and that the staged file name comes from
``importlib.machinery.EXTENSION_SUFFIXES`` (simulated per platform by replacing
the list in the namespace the helper functions read).

The fake toolchain is one Python script behind an npm-style pair of entries
(extensionless for POSIX shells, ``.cmd`` for ``PATHEXT`` lookup).  A
``#!/bin/sh`` stand-in is not an executable to Windows: ``shutil.which``
skips it, so the build hook reported "Rust 1.93.1 is not available" and a
direct start failed with WinError 193.  Both entries are written on every
platform and the platform's lookup picks one, so no test branches on the
platform; a test names a fake tool by the path ``shutil.which`` returns.
The fake cargo writes its JSON message as unescaped UTF-8, as the real
``cargo`` does, so that the two non-ASCII path tests measure the decode.
The ``.cmd`` entry names the script through its own directory, so that the
batch text holds no path from ``tmp_path``.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import runpy
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[4]
EDITABLE_PATH = REPO / "src/hyprial/shell/impl/packaging/native_editable.py"
ENGINE_PATH = REPO / "src/hyprial/shell/impl/packaging/native_engine.py"
WINDOWS = [".cp312-win_amd64.pyd", ".pyd"]
WINDOWS_DEBUG = ["_d.cp312-win_amd64.pyd", "_d.pyd"]
LINUX = [".cpython-312-x86_64-linux-gnu.so", ".abi3.so", ".so"]
MACOS = [".cpython-312-darwin.so", ".abi3.so", ".so"]
FREE_THREADED = [".cpython-313t-x86_64-linux-gnu.so", ".so"]

FAKE_TOOL = """import json, os, shutil, subprocess, sys


def report(stream, version):
    stream.buffer.write(b"rustc " + version.encode("ascii") + b" (fake)\\n")
    return 0


name, args = sys.argv[1], sys.argv[2:]
version = args.pop(0) if name != "cargo" else ""
if name == "cargo":
    target = os.environ.get("CARGO_TARGET_DIR") or os.path.join(os.getcwd(), "crates", "target")
    os.makedirs(os.path.join(target, "release"), exist_ok=True)
    library = os.path.join(target, "release", "libactors.so")
    with open(library, "wb") as handle:
        handle.write(b"fresh-artifact")
    with open(os.path.join(target, "cargo-args"), "w", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")
    message = {"reason": "compiler-artifact", "target": {"name": "actors", "kind": ["cdylib"]},
               "filenames": [library]}
    sys.stdout.buffer.write(
        json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\\n"
    )
elif name == "rustup":
    if args[:2] != ["run", version]:
        sys.exit(1)
    if args[2:3] == ["rustc"]:
        sys.exit(report(sys.stdout, version))
    del args[:2]
    tool = shutil.which(args[0])
    if tool is None:
        sys.stderr.write(f"fake rustup: cannot find {args[0]!r} on PATH\\n")
        sys.exit(127)
    sys.exit(subprocess.call([tool, *args[1:]]))
elif name == "rustc":
    sys.exit(report(sys.stdout, version))
"""


def _import_build_hook():
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ModuleNotFoundError:
        names = [
            "hatchling",
            "hatchling.builders",
            "hatchling.builders.hooks",
            "hatchling.builders.hooks.plugin",
            "hatchling.builders.hooks.plugin.interface",
        ]
        for name in names:
            sys.modules.setdefault(name, types.ModuleType(name))
        for parent, child in zip(names, names[1:]):
            setattr(sys.modules[parent], child.rsplit(".", 1)[-1], sys.modules[child])
        sys.modules[names[-1]].BuildHookInterface = type("BuildHookInterface", (), {})
    return importlib.import_module("hyprial.gui_build_hook")


def _engine_globals(engine: dict) -> dict:
    return engine["editable_module_name"].__globals__


@pytest.fixture
def editable() -> dict:
    return runpy.run_path(str(EDITABLE_PATH))


@pytest.mark.parametrize(
    ("suffixes", "expected"),
    [
        (WINDOWS, "actors.pyd"),
        (LINUX, "actors.abi3.so"),
        (MACOS, "actors.abi3.so"),
    ],
)
def test_module_name_comes_from_the_interpreter_suffixes(
    monkeypatch: pytest.MonkeyPatch, editable: dict, suffixes: list[str], expected: str
) -> None:
    monkeypatch.setitem(_engine_globals(editable["ENGINE"]), "EXTENSION_SUFFIXES", suffixes)
    assert editable["ENGINE"]["editable_module_name"]() == expected


@pytest.mark.parametrize("suffixes", [WINDOWS_DEBUG, FREE_THREADED, []])
def test_an_interpreter_without_abi3_or_plain_pyd_has_no_editable_name(
    monkeypatch: pytest.MonkeyPatch, editable: dict, tmp_path: Path, suffixes: list[str]
) -> None:
    engine = editable["ENGINE"]
    monkeypatch.setitem(_engine_globals(engine), "EXTENSION_SUFFIXES", suffixes)
    with pytest.raises(editable["NativeEngineError"], match="debug or free-threaded"):
        engine["editable_module_name"]()
    assert engine["verify_editable"](tmp_path, python=sys.executable) is None


def test_an_empty_suffix_list_does_not_break_loading_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.machinery, "EXTENSION_SUFFIXES", [])
    engine = runpy.run_path(str(ENGINE_PATH))
    assert engine["EDITABLE_MODULE"] == "src/hyprial/_core/actors.<suffix>"


def test_a_free_threaded_build_interpreter_is_refused_before_cargo(
    monkeypatch: pytest.MonkeyPatch, editable: dict, tmp_path: Path
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setitem(
        _engine_globals(editable["ENGINE"]),
        "describe_interpreter",
        lambda python: {"implementation": "cpython", "version": [3, 13], "gil_disabled": 1},
    )
    env = {"PATH": f"{_fake_bin(tmp_path)}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    with pytest.raises(editable["NativeEngineError"], match="free-threaded"):
        editable["build_editable"](root, python=sys.executable, environ=env)
    assert not (root / "crates/target/cargo-args").exists()


def _ascii_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "ascii")


def test_editable_build_decodes_a_non_ascii_artefact_path_as_utf8(
    monkeypatch: pytest.MonkeyPatch, editable: dict, tmp_path: Path
) -> None:
    _ascii_locale(monkeypatch)
    base = tmp_path / "用户张三"
    base.mkdir()
    root = _checkout(base)
    env = {"PATH": f"{_fake_bin(base)}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    module, library, _stamp = editable["build_editable"](root, python=sys.executable, environ=env)

    assert library == root / "crates/target/release/libactors.so"
    assert module.read_bytes() == b"fresh-artifact"


def test_wheel_cargo_decodes_a_non_ascii_artefact_path_as_utf8(
    monkeypatch: pytest.MonkeyPatch, editable: dict, tmp_path: Path
) -> None:
    _ascii_locale(monkeypatch)
    base = tmp_path / "用户张三"
    base.mkdir()
    root = _checkout(base)
    cargo = _tool(_fake_bin(base, rustup=None), "cargo")
    env = {"PATH": f"/usr/bin{os.pathsep}/bin", "HYPRIAL_CARGO": cargo}

    library = editable["ENGINE"]["run_cargo"](root, python=sys.executable, environ=env)

    assert library == root / "crates/target/release/libactors.so"


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    shutil.copytree(
        REPO / "crates", root / "crates", ignore=shutil.ignore_patterns("target", "target-*")
    )
    script = root / "scripts/build/build_native_engine.sh"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!/bin/sh\ntouch '{tmp_path / 'script-ran'}'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    return root


def _fake_bin(
    tmp_path: Path, *, rustup: str | None = "1.93.1", rustc: str | None = None
) -> Path:
    """A directory of fake ``cargo``/``rustup``/``rustc`` that any platform can execute.

    Each tool is one Python script started through two entries of the kind npm
    writes: ``<name>`` for POSIX shells and ``<name>.cmd`` for ``PATHEXT``
    lookup.  Both are written everywhere; only the platform's lookup rules
    decide which one ``shutil.which`` selects, so the test has no platform
    branch.  ``rustup`` and ``rustc`` take the Rust version they answer with:
    a rustup of another version refuses the pinned-toolchain probe, and
    ``rustup=None`` leaves rustup out.  The ``.cmd`` entry names the script by
    its own directory (``%~dp0``), so the batch file stays ASCII whatever the
    path of ``tmp_path`` and however ``cmd.exe`` decodes bytes.
    """

    fake = tmp_path / "bin"
    fake.mkdir()
    tool = fake / "tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    interpreter = f'"{sys.executable}" -X utf8 "%~dp0tool.py"'
    shell_interpreter = "'" + sys.executable.replace("'", "'\\''") + "'"
    shell_tool = "'" + str(tool).replace("'", "'\\''") + "'"
    for name, version in (("cargo", ""), ("rustup", rustup), ("rustc", rustc)):
        if version is None:
            continue
        role = name if not version else f"{name} {version}"
        (fake / name).write_text(
            f'#!/bin/sh\nexec {shell_interpreter} -X utf8 {shell_tool} {role} "$@"\n',
            encoding="utf-8",
        )
        (fake / name).chmod(0o755)
        (fake / f"{name}.cmd").write_text(
            f'@echo off\r\n{interpreter} {role} %*\r\n', encoding="utf-8"
        )
    return fake


def _tool(directory: Path, name: str) -> str:
    """The entry ``shutil.which`` selects for ``name`` in ``directory``.

    The product code starts what ``shutil.which`` returns, so a test names a
    fake tool by that path: the extensionless entry on POSIX, ``<name>.cmd``
    where ``PATHEXT`` governs the lookup.
    """

    found = shutil.which(name, path=str(directory))
    assert found is not None, f"no executable entry for {name} in {directory}"
    return found


def test_windows_hook_builds_in_process_and_stages_the_pyd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook_module = _import_build_hook()
    engine = hook_module._NATIVE_TOOLS
    namespace = _engine_globals(engine)
    monkeypatch.setitem(namespace, "EXTENSION_SUFFIXES", WINDOWS)
    monkeypatch.setitem(namespace, "probe_abi_version", lambda path, python: 1)
    root = _checkout(tmp_path)
    monkeypatch.setenv("PATH", f"{_fake_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HYPRIAL_BUILD_NATIVE", "1")
    monkeypatch.delenv("HYPRIAL_BUILD_SKIP_NATIVE", raising=False)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)

    class _Hook(hook_module.CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = SimpleNamespace(version="0.5.5")
            self.target_name = "editable"

    _Hook().initialize("editable", {})

    assert not (tmp_path / "script-ran").exists()
    staged = root / "src/hyprial/_core/actors.pyd"
    assert staged.read_bytes() == b"fresh-artifact"
    assert not (root / "src/hyprial/_core/actors.abi3.so").exists()
    record = json.loads((root / "src/hyprial/_core/actors.stamp.json").read_text(encoding="utf-8"))
    assert record["module"] == "actors.pyd"
    assert record["features"] == "extension-module" and record["profile"] == "release"
    assert (root / "scripts/build/native-engine.epoch").is_file()
    assert "matches crates/" in engine["verify_editable"](root, python=sys.executable)


def test_build_runs_the_pinned_rustup_toolchain_and_honours_profile_and_features(
    monkeypatch: pytest.MonkeyPatch, editable: dict, tmp_path: Path
) -> None:
    root = _checkout(tmp_path)
    fake = _fake_bin(tmp_path)
    library = root / "crates/target/release/libactors.so"
    env = {"PATH": f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    module, reported, _stamp = editable["build_editable"](
        root,
        python=sys.executable,
        profile="debug",
        features=editable["CONFORMANCE_FEATURES"],
        environ=env,
    )

    assert reported == library
    args = (root / "crates/target/cargo-args").read_text(encoding="utf-8").split()
    assert "--release" not in args
    assert args[args.index("--features") + 1] == "extension-module,conformance-handlers"
    assert editable["cargo_prefix"](env) == [_tool(fake, "rustup"), "run", "1.93.1", "cargo"]
    os.utime(library, (1000, 1000))
    module, _reported, _stamp = editable["build_editable"](root, python=sys.executable, environ=env)
    assert module.stat().st_mtime > 1000
    assert "--release" in (root / "crates/target/cargo-args").read_text(encoding="utf-8").split()


@pytest.mark.parametrize("rustup", ["1.97.0", None])
def test_missing_pinned_toolchain_names_the_install_command(
    editable: dict, tmp_path: Path, rustup: str | None
) -> None:
    """rustup without the pinned toolchain, or no rustup and a wrong rustc."""

    fake = _fake_bin(tmp_path, rustup=rustup, rustc="1.97.0")
    env = {"PATH": f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    with pytest.raises(editable["NativeEngineError"]) as raised:
        editable["cargo_prefix"](env)
    assert "rustup toolchain install 1.93.1" in str(raised.value)
    assert "HYPRIAL_BUILD_NATIVE" in str(raised.value)


def test_without_rustup_a_matching_rustc_selects_cargo_on_path(
    editable: dict, tmp_path: Path
) -> None:
    fake = _fake_bin(tmp_path, rustup=None, rustc="1.93.1")
    env = {"PATH": f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    assert editable["cargo_prefix"](env) == [_tool(fake, "cargo")]


def test_main_rejects_unknown_arguments(editable: dict) -> None:
    assert editable["main"](["build", "--free-threaded"]) == 2
    assert editable["main"](["stamp"]) == 2
