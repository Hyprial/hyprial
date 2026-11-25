#!/usr/bin/env python3
# Decisions (shell-6.md §D.4 6c-1, 6c-2, §G.2; packaging/clients/README.md):
# - The lock (runtime-lock.json) pins one python-build-standalone (PBS)
#   "install_only_stripped" asset per target by URL, size and SHA-256, plus
#   the hyprial wheel platform tag and the uv target triple used to pick
#   dependency wheels. Unknown keys are refused, so a typo cannot silently
#   drop a check. Targets are macOS arm64 (packaged) and Linux x86-64 (only
#   for the script's own tests and a later Linux package); no Windows entry.
# - The archive is verified (size and SHA-256) before a single member is
#   read. Extraction is by hand: only regular files and directories under
#   `python/` are written; absolute names, `..` and other prefixes are
#   refused; symlink and hardlink members are skipped. In PBS 20261110 every
#   link is an alias (`bin/python3 -> python3.12`, pkg-config and man page
#   aliases), and the shell names `bin/python3.12` exactly, so the staged
#   tree has no symlink at all (Tauri's resource copier and notarization
#   both prefer plain files).
# - Pruned: every `bin/` entry except `python3.12` (pip and idle scripts
#   with absolute shebangs), `include/`, `share/`, `lib/pkgconfig/`, Tcl/Tk
#   (`lib/tcl*`, `lib/tk*`, `lib/itcl*`, `lib/thread*`, `lib/libtcl*`,
#   `lib/libtk*`, `_tkinter`), the stdlib `test`, `idlelib`, `tkinter`,
#   `turtledemo`, `ensurepip`, the static `config-3.12-*` directory, the
#   bundled pip in `site-packages`, and every upstream `__pycache__`.
#   `libpython3.12.*` stays: whether the interpreter needs it differs by
#   platform and is not worth a per-target rule.
# - Dependencies: `uv export --locked --no-dev --no-emit-project` of the
#   repository lock (hashes included), installed with
#   `uv pip install --target <site-packages> --require-hashes --only-binary
#   :all: --python-platform <lock triple> --python-version <lock python>
#   --link-mode copy`. The staged interpreter is never executed here (a
#   cross or fake tree cannot run); uv's own Python 3.12 drives the install
#   and the triple selects the wheels (macOS: minimum 13.0, the hyprial
#   wheel's own tag). The hyprial wheel follows with `--no-deps`; its
#   platform tag must be the lock's or `any`. `--link-mode copy` keeps the
#   staged files from sharing inodes with uv's cache.
# - uv 0.11.7 has no `--no-installer-metadata`, so the script removes
#   `INSTALLER`, `REQUESTED` and `direct_url.json` (which names the local
#   wheel path) and their RECORD lines, and the console-script `bin/`
#   directory uv writes under `--target` (absolute shebangs).
# - Bytecode: the tree is compiled once here, with
#   `--invalidation-mode unchecked-hash`, `PYTHONHASHSEED=0` and the staging
#   root stripped from the recorded file names. The bundle is then never
#   written at run time (CPython would otherwise create `__pycache__` inside
#   `Contents/Resources` on the first launch and break the code seal). This
#   replaces the plan's "prune `__pycache__`" for the files we ship;
#   upstream bytecode is still pruned. Two stagings of the same inputs give
#   identical bytes because compilation is single-process (`-j 1`): with
#   `-j 0` the marshal reference flags depend on which worker compiled which
#   file, and three stagings of the real tree differed in three `.pyc`
#   files (measured on Linux). Any compileall failure (exit 1) refuses the
#   staging and names the modules; on the real Linux tree all 15062 modules
#   compile, so there is no exclusion list.
# - File modes are normalised to 0755 (any execute bit) or 0644, directories
#   0755. `runtime-manifest.json` at the root lists target, Python, PBS tag
#   and SHA-256, the wheel (file, version, SHA-256), the source commit (from
#   the wheel's `hyprial/_gui/release.json`) and SHA-256 plus mode of every
#   other file; it holds no absolute path and no time stamp.
# - The tree is assembled in a sibling directory and swapped in at the end.
#   An existing output directory is only replaced when it holds a
#   `runtime-manifest.json` (a previous staging); anything else is refused.
"""Stage the bundled Python runtime of the native shell.

Usage (from the repository root, macOS arm64)::

    uv run --no-project --python 3.12 python packaging/clients/stage_runtime.py \\
        --target aarch64-apple-darwin --wheel dist/hyprial-…-macosx_13_0_arm64.whl

The default output is ``clients/native/src-tauri/runtime`` (git-ignored),
which ``packaging/clients/tauri.bundle.json`` copies into
``HyprialOS.app/Contents/Resources/runtime``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = Path(__file__).resolve().parent / "runtime-lock.json"
DEFAULT_OUT = ROOT / "clients" / "native" / "src-tauri" / "runtime"
MANIFEST_NAME = "runtime-manifest.json"
INTERPRETER = "bin/python3.12"
SITE_PACKAGES = "lib/python3.12/site-packages"
PBS_PREFIX = "https://github.com/astral-sh/python-build-standalone/releases/download/"
LOCK_KEYS = {"version", "python", "pbsTag", "targets"}
TARGET_KEYS = {"url", "sha256", "size", "wheelPlatform", "pythonPlatform"}
KNOWN_TARGETS = {"aarch64-apple-darwin", "x86_64-unknown-linux-gnu"}
PRUNED = (
    "include", "share", "lib/pkgconfig", "lib/tcl*", "lib/tk*", "lib/itcl*", "lib/thread*",
    "lib/libtcl*", "lib/libtk*", "lib/python3.12/test", "lib/python3.12/idlelib",
    "lib/python3.12/tkinter", "lib/python3.12/turtledemo", "lib/python3.12/ensurepip",
    "lib/python3.12/config-3.12-*", "lib/python3.12/lib-dynload/_tkinter*",
    "lib/python3.12/site-packages/*",
)
INSTALLER_METADATA = {"INSTALLER", "REQUESTED", "direct_url.json"}


class StageError(Exception):
    """A refusal: the runtime is not staged and nothing is half-written."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StageError(message)


