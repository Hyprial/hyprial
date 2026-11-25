"""Shared fixtures for the UUID-addressing naming tests (spec §2.2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents import addressing

OWNER = "op"
NODE = "node-1"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[AgentRegistry]:
    instance = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def id_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)


@pytest.fixture
def switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the pre-flip state for a test about switch-off behaviour."""

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
