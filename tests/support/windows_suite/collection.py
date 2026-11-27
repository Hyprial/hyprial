"""Record what this process collected, and every collection error, in the ledger.

Decision (2026-11-26): the ``CollectionRecorder`` only
observes.  It never ignores, deselects or skips anything, so a collection error
stays a collection error in pytest's own result and additionally appears in the
``collect`` ledger event that ``run_waves.py`` counts as ``collection-error``.
Each xdist worker collects the whole selection, so every worker reports the
same errors; the reader de-duplicates by ``nodeid``.  The recorder is registered
only in processes that collect (xdist workers, or the single ``-n 0`` process).

Decision (same date): the ``PosixOnlyPlugin`` in this module is the one place
that keeps POSIX-only tests out of a Windows run, and it is registered on
every platform.  On POSIX both hooks return without acting, so a POSIX run
collects and skips exactly what it collected and skipped before.  On Windows,
``scripts/ci/windows/posix-only.json`` names whole directories and whole files
that are not collected at all, and the ``posix_only`` marker skips single test
cases.  Every path is compared as a string relative to the repository root,
so the comparison does not depend on the platform's path flavour or on
whether pytest was started with relative or absolute arguments.

Decision (same date): the registry is a data file outside ``tests/support``
and is read once per process.  Its shape is checked by the gate under
``tests/tooling/ci/windows/registries/``; this module only
reads it.  A registry it cannot read or parse is treated as empty, so a broken
file cannot turn every Windows run into a collection error; the gate test is
what turns a broken file red.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.support.windows_suite import host

SUMMARY_LIMIT = 500
PLUGIN_NAME = "hyprial-windows-posix-only"
ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = ROOT / "scripts/ci/windows/posix-only.json"


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


def load_posix_only(path: Path = REGISTRY_PATH) -> dict[str, tuple[str, ...]]:
    """``{"dir" | "file": (repository-relative POSIX path, ...)}`` read from ``path``; empty when unreadable."""

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"dir": (), "file": ()}
    if not isinstance(entries, list):
        return {"dir": (), "file": ()}
    grouped: dict[str, list[str]] = {"dir": [], "file": []}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind in grouped and isinstance(entry.get("path"), str):
            grouped[kind].append(entry["path"].rstrip("/"))
    return {"dir": tuple(grouped["dir"]), "file": tuple(grouped["file"])}


def repository_relative(path: Path) -> str | None:
    """``path`` as a POSIX string relative to the repository root, or ``None``."""

    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return None


class PosixOnlyPlugin:
    """Not collected: registered POSIX-only paths.  Skipped: ``posix_only`` cases.  Windows only."""

    def __init__(self, registry: dict[str, tuple[str, ...]] | None = None) -> None:
        loaded = load_posix_only() if registry is None else registry
        self.directories: tuple[str, ...] = tuple(path.rstrip("/") for path in loaded.get("dir", ()))
        self.files: tuple[str, ...] = loaded.get("file", ())

    def pytest_ignore_collect(self, collection_path: Path, config: pytest.Config) -> bool | None:
        """Return ``True`` on Windows for a registered path, ``None`` for everything else."""

        if not host.is_windows():
            return None
        relative = repository_relative(collection_path)
        if relative is None:
            return None
        if any(relative == directory or relative.startswith(directory + "/") for directory in self.directories):
            return True
        return True if relative in self.files else None

    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        """Skip every item carrying ``posix_only`` on Windows; do nothing elsewhere."""

        if not host.is_windows():
            return
        for item in items:
            reasons = [
                str(asked.args[0])
                for asked in item.iter_markers("posix_only")
                if asked.args
            ]
            if reasons:
                item.add_marker(
                    pytest.mark.skip(reason=f"posix_only: {', '.join(reasons)}")
                )


def configure_posix_only(config: pytest.Config) -> None:
    """Register the plugin once per session, on every platform."""

    manager = config.pluginmanager
    if manager.get_plugin(PLUGIN_NAME) is None:
        manager.register(PosixOnlyPlugin(), PLUGIN_NAME)


__all__ = [
    "PLUGIN_NAME",
    "CollectionRecorder",
    "PosixOnlyPlugin",
    "configure_posix_only",
    "load_posix_only",
    "repository_relative",
]
