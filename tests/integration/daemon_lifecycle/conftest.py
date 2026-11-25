"""One ownerless home per test, torn down even when the test failed."""

from __future__ import annotations

import os

import pytest

from .support import LifecycleHome

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX unix-socket and signal lifecycle; Windows is not a current target (KP-080)"
)


@pytest.fixture
def lifecycle():
    home = LifecycleHome()
    try:
        yield home
    finally:
        home.cleanup()
