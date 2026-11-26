# Decisions (wheel native stage, build/distribution ADR section 3; HQ R5):
# - Module path is hyprial._core.actors (the frozen ABI spec supersedes the
#   ADR's hyprial._core): hyprial._core stays a PEP 420 namespace package and
#   the wheel carries hyprial/_core/actors.abi3.so (Windows: actors.pyd).
# - The stage runs exactly `cargo build --release --locked --manifest-path
#   crates/Cargo.toml -p hyprial-py --features extension-module` with
#   PYO3_PYTHON set to the absolute path of the build interpreter, so PyO3
#   never searches PATH for a system python.  RUSTUP_TOOLCHAIN defaults to
#   the ADR's pinned 1.93.1 (a caller's own RUSTUP_TOOLCHAIN wins); a cargo
#   that is not a rustup proxy ignores it.  HYPRIAL_CARGO names an explicit
#   cargo, otherwise cargo on PATH; neither is a loud NativeEngineError.
# - The artifact path comes from cargo's own JSON artifact messages, not a
#   guessed crates/target path, so CARGO_TARGET_DIR overrides keep working.
# - macOS floor: a macosx_X_Y wheel tag pins MACOSX_DEPLOYMENT_TARGET=X.Y
#   for cargo, overriding an inherited value, so a builder exporting a newer
#   target cannot ship a module that needs a newer macOS than its tag says.
#   The Mach-O LC_BUILD_VERSION minos is not parsed; the pin is the guard.
# - Reproducibility (ADR 同包wheel及校验值): rustc embeds absolute source
#   paths (panic locations of every registry crate), so two builders of one
#   commit produced different bytes.  The stage appends
#   --remap-path-prefix=<root>=/src and <CARGO_HOME>=/cargo through
#   CARGO_ENCODED_RUSTFLAGS, keeping a caller's CARGO_ENCODED_RUSTFLAGS or
#   (split on whitespace) RUSTFLAGS in front.  Setting that variable makes
#   cargo ignore build.rustflags / target.<triple>.rustflags from config
#   files (待 HQ 追认: config-file rustflags are superseded on the wheel
#   path).  The hook prints the staged module's SHA-256.
# - The extension is built for the build host only.  A wheel whose tailcat
#   target differs from the host fails instead of carrying a host-arch
#   module under a foreign platform tag; cross builds belong to the native
#   CI matrix (ADR section 4), which does not exist yet.
# - Every staged artifact is imported by the build interpreter in a
#   subprocess and must report ENGINE_ABI_VERSION == 1; on ELF it must not
#   carry a DT_NEEDED libpython (the extension-module feature's job).
# - abi3 is never faked (ADR: 不伪写abi3标签; free-threaded 须明确报错).
#   Before cargo runs, the build interpreter must report CPython, a
#   GIL-enabled build (Py_GIL_DISABLED unset or 0) and version >= 3.12:
#   PyO3 silently drops the limited API on a free-threaded or non-CPython
#   interpreter, and the import probe on that same interpreter would pass
#   while the cp312-abi3 wheel fails on a GIL CPython 3.12.  PYTHON_TAG must
#   also equal the abi3-py3XX pyo3 feature of crates/hyprial-py/Cargo.toml,
#   so the two cannot drift apart.  The built module's undefined Py* symbols
#   are not checked against the limited API list (the interpreter guard and
#   PyO3's abi3 feature are the enforcement).
# - glibc floor: the shipped tag table promises manylinux_2_17.  A module
#   built on a newer glibc host references newer symbol versions (2.34 on
#   Ubuntu 24.04), so it would lie under that tag, and the ADR forbids both
#   relabelling and silently raising the floor (不能在較新glibc主機上編譯後
#   直接貼manylinux_2_17標籤…也不能偷偷提高最低平台版本).  Such a build fails
#   closed.  Only the explicit developer opt-in
#   HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1 (exact value 1) tags the wheel with the
#   floor the module really needs, prints that it must not be published,
#   and drops the manylinux2014 alias with the false floor.  Release wheels
#   come from a manylinux2014 build so the declared floor holds.
# - Opt-out: HYPRIAL_BUILD_SKIP_NATIVE=1 builds without the native stage and
#   says so on stderr; any other value is not an opt-out, so a typo fails
#   closed.  HYPRIAL_TAILCAT_ALLOW_MISSING never skips the native stage.  The
#   opt-out exists until build images carry Rust (待 HQ 追认); a wheel built
#   with it must not be published.
# - Editable builds stay toolchain-free unless `HYPRIAL_BUILD_NATIVE=1` is
#   explicit.  The opt-in hook calls build_editable in native_editable.py
#   in-process (KP-135: a hook that executed the .sh failed with WinError 193
#   on Windows), with the current build interpreter; it stages
#   src/hyprial/_core/actors<suffix> and writes actors.stamp.json with the
#   digest of the cargo inputs under crates/: Cargo.toml, Cargo.lock and
#   *.rs at any depth, never a top-level crates/target* directory.  Python
#   helpers, their __pycache__ and notes under crates/ are not inputs, and a
#   nested target_* source directory is.  A test pins that no crate source
#   uses include!/include_str!/include_bytes! and that crates/ has no
#   .cargo/ or rust-toolchain file, so the digest cannot miss an input.
#   pyproject.toml's [tool.uv] cache-keys cover the same inputs plus the
#   staged module and stamp, so `uv sync` re-runs this check whenever one
#   changes (uv otherwise re-runs the editable hook only for pyproject.toml).
#   When the staged module exists the editable hook verifies digest and ABI version
#   and fails on a stale or foreign module; an absent module is reported,
#   not an error, because nothing imports hyprial._core.actors before the
#   backend switch and the import contract itself fails closed (待 HQ 追认).
# - uv records an editable build's cache-key state before the hook runs and
#   reduces the keyed files to their newest mtime.  A key on the staged module
#   alone therefore matches again once the module is deleted after a first
#   build, and uv reports "Checked" while the module stays missing.  The opt-in
#   hook touches scripts/build/native-engine.epoch (tracked, so it survives
#   git clean -fdX and rm -rf of src/hyprial/_core) before it builds; the staged
#   files are then newer than the epoch, and a deleted module leaves the epoch as
#   the newest keyed file, a state no earlier sync recorded.
# - The developer script never guesses cargo's target directory: it asks
#   cargo for --message-format=json and takes the artefact path from
#   library_from_messages (the `library` command of main), so
#   CARGO_TARGET_DIR, CARGO_BUILD_TARGET_DIR and build.target-dir in
#   .cargo/config.toml all resolve inside cargo.
# - The editable module's file name is a fact of the interpreter running this
#   file, not a platform branch: `actors` plus the stable-ABI suffix of
#   importlib.machinery.EXTENSION_SUFFIXES (`.abi3.so` on Linux and macOS),
#   else the plain `.pyd` (Windows, where CPython lists no abi3 suffix).  A
#   list with neither (a Windows debug `_d.pyd` or a free-threaded build) has
#   no editable name and build_editable fails; whether an abi3 module loads
#   there is unproven.  verify_editable resolves the name at call time and
#   treats "no name" as "nothing staged"; EDITABLE_MODULE is only a message
#   hint and never fails at import, so wheel and sdist builds are unaffected.
# - cargo's JSON messages are UTF-8 whatever the locale (cp936/cp1252 on
#   Windows); both cargo runs decode them as UTF-8 with errors="replace".
"""Native actor engine (``hyprial._core.actors``) stage of the wheel build.

Import-pure on purpose (no ``hyprial`` imports): the Hatch build hook loads
this file with ``runpy`` straight from the source tree, and the developer
script runs it as ``stamp`` to record what an editable module was built from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path

SKIP_ENV = "HYPRIAL_BUILD_SKIP_NATIVE"
"""``1`` builds without the native engine; such a wheel must not be published."""

ALLOW_GLIBC_RAISE_ENV = "HYPRIAL_BUILD_ALLOW_GLIBC_RAISE"
"""``1`` tags a newer-glibc module honestly instead of failing; never published."""

PREBUILT_ENV = "HYPRIAL_NATIVE_PREBUILT_DIR"
"""Directory of per-target modules built and smoke-tested on their own hosts."""

PREBUILT_MANIFEST = "native-prebuilt.json"
PREBUILT_SCHEMA = "hyprial.native-prebuilt/v1"

CARGO_ENV = "HYPRIAL_CARGO"
"""Explicit ``cargo`` executable; unset means ``cargo`` on PATH."""

RUST_TOOLCHAIN = "1.93.1"
CRATES_DIR = "crates"
MANIFEST = "crates/Cargo.toml"
CRATE_MANIFEST = "crates/hyprial-py/Cargo.toml"
LOCKFILE = "crates/Cargo.lock"
CARGO_PACKAGE = "hyprial-py"
CARGO_FEATURES = "extension-module"
LIBRARY_NAME = "actors"
MODULE_NAME = "hyprial._core.actors"
WHEEL_MODULE_DIR = "hyprial/_core"
EDITABLE_MODULE_DIR = "src/hyprial/_core"
STAMP_NAME = "actors.stamp.json"
ENGINE_ABI_VERSION = 1
PYTHON_TAG = "cp312"
ABI3_FLOOR = (3, 12)
ABI_TAG = "abi3"
MANYLINUX_FLOOR = (2, 17)
CARGO_TIMEOUT_SECONDS = 3600.0
PROBE_TIMEOUT_SECONDS = 120.0

_LIBRARY_SUFFIXES = (".so", ".dylib", ".dll")
_PROBE = (
    "import importlib.machinery, importlib.util, sys\n"
    "path = sys.argv[1]\n"
    "loader = importlib.machinery.ExtensionFileLoader(sys.argv[2], path)\n"
    "spec = importlib.util.spec_from_file_location(sys.argv[2], path, loader=loader)\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "loader.exec_module(module)\n"
    "print(repr(getattr(module, 'ENGINE_ABI_VERSION', None)))\n"
)
_INTERPRETER_PROBE = (
    "import json, sys, sysconfig\n"
    "print(json.dumps({'implementation': sys.implementation.name, "
    "'version': list(sys.version_info[:2]), "
    "'gil_disabled': sysconfig.get_config_var('Py_GIL_DISABLED')}))\n"
)
_ABI3_FEATURE = re.compile(r"abi3-py3(\d+)")
_GLIBC = re.compile(r"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?")
_MANYLINUX = re.compile(r"manylinux_(\d+)_(\d+)_(\w+)")
_MACOSX = re.compile(r"macosx_(\d+)_(\d+)_")
_RUSTFLAGS_SEPARATOR = "\x1f"
_CARGO_INPUT_NAMES = frozenset({"Cargo.toml", "Cargo.lock"})
_SHT_DYNAMIC = 6
_SHT_GNU_VERNEED = 0x6FFFFFFE
_DT_NEEDED = 1


class NativeEngineError(RuntimeError):
    """The wheel cannot carry a trustworthy native actor engine."""


def skip_requested(environ: Mapping[str, str] | None = None) -> bool:
    """True only for the explicit opt-out value ``HYPRIAL_BUILD_SKIP_NATIVE=1``."""

    env = os.environ if environ is None else environ
    return env.get(SKIP_ENV, "") == "1"


def wheel_module_name(target: str) -> str:
    """The extension filename inside the wheel for a ``goos/goarch`` target."""

    return f"{LIBRARY_NAME}.pyd" if target.startswith("windows/") else f"{LIBRARY_NAME}.abi3.so"


def editable_module_suffix() -> str | None:
    """This interpreter's stable-ABI extension suffix, else ``.pyd``; ``None`` for neither."""

    stable = [suffix for suffix in EXTENSION_SUFFIXES if suffix.startswith(f".{ABI_TAG}.")]
    if stable:
        return stable[0]
    return ".pyd" if ".pyd" in EXTENSION_SUFFIXES else None