def validate_lock(lock: Any) -> dict[str, Any]:
    """Return the lock unchanged when every field is well formed."""

    _require(isinstance(lock, dict), "lock: not an object")
    unknown = set(lock) - LOCK_KEYS
    _require(not unknown, f"lock: unknown keys {sorted(unknown)}")
    _require(lock.get("version") == 1, "lock: version must be 1")
    python = lock.get("python")
    tag = lock.get("pbsTag")
    _require(isinstance(python, str) and re.fullmatch(r"3\.12\.\d+", python) is not None, "lock: python must be 3.12.x")
    _require(isinstance(tag, str) and re.fullmatch(r"\d{8}", tag) is not None, "lock: pbsTag must be a PBS release tag")
    targets = lock.get("targets")
    _require(isinstance(targets, dict) and bool(targets), "lock: targets missing")
    for target, entry in targets.items():
        _require(target in KNOWN_TARGETS, f"lock: unknown target {target}")
        _require(isinstance(entry, dict), f"lock: {target} is not an object")
        unknown = set(entry) - TARGET_KEYS
        _require(not unknown, f"lock: {target} unknown keys {sorted(unknown)}")
        missing = TARGET_KEYS - set(entry)
        _require(not missing, f"lock: {target} missing keys {sorted(missing)}")
        asset = f"cpython-{python}%2B{tag}-{target}-install_only_stripped.tar.gz"
        _require(entry["url"] == f"{PBS_PREFIX}{tag}/{asset}", f"lock: {target} url is not the PBS asset {asset}")
        _require(isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None, f"lock: {target} sha256 is not 64 lowercase hex")
        _require(isinstance(entry["size"], int) and entry["size"] > 0, f"lock: {target} size must be a positive integer")
        for key in ("wheelPlatform", "pythonPlatform"):
            _require(isinstance(entry[key], str) and bool(entry[key]), f"lock: {target} {key} must be a non-empty string")
    return lock


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    """Read and validate a runtime lock file."""

    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StageError(f"lock: cannot read {path}: {error}") from error
    return validate_lock(lock)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, entry: dict[str, Any]) -> None:
    """Refuse an archive whose size or SHA-256 differs from the lock."""

    _require(path.is_file(), f"archive missing: {path}")
    size = path.stat().st_size
    _require(size == entry["size"], f"archive size {size} != lock size {entry['size']}")
    actual = sha256_file(path)
    _require(actual == entry["sha256"], f"archive sha256 {actual} != lock sha256 {entry['sha256']}")


