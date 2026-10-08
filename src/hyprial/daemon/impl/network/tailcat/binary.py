"""Locating and verifying the ``hyprial-tailcat`` sidecar binary.

The Go sidecar ships inside the wheel (``hyprial/_bin``), may be installed
by the operator into ``$HYPRIAL_HOME/bin``, or be named explicitly through
``HYPRIAL_TAILCAT_BINARY``.  This module answers exactly two questions:
where the binary is, and whether it is the build this daemon was cut against
(the Tailcat engine commit is the compatibility pin).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

TAILCAT_COMMIT = "b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f"
"""The Tailcat engine commit this daemon's protocol v3 client is built for."""

SIDECAR_BINARY_NAME = "hyprial-tailcat"
"""Platform-neutral basename; use :func:`sidecar_binary_name` for a path."""

BINARY_ENV = "HYPRIAL_TAILCAT_BINARY"
"""Explicit sidecar path; outranks the wheel copy and ``$HYPRIAL_HOME/bin``."""

WHEEL_BIN_DIR = "_bin"
"""Directory inside the ``hyprial`` package where wheels ship the sidecar."""

_VERSION_TIMEOUT_SECONDS = 10.0


class TailcatSidecarError(RuntimeError):
    """A sidecar lookup/verify failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def sidecar_binary_name() -> str:
    """Return the sidecar filename for the current runtime platform."""

    suffix = ".exe" if sys.platform == "win32" else ""
    return f"{SIDECAR_BINARY_NAME}{suffix}"


def sidecar_binary_path(home: Path) -> Path:
    """Return the platform-specific conventional install location."""

    return Path(home) / "bin" / sidecar_binary_name()


def wheel_binary_path(package_root: Path | None = None) -> Path:
    """Return the platform-specific wheel-shipped binary path.

    Resolved relative to this file rather than through
    ``importlib.resources``: the supervisor launches the process from this
    path, and ``as_file`` would hand out a temporary copy that vanishes
    under a running child.  An explicit ``package_root`` exists for tests.
    """

    root = (
        Path(__file__).resolve().parents[4]
        if package_root is None
        else Path(package_root)
    )
    return root / WHEEL_BIN_DIR / sidecar_binary_name()


def _executable(candidate: Path) -> bool:
    try:
        info = candidate.stat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return sys.platform == "win32" or os.access(candidate, os.X_OK)


def locate_tailcat_sidecar(
    home: Path, environ: Mapping[str, str] | None = None
) -> Path:
    """The sidecar binary to run: ``HYPRIAL_TAILCAT_BINARY`` > wheel > ``home/bin``.

    A missing or non-executable candidate is ``SIDECAR_MISSING`` -- never a
    silent fallthrough to whatever happens to be on PATH, because an
    unpinned binary is an unreviewed protocol peer.  An explicitly set
    ``HYPRIAL_TAILCAT_BINARY`` is a pin: if it does not resolve, the error
    names it instead of quietly continuing with the next source.
    """

    env = os.environ if environ is None else environ
    raw = env.get(BINARY_ENV, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not _executable(candidate):
            raise TailcatSidecarError(
                "SIDECAR_MISSING",
                f"tailcat sidecar is not an executable file: {candidate} "
                f"({BINARY_ENV} is explicit and was not silently bypassed)",
            )
        return candidate
    for candidate in (wheel_binary_path(), sidecar_binary_path(home)):
        if _executable(candidate):
            return candidate
    raise TailcatSidecarError(
        "SIDECAR_MISSING",
        f"tailcat sidecar is not an executable file: no wheel copy at "
        f"{wheel_binary_path()} and no install at {sidecar_binary_path(home)} "
        f"(set {BINARY_ENV}, install a wheel with the bundled sidecar, or "
        f"put {sidecar_binary_name()} into {sidecar_binary_path(home).parent})",
    )


def verify_tailcat_sidecar(
    home: Path, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Run ``<sidecar> version`` and pin the engine commit; return the record.

    The sidecar prints one JSON line ``{"v":3,"sidecar":...,"tailcat":...}``;
    anything else -- unparseable output, a protocol version that is not 3, a
    tailcat commit other than :data:`TAILCAT_COMMIT` -- is ``SIDECAR_INVALID``.
    """

    binary = locate_tailcat_sidecar(home, environ)
    try:
        completed = subprocess.run(
            [str(binary), "version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TailcatSidecarError(
            "SIDECAR_INVALID", f"cannot run {binary} version: {error}"
        ) from error
    if completed.returncode != 0:
        raise TailcatSidecarError(
            "SIDECAR_INVALID",
            f"{binary} version exited with status {completed.returncode}",
        )
    try:
        record = json.loads(completed.stdout.decode("utf-8", "replace").strip())
    except ValueError as error:
        raise TailcatSidecarError(
            "SIDECAR_INVALID", f"{binary} version did not print JSON: {error}"
        ) from error
    if not isinstance(record, dict) or record.get("v") != 3:
        raise TailcatSidecarError(
            "SIDECAR_INVALID", f"{binary} version is not a protocol-v3 record"
        )
    if record.get("tailcat") != TAILCAT_COMMIT:
        raise TailcatSidecarError(
            "SIDECAR_INVALID",
            f"{binary} embeds tailcat {record.get('tailcat')!r}; "
            f"this daemon requires {TAILCAT_COMMIT}",
        )
    return record