def editable_module_name() -> str:
    """``actors`` plus :func:`editable_module_suffix`; NativeEngineError when there is none."""

    suffix = editable_module_suffix()
    if suffix is None:
        raise NativeEngineError(
            f"this interpreter lists no .{ABI_TAG}.* or plain .pyd extension suffix "
            f"({list(EXTENSION_SUFFIXES)}): a debug or free-threaded interpreter cannot take "
            f"the {ABI_TAG} editable module; use a GIL-enabled release CPython >= "
            f"{ABI3_FLOOR[0]}.{ABI3_FLOOR[1]}"
        )
    return LIBRARY_NAME + suffix


EDITABLE_MODULE = f"{EDITABLE_MODULE_DIR}/{LIBRARY_NAME}{editable_module_suffix() or '.<suffix>'}"
"""The staged editable module, relative to the source root (a hint for messages)."""


def wheel_module_path(target: str) -> str:
    """The wheel-relative destination of the extension."""

    return f"{WHEEL_MODULE_DIR}/{wheel_module_name(target)}"


def wheel_tag(platform_tag: str) -> str:
    """The full wheel tag of a wheel that carries the abi3 extension."""

    return f"{PYTHON_TAG}-{ABI_TAG}-{platform_tag}"


def find_cargo(environ: Mapping[str, str] | None = None) -> str | None:
    """``HYPRIAL_CARGO`` if set, else ``cargo`` on PATH; ``None`` if neither."""

    env = os.environ if environ is None else environ
    explicit = env.get(CARGO_ENV, "").strip()
    if explicit:
        return explicit
    return shutil.which("cargo", path=env.get("PATH"))


