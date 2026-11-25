"""Close the AgentRegistry every domain test opens.

Why this file exists
--------------------
The native actor engine keeps a cell's Python handler and factory inside the
PyO3 engine object.  CPython's cyclic collector cannot see references held by
a Rust object that does not implement ``__traverse__``/``__clear__``, so for a
host that was never explicitly shut down the engine keeps the whole actor /
session / registry graph alive after the test drops its own references.
The reference-abi engine is ordinary Python, so the same cycle stays collectable
there.

Production does not lean on the collector: the daemon closes its registry
explicitly at shutdown.  The domain tests open registries with ``tmp_path``
and never closed them, which leaked three sqlite descriptors per test under
the native default.  This fixture makes the test-side lifecycle match
production: every registry constructed during a test is closed when the test
ends, whatever the engine's collector visibility.

The ``_fd_hygiene`` dependency exists only for ordering: it makes this
fixture set up after the descriptor snapshot and tear down before the leak
assertion, so the assertion measures the closed state.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyprial.identity.impl.agents import AgentRegistry


@pytest.fixture(autouse=True)
def _close_opened_agent_registries(
    monkeypatch: pytest.MonkeyPatch,
    _fd_hygiene: None,
) -> Iterator[None]:
    opened: list[AgentRegistry] = []
    original_init = AgentRegistry.__init__

    def tracking_init(self: AgentRegistry, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(AgentRegistry, "__init__", tracking_init)
    yield
    for registry in reversed(opened):
        registry.close()
