"""Shared fixtures for the agent error-enumeration tests."""

from __future__ import annotations

import pytest

from pathlib import Path

from .enumeration import _application


@pytest.fixture
def app():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hyprial-stop-inventory-") as directory:
        application = _application(Path(directory))
        try:
            yield application
        finally:
            application._close()
            application._close_home_lease()
