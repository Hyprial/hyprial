"""Atomic unpack of the wheel-bundled GUI into ``$HYPRIAL_HOME/apps/gui``.

The bundle's own sha256 is verified by ``product.product_bundle`` before this
module ever sees the artifact; extraction here is still hostile-input safe
(regular files only, no absolute paths, no ``..``, no links).  The tree is
prepared completely -- runtime dependencies included -- in a staging directory
and renamed into place, so a failure never leaves a half-unpacked GUI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

from hyprial.kernel import atomic_json_write

from .errors import GuiError
from .product import GuiBundle

_STATE_FILE = "bundle.json"
_STATE_SCHEMA = "hyprial.gui-bundle/v1"
_NPM_TIMEOUT = 600.0


def app_root(hyprial_home: Path) -> Path:
    return hyprial_home / "apps" / "gui"


def gui_source(hyprial_home: Path) -> Path:
    return app_root(hyprial_home) / "source"


def unpacked_state(hyprial_home: Path) -> dict[str, Any] | None:
    """The bundle marker of the unpacked GUI, or None when never unpacked."""

    try:
        raw = json.loads((app_root(hyprial_home) / _STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise GuiError("GUI_STATE_INVALID", f"cannot read the unpacked GUI record: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != _STATE_SCHEMA:
        raise GuiError("GUI_STATE_INVALID", "the unpacked GUI record has an invalid schema")
    return raw


def _reject_unsafe_member(member: tarfile.TarInfo) -> None:
    name = member.name
    parts = Path(name).parts
    if (
        member.islnk()
        or member.issym()
        or Path(name).is_absolute()
        or ".." in parts
        or "\\" in name
    ):
        raise GuiError("GUI_PRODUCT_UNAVAILABLE", f"unsafe member in the bundled GUI: {name!r}")
    if not (member.isdir() or member.isreg()):
        raise GuiError("GUI_PRODUCT_UNAVAILABLE", f"unsupported member in the bundled GUI: {name!r}")


def _install_dependencies(tree: Path) -> None:
    """Install the GUI's locked runtime dependencies inside the staged tree."""

    npm = shutil.which("npm")
    if npm is None:
        raise GuiError(
            "GUI_UNPACK_FAILED",
            "The bundled GUI requires Node >= 24 with npm on PATH",
        )
    try:
        completed = subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=_NPM_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuiError("GUI_UNPACK_FAILED", f"cannot install GUI dependencies: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-400:]
        raise GuiError("GUI_UNPACK_FAILED", f"GUI dependency install failed: {detail}")


def ensure_unpacked(
    hyprial_home: Path, bundle: GuiBundle, *, force: bool = False
) -> dict[str, Any]:
    """Unpack the bundle into apps/gui; skip when the same version is in place.

    Returns a result record: ``unpacked`` is False when the existing tree
    already matches the bundle and ``force`` was not given.
    """

    root = app_root(hyprial_home)
    source = gui_source(hyprial_home)
    state = unpacked_state(hyprial_home)
    if (
        not force
        and state is not None
        and state.get("version") == bundle.version
        and state.get("sha256") == bundle.sha256
        and source.is_dir()
    ):
        return {
            "ok": True,
            "unpacked": False,
            "version": bundle.version,
            "commit": bundle.commit,
            "source": str(source),
        }

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=root))
    previous = staging / "previous"
    swapped = False
    installed = False
    committed = False
    try:
        extract_root = staging / "tree"
        extract_root.mkdir()
        try:
            with tarfile.open(bundle.artifact) as archive:
                for member in archive.getmembers():
                    _reject_unsafe_member(member)
                archive.extractall(extract_root, filter="fully_trusted")
        except (OSError, tarfile.TarError) as error:
            raise GuiError("GUI_UNPACK_FAILED", f"cannot unpack the bundled GUI: {error}") from error
        tree = extract_root / f"gui-{bundle.version}"
        if not tree.is_dir():
            raise GuiError(
                "GUI_PRODUCT_UNAVAILABLE",
                "the bundled GUI does not contain its declared version tree",
            )
        launch_script = tree / bundle.launch[1]
        if not launch_script.is_file() or launch_script.is_symlink():
            raise GuiError(
                "GUI_PRODUCT_UNAVAILABLE",
                f"the bundled GUI is missing its launch script {bundle.launch[1]!r}",
            )
        _install_dependencies(tree)
        if source.exists() or source.is_symlink():
            os.replace(source, previous)
            swapped = True
        os.replace(tree, source)
        installed = True
        atomic_json_write(
            root / _STATE_FILE,
            {
                "schema": _STATE_SCHEMA,
                "version": bundle.version,
                "commit": bundle.commit,
                "sha256": bundle.sha256,
                "source": "source",
            },
        )
        committed = True
        return {
            "ok": True,
            "unpacked": True,
            "version": bundle.version,
            "commit": bundle.commit,
            "source": str(source),
        }
    finally:
        if not committed:
            # Source and marker are one transaction. A marker failure must not
            # keep the new source or discard the last working tree. If recovery
            # itself fails, let the exception escape before deleting staging;
            # it still contains the recovery copy.
            if installed:
                os.replace(source, staging / "rejected")
            if swapped:
                os.replace(previous, source)
        shutil.rmtree(staging, ignore_errors=True)