def fetch_archive(entry: dict[str, Any], cache_dir: Path) -> Path:
    """Download the locked asset once into ``cache_dir`` and verify it."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    name = entry["url"].rsplit("/", 1)[1].replace("%2B", "+")
    target = cache_dir / name
    if target.is_file():
        verify_archive(target, entry)
        return target
    partial = target.with_name(target.name + ".part")
    with urllib.request.urlopen(entry["url"], timeout=300) as response, partial.open("wb") as sink:
        shutil.copyfileobj(response, sink)
    verify_archive(partial, entry)
    partial.replace(target)
    return target


def _pruned(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if "__pycache__" in parts:
        return True
    if parts and parts[0] == "bin" and relative != INTERPRETER:
        return len(parts) > 1
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        if any(fnmatch.fnmatchcase(prefix, pattern) for pattern in PRUNED):
            return True
    return False


def extract(archive: Path, destination: Path) -> list[str]:
    """Write the regular files and directories of ``python/``; return skipped links."""

    links: list[str] = []
    with tarfile.open(archive, mode="r:gz") as source:
        members = source.getmembers()
        for member in members:
            name = member.name
            path = PurePosixPath(name)
            _require(not path.is_absolute() and ".." not in path.parts, f"unsafe member {name}")
            _require(path.parts[:1] == ("python",), f"unexpected member {name}")
            _require(member.isfile() or member.isdir() or member.issym() or member.islnk(), f"unsupported member type {name}")
        for member in members:
            relative = "/".join(PurePosixPath(member.name).parts[1:])
            if not relative or _pruned(relative):
                continue
            if member.issym() or member.islnk():
                links.append(relative)
                continue
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            _require(stream is not None, f"unreadable member {member.name}")
            with stream, target.open("wb") as sink:
                shutil.copyfileobj(stream, sink)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return sorted(links)


def _wheel_tags(wheel: Path) -> tuple[str, str, str]:
    parts = wheel.name[: -len(".whl")].split("-")
    _require(wheel.suffix == ".whl" and len(parts) in (5, 6), f"not a wheel file name: {wheel.name}")
    return parts[0], parts[1], parts[-1]


def wheel_facts(wheel: Path, entry: dict[str, Any]) -> dict[str, str]:
    """Name, version, SHA-256 and source commit of the hyprial wheel."""

    _require(wheel.is_file(), f"wheel missing: {wheel}")
    name, version, platform = _wheel_tags(wheel)
    _require(name == "hyprial", f"not a hyprial wheel: {wheel.name}")
    _require(platform == "any" or entry["wheelPlatform"] in platform.split("."), f"wheel platform {platform} is not {entry['wheelPlatform']}")
    with zipfile.ZipFile(wheel) as archive:
        try:
            release = json.loads(archive.read("hyprial/_gui/release.json"))
        except (KeyError, json.JSONDecodeError) as error:
            raise StageError(f"wheel has no readable hyprial/_gui/release.json: {error}") from error
    commit = release.get("sourceCommit") if isinstance(release, dict) else None
    _require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "wheel release.json has no 40-hex sourceCommit")
    return {"file": wheel.name, "version": version, "sha256": sha256_file(wheel), "sourceCommit": commit}


def _run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(argv, cwd=None if cwd is None else str(cwd), env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise StageError(f"command failed ({result.returncode}): {' '.join(argv[:4])} …\n{result.stdout[-3000:]}{result.stderr[-3000:]}")


def export_requirements(destination: Path) -> Path:
    """``uv export`` of the repository lock, product dependencies only, with hashes."""

    output = destination / "requirements.txt"
    _run(["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--no-header", "--output-file", str(output)], cwd=ROOT)
    return output


def install(site: Path, requirements: Path, wheel: Path, lock: dict[str, Any], entry: dict[str, Any], *, offline: bool) -> None:
    """Install the locked dependencies, then the hyprial wheel, into ``site``."""

    common = [
        "--target", str(site), "--python", sys.executable, "--python-version", lock["python"],
        "--python-platform", entry["pythonPlatform"], "--only-binary", ":all:", "--link-mode", "copy",
        "--no-compile-bytecode", "--quiet",
    ]
    if offline:
        common.append("--offline")
    _run(["uv", "pip", "install", *common, "--require-hashes", "-r", str(requirements)])
    _run(["uv", "pip", "install", *common, "--no-deps", str(wheel)])
    shutil.rmtree(site / "bin", ignore_errors=True)
    for info in site.glob("*.dist-info"):
        for name in INSTALLER_METADATA:
            (info / name).unlink(missing_ok=True)
        record = info / "RECORD"
        if record.is_file():
            lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
            kept = [line for line in lines if PurePosixPath(line.split(",", 1)[0]).name not in INSTALLER_METADATA or not line.split(",", 1)[0].startswith(info.name + "/")]
            record.write_text("".join(kept), encoding="utf-8")


def compile_command(root: Path, python: str | None = None) -> list[str]:
    """The one ``compileall`` command line used for the staged tree."""

    return [
        python or sys.executable, "-s", "-m", "compileall", "-q", "-j", "1",
        "--invalidation-mode", "unchecked-hash", "-s", str(root), str(root / "lib"),
    ]


def compile_bytecode(root: Path) -> None:
    """Deterministic, never-revalidated bytecode for every module in the tree.

    Compilation is single-process: with ``-j 0`` the marshal reference flags
    of a code object depend on which worker compiled which file, so two
    stagings of the same tree differed in a few ``.pyc`` files. A module that
    does not compile is a refusal: without its bytecode CPython would write
    ``__pycache__`` into the signed bundle on first launch.
    """

    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONHASHSEED"] = "0"
    result = subprocess.run(compile_command(root), env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        failed = sorted({name.replace(str(root) + os.sep, "") for name in re.findall(r"\*\*\* Error compiling '([^']+)'", result.stdout + result.stderr)})
        detail = f"modules that do not compile: {failed[:20]}" if failed else (result.stdout + result.stderr)[-2000:]
        raise StageError(f"compileall failed ({result.returncode}); {detail}")


def normalise(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        _require(not stat.S_ISLNK(mode), f"symlink in staged tree: {path.relative_to(root)}")
        _require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), f"special file in staged tree: {path.relative_to(root)}")
        if stat.S_ISDIR(mode):
            path.chmod(0o755)
        else:
            path.chmod(0o755 if mode & 0o111 else 0o644)


def manifest_files(root: Path) -> dict[str, dict[str, str]]:
    """The manifest's ``files``: SHA-256 and mode of every file but the manifest (sign_bundle.py reuses it)."""

    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and relative != MANIFEST_NAME:
            files[relative] = {"sha256": sha256_file(path), "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"}
    return files


