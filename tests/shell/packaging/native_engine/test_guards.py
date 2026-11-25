"""Guards of the wheel's native stage found by the 2026-11-04 review.

Each case drives the import-pure helper (or the real Hatch hook) with fakes
for cargo and, where the case is about the build interpreter, a fake
interpreter that answers the stage's interpreter probe; no Rust toolchain
is needed.  Fixtures come from ``test_stage``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from hyprial.shell.impl.packaging import native_engine
from hyprial.shell.impl.packaging.tailcat_wheel import TARGET_ENV, host_target

from .test_stage import (
    REPO,
    _crates_root,
    _fake_cargo,
    native_env as native_env,
)


def _fake_interpreter(tmp_path: Path, report: dict) -> Path:
    """An interpreter that answers every invocation with ``report`` as JSON."""

    script = tmp_path / "fake-python" / "python"
    script.parent.mkdir()
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(json.dumps(report))}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        ({"implementation": "cpython", "version": [3, 14], "gil_disabled": 1}, "free-threaded"),
        ({"implementation": "pypy", "version": [3, 12], "gil_disabled": None}, "CPython"),
        ({"implementation": "cpython", "version": [3, 11], "gil_disabled": 0}, "3.12"),
    ],
)
def test_a_non_abi3_build_interpreter_is_refused_before_cargo(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
    report: dict,
    reason: str,
) -> None:
    root = _crates_root(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    python = _fake_interpreter(tmp_path, report)
    with pytest.raises(native_engine.NativeEngineError, match=reason) as caught:
        native_engine.build_extension(
            root, host_target(), host=host_target(), output=tmp_path / "out", python=str(python)
        )
    assert "cp312-abi3" in str(caught.value)
    assert not (tmp_path / "cargo.log").exists()


def test_the_gil_enabled_test_interpreter_passes_the_interpreter_guard() -> None:
    assert native_engine.require_abi3_interpreter(sys.executable) is None


@pytest.mark.parametrize(
    ("features", "match"),
    [('["abi3-py313"]', "abi3-py313"), ('["py-clone"]', "no abi3-py3")],
)
def test_the_python_tag_must_match_the_pyo3_abi3_feature(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch, features: str, match: str
) -> None:
    root = _crates_root(tmp_path)
    (root / "crates" / "hyprial-py" / "Cargo.toml").write_text(
        f'[dependencies]\npyo3 = {{ version = "0.30", features = {features} }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    with pytest.raises(native_engine.NativeEngineError, match=match):
        native_engine.build_extension(
            root, host_target(), host=host_target(), output=tmp_path / "out"
        )
    assert not (tmp_path / "cargo.log").exists()


def test_the_real_crate_declares_the_feature_the_wheel_tag_claims() -> None:
    assert native_engine.declared_python_tag(REPO) == native_engine.PYTHON_TAG == "cp312"


DECLARED_2_17 = "manylinux_2_17_x86_64.manylinux2014_x86_64"


def test_a_glibc_floor_above_the_declared_tag_fails_closed_by_default() -> None:
    with pytest.raises(native_engine.NativeEngineError) as caught:
        native_engine.platform_tag_for_floor(DECLARED_2_17, (2, 34), {})
    text = str(caught.value)
    assert "GLIBC_2.34" in text and "manylinux_2_17" in text
    assert "manylinux2014" in text and native_engine.ALLOW_GLIBC_RAISE_ENV in text
    for value in ("", "0", "true", " 1"):
        with pytest.raises(native_engine.NativeEngineError):
            native_engine.platform_tag_for_floor(
                DECLARED_2_17, (2, 34), {native_engine.ALLOW_GLIBC_RAISE_ENV: value}
            )
    assert native_engine.platform_tag_for_floor(DECLARED_2_17, (2, 17), {}) == (DECLARED_2_17, None)


def test_the_explicit_developer_opt_in_tags_honestly_and_forbids_publishing() -> None:
    tag, warning = native_engine.platform_tag_for_floor(
        DECLARED_2_17, (2, 34), {native_engine.ALLOW_GLIBC_RAISE_ENV: "1"}
    )
    assert tag == "manylinux_2_34_x86_64"
    assert warning is not None and "must not be published" in warning
    assert native_engine.ALLOW_GLIBC_RAISE_ENV in warning


def _env_cargo(tmp_path: Path) -> tuple[Path, Path]:
    """A cargo that dumps its environment as JSON and reports one cdylib."""

    out = tmp_path / "env-cargo-out"
    out.mkdir()
    produced = out / "libactors.so"
    dump = tmp_path / "cargo-env.json"
    message = json.dumps(
        {
            "reason": "compiler-artifact",
            "target": {"name": "actors", "kind": ["cdylib"]},
            "filenames": [str(produced)],
        }
    )
    script = tmp_path / "env-bin" / "cargo"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib\n"
        f"pathlib.Path({str(dump)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"pathlib.Path({str(produced)!r}).write_bytes(b'native engine bytes')\n"
        f"print({message!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, dump


def test_a_darwin_tag_pins_the_deployment_target_over_an_inherited_one() -> None:
    env = native_engine.cargo_environment(
        sys.executable, {"MACOSX_DEPLOYMENT_TARGET": "14.0"}, platform_tag="macosx_13_0_arm64"
    )
    assert env["MACOSX_DEPLOYMENT_TARGET"] == "13.0"
    linux = native_engine.cargo_environment(
        sys.executable, {}, platform_tag=DECLARED_2_17
    )
    assert "MACOSX_DEPLOYMENT_TARGET" not in linux


def test_the_build_forwards_the_wheel_platform_tag_to_cargo(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crates_root(tmp_path)
    cargo, dump = _env_cargo(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(cargo))
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "15.0")
    monkeypatch.setattr(native_engine, "probe_abi_version", lambda path, python: 1)
    native_engine.build_extension(
        root,
        host_target(),
        host=host_target(),
        output=tmp_path / "out",
        platform_tag="macosx_13_0_x86_64",
    )
    assert json.loads(dump.read_text(encoding="utf-8"))["MACOSX_DEPLOYMENT_TARGET"] == "13.0"


SEP = "\x1f"


def test_builder_paths_are_remapped_without_dropping_user_rustflags(tmp_path: Path) -> None:
    root = tmp_path / "root"
    remap = [
        f"--remap-path-prefix={root.resolve()}=/src",
        "--remap-path-prefix=/ch=/cargo",
    ]
    plain = native_engine.cargo_environment(sys.executable, {"CARGO_HOME": "/ch"}, root=root)
    assert plain["CARGO_ENCODED_RUSTFLAGS"].split(SEP) == remap
    spaced = native_engine.cargo_environment(
        sys.executable, {"CARGO_HOME": "/ch", "RUSTFLAGS": " -C  target-cpu=native "}, root=root
    )
    assert spaced["CARGO_ENCODED_RUSTFLAGS"].split(SEP) == ["-C", "target-cpu=native", *remap]
    encoded = native_engine.cargo_environment(
        sys.executable,
        {"CARGO_HOME": "/ch", "RUSTFLAGS": "-Cignored", "CARGO_ENCODED_RUSTFLAGS": f"-Ca{SEP}-Cb c"},
        root=root,
    )
    assert encoded["CARGO_ENCODED_RUSTFLAGS"].split(SEP) == ["-Ca", "-Cb c", *remap]
    home = native_engine.cargo_environment(sys.executable, {"HOME": "/h"}, root=root)
    assert home["CARGO_ENCODED_RUSTFLAGS"].split(SEP)[-1] == (
        f"--remap-path-prefix={Path('/h', '.cargo')}=/cargo"
    )


def test_the_hook_reports_the_staged_module_sha256(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from .test_stage import _hook, _hook_module, _probe_ok

    module = _hook_module()
    root = _crates_root(tmp_path)
    cargo, dump = _env_cargo(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(cargo))
    _probe_ok(module, monkeypatch)
    hook = _hook(module, root)
    hook._stages = []
    hook._stage_native(root, {"force_include": {}})
    digest = hashlib.sha256(b"native engine bytes").hexdigest()
    assert f"sha256 {digest}" in capsys.readouterr().err
    assert "--remap-path-prefix=" in json.loads(dump.read_text(encoding="utf-8"))[
        "CARGO_ENCODED_RUSTFLAGS"
    ]
    hook.finalize("standard", {}, "unused")


def test_the_source_digest_ignores_non_cargo_byproducts(tmp_path: Path) -> None:
    root = _crates_root(tmp_path)
    before = native_engine.source_digest(root)
    cache = root / "crates" / "hyprial-py" / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "behavior_support.cpython-312.pyc").write_bytes(b"\x00bytecode")
    (root / "crates" / "hyprial-py" / "tests" / "notes.md").write_text("n\n", encoding="utf-8")
    (root / "crates" / "target-conformance" / "release").mkdir(parents=True)
    (root / "crates" / "target-conformance" / "release" / "x.rs").write_text("//\n", encoding="utf-8")
    assert native_engine.source_digest(root) == before


def test_only_top_level_target_dirs_are_pruned_from_the_digest(tmp_path: Path) -> None:
    root = _crates_root(tmp_path)
    before = native_engine.source_digest(root)
    nested = root / "crates" / "hyprial-py" / "src" / "target_specific"
    nested.mkdir()
    (nested / "mod.rs").write_text("// real source\n", encoding="utf-8")
    assert native_engine.source_digest(root) != before
    after_rs = native_engine.source_digest(root)
    (root / "crates" / "hyprial-py" / "build.rs").write_text("fn main() {}\n", encoding="utf-8")
    assert native_engine.source_digest(root) != after_rs


def test_crate_sources_pull_in_no_file_the_digest_does_not_hash() -> None:
    crates = REPO / "crates"
    pattern = re.compile(r"\binclude(?:_str|_bytes)?!\s*\(")
    for path in crates.glob("*/**/*.rs"):
        if path.relative_to(crates).parts[0].startswith("target"):
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), path
    assert not list(crates.glob("**/.cargo")) and not list(crates.glob("rust-toolchain*"))


def _cache_keys() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = data.get("tool", {}).get("uv", {}).get("cache-keys", [])
    return [entry["file"] for entry in entries if isinstance(entry, dict) and "file" in entry]


def _key_matches(key: str, relative: str) -> bool:
    return fnmatch.fnmatchcase(relative, key) or fnmatch.fnmatchcase(
        relative, key.replace("**/", "")
    )


def test_uv_rebuilds_the_editable_hook_when_any_digest_input_changes() -> None:
    keys = _cache_keys()
    assert "pyproject.toml" in keys
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "crates"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    inputs = [path for path in tracked if native_engine.is_cargo_input(path)]
    assert "crates/Cargo.lock" in inputs and "crates/hyprial-py/build.rs" in inputs
    for relative in inputs:
        assert any(_key_matches(key, relative) for key in keys), relative
    for relative in (
        native_engine.EDITABLE_MODULE,
        "src/hyprial/_core/actors.pyd",
        f"src/hyprial/_core/{native_engine.STAMP_NAME}",
    ):
        assert any(_key_matches(key, relative) for key in keys), relative
    for relative in ("crates/target/release/build/x/out/host.rs", "crates/hyprial-py/tests/a.py"):
        assert not any(_key_matches(key, relative) for key in keys), relative


_SDIST_TREE = (
    "pyproject.toml",
    "VERSION",
    ".gitignore",
    "src/hyprial/gui_build_hook.py",
    "src/hyprial/shell/impl/packaging/tailcat_wheel.py",
    "src/hyprial/shell/impl/packaging/native_engine.py",
    "src/hyprial/shell/impl/gui/install/resources.py",
    "crates/Cargo.toml",
    "crates/Cargo.lock",
    "crates/hyprial-py/Cargo.toml",
    "crates/hyprial-py/src/lib.rs",
)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_the_sdist_carries_no_cargo_target_dir_or_generated_gui_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    for relative in _SDIST_TREE:
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, tree / relative)
    (tree / "src/hyprial/__init__.py").write_text("", encoding="utf-8")
    (tree / "sidecar/hyprial-tailcat").mkdir(parents=True)
    (tree / "sidecar/hyprial-tailcat/go.mod").write_text("module x\n", encoding="utf-8")
    planted = (
        "crates/target-x/release/libactors.so",
        "crates/target-conformance/foo",
        "gui/node_modules/pkg/index.js",
        "gui/dist/index.html",
    )
    for relative in planted:
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).write_text("generated\n", encoding="utf-8")
    (tree / "gui/package.json").write_text("{}\n", encoding="utf-8")
    built = subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(tmp_path / "dist")],
        cwd=tree,
        env={**os.environ, "HYPRIAL_BUILD_SKIP_NATIVE": ""},
        capture_output=True,
        text=True,
        timeout=600.0,
        check=False,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    (archive,) = (tmp_path / "dist").glob("*.tar.gz")
    with tarfile.open(archive) as handle:
        names = [name.split("/", 1)[1] for name in handle.getnames() if "/" in name]
    assert "crates/hyprial-py/src/lib.rs" in names and "gui/package.json" in names
    leaked = [name for name in names if name.startswith(planted) or name.startswith(
        ("crates/target", "gui/node_modules", "gui/dist")
    )]
    assert leaked == []


@pytest.mark.parametrize("case", ["no-cargo", "foreign-target", "no-crates"])
def test_an_impossible_native_build_fails_before_the_tailcat_and_gui_stages(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from .test_stage import _hook, _hook_module

    module = _hook_module()
    root = _crates_root(tmp_path)
    if case == "foreign-target":
        monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
        foreign = "windows/amd64" if host_target() != "windows/amd64" else "linux/amd64"
        monkeypatch.setenv(TARGET_ENV, foreign)
    if case == "no-crates":
        monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
        (root / "crates" / "Cargo.lock").unlink()
    stages: list[str] = []
    hook = _hook(module, root)
    monkeypatch.setattr(hook, "_stage_tailcat", lambda *args: stages.append("tailcat"))
    monkeypatch.setattr(hook, "_stage_gui", lambda *args: stages.append("gui"))
    with pytest.raises(module.NativeEngineError):
        hook.initialize("standard", {"force_include": {}})
    assert stages == []
    assert not (tmp_path / "cargo.log").exists()


def test_the_preflight_lets_a_possible_build_reach_every_stage(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_stage import _hook, _hook_module, _probe_ok

    module = _hook_module()
    root = _crates_root(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    _probe_ok(module, monkeypatch)
    stages: list[str] = []
    hook = _hook(module, root)
    monkeypatch.setattr(hook, "_stage_tailcat", lambda *args: stages.append("tailcat"))
    monkeypatch.setattr(hook, "_stage_gui", lambda *args: stages.append("gui"))
    build_data: dict = {"force_include": {}}
    hook.initialize("standard", build_data)
    assert stages == ["tailcat", "gui"]
    assert build_data["tag"].startswith("cp312-abi3-")
    hook.finalize("standard", build_data, "unused")
