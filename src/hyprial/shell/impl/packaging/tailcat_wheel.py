"""Wheel packaging of the ``hyprial-tailcat`` sidecar binary.

Deliberately import-pure (no ``hyprial`` imports) so the hatch custom build
hook can load this module with ``runpy`` straight from the source tree -- the
same stance as ``shell/impl/gui/bundle.py``.  Everything here answers one of
three questions: which Go target a build is for, which wheel platform tag
that target maps to, and where an executable binary for that target comes
from (a prebuilt copy, or a fresh ``go build`` of ``sidecar/hyprial-tailcat``).

A missing sidecar is never silently skipped: a wheel without the binary is a
product that cannot start forwarding, so acquisition failures raise.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

TARGET_ENV = "HYPRIAL_TAILCAT_TARGET"
"""``goos/goarch`` for the binary a wheel bundles; unset means the host."""

PREBUILT_ENV = "HYPRIAL_TAILCAT_PREBUILT"
"""Path to an already-built binary to copy verbatim (CI, offline builds)."""

GO_ENV = "HYPRIAL_GO"
"""Explicit ``go`` executable; unset means whatever ``go`` is on PATH."""

ALLOW_MISSING_ENV = "HYPRIAL_TAILCAT_ALLOW_MISSING"
"""Set to ``1`` by the build hook's development escape hatch, not here."""

SIDECAR_SOURCE_DIR = "sidecar/hyprial-tailcat"
"""The Go module, relative to the repository/sdist root."""

SIDECAR_BUILD_PACKAGE = "./cmd/hyprial-tailcat"
BINARY_BASENAME = "hyprial-tailcat"

PLATFORM_TAGS = {
    "darwin/arm64": "macosx_13_0_arm64",
    "darwin/amd64": "macosx_13_0_x86_64",
    "linux/amd64": "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "linux/arm64": "manylinux_2_17_aarch64.manylinux2014_aarch64",
    "windows/amd64": "win_amd64",
}
"""goos/goarch to wheel platform tag. macOS follows the pinned Go1.27 floor.
Every supported build target is listed; anything else is a loud
``ValueError``, not a guessed tag."""

_GOARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


class TailcatWheelError(RuntimeError):
    """No usable sidecar binary could be produced for the wheel."""


def host_target() -> str:
    """The ``goos/goarch`` of the machine running the build."""

    system = platform.system().lower()
    goarch = _GOARCH_BY_MACHINE.get(platform.machine().lower())
    target = f"{system}/{goarch or ''}"
    if target not in PLATFORM_TAGS:
        raise ValueError(
            f"cannot derive a tailcat sidecar target for this host "
            f"(system={system!r}, machine={platform.machine()!r}); "
            f"set {TARGET_ENV}=goos/goarch"
        )
    return target


def resolve_target(environ: Mapping[str, str] | None = None) -> str:
    """``HYPRIAL_TAILCAT_TARGET`` if set and well-formed, else the host."""

    env = os.environ if environ is None else environ
    raw = env.get(TARGET_ENV, "").strip()
    if not raw:
        return host_target()
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"{TARGET_ENV} must be goos/goarch (for example linux/arm64), got {raw!r}"
        )
    if raw not in PLATFORM_TAGS:
        raise ValueError(f"unsupported tailcat sidecar target: {raw}")
    return raw


def platform_tag(target: str) -> str:
    """The wheel platform tag for a ``goos/goarch`` target."""

    try:
        return PLATFORM_TAGS[target]
    except KeyError:
        raise ValueError(f"unsupported tailcat sidecar target: {target}") from None


def binary_name(target: str) -> str:
    """The binary filename on the target OS (Windows appends ``.exe``)."""

    return BINARY_BASENAME + (".exe" if target.startswith("windows/") else "")


def find_go(environ: Mapping[str, str] | None = None) -> str | None:
    """``HYPRIAL_GO`` if set, else ``go`` on PATH; ``None`` if neither."""

    env = os.environ if environ is None else environ
    explicit = env.get(GO_ENV, "").strip()
    if explicit:
        return explicit
    return shutil.which("go", path=env.get("PATH"))


def build_tailcat(
    root: Path,
    target: str,
    *,
    output: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Put an executable ``hyprial-tailcat`` for ``target`` into ``output``.

    ``HYPRIAL_TAILCAT_PREBUILT`` short-circuits the toolchain: the file is
    copied and forced executable.  Otherwise ``go build`` runs inside
    ``root/sidecar/hyprial-tailcat`` with ``CGO_ENABLED=0 GOOS GOARCH``.  With
    neither input available the call raises :class:`TailcatWheelError` -- a
    wheel must never quietly ship without the sidecar.
    """

    if target not in PLATFORM_TAGS:
        raise ValueError(f"unsupported tailcat sidecar target: {target}")
    env = dict(os.environ)
    if environ is not None:
        env.update(environ)
    destination = Path(output) / binary_name(target)
    source_dir = Path(root) / SIDECAR_SOURCE_DIR

    prebuilt = env.get(PREBUILT_ENV, "").strip()
    if prebuilt:
        origin = Path(prebuilt).expanduser()
        if not origin.is_file():
            raise TailcatWheelError(
                f"{PREBUILT_ENV} points at {origin}, which is not a file"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
        destination.chmod(0o755)
        return destination

    go = find_go(env)
    if go is None:
        raise TailcatWheelError(
            f"no sidecar binary for {target}: {PREBUILT_ENV} is unset and no go "
            f"toolchain was found (set {GO_ENV} or put go on PATH)"
        )
    if not (source_dir / "go.mod").is_file():
        raise TailcatWheelError(
            f"sidecar source module missing: {source_dir} has no go.mod "
            f"(needed to build {target} with {go})"
        )
    command = [go, "build", "-trimpath", "-o", str(destination), SIDECAR_BUILD_PACKAGE]
    destination.parent.mkdir(parents=True, exist_ok=True)
    env.update({"CGO_ENABLED": "0", "GOOS": target.split("/")[0], "GOARCH": target.split("/")[1]})
    try:
        completed = subprocess.run(
            command,
            cwd=str(source_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise TailcatWheelError(f"cannot run the go toolchain {go}: {error}") from error
    if completed.returncode != 0 or not destination.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise TailcatWheelError(
            f"go build for {target} failed (exit {completed.returncode}): {detail}"
        )
    destination.chmod(0o755)
    return destination
