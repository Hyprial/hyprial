"""Isolated, device-ready hyprial home helper for GUI and E2E acceptance tests.

Decision record (S2, 2026-11-21):
- Purpose: prepare a serving-ready isolated hyprial home (minted node id,
  committed settings identity, device record, and completed initialize receipt)
  using the product's own storage functions without running a real issuer login.
- Allowed callers: browser acceptance tests under ``gui/tests/**``, pytest
  suites under ``tests/**``, and future ``contract/e2e`` runners that need a
  GUI-ready home. Product modules under ``src/hyprial/**`` MUST NOT import
  this package.
- Why not a product CLI command: keeping this helper under ``tests/support/``
  ensures ``pyproject.toml`` (``packages = ["src/hyprial"]``) excludes it from
  shipped wheels and prevents real users from synthesizing placeholder device
  identities on real homes.
"""

from __future__ import annotations

from .prepare import ReadyHome, ReadyHomeError, prepare_ready_home

__all__ = ["ReadyHome", "ReadyHomeError", "prepare_ready_home"]
