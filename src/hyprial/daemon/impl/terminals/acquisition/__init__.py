"""Pinned hc acquisition public surface."""

from .download import download_hc_archive
from .extract import ensure_pinned_hc_installed
from .extract import extract_hc_archive
from .extract import installed_hc_executable
from .extract import remove_hc_version_dir
from .lock import HcAcquisitionError
from .lock import HcLock
from .lock import HcTargetSpec
from .lock import load_bundled_hc_lock

__all__ = [
    "HcAcquisitionError",
    "HcLock",
    "HcTargetSpec",
    "download_hc_archive",
    "ensure_pinned_hc_installed",
    "extract_hc_archive",
    "installed_hc_executable",
    "load_bundled_hc_lock",
    "remove_hc_version_dir",
]
