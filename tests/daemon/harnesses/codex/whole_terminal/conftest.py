from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root


@pytest.fixture
def socket_root():
    """A short path: a Unix socket cannot live under pytest's tmp_path."""

    directory = Path(
        tempfile.mkdtemp(prefix="codex-wt-", dir=hyprial_test_tmp_root())
    )
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
