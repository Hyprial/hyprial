"""Fixtures for the dev-upgrade smoke tests: hermetic, no real sleeping.

The suite-wide ``HYPRIAL_DAEMON_AUTOSTART=0`` / ``HYPRIAL_SERVICE_AUTOINSTALL=0``
pins of ``tests/conftest.py`` are removed here because the smoke refuses the
first one; tests that need it set it themselves.  ``socket.getfqdn`` is stubbed:
the host guard must not do a real name lookup (on macOS that lookup also leaves
a ``com.apple.netsrc`` descriptor open, which the suite's descriptor-leak check
reports against whichever test called it first).  ``subprocess.Popen`` refuses:
the GUI child and every command go through the fake runner, so a real child
process in these tests is a bug.
"""

from __future__ import annotations

import sys
from typing import Any, Iterator

import pytest

from .gui_support import Listener


@pytest.fixture()
def listener() -> Iterator[Listener]:
    item = Listener()
    try:
        yield item
    finally:
        item.close()


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_real_sleep(seconds: float) -> None:
        raise AssertionError(f"real time.sleep({seconds}) in a smoke test")

    def refuse_real_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"real subprocess in a smoke test: {args[:1]!r}")

    monkeypatch.setattr("time.sleep", refuse_real_sleep)
    monkeypatch.setattr("subprocess.Popen", refuse_real_process)
    monkeypatch.delenv("HYPRIAL_DAEMON_AUTOSTART", raising=False)
    monkeypatch.delenv("HYPRIAL_SERVICE_AUTOINSTALL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("socket.getfqdn", lambda name="": "smoke-host.invalid")
