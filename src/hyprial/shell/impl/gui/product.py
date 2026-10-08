"""The GUI bundled inside this product wheel, validated before any use.

The bundle is produced at wheel build time by ``gui_build_hook`` /
``bundle.build_gui``: ``release.json`` pins the exact artifact bytes, and the
artifact itself carries the GUI's own launch manifest.  Nothing here consults
any external registry; the wheel is the only source of truth.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .errors import GuiError


@dataclass(frozen=True, slots=True)
class GuiBundle:
    """The validated GUI component paired with this installed product."""

    version: str
    commit: str
    artifact: Path
    sha256: str
    #: Relative path of the launch manifest inside the unpacked tree.
    manifest: str
    #: Launch description from that manifest (``["bash", "<script>"]``).
    launch: tuple[str, ...]


def _bundle_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "_gui"


def _read_launch(artifact: Path, version: str, manifest: str) -> tuple[str, ...]:
    """Read the bundled tree's own launch declaration without unpacking."""

    try:
        with tarfile.open(artifact) as archive:
            member = archive.extractfile(f"gui-{version}/{manifest}")
            if member is None:
                raise ValueError(f"missing {manifest} in the bundled GUI")
            raw = json.loads(member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read the bundled GUI launch manifest: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("bundled GUI launch manifest must be an object")
    start = raw.get("start")
    if (
        not isinstance(start, list)
        or len(start) != 2
        or start[0] != "bash"
        or not isinstance(start[1], str)
        or not start[1]
    ):
        raise ValueError("bundled GUI launch manifest must declare start as ['bash', '<script>']")
    return ("bash", start[1])


def product_bundle() -> GuiBundle:
    """Validate the paired GUI bundle and describe it; never fall back."""

    bundle = _bundle_dir()
    manifest_file = bundle / "release.json"
    try:
        data = json.loads(manifest_file.read_text())
        if data["schema"] != "hyprial.product-gui/v1" or data["file"] != "gui.tar.gz":
            raise ValueError("invalid product GUI manifest")
        artifact = bundle / data["file"]
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != data["sha256"]:
            raise ValueError("product GUI checksum mismatch")
        version = str(data["version"])
        manifest = str(data["manifest"])
        launch = _read_launch(artifact, version, manifest)
        return GuiBundle(
            version=version,
            commit=str(data["commit"]),
            artifact=artifact,
            sha256=digest,
            manifest=manifest,
            launch=launch,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GuiError(
            "GUI_PRODUCT_UNAVAILABLE",
            "This Hyprial installation has no valid matching GUI component; install a complete product build",
        ) from error
