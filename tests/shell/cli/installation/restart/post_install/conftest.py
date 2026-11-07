"""Bound the import guard's full-package copies to one test's lifetime."""

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def tmp_path(tmp_path: Path) -> Iterator[Path]:
    """Keep pytest's directory, but do not retain hundreds of package copies.

    The mutation matrix copies the complete package for each case. Pytest's
    normal tmp_path retention keeps all those trees until session cleanup,
    exhausting the shared 2 GiB CI tmpfs and breaking unrelated SQLite tests.
    This nested workspace also releases partial copies on setup/call failure.
    Subprocess stdout/stderr remain in pytest's captured failure report.
    """

    with TemporaryDirectory(prefix="guard-", dir=tmp_path) as workspace:
        yield Path(workspace)
