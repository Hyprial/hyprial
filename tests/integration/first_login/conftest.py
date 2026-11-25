"""Reuse the lifecycle home fixture: one isolated home per test, torn down always."""

from __future__ import annotations

from tests.integration.daemon_lifecycle.conftest import lifecycle, pytestmark  # noqa: F401
