"""Managed first-run harness facts shared by the installer and the daemon.

The shell owns the consented install and its isolated activation launch.  The
daemon owns the later Squire launch.  Both must agree on where a managed
version lives and whether it has been activated, without either one guessing
from the user's global ``PATH``.

An install receipt alone is not activation: a version is usable by the daemon
only when ``activation.json`` says a real launch in the isolated Hyprial home
succeeded and names the same launcher.  The daemon re-checks the launcher on
every read, so a stale or edited marker never becomes a launch pin.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from hyprial.kernel.impl.manysys.interface import managed_launcher_names

MANAGED_HARNESS_PACKAGE = "@earendil-works/pi-coding-agent"
MANAGED_HARNESS_VERSION = "1.3.0"
MANAGED_HARNESS_INSTALL_RECEIPT = "install.json"
MANAGED_HARNESS_ACTIVATION_RECEIPT = "activation.json"


def managed_harness_root(hyprial_home: Path) -> Path:
    """The only directory a consented managed harness install may write."""

    return Path(hyprial_home) / "state" / "managed-harnesses"


def managed_harness_version_root(
    hyprial_home: Path,
    harness: str = "pi",
    version: str = MANAGED_HARNESS_VERSION,
) -> Path:
    """The versioned root of one managed harness installation."""

    return managed_harness_root(hyprial_home) / harness / version


def managed_harness_activation_path(
    hyprial_home: Path,
    harness: str = "pi",
    version: str = MANAGED_HARNESS_VERSION,
) -> Path:
    return (
        managed_harness_version_root(hyprial_home, harness, version)
        / MANAGED_HARNESS_ACTIVATION_RECEIPT
    )


def managed_harness_binary(version_root: Path, harness: str = "pi") -> Path | None:
    """Return the launcher inside one version root, if it is actually a file."""

    bin_dir = Path(version_root) / "node_modules" / ".bin"
    for name in managed_launcher_names(harness):
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return None


def find_managed_harness_binary(harness_root: Path, harness: str = "pi") -> Path | None:
    """Return the newest installed launcher below one harness root."""

    root = Path(harness_root)
    if root.is_symlink():
        return None
    try:
        children = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    versions = [
        child
        for child in children
        if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
    ]
    versions.sort(key=lambda child: _version_sort_key(child.name), reverse=True)
    for version_root in versions:
        candidate = managed_harness_binary(version_root, harness)
        if candidate is not None:
            return candidate
    return None


def activated_managed_harness_command(
    hyprial_home: Path,
    harness: str = "pi",
) -> tuple[str, ...] | None:
    """Return the daemon launch command for an activated managed harness.

    The command is returned only when the version carries a matching install
    receipt and an activation marker that a real isolated launch produced.  The
    named launcher must exist and resolve to this version's own launcher; a
    marker copied from another version, an unactivated install or a missing
    file is never a launch pin.
    """

    root = managed_harness_root(hyprial_home) / harness
    if root.is_symlink():
        return None
    try:
        children = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    versions = [
        child
        for child in children
        if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
    ]
    versions.sort(key=lambda child: _version_sort_key(child.name), reverse=True)
    for version_root in versions:
        receipt = _read_object(
            version_root / MANAGED_HARNESS_INSTALL_RECEIPT
        )
        if (
            receipt is None
            or receipt.get("managed") is not True
            or receipt.get("package") != MANAGED_HARNESS_PACKAGE
            or receipt.get("version") != version_root.name
        ):
            continue
        activation = _read_object(
            version_root / MANAGED_HARNESS_ACTIVATION_RECEIPT
        )
        if (
            activation is None
            or activation.get("applied") is not True
            or activation.get("pathModified") is not False
            or activation.get("version") != version_root.name
        ):
            continue
        raw_command = activation.get("command")
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or any(not isinstance(part, str) or not part for part in raw_command)
        ):
            continue
        command = tuple(raw_command)
        if not Path(command[0]).is_absolute():
            continue
        expected = managed_harness_binary(version_root, harness)
        if expected is None:
            continue
        try:
            if Path(command[0]).resolve(strict=True) != expected.resolve(strict=True):
                continue
        except OSError:
            continue
        return command
    return None


def _resolve_home(home: Path | None) -> Path:
    candidate = Path.home() / ".hyprial" if home is None else Path(home)
    return candidate.resolve()


def _validate_hc_version(version: str) -> str:
    if (
        not isinstance(version, str)
        or not version
        or version in {".", ".."}
        or "/" in version
        or "\\" in version
        or "\x00" in version
        or ":" in version
        or PureWindowsPath(version).drive
        or Path(version).name != version
    ):
        raise ValueError("hc version must be one safe path segment")
    return version


def hc_state_root(home: Path | None = None) -> Path:
    return _resolve_home(home) / "state" / "hc"


def hc_bin_root(home: Path | None = None) -> Path:
    return hc_state_root(home) / "bin"


def hc_version_root(version: str, home: Path | None = None) -> Path:
    return hc_bin_root(home) / _validate_hc_version(version)


def hc_core_root(home: Path | None = None) -> Path:
    return hc_state_root(home) / "core"


def hc_core_socket_path(home: Path | None = None) -> Path:
    return hc_core_root(home) / "hc.sock"


def hc_session_name(home: Path | None = None) -> str:
    digest = hashlib.sha256(str(_resolve_home(home)).encode("utf-8")).hexdigest()[:12]
    return f"hyprial-{digest}"


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _version_sort_key(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


__all__ = [
    "MANAGED_HARNESS_ACTIVATION_RECEIPT",
    "MANAGED_HARNESS_INSTALL_RECEIPT",
    "MANAGED_HARNESS_PACKAGE",
    "MANAGED_HARNESS_VERSION",
    "activated_managed_harness_command",
    "find_managed_harness_binary",
    "hc_bin_root",
    "hc_core_root",
    "hc_core_socket_path",
    "hc_session_name",
    "hc_state_root",
    "hc_version_root",
    "managed_harness_activation_path",
    "managed_harness_binary",
    "managed_harness_root",
    "managed_harness_version_root",
]
