"""Shared fixtures for the UUID-addressing fail-loud tests (spec §2.3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity.impl.agents import addressing
from hyprial.kernel import canonical_agent_uri
from tests.kernel.actors.identity.support import _registered, application
from tests.support.runtime._owned_applications import close_application

OWNER = "op"
NODE = "node-1"


def name_uri(name: str) -> str:
    return canonical_agent_uri(OWNER, NODE, name)


def send(app: DaemonApplication, target: str) -> dict:
    return app.handle(
        "message.send",
        {
            "from": _registered(app, "driver"),
            "to": [target],
            "summary": "hi",
            "message": "hi",
        },
    )


@pytest.fixture
def id_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)


def _populated(tmp_path: Path) -> Iterator[DaemonApplication]:
    instance = application(tmp_path)
    try:
        _registered(instance, "driver")
        instance.handle("agent.create", {"name": "worker"})
        yield instance
    finally:
        close_application(instance)


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[DaemonApplication]:
    """Switch off: name-minted records, the pre-flip behaviour."""

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    yield from _populated(tmp_path)


@pytest.fixture
def flipped(id_form: None, tmp_path: Path) -> Iterator[DaemonApplication]:
    """Switch on before any record exists, so records are id-minted."""

    yield from _populated(tmp_path)
