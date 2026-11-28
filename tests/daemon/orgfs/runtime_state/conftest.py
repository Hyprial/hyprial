# Decisions:
# - Phase D2: every runtime test runs in an isolated home (``HYPRIAL_HOME``,
#   ``HARNESS_STATE_DIR`` under ``tmp_path``) with peer discovery off, as the
#   contract suite does; ``peers`` closes every node it made.
"""Fixtures for the state runtime tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .support import Peers


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


@pytest.fixture
def peers(tmp_path) -> Iterator[Peers]:
    group = Peers(tmp_path)
    try:
        yield group
    finally:
        group.close()
