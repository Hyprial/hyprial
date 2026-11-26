"""Record what this process collected, and every collection error, in the ledger.

Decision (2026-11-26): this module only
observes.  It never ignores, deselects or skips anything, so a collection error
stays a collection error in pytest's own result and additionally appears in the
``collect`` ledger event that ``run_waves.py`` counts as ``collection-error``.
Each xdist worker collects the whole selection, so every worker reports the
same errors; the reader de-duplicates by ``nodeid``.  The recorder is registered
only in processes that collect (xdist workers, or the single ``-n 0`` process).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

SUMMARY_LIMIT = 500


class CollectionRecorder:
    """Writes one ``collect`` event when this process finishes collecting."""

    def __init__(self, write: Callable[..., None]) -> None:
        self._write = write
        self._errors: list[dict[str, Any]] = []

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._errors.append(
                {"nodeid": report.nodeid, "summary": report.longreprtext[:SUMMARY_LIMIT]}
            )

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self._write("collect", collected=len(session.items), errors=list(self._errors))
