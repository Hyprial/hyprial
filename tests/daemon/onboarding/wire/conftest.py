"""Fixtures shared by the bootstrap dispatcher and wire host tests."""

from __future__ import annotations

import pytest

from wire_support import OwnerlessHome


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
    from hyprial.daemon.impl.application.onboarding.bootstrap.wire import (
        BootstrapWireHost,
    )

    from wire_support import ControlledIdentity, ServedWire, gateway_options

    served: list[ServedWire] = []

    def factory(**options):
        host = BootstrapWireHost.open(
            ownerless.home,
            state_dir=ownerless.state,
            gateway_options=gateway_options(),
            **options,
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
