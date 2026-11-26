"""Opt-in native builds for source editable installations."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tomllib
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


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


_build_hook = _import_build_hook()
BUILD_NATIVE_ENV = _build_hook.BUILD_NATIVE_ENV
SKIP_NATIVE_ENV = _build_hook.SKIP_NATIVE_ENV
CustomBuildHook = _build_hook.CustomBuildHook
NativeEngineError = _build_hook.NativeEngineError


def _hook(root: Path) -> CustomBuildHook:
    class _Hook(CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = SimpleNamespace(version="0.5.5")
            self.target_name = "editable"

    return _Hook()


def _editable_tools(monkeypatch: pytest.MonkeyPatch, verify):
    tools = dict(_build_hook._NATIVE_TOOLS)
    tools["verify_editable"] = verify
    tools["skip_requested"] = lambda: False
    monkeypatch.setattr(_build_hook, "_NATIVE_TOOLS", tools)
    return tools


def _fake_build(monkeypatch: pytest.MonkeyPatch, calls: list, on_build=None) -> None:
    editable = dict(_build_hook._NATIVE_EDITABLE)

    def build(root: Path, *, python: str):
        calls.append(("build", root, python))
        if on_build is not None:
            on_build()
        return root / "module", root / "library", root / "stamp"

    editable["build_editable"] = build
    monkeypatch.setattr(_build_hook, "_NATIVE_EDITABLE", editable)


def test_opt_in_rebuilds_missing_module_with_the_build_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "stable-python"))
    calls: list[tuple] = []

    def verify(root: Path, *, python: str) -> str | None:
        calls.append(("verify", root, python))
        return None if len(calls) == 1 else "native module is valid"

    _editable_tools(monkeypatch, verify)
    _fake_build(monkeypatch, calls)
    monkeypatch.setenv(BUILD_NATIVE_ENV, "1")

    _hook(tmp_path).initialize("editable", {})

    expected_python = os.path.realpath(tmp_path / "stable-python")
    assert calls == [
        ("verify", tmp_path, expected_python),
        ("build", tmp_path, expected_python),
        ("verify", tmp_path, expected_python),
    ]


def test_opt_in_rebuilds_a_stale_module_but_default_preserves_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempts = 0

    def verify(_root: Path, *, python: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NativeEngineError("stale module")
        return "native module is valid"

    _editable_tools(monkeypatch, verify)
    _fake_build(monkeypatch, [])
    monkeypatch.setenv(BUILD_NATIVE_ENV, "1")
    _hook(tmp_path).initialize("editable", {})
    assert attempts == 2

    monkeypatch.delenv(BUILD_NATIVE_ENV)
    attempts = 0
    with pytest.raises(NativeEngineError, match="stale module"):
        _hook(tmp_path).initialize("editable", {})


def test_native_switch_rejects_typos_and_skip_conflicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(BUILD_NATIVE_ENV, "true")
    with pytest.raises(NativeEngineError, match="accepts only"):
        _hook(tmp_path).initialize("editable", {})

    monkeypatch.setenv(BUILD_NATIVE_ENV, "1")
    monkeypatch.setenv(SKIP_NATIVE_ENV, "1")
    with pytest.raises(NativeEngineError, match="conflicts"):
        _hook(tmp_path).initialize("editable", {})


def test_uv_cache_keys_include_native_switches() -> None:
    document = tomllib.loads(
        (Path(_build_hook.__file__).parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    keys = document["tool"]["uv"]["cache-keys"]
    assert {entry["env"] for entry in keys if "env" in entry} >= {
        BUILD_NATIVE_ENV,
        SKIP_NATIVE_ENV,
    }


def test_opt_in_build_touches_the_epoch_before_it_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    epoch = tmp_path / "scripts/build/native-engine.epoch"
    epoch.parent.mkdir(parents=True)
    epoch.write_text("epoch\n", encoding="utf-8")
    os.utime(epoch, (1000, 1000))
    seen: list[float] = []
    states = iter([None, "native module is valid"])
    _editable_tools(monkeypatch, lambda _root, *, python: next(states))
    _fake_build(monkeypatch, [], on_build=lambda: seen.append(epoch.stat().st_mtime))
    monkeypatch.setenv(BUILD_NATIVE_ENV, "1")

    _hook(tmp_path).initialize("editable", {})

    assert seen and seen[0] > 1000


def test_epoch_is_created_when_missing_and_untouched_without_a_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    epoch = tmp_path / "scripts/build/native-engine.epoch"
    epoch.parent.mkdir(parents=True)
    epoch.write_text("epoch\n", encoding="utf-8")
    os.utime(epoch, (1000, 1000))
    _editable_tools(monkeypatch, lambda _root, *, python: "native module is valid")
    monkeypatch.setenv(BUILD_NATIVE_ENV, "1")
    _hook(tmp_path).initialize("editable", {})
    assert epoch.stat().st_mtime == 1000

    epoch.unlink()
    _hook(tmp_path)._advance_native_epoch(tmp_path)
    assert epoch.is_file()


def test_uv_cache_keys_include_the_tracked_epoch_outside_the_staged_files() -> None:
    root = Path(_build_hook.__file__).parents[2]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    keys = [entry["file"] for entry in document["tool"]["uv"]["cache-keys"] if "file" in entry]
    assert "scripts/build/native-engine.epoch" in keys
    assert "src/hyprial/_core/actors.*" in keys
    assert (root / "scripts/build/native-engine.epoch").is_file()
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "scripts/build/native-engine.epoch"],
        cwd=root,
        check=False,
    )
    assert ignored.returncode != 0


def test_native_script_reports_install_command_without_rustup(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[4]
    root = tmp_path / "checkout"
    shutil.copytree(
        source_root / "crates",
        root / "crates",
        ignore=shutil.ignore_patterns("target", "target-ft"),
    )
    (root / "scripts/build").mkdir(parents=True)
    shutil.copy2(
        source_root / "scripts/build/build_native_engine.sh",
        root / "scripts/build/build_native_engine.sh",
    )
    script = root / "scripts/build/build_native_engine.sh"
    script.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "rustc").write_text(
        "#!/bin/sh\necho 'rustc 1.97.0 (fake)'\n", encoding="utf-8"
    )
    (fake_bin / "rustc").chmod(0o755)
    uv_path = Path(shutil.which("uv") or "uv").parent
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{uv_path}:/usr/bin:/bin"
    completed = subprocess.run(
        [str(script)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "rustup toolchain install 1.93.1" in completed.stderr
    assert "HYPRIAL_BUILD_NATIVE" in completed.stderr
