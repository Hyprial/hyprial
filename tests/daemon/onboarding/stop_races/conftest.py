"""Wire-host fixtures for the stop and initialize-failure decisions.

The controlled ports live in ``tests/daemon/onboarding/wire/wire_support.py``;
the two suites share one controlled identity, home and served-host helper
through the package import ``tests.daemon.onboarding.wire.wire_support``
instead of copying them or editing ``sys.path``.
"""

from __future__ import annotations

import pytest

from tests.daemon.onboarding.wire.wire_support import (
    ControlledIdentity,
    OwnerlessHome,
    ServedWire,
    gateway_options,
)


@pytest.fixture
def ownerless(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    fixture = OwnerlessHome()
    monkeypatch.setenv("HYPRIAL_HOME", str(fixture.home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(fixture.state))
    try:
        yield fixture
    finally:
        fixture.cleanup()


@pytest.fixture
def open_wire(ownerless):
    from hyprial.daemon import BootstrapWireHost

    served: list[ServedWire] = []

    def factory() -> ServedWire:
        host = BootstrapWireHost.open(
            ownerless.home, state_dir=ownerless.state, gateway_options=gateway_options()
        )
        host.dispatcher.runtime._identity_factory = ControlledIdentity
        running = ServedWire(host)
        served.append(running)
        return running

    try:
        yield factory
    finally:
        for running in served:
            running.host.request_stop()
            running.wait()
            running.host.close()
