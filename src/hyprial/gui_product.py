"""The GUI paired with this installed product; never a socialware lookup."""

import hashlib
import json
from pathlib import Path

from hyprial.installers.core import CatalogEntry, InstallError


def product_source() -> tuple[CatalogEntry, str]:
    bundle = Path(__file__).with_name("_gui")
    manifest = bundle / "release.json"
    try:
        data = json.loads(manifest.read_text())
        if data["schema"] != "hyprial.product-gui/v1" or data["file"] != "gui.tar.gz":
            raise ValueError("invalid product GUI manifest")
        artifact = bundle / data["file"]
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != data["sha256"]:
            raise ValueError("product GUI checksum mismatch")
        from hyprial.installers.core import _parse_catalog_entry

        entry = _parse_catalog_entry("gui", {
            "release": artifact.as_uri(), "sha256": data["sha256"],
            "version": data["version"], "commit": data["commit"],
            "manifest": data["manifest"],
        }, registry_is_local=True)
        return entry, manifest.as_uri()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InstallError(
            "GUI_PRODUCT_UNAVAILABLE",
            "This Hyprial installation has no valid matching GUI component; install a complete product build",
        ) from error
