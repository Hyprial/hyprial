"""The developer script stages the artefact cargo reports, never a guessed path."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_CARGO = """#!/bin/sh
dir="${CARGO_TARGET_DIR:-${CARGO_BUILD_TARGET_DIR:-}}"
if [ -z "$dir" ] && [ -f .cargo/config.toml ]; then
  dir=$(sed -n 's/^target-dir *= *"\\(.*\\)"$/\\1/p' .cargo/config.toml)
fi
dir="${dir:-crates/target}"
case "$dir" in /*) ;; *) dir="$PWD/$dir" ;; esac
mkdir -p "$dir/release"
printf fresh-artifact > "$dir/release/libactors.so"
printf '{"reason":"compiler-artifact","target":{"name":"actors","kind":["cdylib"]},"filenames":["%s/release/libactors.so"]}\\n' "$dir"
"""

FAKE_RUSTUP = """#!/bin/sh
if [ "$1" = run ] && [ "$3" = rustc ]; then
  echo 'rustc 1.93.1 (fake)'
  exit 0
fi
shift 2
exec "$@"
"""


def _checkout(tmp_path: Path) -> Path:
    source_root = Path(__file__).parents[4]
    root = tmp_path / "checkout"
    shutil.copytree(
        source_root / "crates",
        root / "crates",
        ignore=shutil.ignore_patterns("target", "target-ft"),
    )
    (root / "scripts/build").mkdir(parents=True)
    script = root / "scripts/build/build_native_engine.sh"
    shutil.copy2(source_root / "scripts/build/build_native_engine.sh", script)
    script.chmod(0o755)
    native_engine = root / "src/hyprial/shell/impl/packaging/native_engine.py"
    native_engine.parent.mkdir(parents=True)
    shutil.copy2(source_root / "src/hyprial/shell/impl/packaging/native_engine.py", native_engine)
    shutil.copy2(
        source_root / "src/hyprial/shell/impl/packaging/native_editable.py",
        native_engine.with_name("native_editable.py"),
    )
    return root


def _fake_toolchain(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, text in (("rustup", FAKE_RUSTUP), ("cargo", FAKE_CARGO)):
        (fake_bin / name).write_text(text, encoding="utf-8")
        (fake_bin / name).chmod(0o755)
    return fake_bin


@pytest.mark.parametrize("way", ["CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "config", "default"])
def test_script_stages_the_artefact_cargo_reports(tmp_path: Path, way: str) -> None:
    root = _checkout(tmp_path)
    decoy = root / "crates/target/release/libactors.so"
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"STALE-DECOY-MARKER")
    external = tmp_path / "external-target"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")
    }
    env["PATH"] = f"{_fake_toolchain(tmp_path)}:{env['PATH']}"
    env["PYO3_PYTHON"] = sys.executable
    if way in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
        env[way] = "external-target"
    elif way == "config":
        (root / ".cargo").mkdir()
        (root / ".cargo/config.toml").write_text(
            f'[build]\ntarget-dir = "{external}"\n', encoding="utf-8"
        )
    decoy_before = hashlib.sha256(decoy.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(root / "scripts/build/build_native_engine.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    staged = (root / "src/hyprial/_core/actors.abi3.so").read_bytes()
    assert staged == b"fresh-artifact"
    if way != "default":
        assert hashlib.sha256(decoy.read_bytes()).hexdigest() == decoy_before
    assert (root / "src/hyprial/_core/actors.stamp.json").is_file()
    assert "staged src/hyprial/_core/actors.abi3.so (from " in completed.stdout
