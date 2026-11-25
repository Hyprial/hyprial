"""Wire-host fixtures for the daemon-driven first login tests.

The controlled ports live in ``first_login_support`` and in
``tests/daemon/onboarding/wire/wire_support.py``; they are imported through the
package path, not copied and not through ``sys.path``.
"""

from __future__ import annotations

import pytest

from tests.daemon.onboarding.first_login.first_login_support import driven_options
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
def open_driven(ownerless):
    from hyprial.daemon import BootstrapWireHost

    served: list[ServedWire] = []

    def factory(
        identity=None, provisioner=None, user_key=None, plain: bool = False
    ) -> ServedWire:
        host = BootstrapWireHost.open(
            ownerless.home,
            state_dir=ownerless.state,
            gateway_options=(
                gateway_options() if plain else driven_options(provisioner, user_key)
            ),
        )
        source = identity
        host.dispatcher.runtime._identity_factory = (
            ControlledIdentity if source is None else (lambda: source)
        )
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
