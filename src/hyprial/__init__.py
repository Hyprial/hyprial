"""Harness Bridge Python implementation."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("hyprial")
except PackageNotFoundError:  # Source-only imports have no distribution metadata.
    __version__ = "0+unknown"