def missing_toolchain_message(detail: str) -> str:
    """The one error text for a build host without a usable Rust toolchain."""

    return (
        f"the native actor engine ({MODULE_NAME}) needs a Rust toolchain: {detail}. "
        f"Install Rust {RUST_TOOLCHAIN} (rustup toolchain install {RUST_TOOLCHAIN}) "
        f"or set {CARGO_ENV}; {SKIP_ENV}=1 builds a wheel without the native "
        "engine, which must not be published"
    )


def require_sources(root: Path) -> None:
    """Fail unless the crates workspace manifest and lockfile are present."""

    for relative in (MANIFEST, LOCKFILE):
        if not (Path(root) / relative).is_file():
            raise NativeEngineError(
                f"native engine sources are missing: {Path(root) / relative} "
                f"(the source tree or sdist must carry {CRATES_DIR}/)"
            )


def declared_python_tag(root: Path) -> str:
    """The ``cp3XX`` tag the hyprial-py crate's ``pyo3`` ``abi3-py3XX`` feature builds."""

    manifest = Path(root) / CRATE_MANIFEST
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NativeEngineError(f"cannot read {manifest}: {error}") from error
    pyo3 = data.get("dependencies", {}).get("pyo3")
    features = pyo3.get("features", []) if isinstance(pyo3, dict) else []
    found = [match.group(1) for match in map(_ABI3_FEATURE.fullmatch, features) if match]
    if len(found) != 1:
        raise NativeEngineError(
            f"{manifest} declares no abi3-py3XX pyo3 feature (found {features!r}); the "
            f"wheel claims {PYTHON_TAG}-{ABI_TAG} and must not carry a version-specific module"
        )
    return f"cp3{found[0]}"


