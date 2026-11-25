"""One ownerless home per test, torn down even when the test failed."""

from __future__ import annotations

import pytest

from tests.integration.daemon_lifecycle.support import LifecycleHome


@pytest.fixture
def lifecycle():
    home = LifecycleHome()
    try:
        yield home
    finally:
        home.cleanup()