def build_manifest(root: Path, lock: dict[str, Any], target: str, facts: dict[str, str]) -> dict[str, Any]:
    files = manifest_files(root)
    return {
        "version": 1,
        "target": target,
        "python": lock["python"],
        "pbsTag": lock["pbsTag"],
        "pbsSha256": lock["targets"][target]["sha256"],
        "wheel": {key: facts[key] for key in ("file", "version", "sha256")},
        "sourceCommit": facts["sourceCommit"],
        "files": files,
    }


def _leaks(root: Path, needles: list[bytes]) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                found.append(path.relative_to(root).as_posix())
    return found


def stage(*, target: str, wheel: Path, out: Path = DEFAULT_OUT, lock_path: Path = LOCK_PATH, archive: Path | None = None, cache_dir: Path | None = None, requirements: Path | None = None, offline: bool = False) -> dict[str, Any]:
    """Stage the runtime for ``target`` into ``out`` and return its manifest."""

    lock = load_lock(lock_path)
    _require(target in lock["targets"], f"target {target} is not in the lock")
    entry = lock["targets"][target]
    wheel = Path(wheel).resolve()
    out = Path(out).absolute()
    if out.exists():
        _require(out.is_dir() and (out / MANIFEST_NAME).is_file(), f"{out} exists and holds no {MANIFEST_NAME}; refusing to replace it")
    facts = wheel_facts(wheel, entry)
    if archive is None:
        archive = fetch_archive(entry, cache_dir or Path(tempfile.gettempdir()) / "hyprial-shell-runtime-cache")
    verify_archive(Path(archive), entry)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.staging-", dir=out.parent))
    work = Path(tempfile.mkdtemp(prefix="hyprial-runtime-work-"))
    try:
        tree = staging / "tree"
        tree.mkdir()
        extract(Path(archive), tree)
        interpreter = tree / INTERPRETER
        _require(interpreter.is_file() and not interpreter.is_symlink(), f"archive has no regular {INTERPRETER}")
        site = tree / SITE_PACKAGES
        site.mkdir(parents=True, exist_ok=True)
        install(site, requirements or export_requirements(work), wheel, lock, entry, offline=offline)
        compile_bytecode(tree)
        normalise(tree)
        leaks = _leaks(tree, [str(staging).encode(), str(wheel.parent).encode(), str(work).encode()])
        _require(not leaks, f"build paths baked into {leaks[:5]}")
        manifest = build_manifest(tree, lock, target, facts)
        (tree / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (tree / MANIFEST_NAME).chmod(0o644)
        if out.exists():
            shutil.rmtree(out)
        tree.replace(out)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage the native shell's bundled Python runtime.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--archive", type=Path, help="a local copy of the locked PBS asset (verified like a download)")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--requirements", type=Path, help="hashed requirements instead of `uv export` of the repository lock")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = stage(target=args.target, wheel=args.wheel, out=args.out, lock_path=args.lock, archive=args.archive, cache_dir=args.cache_dir, requirements=args.requirements, offline=args.offline)
    except StageError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "out": str(args.out), "files": len(manifest["files"]), "wheel": manifest["wheel"], "sourceCommit": manifest["sourceCommit"], "pbsTag": manifest["pbsTag"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
