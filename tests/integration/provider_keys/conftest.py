"""Fixtures for the cross-slice owner-key tests: isolated environment, owner worlds, launch clock."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.provider_keys import launch as launch_module
from hyprial.shell.impl.cli.commands.common.services import get_services
from tests.daemon.provider_keys.service.conftest import World

WORKER_ENV = ("HYPRIAL_WORKER_ACTOR", "HYPRIAL_WORKER_SESSION_REF", "HYPRIAL_MANAGED_WORKER")


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HOME, HYPRIAL_HOME and TMPDIR under tmp_path; no worker identity; non-TTY stdin."""
    for name in WORKER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: False)


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def worlds(tmp_path: Path):
    """Two independent owner worlds under one tmp_path: the CLI flow and the GUI flow."""
    made = []
    for name in ("cli", "gui"):
        (tmp_path / name).mkdir()
        made.append(World(tmp_path / name))
    yield made
    for instance in made:
        instance.close()


@pytest.fixture
def launch_clock(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Bind the composition's per-launch resolver to a test clock.

    ``compose_daemon_worker_launch`` builds its resolver with the wall clock; the
    owner world signs grants on its own clock, so launches read ``clock[0]`` when
    set, else the app's ``_provider_key_clock``.
    """
    original = launch_module.launch_resolver
    clock: list[Any] = [None]

    def resolver(app: Any, *, registry: Any) -> Any:
        return original(app, registry=registry, clock=clock[0] or app._provider_key_clock)

    monkeypatch.setattr(launch_module, "launch_resolver", resolver)
    return clock