def require_abi3_feature(root: Path) -> None:
    """Fail unless the crate's abi3 feature is the one :data:`PYTHON_TAG` claims."""

    declared = declared_python_tag(root)
    if declared != PYTHON_TAG:
        raise NativeEngineError(
            f"{Path(root) / CRATE_MANIFEST} builds abi3-py3{declared[3:]} but the wheel "
            f"tag claims {PYTHON_TAG}-{ABI_TAG}; change both together"
        )


def describe_interpreter(python: str) -> dict:
    """Implementation, ``(major, minor)`` and ``Py_GIL_DISABLED`` of ``python``."""

    try:
        completed = subprocess.run(
            [python, "-I", "-c", _INTERPRETER_PROBE],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeEngineError(f"cannot inspect the build interpreter {python}: {error}") from error
    lines = (completed.stdout or "").strip().splitlines()
    try:
        report = json.loads(lines[-1]) if completed.returncode == 0 and lines else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        detail = (completed.stderr or completed.stdout or "").strip()[-600:]
        raise NativeEngineError(f"cannot inspect the build interpreter {python}: {detail}")
    return report


def require_abi3_interpreter(python: str) -> None:
    """Fail unless ``python`` is a GIL-enabled CPython >= 3.12 (an abi3 build host)."""

    report = describe_interpreter(python)
    problems = []
    if report.get("implementation") != "cpython":
        problems.append(f"it is {report.get('implementation')!r}, not CPython")
    if report.get("gil_disabled") not in (None, 0):
        problems.append("it is a free-threaded build (PyO3 builds no abi3 module for it)")
    version = report.get("version")
    if not isinstance(version, list) or tuple(version) < ABI3_FLOOR:
        problems.append(f"its version {version!r} is below {ABI3_FLOOR[0]}.{ABI3_FLOOR[1]}")
    if problems:
        raise NativeEngineError(
            f"the build interpreter {python} cannot build the {PYTHON_TAG}-{ABI_TAG} "
            f"extension: {'; '.join(problems)}. Build the wheel with a GIL-enabled "
            f"CPython >= {ABI3_FLOOR[0]}.{ABI3_FLOOR[1]} (uv build --wheel --python 3.12)"
        )


def cargo_command(cargo: str, root: Path) -> list[str]:
    """The exact cargo invocation of the wheel stage."""

    return [
        cargo,
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        str(Path(root) / MANIFEST),
        "-p",
        CARGO_PACKAGE,
        "--features",
        CARGO_FEATURES,
        "--message-format=json-render-diagnostics",
    ]


def macos_deployment_target(platform_tag: str | None) -> str | None:
    """``13.0`` for ``macosx_13_0_<arch>``; ``None`` for any other platform tag."""

    match = _MACOSX.match(platform_tag or "")
    return f"{match.group(1)}.{match.group(2)}" if match else None


def cargo_home(environ: Mapping[str, str]) -> Path:
    """``CARGO_HOME``, else ``~/.cargo`` of the environment's ``HOME``."""

    explicit = environ.get("CARGO_HOME", "").strip()
    if explicit:
        return Path(explicit)
    home = environ.get("HOME", "").strip()
    return Path(home or Path.home()) / ".cargo"


def remapped_rustflags(environ: Mapping[str, str], root: Path) -> str:
    """The caller's rustflags plus the builder-path remaps, cargo-encoded."""

    encoded = environ.get("CARGO_ENCODED_RUSTFLAGS", "")
    flags = encoded.split(_RUSTFLAGS_SEPARATOR) if encoded else environ.get("RUSTFLAGS", "").split()
    flags += [
        f"--remap-path-prefix={Path(root).resolve()}=/src",
        f"--remap-path-prefix={cargo_home(environ)}=/cargo",
    ]
    return _RUSTFLAGS_SEPARATOR.join(flags)


def cargo_environment(
    python: str,
    environ: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
    platform_tag: str | None = None,
) -> dict[str, str]:
    """The cargo environment: PYO3_PYTHON pinned, the toolchain defaulted.

    With ``root`` the builder's source and cargo-home paths are remapped; a
    ``macosx_X_Y`` ``platform_tag`` pins ``MACOSX_DEPLOYMENT_TARGET`` to X.Y.
    """

    env = dict(os.environ if environ is None else environ)
    env["PYO3_PYTHON"] = os.path.abspath(python)
    env.setdefault("RUSTUP_TOOLCHAIN", RUST_TOOLCHAIN)
    deployment = macos_deployment_target(platform_tag)
    if deployment is not None:
        env["MACOSX_DEPLOYMENT_TARGET"] = deployment
    if root is not None:
        env["CARGO_ENCODED_RUSTFLAGS"] = remapped_rustflags(env, root)
    return env


def library_from_messages(stdout: str) -> Path:
    """The cdylib file cargo reported for the ``actors`` library target."""

    found: list[Path] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("reason") != "compiler-artifact":
            continue
        target = message.get("target") or {}
        if target.get("name") != LIBRARY_NAME or "cdylib" not in target.get("kind", []):
            continue
        for name in message.get("filenames", []):
            if Path(name).suffix in _LIBRARY_SUFFIXES:
                found.append(Path(name))
    if len(found) != 1:
        raise NativeEngineError(
            f"cargo reported {len(found)} cdylib files for {LIBRARY_NAME} "
            f"({[str(path) for path in found]}); expected exactly one"
        )
    return found[0]


def run_cargo(
    root: Path,
    *,
    python: str,
    environ: Mapping[str, str] | None = None,
    platform_tag: str | None = None,
) -> Path:
    """Build the extension with cargo and return the cdylib cargo produced."""

    env = cargo_environment(python, environ, root=root, platform_tag=platform_tag)
    cargo = find_cargo(env)
    if cargo is None:
        raise NativeEngineError(
            missing_toolchain_message(f"{CARGO_ENV} is unset and no cargo is on PATH")
        )
    command = cargo_command(cargo, root)
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CARGO_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as error:
        raise NativeEngineError(
            missing_toolchain_message(f"cannot run {cargo}: {error}")
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeEngineError(f"cannot run {' '.join(command)}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise NativeEngineError(
            f"{' '.join(command)} failed (exit {completed.returncode}): {detail}"
        )
    library = library_from_messages(completed.stdout or "")
    if not library.is_file():
        raise NativeEngineError(f"cargo reported {library}, which is not a file")
    return library


def probe_abi_version(path: Path, python: str) -> object:
    """Import ``path`` as ``hyprial._core.actors`` in ``python``; its ABI version."""

    try:
        completed = subprocess.run(
            [python, "-I", "-c", _PROBE, str(path), MODULE_NAME],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeEngineError(f"cannot import-check {path}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise NativeEngineError(
            f"{path} does not import as {MODULE_NAME} with {python}: {detail}"
        )
    lines = (completed.stdout or "").strip().splitlines()
    text = lines[-1] if lines else ""
    try:
        return int(text)
    except ValueError:
        return text


def require_abi(path: Path, python: str) -> None:
    """Fail unless ``path`` imports and reports ``ENGINE_ABI_VERSION == 1``."""

    version = probe_abi_version(path, python)
    if version != ENGINE_ABI_VERSION:
        raise NativeEngineError(
            f"{path} reports ENGINE_ABI_VERSION {version!r}; this source tree "
            f"requires {ENGINE_ABI_VERSION}"
        )


def _elf_sections(data: bytes) -> list[tuple[int, int, int, int]] | None:
    """``(type, offset, size, link)`` per section of a little-endian ELF64."""

    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    sections = []
    for index in range(shnum):
        base = shoff + index * shentsize
        if base + 64 > len(data):
            return None
        sh_type = struct.unpack_from("<I", data, base + 4)[0]
        offset, size = struct.unpack_from("<QQ", data, base + 0x18)
        link = struct.unpack_from("<I", data, base + 0x28)[0]
        sections.append((sh_type, offset, size, link))
    return sections


def _c_string(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    return data[offset : end if end >= 0 else len(data)].decode("ascii", "replace")


def elf_dependencies(data: bytes) -> tuple[list[str], tuple[int, ...] | None]:
    """DT_NEEDED names and the highest GLIBC symbol version an ELF64 requires.

    Non-ELF64 input returns ``([], None)``: the audit applies to Linux only.
    """

    sections = _elf_sections(data)
    if sections is None:
        return [], None
    needed: list[str] = []
    glibc: tuple[int, ...] | None = None
    for sh_type, offset, size, link in sections:
        if link >= len(sections):
            continue
        strtab = sections[link][1]
        if sh_type == _SHT_DYNAMIC:
            for entry in range(offset, offset + size - 15, 16):
                tag, value = struct.unpack_from("<qQ", data, entry)
                if tag == 0:
                    break
                if tag == _DT_NEEDED:
                    needed.append(_c_string(data, strtab + value))
        elif sh_type == _SHT_GNU_VERNEED:
            entry = offset
            while entry + 16 <= len(data):
                _version, count, _file, aux, following = struct.unpack_from(
                    "<HHIII", data, entry
                )
                pointer = entry + aux
                for _ in range(count):
                    _hash, _flags, _other, name, after = struct.unpack_from(
                        "<IHHII", data, pointer
                    )
                    match = _GLIBC.fullmatch(_c_string(data, strtab + name))
                    if match:
                        version = tuple(int(part) for part in match.groups() if part)
                        glibc = version if glibc is None else max(glibc, version)
                    if not after:
                        break
                    pointer += after
                if not following:
                    break
                entry += following
    return needed, glibc


def audit_library(path: Path) -> tuple[int, ...] | None:
    """Fail on a libpython dependency; return the module's glibc floor (ELF)."""

    needed, glibc = elf_dependencies(Path(path).read_bytes())
    linked = [name for name in needed if name.startswith("libpython")]
    if linked:
        raise NativeEngineError(
            f"{path} links {linked}; an abi3 extension must resolve the Python "
            f"API from the loading interpreter (build with --features {CARGO_FEATURES})"
        )
    return glibc


def platform_tag_for_floor(
    platform_tag: str,
    glibc: tuple[int, ...] | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """The platform tag a module with this glibc floor can honestly carry.

    Returns ``(tag, warning)``; ``warning`` is ``None`` when the declared
    manylinux floor already covers the module.  A higher floor raises
    :class:`NativeEngineError` unless ``HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1``.
    """

    match = _MANYLINUX.match(platform_tag)
    if glibc is None or match is None:
        return platform_tag, None
    declared = (int(match.group(1)), int(match.group(2)))
    floor = tuple(glibc[:2])
    if floor <= declared:
        return platform_tag, None
    raised = f"manylinux_{floor[0]}_{floor[1]}_{match.group(3)}"
    needs = (
        f"the native engine needs GLIBC_{floor[0]}.{floor[1]} but the declared "
        f"floor is manylinux_{declared[0]}_{declared[1]}"
    )
    env = os.environ if environ is None else environ
    if env.get(ALLOW_GLIBC_RAISE_ENV, "") != "1":
        raise NativeEngineError(
            f"{needs}; the wheel would lie under its tag. Build it in a manylinux2014 "
            f"image (scripts/build/manylinux_native_evidence.sh), or set "
            f"{ALLOW_GLIBC_RAISE_ENV}=1 for a local {raised} wheel that must not be published"
        )
    return raised, (
        f"warning: {needs}; {ALLOW_GLIBC_RAISE_ENV}=1 -- tagging this wheel {raised}. "
        "It must not be published: release wheels are built in a manylinux2014 image"
    )


def prebuilt_directory(environ: Mapping[str, str] | None = None) -> Path | None:
    """The prebuilt-module directory, or ``None`` when the build must compile."""

    env = os.environ if environ is None else environ
    raw = env.get(PREBUILT_ENV, "").strip()
    return Path(raw) if raw else None


def _prebuilt_manifest(directory: Path) -> dict:
    path = Path(directory) / PREBUILT_MANIFEST
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise NativeEngineError(
            f"{PREBUILT_ENV} has no readable manifest {path}: {error}"
        ) from error
    except ValueError as error:
        raise NativeEngineError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise NativeEngineError(f"{path} must contain a JSON object")
    if document.get("schema") != PREBUILT_SCHEMA:
        raise NativeEngineError(
            f"{path} declares schema {document.get('schema')!r}; expected {PREBUILT_SCHEMA}"
        )
    if not isinstance(document.get("targets"), dict):
        raise NativeEngineError(f"{path} has no targets object")
    return document


def module_architecture(path: Path, target: str) -> str:
    """The architecture of a staged module, refused unless it matches ``target``.

    Assembling on one host must not import or execute another platform's
    module, so this reads only the format header: ELF ``e_machine``, thin
    Mach-O ``cputype`` or PE ``Machine``.
    """

    data = Path(path).read_bytes()
    expected = target.partition("/")[2]
    detected: str | None = None
    if data[:4] == b"\x7fELF":
        if len(data) < 20 or data[4] != 2 or data[5] != 1:
            raise NativeEngineError(f"{path} is not a little-endian ELF64 module")
        machine = struct.unpack_from("<H", data, 18)[0]
        detected = {62: "amd64", 183: "arm64"}.get(machine)
    elif data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        if len(data) < 8:
            raise NativeEngineError(f"{path} is a truncated Mach-O module")
        cputype = struct.unpack_from("<I", data, 4)[0]
        detected = {0x01000007: "amd64", 0x0100000C: "arm64"}.get(cputype)
    elif data[:4] == b"\xca\xfe\xba\xbe":
        raise NativeEngineError(
            f"{path} is a universal Mach-O; the wheel needs one thin module per target"
        )
    elif data[:2] == b"MZ":
        if len(data) < 0x40:
            raise NativeEngineError(f"{path} is a truncated PE module")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if pe + 6 > len(data) or data[pe : pe + 4] != b"PE\0\0":
            raise NativeEngineError(f"{path} is not a PE module")
        machine = struct.unpack_from("<H", data, pe + 4)[0]
        detected = {0x8664: "amd64", 0xAA64: "arm64"}.get(machine)
    else:
        raise NativeEngineError(f"{path} is not an ELF, Mach-O or PE module")
    if detected is None:
        raise NativeEngineError(f"{path} has an unsupported architecture for {target}")
    if detected != expected:
        raise NativeEngineError(f"{path} is {detected}, but target {target} needs {expected}")
    return detected


def verify_prebuilt(
    root: Path,
    target: str,
    *,
    platform_tag: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Verify the staged module another host built for ``target``.

    The build hook may run on any host (the release job assembles all five
    wheels on one machine), so this trusts neither the host nor a loadable
    import.  It checks that the manifest was produced from this tree's
    crates/ digest, that the module's bytes and size are the recorded ones,
    that its format and architecture match the target, and, for Linux, that
    it links no libpython.  The per-host build job owns the import and
    ``ENGINE_ABI_VERSION`` smoke test; its receipt is what the workflow
    compares against this manifest before the hook runs.
    """

    directory = prebuilt_directory(environ)
    if directory is None:
        raise NativeEngineError(f"{PREBUILT_ENV} is unset")
    if not directory.is_dir():
        raise NativeEngineError(f"{PREBUILT_ENV}={directory} is not a directory")
    document = _prebuilt_manifest(directory)
    digest = source_digest(root)
    if document.get("source_sha256") != digest:
        raise NativeEngineError(
            f"{directory / PREBUILT_MANIFEST} was built from source_sha256 "
            f"{document.get('source_sha256')!r}, but crates/ now hashes to {digest}"
        )
    entry = document["targets"].get(target)
    if not isinstance(entry, dict):
        raise NativeEngineError(
            f"{directory / PREBUILT_MANIFEST} has no entry for {target}"
        )
    module_name = wheel_module_name(target)
    if entry.get("module") != module_name:
        raise NativeEngineError(
            f"{directory / PREBUILT_MANIFEST} names {entry.get('module')!r} for "
            f"{target}; expected {module_name!r}"
        )
    module = Path(directory) / target.replace("/", "-") / module_name
    if module.is_symlink() or not module.is_file():
        raise NativeEngineError(f"{module} is not a regular prebuilt module file")
    data = module.read_bytes()
    declared_sha = entry.get("sha256")
    actual_sha = hashlib.sha256(data).hexdigest()
    if declared_sha != actual_sha:
        raise NativeEngineError(
            f"{module} sha256 is {actual_sha}, but the manifest records {declared_sha!r}"
        )
    if entry.get("size") != len(data):
        raise NativeEngineError(
            f"{module} is {len(data)} bytes, but the manifest records {entry.get('size')!r}"
        )
    if entry.get("engine_abi_version") != ENGINE_ABI_VERSION:
        raise NativeEngineError(
            f"{module} receipt claims ENGINE_ABI_VERSION "
            f"{entry.get('engine_abi_version')!r}; this tree requires {ENGINE_ABI_VERSION}"
        )
    if platform_tag is not None and entry.get("platform_tag") != platform_tag:
        raise NativeEngineError(
            f"{module} was built for platform tag {entry.get('platform_tag')!r}; "
            f"this target needs {platform_tag}"
        )
    module_architecture(module, target)
    glibc = audit_library(module) if target.startswith("linux/") else None
    return {
        "module": module,
        "sha256": actual_sha,
        "glibc": glibc,
        "platform_tag": entry.get("platform_tag"),
    }


def stage_prebuilt(
    root: Path,
    target: str,
    *,
    output: Path,
    platform_tag: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, tuple[int, ...] | None]:
    """Copy a verified prebuilt module into the wheel's staging directory."""

    record = verify_prebuilt(root, target, platform_tag=platform_tag, environ=environ)
    destination = Path(output) / wheel_module_name(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(record["module"], destination)
    return destination, record["glibc"]


def preflight(
    root: Path,
    target: str,
    *,
    host: str,
    python: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Every cheap precondition of :func:`build_extension`, before any compile.

    Host-only target, crates/ manifest and lockfile, the abi3 feature, an
    abi3-capable build interpreter and a cargo to run.
    """

    if target != host:
        raise NativeEngineError(
            f"the native engine is built for the build host ({host}) only; the "
            f"wheel target is {target}. Build that wheel on a {target} host"
        )
    require_sources(root)
    require_abi3_feature(root)
    require_abi3_interpreter(python or sys.executable)
    if find_cargo(environ) is None:
        raise NativeEngineError(
            missing_toolchain_message(f"{CARGO_ENV} is unset and no cargo is on PATH")
        )


def build_extension(
    root: Path,
    target: str,
    *,
    host: str,
    output: Path,
    python: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform_tag: str | None = None,
) -> tuple[Path, tuple[int, ...] | None]:
    """Build, verify and stage the extension for ``target`` under ``output``.

    ``platform_tag`` is the wheel's platform tag (it pins the macOS
    deployment target).  Returns the staged file (named as inside the wheel)
    and its glibc floor.
    """

    interpreter = python or sys.executable
    preflight(root, target, host=host, python=interpreter, environ=environ)
    library = run_cargo(root, python=interpreter, environ=environ, platform_tag=platform_tag)
    destination = Path(output) / wheel_module_name(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, destination)
    glibc = audit_library(destination)
    require_abi(destination, interpreter)
    return destination, glibc


def is_cargo_input(relative: str) -> bool:
    """True for a crates/ path cargo reads: ``Cargo.toml``, ``Cargo.lock``, ``*.rs``.

    Paths inside a top-level ``crates/target*`` directory never are.
    """

    parts = Path(relative).parts
    if parts[:1] == (CRATES_DIR,):
        parts = parts[1:]
    if not parts or (len(parts) > 1 and parts[0].startswith("target")):
        return False
    return parts[-1] in _CARGO_INPUT_NAMES or parts[-1].endswith(".rs")


def source_digest(root: Path) -> str:
    """SHA-256 over the cargo inputs under crates/ (see :func:`is_cargo_input`)."""

    crates = Path(root) / CRATES_DIR
    if not crates.is_dir():
        raise NativeEngineError(f"native engine sources are missing: {crates}")
    files: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(crates):
        top = Path(directory) == crates
        dirnames[:] = sorted(name for name in dirnames if not (top and name.startswith("target")))
        for name in filenames:
            path = Path(directory) / name
            relative = path.relative_to(crates).as_posix()
            if is_cargo_input(relative):
                files.append((relative, path))
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def stamp_path(module: Path) -> Path:
    """The stamp file written beside a staged editable module."""

    return Path(module).parent / STAMP_NAME


def write_stamp(root: Path, module: Path, *, features: str, profile: str) -> Path:
    """Record which crates/ inputs an editable module was built from."""

    module = Path(module)
    record = {
        "module": module.name,
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "source_sha256": source_digest(root),
        "features": features,
        "profile": profile,
        "engine_abi_version": ENGINE_ABI_VERSION,
    }
    path = stamp_path(module)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_editable(root: Path, *, python: str | None = None) -> str | None:
    """Check the staged editable module; ``None`` when none is staged.

    Raises :class:`NativeEngineError` for a module without a stamp, a module
    whose bytes or crates/ inputs no longer match the stamp, or a module that
    does not report ENGINE_ABI_VERSION 1.  Returns a one-line status.
    """

    suffix = editable_module_suffix()
    if suffix is None:
        return None
    module = Path(root) / EDITABLE_MODULE_DIR / f"{LIBRARY_NAME}{suffix}"
    if not module.is_file():
        return None
    rebuild = (
        "rebuild it with `HYPRIAL_BUILD_NATIVE=1 uv sync` or "
        "scripts/build/build_native_engine.sh"
    )
    stamp = stamp_path(module)
    try:
        record = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NativeEngineError(
            f"{module} has no readable build stamp {stamp} ({error}); {rebuild}"
        ) from error
    if record.get("module_sha256") != hashlib.sha256(module.read_bytes()).hexdigest():
        raise NativeEngineError(f"{module} is not the module its stamp describes; {rebuild}")
    if record.get("source_sha256") != source_digest(root):
        raise NativeEngineError(f"{module} is stale: crates/ changed since it was built; {rebuild}")
    require_abi(module, python or sys.executable)
    return f"{MODULE_NAME}: staged module matches crates/ ({record.get('features')})"


def main(argv: Sequence[str] | None = None) -> int:
    """``stamp ...`` or ``library <messages-file>`` for the developer script."""

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "library":
        try:
            text = Path(args[1]).read_text(encoding="utf-8")
            print(library_from_messages(text))
        except (OSError, NativeEngineError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    if len(args) != 5 or args[0] != "stamp":
        print(
            "usage: native_engine.py stamp <root> <module> <features> <profile> | "
            "library <cargo-messages-file>",
            file=sys.stderr,
        )
        return 2
    path = write_stamp(Path(args[1]), Path(args[2]), features=args[3], profile=args[4])
    print(f"stamped {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
