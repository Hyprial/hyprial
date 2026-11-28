# Decisions (editable native build without bash, KP-135 item 1, 2026-11-24):
# - The editable hook used to execute scripts/build/build_native_engine.sh
#   with subprocess.  Windows cannot start a .sh (WinError 193), and running
#   it through bash would still hit `.venv/bin/python`, `readlink -f`, the
#   fixed `actors.abi3.so` name and System32\bash.exe being the WSL launcher.
#   The default editable path -- check the pinned toolchain, cargo build,
#   take the artefact from cargo's JSON messages, stage it under this
#   interpreter's extension name, write the stamp -- is therefore this
#   import-pure Python entry.  The hook calls build_editable in-process; the
#   shell script keeps its command line and delegates its default path to
#   `native_editable.py build`, so CI, docs and the hook share one code path.
#   --free-threaded (conformance only, 3.14t into crates/target-ft) stays in
#   the shell script.
# - Toolchain parity with the script: with rustup on PATH, Rust 1.93.1 must
#   answer `rustup run 1.93.1 rustc --version` and cargo runs as
#   `rustup run 1.93.1 cargo`; without rustup, `rustc --version` on PATH must
#   report 1.93.1 and cargo is native_engine.find_cargo (HYPRIAL_CARGO, else
#   PATH).  One deliberate difference from the old script: it ignored
#   HYPRIAL_CARGO and ran cargo from PATH.  Without rustup the version check
#   therefore still targets rustc on PATH while cargo may be HYPRIAL_CARGO,
#   so a private HYPRIAL_CARGO toolchain without rustc on PATH is refused as
#   "Rust 1.93.1 is not available", and a matching PATH rustc does not pin
#   that cargo's version.  With rustup, HYPRIAL_CARGO is ignored as before.
#   No remap rustflags and no MACOSX_DEPLOYMENT_TARGET pin: those
#   belong to the wheel stage, and adding them here would change cargo's
#   fingerprint between the two loops.
# - cargo's stderr is inherited (live progress and diagnostics); stdout, the
#   JSON messages, is captured and decoded as UTF-8 (errors="replace"), as the
#   script's file read did; the locale encoding (cp936/cp1252 on Windows)
#   would break or garble a non-ASCII artefact path.
# - build_editable first runs native_engine.require_abi3_interpreter on the
#   PYO3_PYTHON interpreter (CPython, GIL-enabled, >= 3.12), and the module
#   name must exist (editable_module_name), so a free-threaded or Windows
#   debug interpreter fails with NativeEngineError instead of staging a
#   module under a name nothing proved loadable.  The staged file is written with copyfile so
#   it is newer than scripts/build/native-engine.epoch even when cargo
#   reused a cached artefact (uv's cache-key logic depends on that order).
# - native_engine.py is loaded with runpy and exposed as ENGINE, so the build
#   hook takes both from one load and sees a single NativeEngineError class.
"""Editable (source-tree) build of the native actor engine, without bash.

Import-pure on purpose (no ``hyprial`` imports): the Hatch build hook loads
this file with ``runpy``, and ``scripts/build/build_native_engine.sh`` runs
it as ``build [--debug] [--conformance]``.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ENGINE = runpy.run_path(str(Path(__file__).with_name("native_engine.py")))
NativeEngineError = ENGINE["NativeEngineError"]
RUST_TOOLCHAIN: str = ENGINE["RUST_TOOLCHAIN"]
DEFAULT_FEATURES: str = ENGINE["CARGO_FEATURES"]
CONFORMANCE_FEATURES = f"{DEFAULT_FEATURES},conformance-handlers"
BUILD_NATIVE_ENV = "HYPRIAL_BUILD_NATIVE"
_MISSING_RUST = (
    f"Rust {RUST_TOOLCHAIN} is not available: install it with "
    f"`rustup toolchain install {RUST_TOOLCHAIN}`, or skip the native build by "
    f"leaving {BUILD_NATIVE_ENV} unset when running uv sync"
)


def _reports_toolchain(command: list[str], env: Mapping[str, str]) -> bool:
    try:
        completed = subprocess.run(
            command,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=ENGINE["PROBE_TIMEOUT_SECONDS"],
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and RUST_TOOLCHAIN in (completed.stdout or "")


def cargo_prefix(environ: Mapping[str, str] | None = None) -> list[str]:
    """The pinned-toolchain cargo command, as the shell script selected it."""

    env = os.environ if environ is None else environ
    path = env.get("PATH")
    rustup = shutil.which("rustup", path=path)
    if rustup is not None:
        if not _reports_toolchain([rustup, "run", RUST_TOOLCHAIN, "rustc", "--version"], env):
            raise NativeEngineError(_MISSING_RUST)
        return [rustup, "run", RUST_TOOLCHAIN, "cargo"]
    rustc = shutil.which("rustc", path=path)
    if rustc is None or not _reports_toolchain([rustc, "--version"], env):
        raise NativeEngineError(_MISSING_RUST)
    cargo = ENGINE["find_cargo"](env)
    if cargo is None:
        raise NativeEngineError(_MISSING_RUST)
    return [cargo]


def cargo_command(prefix: Sequence[str], root: Path, *, profile: str, features: str) -> list[str]:
    """The editable cargo invocation (``--release`` unless ``profile`` is debug)."""

    return [
        *prefix,
        "build",
        *(["--release"] if profile == "release" else []),
        "--locked",
        "--manifest-path",
        str(Path(root) / ENGINE["MANIFEST"]),
        "--features",
        features,
        "-p",
        ENGINE["CARGO_PACKAGE"],
        "--message-format=json-render-diagnostics",
    ]


def build_editable(
    root: Path,
    *,
    python: str,
    profile: str = "release",
    features: str = DEFAULT_FEATURES,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """Build, stage and stamp the editable module; ``(module, library, stamp)``."""

    root = Path(root)
    env = dict(os.environ if environ is None else environ)
    env["PYO3_PYTHON"] = os.path.abspath(python)
    ENGINE["require_abi3_interpreter"](env["PYO3_PYTHON"])
    module = root / ENGINE["EDITABLE_MODULE_DIR"] / ENGINE["editable_module_name"]()
    command = cargo_command(cargo_prefix(env), root, profile=profile, features=features)
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=ENGINE["CARGO_TIMEOUT_SECONDS"],
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeEngineError(f"cannot run {' '.join(command)}: {error}") from error
    if completed.returncode != 0:
        raise NativeEngineError(
            f"{' '.join(command)} failed (exit {completed.returncode}); "
            "cargo's diagnostics are on stderr above"
        )
    library = ENGINE["library_from_messages"](completed.stdout or "")
    if not library.is_file():
        raise NativeEngineError(f"cargo reported {library}, which is not a file")
    try:
        module.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(library, module)
    except OSError as error:
        raise NativeEngineError(
            f"cannot stage {library} as {module}: {error} (is a process still using it?)"
        ) from error
    stamp = ENGINE["write_stamp"](root, module, features=features, profile=profile)
    return module, library, stamp


def main(argv: Sequence[str] | None = None) -> int:
    """``build [--debug] [--conformance]`` from the source root (the script's default path)."""

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "build" or set(args[1:]) - {"--debug", "--conformance"}:
        print("usage: native_editable.py build [--debug] [--conformance]", file=sys.stderr)
        return 2
    root = Path.cwd()
    try:
        module, library, stamp = build_editable(
            root,
            python=os.environ.get("PYO3_PYTHON") or sys.executable,
            profile="debug" if "--debug" in args else "release",
            features=CONFORMANCE_FEATURES if "--conformance" in args else DEFAULT_FEATURES,
        )
    except NativeEngineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"staged {module.relative_to(root).as_posix()} (from {library})")
    print(f"stamped {stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
